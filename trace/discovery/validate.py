"""Validate / normalize / repair Discovery Engine output against the discovery.json
contract (discovery.schema.json / DISCOVERY_SCHEMA.md).

Mirrors the event-log validator: coerce and repair obvious format slips, flag
problems out-of-band, never crash on a model slip. Repairs are domain-neutral and
carry no answer-key-derived content.
"""

VALID_CONFIDENCE = {"high", "medium", "low"}

_BUNDLE_FIELDS = ["name", "description", "trigger", "steps", "frequency",
                  "evidence_event_ids", "why_automatable", "confidence"]
_ANTI_FIELDS = ["name", "description", "why_judgment", "recommended_handling",
                "evidence_event_ids", "confidence"]


class DiscoveryReport:
    def __init__(self):
        self.errors = []   # structural problems repaired or dropped
        self.flags = []    # things worth a human eyeball

    def error(self, msg):
        self.errors.append(msg)

    def flag(self, msg):
        self.flags.append(msg)

    def __str__(self):
        out = []
        if self.errors:
            out.append(f"{len(self.errors)} repair(s):")
            out += [f"  - {e}" for e in self.errors]
        if self.flags:
            out.append(f"{len(self.flags)} flag(s):")
            out += [f"  - {f}" for f in self.flags]
        return "\n".join(out) if out else "clean"


def empty_discovery():
    return {"bundles": [], "anti_bundles": [],
            "unclustered_or_noise_event_ids": [], "notes": ""}


def _as_list(v):
    if isinstance(v, list):
        return v
    if v in (None, ""):
        return []
    return [v]


def _clean_evidence(raw_ids, valid_ids, report, where):
    """Keep only evidence ids that exist in the event log; flag unknown ones."""
    out, seen = [], set()
    for rid in _as_list(raw_ids):
        rid = str(rid).strip()
        if not rid:
            continue
        if rid in valid_ids:
            if rid not in seen:
                seen.add(rid)
                out.append(rid)
        else:
            report.error(f'{where} cited unknown evidence id "{rid}" (dropped)')
    return out


def _norm_confidence(c, report, where):
    if c in VALID_CONFIDENCE:
        return c
    report.error(f'{where} had invalid confidence "{c}" -> defaulted to low')
    return "low"


def _norm_bundle(b, idx, valid_ids, report):
    if not isinstance(b, dict):
        report.error("dropped a non-object bundle")
        return None
    ev = _clean_evidence(b.get("evidence_event_ids"), valid_ids, report, f"bundle #{idx}")
    if not ev:
        report.flag(f"bundle #{idx} has no valid evidence ids")
    out = {
        "id": str(b.get("id") or f"B{idx}"),
        "name": str(b.get("name") or "").strip(),
        "description": str(b.get("description") or "").strip(),
        "trigger": str(b.get("trigger") or "").strip(),
        "steps": [str(s) for s in _as_list(b.get("steps"))],
        "frequency": str(b.get("frequency") or "").strip(),
        "evidence_event_ids": ev,
        "why_automatable": str(b.get("why_automatable") or "").strip(),
        "confidence": _norm_confidence(b.get("confidence"), report, f"bundle #{idx}"),
    }
    return out


def _norm_anti(a, idx, valid_ids, report):
    if not isinstance(a, dict):
        report.error("dropped a non-object anti-bundle")
        return None
    ev = _clean_evidence(a.get("evidence_event_ids"), valid_ids, report, f"anti-bundle #{idx}")
    if not ev:
        report.flag(f"anti-bundle #{idx} has no valid evidence ids")
    return {
        "id": str(a.get("id") or f"A{idx}"),
        "name": str(a.get("name") or "").strip(),
        "description": str(a.get("description") or "").strip(),
        "why_judgment": str(a.get("why_judgment") or "").strip(),
        "recommended_handling": str(a.get("recommended_handling") or "").strip(),
        "evidence_event_ids": ev,
        "confidence": _norm_confidence(a.get("confidence"), report, f"anti-bundle #{idx}"),
    }


def _dedupe_ids(items, prefix):
    """Ensure ids are unique and non-empty; renumber on collision."""
    seen, n = set(), 0
    for it in items:
        n += 1
        if not it["id"] or it["id"] in seen:
            it["id"] = f"{prefix}{n}"
        seen.add(it["id"])
    return items


def validate_discovery(obj, valid_ids, report):
    """Validate/repair a discovery object against the contract. Always returns a
    schema-shaped dict (never raises)."""
    if not isinstance(obj, dict):
        report.error("discovery output was not a JSON object")
        return empty_discovery()

    bundles = []
    for i, b in enumerate(_as_list(obj.get("bundles")), start=1):
        nb = _norm_bundle(b, i, valid_ids, report)
        if nb is not None:
            bundles.append(nb)
    anti = []
    for i, a in enumerate(_as_list(obj.get("anti_bundles")), start=1):
        na = _norm_anti(a, i, valid_ids, report)
        if na is not None:
            anti.append(na)

    _dedupe_ids(bundles, "B")
    _dedupe_ids(anti, "A")

    noise = _clean_evidence(obj.get("unclustered_or_noise_event_ids"),
                            valid_ids, report, "unclustered_or_noise")
    notes = obj.get("notes")
    notes = str(notes) if notes is not None else ""

    return {
        "bundles": bundles,
        "anti_bundles": anti,
        "unclustered_or_noise_event_ids": noise,
        "notes": notes,
    }
