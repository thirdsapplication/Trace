"""Peer selection — which OTHER agents' material is worth putting in front of this
agent's learning cycle.

A cycle that reads only its own agent's history can only ever relearn what that agent
already lived through: the same wasted re-reads, discovered the same slow way, once per
customer. Peers are how one agent's efficiency lesson reaches the next one. The risk on
the other side is worse than a missed lesson — an unrelated agent's transcripts read as
authoritative to the model that receives them — so this module is deliberately stingy
about who gets in.

Matching runs on TOOL NAMES and one embedding of an agent's public profile, never on
transcripts. That is the property the whole collective path rests on: peer discovery
never needs to see, and therefore never needs to un-scrub, the material the scrub pass
exists to protect.

Pure computation — no model calls, no network, no storage, nothing from trace.ui. The
bundles arrive from the registry and the payloads leave for the learning call; keeping
the rule in one testable place is why "who counts as a peer" gets the same answer on
the publish side and the consume side.
"""

import json
from datetime import datetime, timedelta, timezone

from . import activity, config


def _as_int(value, default=0):
    """An int from whatever a stored fingerprint or a caller handed us."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tool_set(fp):
    """The tool names in a fingerprint, as a set. Tolerates a missing or malformed
    fingerprint — a bundle written by an older build must not break selection."""
    tools = fp.get("tools") if isinstance(fp, dict) else None
    if not isinstance(tools, dict):
        return set()
    return {str(name) for name in tools if str(name).strip()}


def fingerprint(events_list):
    """What an agent actually DOES, from ``events_list`` (a list of per-session event
    lists): ``{"tools": {name: count}, "sessions": int, "avg_tokens": int}``.

    Tool names are not customer data. They are the names of the integrations the agent
    was wired to — identical strings across every deployment of the same connector,
    carrying nothing about whose mail or whose spreadsheet was touched. That is what
    makes the design work: a fingerprint can be stored in plaintext and matched against
    another customer's fingerprint without unscrubbing anything, so finding a peer never
    reaches into the material that needed scrubbing in the first place.
    """
    tools = {}
    sessions = 0
    total = 0
    for events in (events_list or []):
        if not isinstance(events, (list, tuple)):
            continue
        sessions += 1
        for e in events:
            if not isinstance(e, dict):
                continue
            tl = (e.get("type") or "").lower()
            # The same test as activity._compact's tool-call branch, duplicated
            # verbatim on purpose: if the two ever drift, an agent's transcript would
            # show calls its fingerprint does not count, and it would be matched
            # against peers on a tool list that is not the one it ran.
            if "mcp_tool_use" not in tl and not tl.endswith("tool_use"):
                continue
            name = str(e.get("name") or "").strip()
            # _compact labels an unnamed call "tool" so the transcript still reads;
            # here it is dropped instead, because a synthetic "tool" bucket would
            # intersect with every other agent's unnamed calls and manufacture overlap
            # between agents that share no integration at all.
            if not name:
                continue
            tools[name] = tools.get(name, 0) + 1
        total += _as_int(activity.session_token_usage(events).get("total_tokens"))
    return {"tools": tools,
            "sessions": sessions,
            "avg_tokens": (total // sessions) if sessions else 0}


def profile_text(agent, fp):
    """The text that gets embedded to answer "do these two agents do the same JOB" —
    the agent's stated purpose PLUS its tools, most-used first.

    The tool list is in here because name, description and trigger are written ONCE, at
    agent creation, and describe what the agent was MEANT to do. Two agents can read
    identically and behave nothing alike, and an agent can drift a long way from its own
    description without a word of it changing. The ranked tool list is the only part of
    this profile that comes from what actually happened.
    """
    a = agent if isinstance(agent, dict) else {}
    tools = fp.get("tools") if isinstance(fp, dict) else None
    tools = tools if isinstance(tools, dict) else {}
    # Ties broken by name, and counts deliberately left out of the text: the same
    # behaviour must always produce the same profile, or an agent re-embeds to a
    # slightly different point every cycle and its cosines against unchanged peers
    # drift for reasons that have nothing to do with what it does.
    ranked = [str(name) for name, _ in
              sorted(tools.items(), key=lambda kv: (-_as_int(kv[1]), str(kv[0])))]
    return "\n".join([
        f"Name: {str(a.get('name') or '').strip()}",
        f"Description: {str(a.get('description') or '').strip()}",
        f"Trigger: {str(a.get('trigger') or '').strip()}",
        "Tools used (most frequent first): " + (", ".join(ranked) or "(none)"),
    ])


def jaccard(a, b):
    """|a ∩ b| / |a ∪ b| over two sets. 0.0 when both are empty — two agents that
    called no tools at all have nothing in common, not everything."""
    sa = set(a or [])
    sb = set(b or [])
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def cosine(a, b):
    """Cosine similarity of two equal-length float vectors, in plain Python.

    Returns 0.0 on a length mismatch, empty input, a zero vector, or non-numeric
    contents. Every one of those means "cannot compare", so callers must read 0.0 as an
    absence of evidence for similarity and never as evidence of difference — which is
    exactly why ``classify`` reports a missing vector as None instead of calling this.
    """
    try:
        va = [float(x) for x in (a or [])]
        vb = [float(x) for x in (b or [])]
    except (TypeError, ValueError):
        return 0.0
    if not va or len(va) != len(vb):
        return 0.0
    na = sum(x * x for x in va) ** 0.5
    nb = sum(x * x for x in vb) ** 0.5
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(va, vb)) / (na * nb)


def classify(fp_a, fp_b, vec_a, vec_b):
    """Is B worth showing to A, and on what grounds? Returns ``{"included": bool,
    "tier": str|None, "overlap": float, "cosine": float|None, "rank": float}``.

    TOOL OVERLAP GATES, COSINE ONLY TIERS. The asymmetry is the whole rule.
    This engine optimises token efficiency, and efficiency lessons are mechanical —
    "check whether the sheet changed before re-reading it", "batch the searches instead
    of one per row". They are properties of the TOOL, not of the business, so they
    transfer between two agents driving the same integrations over completely unrelated
    work. Prose similarity is the thing that scores high by accident: two agents whose
    descriptions are both polite paragraphs about handling incoming requests embed close
    together while sharing not one integration, and nothing either learned helps the
    other. So similarity may sharpen the label on a peer that already shares tools, and
    it may never be the thing that lets a peer in.
    """
    overlap = jaccard(_tool_set(fp_a), _tool_set(fp_b))
    # A missing vector (no VOYAGE_API_KEY in this process, or a bundle published before
    # its embedding was written) must never read as cosine 0.0: 0.0 is a measurement
    # saying "different job", None is the absence of one. Both land on the conservative
    # tier, because nothing has established that the two agents do the same job — but
    # only None says so honestly, and this figure travels into the payload.
    cos = cosine(vec_a, vec_b) if (vec_a and vec_b) else None
    tier = ("same_job" if (cos is not None and cos >= config.PEER_SAME_JOB_COSINE)
            else "same_tooling")
    included = overlap >= config.PEER_MIN_TOOL_OVERLAP
    return {"included": included,
            "tier": tier if included else None,
            "overlap": overlap,
            "cosine": cos,
            # Used ONLY to order the survivors for the top-K cut. It is not a second
            # threshold: nothing is ever admitted or refused on this number, so the
            # cosine term inside it cannot smuggle in a peer the gate turned away.
            "rank": 0.6 * (cos or 0.0) + 0.4 * overlap}


def _as_utc(dt):
    """A naive datetime read as UTC. Subtracting a naive datetime from an aware one
    raises TypeError, and a test injecting ``now=datetime(...)`` must not be the thing
    that takes peer selection down."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_fresh(created_at, now, max_age_days):
    """Whether a bundle is recent enough to learn from.

    An absent or unparseable timestamp counts as FRESH. Dropping bundles because a
    storage backend changed its timestamp format would silently empty every peer list,
    and the symptom — "this agent has no peers" — looks like a fact about the fleet
    rather than like the bug it is.
    """
    raw = str(created_at or "").strip()
    if not raw:
        return True
    try:
        dt = _as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return True
    return (now - dt) <= timedelta(days=max_age_days)


def select_peers(agent_id, fp, vec, bundles, now=None):
    """The peers this agent may learn from, best first and at most
    ``config.PEER_MAX``: ``[{"bundle", "tier", "overlap", "shared_tools", "rank"}]``.

    Three bundles never get considered, whatever they score: the agent's OWN (reading
    its own sessions back through the peer channel would double-count them and let its
    habits corroborate themselves), anything not ``verified`` (only material the scrub
    pass AND its independent verifier cleared may cross a customer boundary), and
    anything older than ``config.PEER_BUNDLE_MAX_AGE_DAYS`` (a stale bundle describes a
    workflow that has since moved on, and its lessons argue for the old shape of it).
    """
    now = _as_utc(now) if isinstance(now, datetime) else datetime.now(timezone.utc)
    own_tools = _tool_set(fp)
    scored = []
    for bundle in (bundles or []):
        if not isinstance(bundle, dict):
            continue
        if agent_id and str(bundle.get("agent_id") or "") == str(agent_id):
            continue
        if not bundle.get("verified"):
            continue
        if not _is_fresh(bundle.get("created_at"), now, config.PEER_BUNDLE_MAX_AGE_DAYS):
            continue
        peer_fp = bundle.get("fingerprint")
        verdict = classify(fp, peer_fp, vec, bundle.get("embedding"))
        if not verdict["included"]:
            continue
        scored.append({"bundle": bundle,
                       "tier": verdict["tier"],
                       "overlap": verdict["overlap"],
                       # Computed here, where both fingerprints are in hand:
                       # peer_payloads is handed the selection alone and never sees
                       # this agent's own fingerprint, so it cannot intersect anything.
                       "shared_tools": sorted(own_tools & _tool_set(peer_fp)),
                       "rank": verdict["rank"]})
    scored.sort(key=lambda s: s["rank"], reverse=True)
    return scored[:config.PEER_MAX]


def peer_payloads(selected, budget_chars=config.LEARNING_PEER_BUDGET_CHARS):
    """The peer entries for the learning payload, in the order ``select_peers`` ranked
    them: ``[{"peer_ref", "tier", "tool_overlap", "shared_tools", "sessions",
    "memory"}]``.

    ``peer_ref`` is the ONLY identity a peer carries into the prompt, and it is a
    positional label, not a reference to anything. No agent id, agent name, owner id or
    email may appear here: the agent receiving this payload belongs to a DIFFERENT
    customer, and nothing it is given — nor anything a person later reads in the cycle's
    analysis or in a log line quoting it — may be usable to work out whose material this
    was. The bundles were already de-identified by the scrub pass; this is the second
    half of the same promise, kept in code rather than asked for in a prompt.
    """
    out = []
    used = 0
    budget = _as_int(budget_chars)
    for entry in (selected or []):
        if not isinstance(entry, dict):
            continue
        bundle = entry.get("bundle")
        bundle = bundle if isinstance(bundle, dict) else {}
        memory = bundle.get("memory")
        payload = {"peer_ref": f"peer agent {len(out) + 1}",
                   "tier": entry.get("tier"),
                   "tool_overlap": entry.get("overlap"),
                   "shared_tools": list(entry.get("shared_tools") or []),
                   "sessions": list(bundle.get("sessions") or []),
                   "memory": memory if isinstance(memory, dict) else {}}
        # Cost is measured on the serialized form, because that is what actually lands
        # in the user message the budget exists to bound.
        cost = len(json.dumps(payload, ensure_ascii=False, default=str))
        # A peer that does not fit is dropped WHOLE and ends the fill. Half a peer's
        # sessions still read as that agent's complete history, and the analysis call
        # would generalise from the truncated half as if it were the whole story.
        if used + cost > budget:
            break
        used += cost
        out.append(payload)
    return out
