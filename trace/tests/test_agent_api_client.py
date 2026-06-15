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

    def test_reuse_branch_forwards_mcp_servers(self):
        captured = {}

        class NS:
            def list(self):
                return [{"id": "agent_1", "name": "Order intake"}]   # existing -> update path
            def update(self, agent_id, **kw):
                captured.update(kw)
                return {"id": agent_id}

        out = self._client(NS()).create_or_reuse_agent(self._ENV)
        self.assertTrue(out["reused"])
        self.assertEqual(captured["mcp_servers"], self._ENV["mcp_servers"])

    def test_reuse_update_failure_is_surfaced_not_swallowed(self):
        # a redeploy must never silently keep stale config: an update failure raises.
        class NS:
            def list(self):
                return [{"id": "agent_1", "name": "Order intake"}]
            def update(self, agent_id, **kw):
                raise RuntimeError("update rejected")

        with self.assertRaises(Exception):
            self._client(NS()).create_or_reuse_agent(self._ENV)

    def test_reuse_without_update_method_is_surfaced(self):
        # SDK with no agents.update -> we cannot refresh in place; raise (don't reuse stale).
        class NS:
            def list(self):
                return [{"id": "agent_1", "name": "Order intake"}]

        with self.assertRaises(Exception):
            self._client(NS()).create_or_reuse_agent(self._ENV)


if __name__ == "__main__":
    unittest.main()
