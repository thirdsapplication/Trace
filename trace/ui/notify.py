"""Outbound notifications: email delivery, and the preference gate in front of it.

INERT WITHOUT ``RESEND_API_KEY`` — exactly the shape embeddings take without
VOYAGE_API_KEY. The app works, the in-app messages and badges still appear, and
notifications simply do not leave the building. That is the intended state until a
key and a verified sending domain exist.

NOTHING HERE MAY RAISE. Every entry point is reached from a run path — an agent
finishing, a decision being persisted — where a delivery problem must never become a
failed run. Errors are swallowed and reported through the return value, which callers
are free to ignore.

Env:
  RESEND_API_KEY   the Resend API key. Absent => this module does nothing.
  EMAIL_FROM       e.g. "palza <notifications@yourdomain.com>". Must be a domain
                   verified in Resend, or Resend refuses the send.
"""

import json
import os
import ssl
import urllib.error
import urllib.request

_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT = 10
# A real product name, because the default urllib agent is blocked at the edge.
_USER_AGENT = "palza/1.0 (+https://github.com/thirdsapplication/Trace)"


def is_configured():
    """True when email can actually be sent. Cheap enough to call per notification."""
    return bool(os.environ.get("RESEND_API_KEY") and _from_address())


def _from_address():
    return os.environ.get("EMAIL_FROM", "").strip()


def send(to, subject, text):
    """Send one plain-text email. Returns True only if Resend accepted it.

    Plain text on purpose: it renders everywhere, cannot break, and these messages
    are three sentences telling someone an agent needs them — not a newsletter.
    """
    key = os.environ.get("RESEND_API_KEY")
    if not key or not _from_address() or not to:
        return False
    body = json.dumps({
        "from": _from_address(),
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "text": text,
    }).encode("utf-8")
    req = urllib.request.Request(_ENDPOINT, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    # Identify ourselves. urllib defaults to "Python-urllib/3.x", and Cloudflare —
    # which fronts the Resend API — refuses that signature with 403 "error code: 1010"
    # before the request ever reaches Resend. Their dashboard shows nothing, so it reads
    # as a config problem when the key and from-address are both perfectly correct.
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        # 4xx here is almost always an unverified from-domain or a bad key. Say so on
        # the server log — silent non-delivery is the worst outcome for a notification.
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:                                  # noqa: BLE001
            detail = ""
        print(f"[notify] resend refused ({e.code}): {detail}", flush=True)
        return False
    except Exception as e:                                 # noqa: BLE001 — never raise
        print(f"[notify] send failed: {type(e).__name__}: {e}", flush=True)
        return False


# --- the notifications themselves -------------------------------------------------

def agent_needs_you(prefs, email, agent_name, text):
    """"Ping me when an agent is blocked" (18) — questions and approvals, as they happen.

    Gated on the preference; the in-app message has already been recorded by the
    caller either way, so declining email loses nothing but the interruption.
    """
    if not email or not (prefs or {}).get("notify_agent_blocked", True):
        return False
    name = agent_name or "An agent"
    return send(email, f"{name} needs you",
                f"{name} says:\n\n  {text}\n\n"
                "Open palza to answer — nothing is sent or changed until you do.")


def team_invite(email, team_name, inviter, link):
    """An invitation is NOT a notification: it is the entire mechanism by which someone
    joins, so it is deliberately not preference-gated. There is also nobody to hold a
    preference — the recipient may not have an account yet."""
    if not email:
        return False
    who = inviter or "A teammate"
    return send(email, f"{who} invited you to {team_name} on palza",
                f"{who} has invited you to join {team_name} on palza.\n\n"
                f"Accept the invitation:\n  {link}\n\n"
                "palza watches one workday, finds the work that repeats, and builds an "
                "agent for each. Nothing leaves without your approval.")
