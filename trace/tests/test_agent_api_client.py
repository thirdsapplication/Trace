"""Stage 4 — api_client retry/backoff helpers (offline, no anthropic SDK needed).

Covers the transient-error policy that keeps a sandbox run from being skipped on a
single 503 overloaded / "file storage temporarily unavailable" blip: retryable
errors back off and retry; non-retryable errors raise immediately; the last
attempt's error still propagates.
"""

import unittest

from trace.agent import api_client


class FakeHTTPError(Exception):
    def __init__(self, status_code, message=""):
        super().__init__(message or f"status {status_code}")
        self.status_code = status_code


class IsRetryableTest(unittest.TestCase):
    def test_retryable_status_codes(self):
        for sc in (408, 409, 429, 500, 502, 503, 529):
            self.assertTrue(api_client._is_retryable(FakeHTTPError(sc)), sc)

    def test_non_retryable_status_codes(self):
        for sc in (400, 401, 403, 404, 422):
            self.assertFalse(api_client._is_retryable(FakeHTTPError(sc)), sc)

    def test_retryable_by_message_when_no_status(self):
        # the exact 503 the user hit, surfaced as a plain message
        self.assertTrue(api_client._is_retryable(
            Exception("File storage is temporarily unavailable. Please retry.")))
        self.assertTrue(api_client._is_retryable(Exception("overloaded_error")))
        self.assertTrue(api_client._is_retryable(Exception("connection reset")))

    def test_plain_unrelated_error_not_retryable(self):
        self.assertFalse(api_client._is_retryable(ValueError("bad envelope shape")))


class RetryTest(unittest.TestCase):
    def test_retries_then_succeeds(self):
        slept = []
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise FakeHTTPError(503, "overloaded")
            return "ok"

        result = api_client._retry(flaky, attempts=5, base=1.0, sleep=slept.append)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(slept, [1.0, 2.0])          # exponential backoff: base*2**i

    def test_non_retryable_raises_immediately(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise FakeHTTPError(401, "unauthorized")

        with self.assertRaises(FakeHTTPError):
            api_client._retry(boom, attempts=5, base=1.0, sleep=lambda s: None)
        self.assertEqual(calls["n"], 1)              # never retried

    def test_exhausts_attempts_and_reraises_last_error(self):
        calls = {"n": 0}

        def always_503():
            calls["n"] += 1
            raise FakeHTTPError(503, "still overloaded")

        with self.assertRaises(FakeHTTPError):
            api_client._retry(always_503, attempts=3, base=0.5, sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)              # tried exactly `attempts` times


class AgentCreateForwardsMcpServersTest(unittest.TestCase):
    """Regression: the agent create/reuse calls must forward ``mcp_servers`` to the
    platform, or the deployed live agent's mcp_toolset has no server backing it (it
    behaves like a tool-less test agent)."""

    def _client(self, fake_ns):
        # bypass __init__ so no anthropic SDK / API key is needed
        c = api_client.ManagedAgentsClient.__new__(api_client.ManagedAgentsClient)
        c._beta_version = "beta-x"
        c._agents_ns = lambda name: fake_ns
        return c

    _ENV = {"name": "Order intake", "model": "m", "system": "s",
            "tools": [{"type": "agent"}, {"type": "mcp_toolset", "mcp_server_name": "Trace gmail"}],
            "mcp_servers": [{"name": "Trace gmail", "type": "url", "url": "https://x?app=gmail"}]}

    def test_create_branch_forwards_mcp_servers(self):
        captured = {}

        class NS:
            def list(self):
                return []                       # no existing agent -> create path
            def create(self, **kw):
                captured.update(kw)
                return {"id": "agent_1"}

        out = self._client(NS()).create_or_reuse_agent(self._ENV)
        self.assertFalse(out["reused"])
        self.assertEqual(captured["mcp_servers"], self._ENV["mcp_servers"])

    def test_reuse_deletes_then_recreates(self):
        # an existing same-named agent is deleted and recreated fresh (so the redeploy
        # reflects the rebuilt config), with mcp_servers forwarded to the new agent.
        deleted, created = [], {}

        class NS:
            def list(self):
                return [{"id": "agent_old", "name": "Order intake"}]   # existing
            def delete(self, agent_id, **kw):
                deleted.append(agent_id)
            def create(self, **kw):
                created.update(kw)
                return {"id": "agent_new"}

        out = self._client(NS()).create_or_reuse_agent(self._ENV)
        self.assertEqual(deleted, ["agent_old"])              # old agent removed
        self.assertEqual(out["id"], "agent_new")
        self.assertTrue(out["recreated"])
        self.assertFalse(out["reused"])
        self.assertEqual(created["mcp_servers"], self._ENV["mcp_servers"])

    def test_reuse_recreates_even_if_delete_fails(self):
        # a failed delete must not block the redeploy: still create the fresh agent.
        created = {}

        class NS:
            def list(self):
                return [{"id": "agent_old", "name": "Order intake"}]
            def delete(self, agent_id, **kw):
                raise RuntimeError("delete failed")
            def create(self, **kw):
                created.update(kw)
                return {"id": "agent_new"}

        out = self._client(NS()).create_or_reuse_agent(self._ENV)
        self.assertEqual(out["id"], "agent_new")
        self.assertTrue(out["recreated"])
        self.assertEqual(created["mcp_servers"], self._ENV["mcp_servers"])


class _NSClient:
    """Client with per-namespace fakes: _agents_ns routes by name."""
    @staticmethod
    def build(ns_map):
        c = api_client.ManagedAgentsClient.__new__(api_client.ManagedAgentsClient)
        c._beta_version = "beta-x"
        c._agents_ns = lambda name: ns_map[name]
        return c


class _FakeMemories:
    def __init__(self, conflict_paths=()):
        self.created = []                      # (store_id, path, content)
        self.conflict_paths = set(conflict_paths)

    def create(self, store_id, path=None, content=None):
        if path in self.conflict_paths:
            raise RuntimeError("memory_path_conflict_error: a memory already exists at this path")
        self.created.append((store_id, path, content))
        return {"id": f"memdoc_{len(self.created)}"}


class _FakeMemoryStores:
    def __init__(self, existing=None, conflict_paths=()):
        self.existing = list(existing or [])   # [{"id","name"}]
        self.created = []
        self.deleted = []
        self.memories = _FakeMemories(conflict_paths)

    def list(self):
        return list(self.existing)

    def create(self, name=None, description=None):
        store = {"id": f"mem_{len(self.created)}", "name": name, "description": description}
        self.created.append(store)
        return store

    def delete(self, store_id):
        self.deleted.append(store_id)


class _FakeDeployments:
    def __init__(self):
        self.created, self.runs, self.archived = [], [], []

    def create(self, **kw):
        self.created.append(kw)
        return {"id": f"dep_{len(self.created)}"}

    def run(self, deployment_id):
        self.runs.append(deployment_id)
        return {"id": "drun_1", "session_id": "sess_1", "created_at": "t"}

    def archive(self, deployment_id):
        self.archived.append(deployment_id)


class _FakeDeploymentRuns:
    def __init__(self, runs=None):
        self._runs = runs or []

    def list(self, deployment_id=None):
        return list(self._runs)


class MemoryStoreClientTest(unittest.TestCase):
    def test_fresh_store_created_and_seeded_with_every_doc(self):
        stores = _FakeMemoryStores()
        c = _NSClient.build({"memory_stores": stores})
        out = c.create_or_reuse_memory_store("trace-u1-memory", "desc")
        self.assertFalse(out["reused"])
        from trace.agent import config
        self.assertEqual({p for _s, p, _c in stores.memories.created},
                         {config.MEMORY_STATUS_DOC, config.MEMORY_LESSONS_DOC,
                          config.MEMORY_SHARED_DOC})

    def test_reused_store_keeps_existing_docs_untouched(self):
        # a redeploy finds the user's store and its documents already there: the
        # conflict-tolerant seed leaves every existing document as-is, so lessons
        # survive redeploys
        from trace.agent import config
        stores = _FakeMemoryStores(
            existing=[{"id": "mem_prior", "name": "trace-u1-memory"}],
            conflict_paths={config.MEMORY_STATUS_DOC, config.MEMORY_LESSONS_DOC,
                            config.MEMORY_SHARED_DOC})
        c = _NSClient.build({"memory_stores": stores})
        out = c.create_or_reuse_memory_store("trace-u1-memory", "desc")
        self.assertEqual(out, {"id": "mem_prior", "reused": True})
        self.assertEqual(stores.memories.created, [])          # nothing overwritten

    def test_reused_store_gets_missing_documents_completed(self):
        # seeds run on the reused path too: a store left half-initialized (e.g. a
        # deploy interrupted between create and seed) gets its missing documents
        # on the next deploy instead of staying broken forever
        from trace.agent import config
        stores = _FakeMemoryStores(existing=[{"id": "mem_prior", "name": "trace-u1-memory"}])
        c = _NSClient.build({"memory_stores": stores})
        out = c.create_or_reuse_memory_store("trace-u1-memory", "desc")
        self.assertEqual(out, {"id": "mem_prior", "reused": True})
        self.assertEqual([(s, p) for s, p, _c in stores.memories.created],
                         [("mem_prior", config.MEMORY_STATUS_DOC),
                          ("mem_prior", config.MEMORY_LESSONS_DOC),
                          ("mem_prior", config.MEMORY_SHARED_DOC)])

    def test_seed_tolerates_path_conflict_but_raises_otherwise(self):
        from trace.agent import config
        stores = _FakeMemoryStores(conflict_paths={config.MEMORY_STATUS_DOC})
        c = _NSClient.build({"memory_stores": stores})
        self.assertIsNone(c.seed_memory("mem_1", config.MEMORY_STATUS_DOC, "x"))  # tolerated
        stores.memories = _FakeMemories()
        stores.memories.create = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("permission denied"))
        with self.assertRaises(RuntimeError):
            c.seed_memory("mem_1", "/other.md", "x")


class RealShapeMemories:
    """The DOCUMENTED Managed Agents memories surface, pinned so a regression back
    to the guessed shapes fails the suite: create is PATH-addressed (409 when the
    path is occupied); update/delete/retrieve take the mem_ id first with
    memory_store_id as a required KEYWORD; list returns Memory entries whose
    content is included only under view="full". The guessed update(store_id,
    path=...) / delete(store_id, path=path) shapes failed in production with
    exactly the TypeErrors these keyword-only signatures reproduce."""

    def __init__(self, docs=None, content_on_list=True):
        self.docs = dict(docs or {})               # path -> content
        self.ids = {p: f"mem_{i}" for i, p in enumerate(self.docs)}
        self.content_on_list = content_on_list
        self.created, self.updated, self.deleted, self.retrieved = [], [], [], []

    def _path_of(self, mem_id):
        for p, i in self.ids.items():
            if i == mem_id:
                return p
        raise RuntimeError(f"Error code: 404 - memory not found: {mem_id}")

    def list(self, store_id, **kw):
        include = self.content_on_list and kw.get("view") == "full"
        return [dict({"type": "memory", "id": self.ids[p], "path": p},
                     **({"content": self.docs[p]} if include else {}))
                for p in self.docs]

    def create(self, store_id, path=None, content=None):
        if path in self.docs:
            raise RuntimeError(
                "Error code: 409 - memory_path_conflict_error: path already occupied")
        self.docs[path] = content
        self.ids[path] = f"mem_{len(self.ids)}"
        self.created.append((store_id, path, content))
        return {"id": self.ids[path]}

    def update(self, mem_id, *, memory_store_id, content=None, path=None):
        p = self._path_of(mem_id)
        self.docs[p] = content
        self.updated.append((mem_id, memory_store_id, content))
        return {"id": mem_id}

    def delete(self, mem_id, *, memory_store_id):
        p = self._path_of(mem_id)
        del self.docs[p], self.ids[p]
        self.deleted.append((mem_id, memory_store_id))

    def retrieve(self, mem_id, *, memory_store_id):
        p = self._path_of(mem_id)
        self.retrieved.append((mem_id, memory_store_id))
        return {"id": mem_id, "path": p, "content": self.docs[p]}


def _memories_client(memories):
    stores = _FakeMemoryStores()
    stores.memories = memories
    return _NSClient.build({"memory_stores": stores})


class WriteMemoryTest(unittest.TestCase):
    """Refreshing a memory document against the real memories surface.

    seed_memory deliberately KEEPS existing content so a redeploy never wipes an
    agent's memory — which makes it useless for delivering lessons that change between
    runs. write_memory replaces instead, and must say so when it cannot: a silent miss
    means the agent runs without its lessons and every downstream table stays empty,
    looking exactly like "nothing to share". That ambiguity is what hid this whole
    feature being dead — twice: first the guidance path, then the learning cycle's
    lessons rewrite, which 409'd on create because the wrong-shaped update/delete
    could never clear the existing document.
    """

    def test_an_existing_document_is_updated_by_memory_id(self):
        m = RealShapeMemories(docs={"/lessons_learned.md": "old"})
        c = _memories_client(m)
        self.assertEqual(c.write_memory("memstore_1", "/lessons_learned.md", "new"),
                         "update")
        self.assertEqual(m.updated, [("mem_0", "memstore_1", "new")])
        self.assertEqual(m.docs["/lessons_learned.md"], "new")
        self.assertEqual(m.created, [], "create on an occupied path would 409")

    def test_a_fresh_path_is_created(self):
        m = RealShapeMemories()
        c = _memories_client(m)
        self.assertEqual(c.write_memory("memstore_1", "/shared_lessons.md", "body"),
                         "create")
        self.assertEqual(m.created, [("memstore_1", "/shared_lessons.md", "body")])

    def test_update_failure_falls_back_to_delete_then_create(self):
        class NoUpdate(RealShapeMemories):
            def update(self, *a, **k):
                raise RuntimeError("permission denied")

        m = NoUpdate(docs={"/lessons_learned.md": "old"})
        c = _memories_client(m)
        self.assertEqual(c.write_memory("memstore_1", "/lessons_learned.md", "new"),
                         "create")
        self.assertEqual(m.deleted, [("mem_0", "memstore_1")])
        self.assertEqual(m.docs["/lessons_learned.md"], "new")

    def test_total_failure_returns_none_rather_than_pretending(self):
        class Broken(RealShapeMemories):
            def update(self, *a, **k):
                raise RuntimeError("permission denied")

            def delete(self, *a, **k):
                raise RuntimeError("permission denied")

            def create(self, *a, **k):
                raise RuntimeError("permission denied")

        c = _memories_client(Broken(docs={"/lessons_learned.md": "old"}))
        self.assertIsNone(c.write_memory("memstore_1", "/lessons_learned.md", "new"),
                          "a failed write must be reported, not swallowed into success")


class DeploymentClientTest(unittest.TestCase):
    def test_manual_deployment_omits_schedule(self):
        deps = _FakeDeployments()
        c = _NSClient.build({"deployments": deps})
        c.create_deployment("trace-B1", "agent_1", "env_1", "Task: go", schedule=None)
        kw = deps.created[0]
        self.assertNotIn("schedule", kw)                       # manual: key entirely absent
        self.assertEqual(kw["initial_events"],
                         [{"type": "user.message",
                           "content": [{"type": "text", "text": "Task: go"}]}])

    def test_cron_deployment_shape(self):
        deps = _FakeDeployments()
        c = _NSClient.build({"deployments": deps})
        c.create_deployment("trace-B1", "agent_1", "env_1", "Task: go",
                            schedule={"expression": "0 9 * * *", "timezone": "America/New_York"},
                            resources=[{"type": "memory_store", "memory_store_id": "mem_1"}],
                            vault_ids=["vlt_1"], metadata={"trace_bundle_id": "B1"})
        kw = deps.created[0]
        self.assertEqual(kw["schedule"], {"type": "cron", "expression": "0 9 * * *",
                                          "timezone": "America/New_York"})
        self.assertEqual(kw["vault_ids"], ["vlt_1"])
        self.assertEqual(kw["resources"][0]["memory_store_id"], "mem_1")

    def test_run_deployment_normalizes(self):
        c = _NSClient.build({"deployments": _FakeDeployments()})
        drun = c.run_deployment("dep_1")
        self.assertEqual(drun, {"id": "drun_1", "session_id": "sess_1",
                                "error_type": None, "error_message": None, "created_at": "t"})

    def test_list_deployment_runs_normalizes_nested_error(self):
        runs = [{"id": "drun_ok", "session_id": "s1", "created_at": "t1"},
                {"id": "drun_bad", "session_id": None, "created_at": "t2",
                 "error": {"type": "vault_not_found", "message": "vault gone"}}]
        c = _NSClient.build({"deployment_runs": _FakeDeploymentRuns(runs)})
        out = c.list_deployment_runs("dep_1")
        self.assertEqual(out[0]["error_type"], None)
        self.assertEqual(out[1]["error_type"], "vault_not_found")
        self.assertEqual(out[1]["error_message"], "vault gone")

    def test_archive_tolerates_failure(self):
        class BoomDeps:
            def archive(self, dep_id):
                raise RuntimeError("already archived")
        c = _NSClient.build({"deployments": BoomDeps()})
        self.assertFalse(c.archive_deployment("dep_1"))        # tolerant, returns False

    def test_cleanup_sweeps_deployments_and_stores(self):
        deps, stores = _FakeDeployments(), _FakeMemoryStores()

        class EmptyAgents:
            def list(self):
                return []
        c = _NSClient.build({"deployments": deps, "memory_stores": stores,
                             "agents": EmptyAgents(), "sessions": None})
        out = c.cleanup(deployment_ids=["dep_1"], memory_store_ids=["mem_1"])
        self.assertEqual(deps.archived, ["dep_1"])
        self.assertEqual(stores.deleted, ["mem_1"])
        self.assertEqual(out["removed"]["deployments"], ["dep_1"])
        self.assertEqual(out["removed"]["memory_stores"], ["mem_1"])
        self.assertEqual(out["errors"], [])


class EventUsageTest(unittest.TestCase):
    """Token usage must survive event normalization — the learning cycle's cost
    accounting reads it, and _full_event_dict used to discard it silently."""

    def test_usage_on_the_event_itself_is_preserved(self):
        d = api_client._full_event_dict({"type": "agent.message", "content": "hi",
                                         "usage": {"input_tokens": 120, "output_tokens": 30}})
        self.assertEqual(d["usage"], {"input_tokens": 120, "output_tokens": 30})

    def test_usage_nested_under_message_is_found(self):
        d = api_client._full_event_dict(
            {"type": "agent.message", "content": "hi",
             "message": {"usage": {"input_tokens": 7, "output_tokens": 2,
                                   "cache_read_input_tokens": 500}}})
        self.assertEqual(d["usage"], {"input_tokens": 7, "output_tokens": 2,
                                      "cache_read_input_tokens": 500})

    def test_usage_nested_under_span_is_found(self):
        d = api_client._full_event_dict(
            {"type": "span.turn", "span": {"usage": {"input_tokens": 9, "output_tokens": 1}}})
        self.assertEqual(d["usage"], {"input_tokens": 9, "output_tokens": 1})

    def test_no_usage_normalizes_to_none(self):
        d = api_client._full_event_dict({"type": "agent.message", "content": "hi"})
        self.assertIsNone(d["usage"])

    def test_junk_usage_values_are_dropped(self):
        d = api_client._full_event_dict(
            {"type": "agent.message", "content": "hi",
             "usage": {"input_tokens": "lots", "output_tokens": None}})
        self.assertIsNone(d["usage"])


class ReadMemoryTest(unittest.TestCase):
    """Reading a memory document back through the real memories surface.

    write_memory has existed since the shared-lessons work; the learning cycle is
    the first thing that must READ what an agent wrote. The first version guessed
    retrieve(store_id, path=...) — wrong on the real SDK, so every read silently
    returned None and Fable analyzed with each doc "(unavailable)". A total
    failure is still None, never a raise — the caller marks the doc unavailable
    rather than analyzing a fabricated one.
    """

    def test_reads_content_straight_from_the_full_view_listing(self):
        m = RealShapeMemories(docs={"/other.md": "no",
                                    "/lessons_learned.md": "the lessons"})
        c = _memories_client(m)
        self.assertEqual(c.read_memory("memstore_1", "/lessons_learned.md"),
                         "the lessons")
        self.assertEqual(m.retrieved, [], "one listing suffices when it has content")

    def test_falls_back_to_retrieve_by_id_when_the_listing_has_no_content(self):
        m = RealShapeMemories(docs={"/lessons_learned.md": "the lessons"},
                              content_on_list=False)
        c = _memories_client(m)
        self.assertEqual(c.read_memory("memstore_1", "/lessons_learned.md"),
                         "the lessons")
        self.assertEqual(m.retrieved, [("mem_0", "memstore_1")])

    def test_block_list_content_is_joined(self):
        m = RealShapeMemories(docs={"/lessons_learned.md":
                                    [{"type": "text", "text": "part one "},
                                     {"type": "text", "text": "part two"}]})
        c = _memories_client(m)
        self.assertEqual(c.read_memory("memstore_1", "/lessons_learned.md"),
                         "part one part two")

    def test_a_missing_document_is_none(self):
        c = _memories_client(RealShapeMemories(docs={"/other.md": "x"}))
        self.assertIsNone(c.read_memory("memstore_1", "/lessons_learned.md"))

    def test_total_failure_returns_none_rather_than_raising(self):
        class Broken(RealShapeMemories):
            def list(self, *a, **k):
                raise RuntimeError("permission denied")

        c = _memories_client(Broken())
        self.assertIsNone(c.read_memory("memstore_1", "/lessons_learned.md"))


class SessionMemoryResourceTest(unittest.TestCase):
    def test_create_session_passes_memory_resource_through(self):
        captured = {}

        class Sessions:
            def create(self, **kw):
                captured.update(kw)
                return {"id": "sess_1"}
        c = _NSClient.build({"sessions": Sessions()})
        c.create_session("agent_1", "env_1",
                         resources=[{"type": "memory_store", "memory_store_id": "mem_1",
                                     "access": "read_write", "instructions": "i"},
                                    {"file_id": "f1", "mount_path": "/workspace/files/a"}],
                         vault_ids=["vlt_1"])
        self.assertEqual(captured["resources"][0]["type"], "memory_store")   # untouched
        self.assertEqual(captured["resources"][1],
                         {"type": "file", "file_id": "f1",
                          "mount_path": "/workspace/files/a"})               # legacy wrap kept
        self.assertEqual(captured["vault_ids"], ["vlt_1"])


if __name__ == "__main__":
    unittest.main()
