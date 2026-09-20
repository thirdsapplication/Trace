"""Phase 4 — connection-layer orchestration (offline; all platform calls injected).

Computes the union of apps a customer's agents need, drives the per-app
connect -> ensure-vault -> bind -> verify sequence through an injected
``ConnectClient``, and maintains the per-customer ``connection`` record. This module
imports NO platform SDK (no ``anthropic``, no Pipedream): every external call goes
through the injected client, so the whole layer runs offline against a stub and a
future OAuth-client swap is a one-module change in ``connect_client.py``.
"""

import datetime
import re

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


def _apps_in_text(text):
    """Connectable apps NAMED in free text (a bundle's name/description/trigger/steps).
    Word-boundary matching — unlike the event-field substring idiom — because prose
    false-positives otherwise ('sales-driven' -> drive, 'spreadsheets' -> sheets).
    Returns ``[{slug, label}]``, one per distinct slug, in table order."""
    low = (text or "").lower()
    out, seen = [], set()
    for k, v in config.CONNECT_APP_TABLE.items():
        if v["slug"] in seen:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", low):
            seen.add(v["slug"])
            out.append({"slug": v["slug"], "label": v["label"]})
    return out


# Browsers are never the app a worker needs to connect — the SERVICE is, and in a
# browser capture the service name appears only in the window title.
BROWSER_APPS = {"safari", "google chrome", "chrome", "firefox", "arc", "brave",
                "microsoft edge", "edge", "opera", "chromium"}


def _unmapped_names(event):
    """Best names for an app we observed but cannot connect, most specific first.

    Reporting ``event["app"]`` alone told the worker "Safari" — true, useless, and
    actively misleading, since Safari is not the thing they needed to connect. For a
    browser the service lives in the window title, so that is what is surfaced;
    for a native app the app name is already right.
    """
    app = (event.get("app") or "").strip()
    window = (event.get("window") or "").strip()
    if app.lower() in BROWSER_APPS:
        return [window] if window else []
    return [app] if app else []



def app_union(approved_bundles, event_log):
    """The de-duplicated set of connectable apps across the customer's approved bundles.
    Returns ``(apps, unmapped)`` where ``apps`` is ``[{slug, label, observed_as}]`` in
    first-seen order and ``unmapped`` lists observed app names whose events matched
    nothing in the app table (surfaced, never silently dropped). Each evidence event is
    scanned across ``app``/``window``/``detail`` so a service named only in the window
    title (browser captures) is still found. Apps NAMED in the bundle's own text
    (name/description/trigger/steps and the worker's review-step correction,
    ``user_description``) are unioned in as well — this is how described
    (no-recording) workflows get a connectable app list, and it also catches a capture
    whose evidence events missed an app the steps or the worker's correction clearly
    name. ``unmapped`` stays
    event-driven: prose naming an unknown app is not deterministically detectable."""
    idx = {e.get("id"): e for e in (event_log or []) if isinstance(e, dict)}
    by_slug, unmapped, seen_unmapped = {}, [], set()
    # WHICH WORKFLOWS NEED EACH APP. This loop already knows — it is iterating bundles —
    # and the answer was thrown away when everything collapsed into by_slug. Frames 12 and
    # 26 both caption an app with the agents that need it ("Retail recap · Inbox triage"),
    # and neither could, for want of keeping a name we already had in hand.
    def _note(slug, bundle):
        name = (bundle.get("user_description") or bundle.get("name")
                or bundle.get("id") or "").strip()
        if not name:
            return
        used = by_slug[slug].setdefault("used_by", [])
        if name not in used:
            used.append(name)

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
                    _note(slug, b)
                continue
            for name in _unmapped_names(ev):           # nothing matched -> surface it
                if name.lower() not in seen_unmapped:
                    seen_unmapped.add(name.lower())
                    unmapped.append(name)
        text = " ".join([str(b.get("name") or ""), str(b.get("description") or ""),
                         str(b.get("trigger") or ""), str(b.get("user_description") or "")] +
                        [str(s) for s in (b.get("steps") or [])])
        for m in _apps_in_text(text):
            if m["slug"] not in by_slug:
                by_slug[m["slug"]] = {"slug": m["slug"], "label": m["label"],
                                      "observed_as": [f"named in workflow: "
                                                      f"{b.get('name') or b.get('id') or '?'}"]}
            _note(m["slug"], b)
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
                           # which workflows need this app (12, 26)
                           "used_by": list(a.get("used_by") or []),
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
            "used_by": list(a.get("used_by") or []),
            "connected": False, "usable": False,
            "account": {"account_id": None, "identity_email": None},
            "verified": "unverified", "flag": None, "binding_ref": None})
        record["apps"][a["slug"]]["label"] = a["label"]
        record["apps"][a["slug"]]["observed_as"] = a.get("observed_as", [])
        # recomputed each time: a re-review can add or drop a workflow
        record["apps"][a["slug"]]["used_by"] = list(a.get("used_by") or [])
    record["unmapped"] = list(unmapped)
    record["updated_at"] = _now()
    return record


def connect_app(client, record, slug):
    """Start the Connect flow for one app. Returns ``{token, connect_url}`` for the UI,
    or ``{reused: True, identity_email}`` when this customer ALREADY has the app
    authorized and no browser round trip is needed.

    Pipedream accounts are scoped to ``external_user_id``, which is stable per Trace user
    across every session and agent — so a worker who connected Gmail for one agent is
    still connected for the next one. This used to mint a fresh Connect token
    unconditionally and reset the entry to unconnected, forcing the whole OAuth dance
    again for an authorization that already existed.

    Reuse is NOT a shortcut past the identity check: the caller still runs
    ``bind_and_verify``, so a reused account whose email doesn't match the session's
    expected identity is flagged and left ``usable=False`` exactly as a fresh one is.
    """
    entry = record["apps"].setdefault(slug, {})
    existing = None
    try:                                                # probe: already authorized?
        existing = client.get_account(record["external_user_id"], slug)
    except Exception:                                   # noqa: BLE001 — treat as absent
        existing = None
    if existing and existing.get("connected"):
        # Stash the account so the Verify step doesn't re-fetch it. Safe ONLY on the
        # reuse path: nobody re-authorizes in between, so it can't go stale. The fresh
        # path must always fetch, since the account doesn't exist until the worker
        # approves in the browser.
        entry.update({"connected": True, "verified": "unverified", "flag": None,
                      "reused_account": existing})
        record["updated_at"] = _now()
        return {"reused": True, "identity_email": existing.get("identity_email"),
                "connect_url": None, "token": None}

    tok = client.create_connect_token(record["external_user_id"], slug)
    entry.update({"connected": False, "verified": "unverified", "flag": None})
    record["updated_at"] = _now()
    return dict(tok or {}, reused=False)


def bind_and_verify(client, record, slug, expected_email):
    """After the worker taps Allow (real or simulated): ensure the vault, bind the
    connection into it, fetch the connected account, and verify its identity against the
    worker-confirmed email. A mismatch is FLAGGED and left ``usable=False`` — never
    proceed silently. Mutates and returns the record."""
    uid = record["external_user_id"]
    entry = record["apps"].setdefault(slug, {"label": slug, "observed_as": []})
    if not record.get("vault_ref"):                      # ensure once per customer
        record["vault_ref"] = client.ensure_vault(uid)["vault_ref"]
    binding = client.bind_connection(record["vault_ref"], uid, slug)
    # On the reuse path connect_app already fetched the account; skip the second
    # round trip (each one is a real hop to Pipedream and Verify felt broken at
    # three of them). Always re-fetch on the fresh path.
    account = entry.pop("reused_account", None) or client.get_account(uid, slug)
    v = verify_identity(account.get("identity_email"), expected_email)

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
    elif v["reason"] == "no_expected_identity":          # the worker never confirmed an email
        entry["verified"], entry["usable"] = "unverified", False
        entry["flag"] = ("could not verify the connected account's identity — "
                         "confirm your work email")
    else:                                                # the APP exposed no email to check
        # Google labels its accounts with the email, so the check is automatic there.
        # Many apps (QuickBooks, Shopify, Stripe, ...) hand Pipedream a token and a
        # company or store name and no email at all — nothing Trace can compare. That
        # is not a mismatch and not a failure of the connection: the worker approved
        # this account seconds ago, under their own customer id. So the entry stays
        # unusable until the worker confirms it is theirs (``confirm_by_worker``), and
        # the UI offers that one tap instead of a dead end.
        entry["verified"], entry["usable"] = "unverified", False
        entry["confirmable"] = bool(account.get("connected"))
        entry["flag"] = (f"{entry.get('label') or slug} doesn't share an account email, so "
                         "palza couldn't check it automatically. If this is the account you "
                         "just approved for work, confirm it below.")
    record["updated_at"] = _now()
    return record


def confirm_by_worker(record, slug):
    """The worker vouches for a connected account whose app exposed no email to verify.
    Only an entry that is connected and awaiting exactly that confirmation can be
    confirmed: a mismatch (a real email that differs) is never confirmable, and neither
    is an app that was never connected. Returns the entry, or None when not applicable."""
    entry = (record.get("apps") or {}).get(slug)
    if not entry or not entry.get("confirmable") or not entry.get("connected"):
        return None
    if entry.get("verified") not in ("unverified", "worker_confirmed"):
        return None
    entry["verified"], entry["usable"], entry["flag"] = "worker_confirmed", True, None
    entry["confirmable"] = False
    entry["confirmed_at"] = _now()
    record["updated_at"] = _now()
    return entry
