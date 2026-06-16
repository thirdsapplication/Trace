"""Phase 4 — connection-layer orchestration (offline; all platform calls injected).

Computes the union of apps a customer's agents need, drives the per-app
connect -> ensure-vault -> bind -> verify sequence through an injected
``ConnectClient``, and maintains the per-customer ``connection`` record. This module
imports NO platform SDK (no ``anthropic``, no Pipedream): every external call goes
through the injected client, so the whole layer runs offline against a stub and a
future OAuth-client swap is a one-module change in ``connect_client.py``.
"""

import datetime

from . import config


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _slug_for(app):
    """Map one observed app string to a Pipedream ``{slug, label}`` via the table's
    substring idiom (same as compiler.derive_connectors), or None if unmapped."""
    low = (app or "").lower()
    return next((v for k, v in config.CONNECT_APP_TABLE.items() if k in low), None)


def _apps_in_event(event):
    """Connectable apps observed in ONE event, matching `app`, `window`, and `detail`
    against the app table. A real browser capture puts the browser in `app`
    (e.g. "Google Chrome") and the service only in the window title
    (e.g. "… - Gmail"), so all three fields are scanned. Returns ``[{slug, label,
    observed_as}]`` — one per distinct slug, in first-seen order."""
    out, seen = [], set()
    for field in ("app", "window", "detail"):
        text = (event.get(field) or "").strip()
        if not text:
            continue
        entry = _slug_for(text)
        if entry and entry["slug"] not in seen:
            seen.add(entry["slug"])
            out.append({"slug": entry["slug"], "label": entry["label"], "observed_as": text})
    return out


def app_union(approved_bundles, event_log):
    """The de-duplicated set of connectable apps across the customer's approved bundles.
    Returns ``(apps, unmapped)`` where ``apps`` is ``[{slug, label, observed_as}]`` in
    first-seen order and ``unmapped`` lists observed app names whose events matched
    nothing in the app table (surfaced, never silently dropped). Each evidence event is
    scanned across ``app``/``window``/``detail`` so a service named only in the window
    title (browser captures) is still found."""
    idx = {e.get("id"): e for e in (event_log or []) if isinstance(e, dict)}
    by_slug, unmapped, seen_unmapped = {}, [], set()
    for b in approved_bundles or []:
        for eid in b.get("evidence_event_ids", []):
            ev = idx.get(eid)
            if not isinstance(ev, dict):
                continue
            matches = _apps_in_event(ev)
            if matches:
                for m in matches:
                    slug = m["slug"]
                    if slug not in by_slug:
                        by_slug[slug] = {"slug": slug, "label": m["label"],
                                         "observed_as": [m["observed_as"]]}
                    elif m["observed_as"] not in by_slug[slug]["observed_as"]:
                        by_slug[slug]["observed_as"].append(m["observed_as"])
                continue
            app = (ev.get("app") or "").strip()            # nothing matched -> surface it
            if app and app.lower() not in seen_unmapped:
                seen_unmapped.add(app.lower())
                unmapped.append(app)
    return list(by_slug.values()), unmapped


def verify_identity(connected_email, expected_email):
    """Compare a connected account's email to the worker-confirmed email. Three-state:
    ``ok`` (match), ``no_expected_identity`` / ``no_connected_identity`` (-> unverified),
    or ``mismatch`` (both present and differ -> flag)."""
    c = (connected_email or "").strip().lower()
    e = (expected_email or "").strip().lower()
    if not e:
        return {"match": False, "reason": "no_expected_identity"}
    if not c:
        return {"match": False, "reason": "no_connected_identity"}
    if c == e:
        return {"match": True, "reason": "ok"}
    return {"match": False, "reason": "mismatch"}


def new_record(external_user_id, union=None, expected_identity=""):
    """A fresh connection record for one customer/session."""
    apps = {}
    for a in (union or []):
        apps[a["slug"]] = {"label": a["label"], "observed_as": a.get("observed_as", []),
                           "connected": False, "usable": False,
                           "account": {"account_id": None, "identity_email": None},
                           "verified": "unverified", "flag": None, "binding_ref": None}
    return {"external_user_id": external_user_id, "expected_identity": expected_identity,
            "vault_ref": None, "apps": apps, "unmapped": [], "updated_at": _now()}


def merge_union(record, union, unmapped):
    """Refresh a record's app set from a recomputed union, preserving any per-app state
    already captured (connected/verified/etc.)."""
    for a in union:
        record["apps"].setdefault(a["slug"], {
            "label": a["label"], "observed_as": a.get("observed_as", []),
            "connected": False, "usable": False,
            "account": {"account_id": None, "identity_email": None},
            "verified": "unverified", "flag": None, "binding_ref": None})
        record["apps"][a["slug"]]["label"] = a["label"]
        record["apps"][a["slug"]]["observed_as"] = a.get("observed_as", [])
    record["unmapped"] = list(unmapped)
    record["updated_at"] = _now()
    return record


def connect_app(client, record, slug):
    """Start the Connect flow for one app. Returns ``{token, connect_url}`` for the UI;
    marks the per-app entry in-progress."""
    tok = client.create_connect_token(record["external_user_id"], slug)
    entry = record["apps"].setdefault(slug, {})
    entry.update({"connected": False, "verified": "unverified", "flag": None})
    record["updated_at"] = _now()
    return tok


def bind_and_verify(client, record, slug, expected_email):
    """After the worker taps Allow (real or simulated): ensure the vault, bind the
    connection into it, fetch the connected account, and verify its identity against the
    worker-confirmed email. A mismatch is FLAGGED and left ``usable=False`` — never
    proceed silently. Mutates and returns the record."""
    uid = record["external_user_id"]
    if not record.get("vault_ref"):                      # ensure once per customer
        record["vault_ref"] = client.ensure_vault(uid)["vault_ref"]
    binding = client.bind_connection(record["vault_ref"], uid, slug)
    account = client.get_account(uid, slug)
    v = verify_identity(account.get("identity_email"), expected_email)

    entry = record["apps"].setdefault(slug, {"label": slug, "observed_as": []})
    entry["binding_ref"] = binding.get("binding_ref")
    entry["token_expires_at"] = binding.get("expires_at")   # for refresh-before-run (WS5)
    entry["connected"] = bool(account.get("connected"))
    entry["account"] = {"account_id": account.get("account_id"),
                        "identity_email": account.get("identity_email")}
    if v["match"]:
        entry["verified"], entry["usable"], entry["flag"] = "yes", True, None
    elif v["reason"] == "mismatch":
        entry["verified"], entry["usable"] = "no", False
        entry["flag"] = (f"connected account {account.get('identity_email')!r} does not "
                         f"match the confirmed worker email {expected_email!r} — not used")
    else:                                                # missing identity on either side
        entry["verified"], entry["usable"] = "unverified", False
        entry["flag"] = ("could not verify the connected account's identity — "
                         "confirm your work email") if v["reason"] == "no_expected_identity" \
            else "the connected account exposed no identity email to verify"
    record["updated_at"] = _now()
    return record
