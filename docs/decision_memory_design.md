# Decision Memory — design doc (Gap #3: uncertainty handling + fleet learning)

Status: **PROPOSED — for review before build.** Author: Trace. Scope: task #5, gap #3.

## 1. Goal

Give an agent a way to say *"I'm genuinely stuck/unsure — I need a human decision"*
instead of its only options today (guess & act, stage a cautious action, or fail). Then
turn each human resolution into a **durable, reusable lesson** that makes not just this
agent but *other similar agents* — potentially across users — resolve the same edge case
autonomously next time.

Two layers:
- **Uncertainty loop** (the immediate feature): pause → ask → human answers → agent
  continues, and the answer feeds future runs.
- **Decision memory / RAG** (the multiplier): resolved edge cases become embedded,
  tagged, retrievable lessons that reduce future pauses fleet-wide.

## 2. The core loop

```
run → agent hits a BLOCKING, consequential ambiguity it can't resolve by reading
    → emits `needs_input` block {question, context, options?, tags}
    → run status = needs_input; structured decision record stored; DM sent to owner
owner answers (structured card or DM)
    → answer stored on the decision; a fresh run is seeded with Q+A as context
    → generalization pass: is this reusable? if yes, distill a de-identified rule,
      embed it, mark reusable=true
future runs (any similar agent, subject to scope)
    → retrieve top-k similar reusable lessons (tag filter × vector similarity)
    → inject as guidance so the agent resolves autonomously instead of re-asking
```

## 3. RAG approach — Voyage call + Supabase pgvector (NOT a third-party RAG SaaS)

"RAG" here is two steps: **text→vector** (an API call to **Voyage**) and **store+search**
(**Supabase pgvector**, already in the schema). No separate RAG product is needed or wanted:

- A hosted RAG/vector SaaS would ship **raw human decisions to a third-party vendor**,
  which breaks the privacy model in §5 (the whole point is that raw decisions stay in our
  DB under RLS and only generalized, scrubbed lessons ever cross user boundaries).
- pgvector is already provisioned (`decision_embeddings`), so storage+search cost is $0
  incremental and the retrieval query stays under our control (needed for the double-gate
  and service-role scoping).

**Provider: Voyage (`voyage-3.5`).** Anthropic's recommended embeddings partner. Default
output is **1024 dims** (supports 256/512/1024/2048 via Matryoshka; 1536 is not a native
Voyage size), so the schema moves from `vector(1536)` → `vector(1024)` (migration 005).
New secret `VOYAGE_API_KEY` (env var, alongside the `SUPABASE_*` vars in render.yaml).

> Implementation note: wrap Voyage behind a tiny `embeddings.py` client with one function
> `embed(texts) -> list[vector]`. Keeps the provider swappable and the call sites clean.

## 4. Data model

Existing (migration 003) — fits the loop almost as-is:

- `agent_decisions(id, agent_id, owner_id, run_id, question, answer, context JSONB,
  reusable BOOL, status, created_at, answered_at)` — owner-scoped RLS.
- `decision_embeddings(id, decision_id, embedding vector, created_at)` — **service-role
  only** RLS (this is what lets fleet retrieval run without exposing raw rows).

### Migration 005 (proposed)
```sql
-- Voyage dims
ALTER TABLE decision_embeddings ALTER COLUMN embedding TYPE vector(1024);
-- generalized, de-identified lesson (the ONLY field allowed to cross user boundaries)
ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS generalized_lesson TEXT;
-- retrieval tags: app / action_kind / workflow_type (+ bundle_id for agent-kind gating)
ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '{}'::jsonb;
-- sharing scope: 'agent' | 'owner' | 'team' | 'global' (default owner = T1)
ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS share_scope TEXT NOT NULL DEFAULT 'owner';
-- status now includes: pending (awaiting answer) | answered | applied
-- pgvector similarity index
CREATE INDEX IF NOT EXISTS decision_embeddings_ivf
  ON decision_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```
Plus a **service-role RPC** `match_decisions(query_embedding, tag_filter, scope_filter,
match_count)` that does the cosine search and returns ONLY `{generalized_lesson, tags,
similarity}` — never `context`/`answer`/`owner_id`. This is the single crossing point.

## 5. Privacy & scoping (the crux — not the plumbing)

Split every decision into two artifacts:
- **Raw instance** — `question`, `answer`, `context`. Stays **owner-scoped** under RLS.
  Never leaves the owner.
- **Generalized lesson** — `generalized_lesson` (LLM-distilled, PII-scrubbed rule) + its
  embedding. The **only** thing eligible to cross user boundaries.

Scope tiers (blast radius), stored in `share_scope`:
- **T0 agent** — same agent. Always on.
- **T1 owner** — same user's other agents. On by default (same data owner). **Default.**
- **T2 team** — same team/org. Opt-in.
- **T3 global** — anonymized fleet. Opt-in; generalized lessons only.

**Build-now vs decide-later:** we build the full mechanism (embeddings, generalization,
`match_decisions` RPC capable of any scope), but **T2/T3 retrieval is gated behind a flag
that is OFF by default** (`DECISION_SHARE_GLOBAL=false`). So T0/T1 (owner's own data) works
live; flipping on cross-user sharing is a one-line policy change once the Phase-B posture is
decided. No consent UI is needed to ship T0/T1.

**"Similar agents, not same user"** = double-gate retrieval:
`agent-kind similarity` (via `bundle_id` tag now; `agent_embeddings` later) **×**
`situation similarity` (`decision_embeddings`). Stops a shipping-confirm lesson leaking
into an invoice-reconciliation agent.

## 6. Components

1. **`UNCERTAINTY_DIRECTIVE`** (config.py) — appended to *every* run trigger (staged or
   not). Strict: pause ONLY for genuinely blocking, consequential ambiguity unresolvable by
   reading; otherwise proceed with best judgment (and stage if outbound). Emit a fenced
   `needs_input` block: `[{question, context, options?}]`. Prevents over-asking.
2. **Parsing** — `activity.extract_questions(events)` mirroring `extract_proposals`;
   `_parse_needs_input` in runner (same fence-parsing shape).
3. **Run status** — add `needs_input` to `_build_run_record` / `session_status`.
4. **Storage** — `registry.decision_add / decisions_for_user / decision_answer /
   decision_set_status` (Supabase + file-mode), tagging {app, action_kind, workflow_type,
   bundle_id}.
5. **Embeddings** — `embeddings.py` (Voyage). On store: embed the generalized lesson,
   insert into `decision_embeddings` (service role).
6. **Generalization pass** — after an answer lands, one LLM call: "is this a reusable rule?
   if so, write a de-identified generalization; else null." Sets `reusable`,
   `generalized_lesson`.
7. **Retrieval + injection** — before a run, embed the run's situation/trigger, call
   `match_decisions` (T0/T1 now), inject top-k lessons into the trigger as guidance.
8. **UI** — structured `needs_input` card on activity/home (question + context + options +
   an answer box), plus the existing DM. Answering posts to a new endpoint.
9. **Answer loop** — `POST /api/u/decisions/<id>/answer` → store answer → seed a fresh run
   with Q+A (your pick: answer feeds the next run as context) → run generalization+embed.

## 7. Build batches (sequential, each compiled + tested)

- **B1 — Uncertainty loop, no RAG ✅ DONE:** directive + parse + `needs_input` status + DM.
- **B2 — Decision storage + structured UI + answer loop ✅ DONE:** `agent_decisions` records,
  tags, the answerable card, `.../answer` endpoint, answer→AUTO-IMMEDIATE-RESUME.
- **B3 — Embeddings + generalization ✅ DONE:** `embeddings.py` (Voyage), migrations 005/006,
  generalization/scrub pass, populate `decision_embeddings`.
- **B4 — Retrieval + injection (T0/T1) ✅ DONE:** `match_decisions` RPC (007), retrieve+inject
  into every run. T2/T3 gated off.
- **B5+ — Phase B (cross-user fleet learning):** see §9. Blocked on the sharing-posture
  decision; the crux is consent/safety, not code.

## 9. Phase B — cross-user fleet learning (decided design)

Phase A is owner-scoped and safe. Phase B widens the blast radius. Two decisions are LOCKED:

**Decision 1 — matching is autonomous; *applying a cross-user lesson* is trust-gated.**
Retrieval (`_retrieve_guidance`) already runs on every run, autonomous or supervised — that
stays. What changes is APPLICATION, gated by the lesson's trust tier:
- **T0/T1 (your own decisions)** → auto-apply on autonomous runs (your agent remembering
  what you already decided).
- **T2/T3 (cross-user)** → **provisional → confirmed promotion.** The FIRST time a cross-user
  lesson would influence your agent, it does NOT act silently: it surfaces a one-time
  human checkpoint ("A similar agent learned: X — apply it here?"). Confirm once → the
  lesson is **promoted to confirmed for that agent** and applies autonomously thereafter.
  A stranger's lesson earns autonomy on your agent by one confirmation, like a probationary
  rule. Even a never-pausing autonomous agent thus gets a single human touch per NEW
  cross-user lesson, then returns to autonomous.
- **Confidence gate:** T2/T3 require a HIGHER `min_similarity` than T0/T1; weak matches are
  dropped silently (never surfaced as noise).

**Decision 2 — provenance + retirement from the start.**
Record which lesson influenced which run, and support retiring/revoking a lesson (which
revokes its per-agent promotions). Without this a poisoned/wrong shared lesson is invisible.

**"And further" — earned fleet-defaults.** A lesson independently confirmed by MANY users
can graduate to a global auto-trusted default (T3) — trust earned through repeated human
confirmation, not asserted by a central authority. This is how the corpus improves
autonomously without hand-approval. (Later batch.)

### Phase B components
1. **Schema (migration 008+):** `lesson_promotions(agent_id, decision_id, status:
   provisional|confirmed|retired, confirmed_at)`; `lesson_applications(run_session_id,
   decision_id, agent_id, applied_at, outcome)` for provenance; consent columns
   (per-user contribute-scope + consume-scope); populate `agent_embeddings` for agent-kind
   gating.
2. **Consent model + UI:** per-user opt-in to contribute (owner→team→global) and consume;
   default `owner`. `DECISION_SHARE_GLOBAL` env gate stays until posture is set.
3. **Scoped retrieval:** extend `match_decisions` with a scope arg + agent-kind gate
   (`agent_embeddings` similarity × `decision_embeddings` similarity), returning each hit's
   trust status for this agent.
4. **Provisional application checkpoint:** a matched-but-unconfirmed cross-user lesson
   surfaces as a "confirm this lesson?" item (reuses the needs-you surface); confirming
   promotes it; then it auto-injects.
5. **Provenance + retirement:** log applications; a retire endpoint/UI that revokes
   promotions and stops injection.
6. **Scrub verification for T3:** second scrub/verify pass before a lesson goes global.

### Phase B open decisions (settle before build)
- Sharing posture: opt-in per tier (default) vs opt-out. (Parked by user.)
- Consent granularity: per-user vs per-agent.
- Fleet-default threshold: how many independent confirmations graduate a lesson to T3.

## 8. Open questions / risks

- **Voyage dependency + key** — new `VOYAGE_API_KEY`; file-mode/dev has no embeddings, so
  retrieval must degrade gracefully to tag-only (or no-op) when unconfigured.
- **Over-asking** — directive must be strict + we should track pause rate; a chatty agent
  is worse than a silent one. Consider a per-run cap (≤1 pause).
- **Generalization quality** — a bad generalization pollutes the fleet pool. Gate T3 behind
  human confirmation of the lesson (Phase B).
- **Scrubbing** — de-identification is LLM-best-effort; for T3 we likely need a second
  scrub/verify pass before a lesson is globally shared. Owner/team tiers are lower risk.
- **`agent_decisions.status` semantics** — reconcile with `pending_actions` (different
  concept: decisions are questions, pending are actions). Keep tables separate.
