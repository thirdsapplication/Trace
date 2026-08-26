"""Teams (frame 30) — membership, invites, and the owner set lesson sharing reads.

Tables come from migration 017. Its own module rather than more of registry.py, which
is already past 1500 lines; it reuses registry's storage helpers so file mode behaves
identically to everything else.

A TEAM COMES INTO BEING ON THE FIRST INVITE. Philip's call, and the right one: there is
no empty-team state to design and no create screen to build, because inviting somebody
is the only reason a team needs to exist at all.

ONE TEAM PER USER, enforced here rather than in the schema. team_members' primary key
allows many, but "my team's lessons" has no answer with two, so the reads take the first
and the writes refuse a second. Loosening that later is a code change, not a migration.

Every function is best-effort against a missing table: 017 may not be applied, and a
Settings tab must not 500 because teams do not exist yet.
"""

from . import registry as reg
from . import supabase_client as supa

ROLES = ("admin", "member")
# Roles that may change the team. "owner" is not written by this module — a trigger in
# the live database sets it on the creator — but it is unambiguously an admin, and
# treating it as a plain member locked the person who created a team out of it.
ADMIN_ROLES = ("admin", "owner")


def _now():
    return reg._now_iso()


def team_for_user(root, uid):
    """``{team_id, name, role}`` for the user's team, or None. First team wins."""
    if not uid:
        return None
    if supa.is_configured():
        try:
            m = supa.get_admin().table("team_members").select("team_id, role") \
                .eq("user_id", uid).order("joined_at").limit(1).execute()
            row = (m.data or [None])[0]
            if not row:
                return None
            t = supa.get_admin().table("teams").select("id, name") \
                .eq("id", row["team_id"]).maybe_single().execute() or supa.NO_ROW
            return {"team_id": str(row["team_id"]),
                    "name": (t.data or {}).get("name") or "Your team",
                    "role": row.get("role") or "member"}
        except Exception:                                  # noqa: BLE001 — pre-migration
            return None
    hit = next((m for m in reg._load(root, "team_members.json").values()
                if m.get("user_id") == uid), None)
    if not hit:
        return None
    team = reg._load(root, "teams.json").get(str(hit["team_id"])) or {}
    return {"team_id": str(hit["team_id"]), "name": team.get("name") or "Your team",
            "role": hit.get("role") or "member"}


def members(root, team_id):
    """Every member of one team, oldest first."""
    if not team_id:
        return []
    if supa.is_configured():
        try:
            r = supa.get_admin().table("team_members") \
                .select("user_id, role, joined_at") \
                .eq("team_id", team_id).order("joined_at").execute()
            return [{"user_id": str(x["user_id"]), "role": x.get("role") or "member",
                     "joined_at": x.get("joined_at")} for x in (r.data or [])]
        except Exception:                                  # noqa: BLE001
            return []
    rows = [m for m in reg._load(root, "team_members.json").values()
            if str(m.get("team_id")) == str(team_id)]
    return sorted(rows, key=lambda m: m.get("joined_at") or "")


def owner_ids(root, uid):
    """The user ids whose lessons count as "my team's" — the uuid[] migration 018 wants.

    Returns [] when the user has no team, which makes retrieve_lessons behave exactly as
    it did before teams existed. That is what makes passing this safe to adopt at once.
    """
    t = team_for_user(root, uid)
    return [m["user_id"] for m in members(root, t["team_id"])] if t else []


def create(root, uid, name):
    """Create a team with ``uid`` as admin, or return the one they already have."""
    if not uid:
        return None
    existing = team_for_user(root, uid)
    if existing:
        return existing
    if supa.is_configured():
        try:
            r = supa.get_admin().table("teams").insert({
                "name": str(name or "Your team")[:120], "created_by": uid}).execute()
            tid = str((r.data or [{}])[0].get("id"))
            # The creator's membership row may already exist: the live database has a
            # trigger that adds it (as "owner") the moment the team is inserted. Doing it
            # again raised a duplicate-key error which this except swallowed into None,
            # and the caller reported team_create_failed -- while the TEAM row stayed
            # committed. Every first invite therefore failed, and left the user owning a
            # team the code then refused to let them administer.
            try:
                supa.get_admin().table("team_members").insert({
                    "team_id": tid, "user_id": uid, "role": "admin"}).execute()
            except Exception:                              # noqa: BLE001
                pass                                       # already a member, by trigger
        except Exception:                                  # noqa: BLE001
            return None
        return team_for_user(root, uid)
    with reg._LOCK:
        teams = reg._load(root, "teams.json")
        tid = f"team_{len(teams) + 1}"
        teams[tid] = {"id": tid, "name": str(name or "Your team")[:120],
                      "created_by": uid, "created_at": _now()}
        reg._save(root, "teams.json", teams)
        mem = reg._load(root, "team_members.json")
        mem[f"{tid}:{uid}"] = {"team_id": tid, "user_id": uid, "role": "admin",
                               "joined_at": _now()}
        reg._save(root, "team_members.json", mem)
    return team_for_user(root, uid)


def invite(root, team_id, email, role, invited_by):
    """Invite one address. Re-inviting the same address REFRESHES it rather than
    failing — the UI offers Resend, and that is exactly what Resend has to mean."""
    email = str(email or "").strip().lower()
    role = role if role in ROLES else "member"
    if not email or not team_id:
        return None
    if supa.is_configured():
        try:
            supa.get_admin().table("team_invites").upsert({
                "team_id": team_id, "email": email, "role": role,
                "invited_by": invited_by, "invited_at": _now(), "accepted_at": None,
            }, on_conflict="team_id,email").execute()
        except Exception:                                  # noqa: BLE001
            return None
        return {"team_id": str(team_id), "email": email, "role": role}
    with reg._LOCK:
        inv = reg._load(root, "team_invites.json")
        key = f"{team_id}:{email}"
        inv[key] = {"id": key, "team_id": team_id, "email": email, "role": role,
                    "invited_by": invited_by, "invited_at": _now(), "accepted_at": None}
        reg._save(root, "team_invites.json", inv)
    return {"team_id": str(team_id), "email": email, "role": role}


def pending_invites(root, team_id):
    """Invites nobody has accepted yet."""
    if not team_id:
        return []
    if supa.is_configured():
        try:
            r = supa.get_admin().table("team_invites") \
                .select("id, email, role, invited_at") \
                .eq("team_id", team_id).is_("accepted_at", "null") \
                .order("invited_at", desc=True).execute()
            return [dict(x, id=str(x["id"])) for x in (r.data or [])]
        except Exception:                                  # noqa: BLE001
            return []
    return sorted((i for i in reg._load(root, "team_invites.json").values()
                   if str(i.get("team_id")) == str(team_id) and not i.get("accepted_at")),
                  key=lambda i: i.get("invited_at") or "", reverse=True)


def cancel_invite(root, invite_id):
    if supa.is_configured():
        try:
            supa.get_admin().table("team_invites").delete() \
                .eq("id", invite_id).execute()
        except Exception:                                  # noqa: BLE001
            pass
        return
    with reg._LOCK:
        inv = reg._load(root, "team_invites.json")
        inv.pop(str(invite_id), None)
        reg._save(root, "team_invites.json", inv)


def accept_for(root, email, uid):
    """Join whichever team invited this address. Called at SIGNUP.

    Without this an invite is decorative — a row created, listed, resent and cancelled,
    that never once put anybody in a team. Returns the team joined, or None.
    """
    email = str(email or "").strip().lower()
    if not email or not uid or team_for_user(root, uid):
        return None                                        # already in a team
    if supa.is_configured():
        try:
            r = supa.get_admin().table("team_invites").select("id, team_id, role") \
                .eq("email", email).is_("accepted_at", "null") \
                .order("invited_at").limit(1).execute()
            row = (r.data or [None])[0]
            if not row:
                return None
            supa.get_admin().table("team_members").insert({
                "team_id": row["team_id"], "user_id": uid,
                "role": row.get("role") or "member"}).execute()
            supa.get_admin().table("team_invites").update({"accepted_at": _now()}) \
                .eq("id", row["id"]).execute()
        except Exception:                                  # noqa: BLE001
            return None
        return team_for_user(root, uid)
    with reg._LOCK:
        inv = reg._load(root, "team_invites.json")
        hit = next((i for i in sorted(inv.values(),
                                      key=lambda x: x.get("invited_at") or "")
                    if i.get("email") == email and not i.get("accepted_at")), None)
        if not hit:
            return None
        mem = reg._load(root, "team_members.json")
        mem[f"{hit['team_id']}:{uid}"] = {
            "team_id": hit["team_id"], "user_id": uid,
            "role": hit.get("role") or "member", "joined_at": _now()}
        reg._save(root, "team_members.json", mem)
        hit["accepted_at"] = _now()
        reg._save(root, "team_invites.json", inv)
    return team_for_user(root, uid)


def set_role(root, team_id, uid, role):
    if role not in ROLES:
        return
    if supa.is_configured():
        try:
            supa.get_admin().table("team_members").update({"role": role}) \
                .eq("team_id", team_id).eq("user_id", uid).execute()
        except Exception:                                  # noqa: BLE001
            pass
        return
    with reg._LOCK:
        mem = reg._load(root, "team_members.json")
        key = f"{team_id}:{uid}"
        if key in mem:
            mem[key]["role"] = role
            reg._save(root, "team_members.json", mem)


def remove_member(root, team_id, uid):
    if supa.is_configured():
        try:
            supa.get_admin().table("team_members").delete() \
                .eq("team_id", team_id).eq("user_id", uid).execute()
        except Exception:                                  # noqa: BLE001
            pass
        return
    with reg._LOCK:
        mem = reg._load(root, "team_members.json")
        mem.pop(f"{team_id}:{uid}", None)
        reg._save(root, "team_members.json", mem)
