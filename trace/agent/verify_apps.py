"""Check every slug in CONNECT_APP_TABLE against Pipedream's real catalogue.

    python -m trace.agent.verify_apps

Why this exists. The table maps text we observe in a capture ("Shopify", a
window title, a domain) onto a Pipedream app slug. The observed side is ours to
choose; the slug side is Pipedream's to define, and a wrong one does not fail
here — it fails at connect time, in front of the worker, after they have already
built an agent. That is the worst possible place to discover a typo.

So the table is written with slugs that are *plausible*, and this turns them into
slugs that are *known*. Run it after adding any entry.

Needs Pipedream credentials, from the environment or the local keystore. Without
them it says so and exits rather than pretending everything is fine.
"""

import sys

from . import config
from .connect_client import ConnectClient


def _credentials():
    """Pipedream credentials from env, falling back to the local keystore."""
    creds = {
        "project_id": config.PIPEDREAM_PROJECT,
        "client_id": config.PIPEDREAM_CLIENT_ID,
        "client_secret": config.PIPEDREAM_CLIENT_SECRET,
        "environment": config.PIPEDREAM_ENVIRONMENT,
    }
    if all(creds[k] for k in ("project_id", "client_id", "client_secret")):
        return creds
    try:
        from ..ui import keystore
        stored = keystore.get_pipedream() or {}
    except Exception:
        stored = {}
    for key in ("project_id", "client_id", "client_secret", "environment"):
        creds[key] = creds.get(key) or stored.get(key) or ""
    return creds


def check(catalogue):
    """Compare the table against a ``[{slug, name}]`` catalogue.

    Returns ``(unknown, known)`` where ``unknown`` is the slugs Pipedream does not
    have — each with the observed keys that would have routed to it, so the report
    says what breaks rather than only what is wrong.
    """
    available = {a.get("slug") for a in catalogue if a.get("slug")}
    by_slug = {}
    for observed, entry in config.CONNECT_APP_TABLE.items():
        by_slug.setdefault(entry["slug"], {"label": entry["label"], "observed": []})
        by_slug[entry["slug"]]["observed"].append(observed)
    unknown = {s: v for s, v in by_slug.items() if s not in available}
    known = {s: v for s, v in by_slug.items() if s in available}
    return unknown, known


def main():
    creds = _credentials()
    missing = [k for k in ("project_id", "client_id", "client_secret") if not creds[k]]
    if missing:
        print("No Pipedream credentials available (missing: "
              f"{', '.join(missing)}).")
        print("Set PIPEDREAM_PROJECT_ID / PIPEDREAM_CLIENT_ID / "
              "PIPEDREAM_CLIENT_SECRET, or save them in the UI's settings.")
        print("\nNothing was verified — every slug in the table remains unconfirmed.")
        return 2

    client = ConnectClient(project_id=creds["project_id"],
                           client_id=creds["client_id"],
                           client_secret=creds["client_secret"],
                           environment=creds["environment"] or "production")
    catalogue = client.list_apps()
    print(f"Pipedream catalogue: {len(catalogue)} apps")

    unknown, known = check(catalogue)
    print(f"table: {len(known) + len(unknown)} apps, {len(known)} confirmed\n")
    if not unknown:
        print("All slugs exist in the catalogue.")
        return 0

    print(f"{len(unknown)} slug(s) Pipedream does NOT have — these would fail at "
          "connect time:\n")
    names = {a.get("slug"): a.get("name") for a in catalogue}
    for slug, info in sorted(unknown.items()):
        print(f"  {slug!r} ({info['label']})")
        print(f"      reached by: {', '.join(info['observed'])}")
        near = [s for s in names if slug.split('_')[0] in s][:5]
        if near:
            print(f"      did you mean: {', '.join(sorted(near))}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
