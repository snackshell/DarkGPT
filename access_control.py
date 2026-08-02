"""
Access control for the DarkGPT bot, backed by Supabase.

A person must be *approved* before the bot will answer them. Unknown users are
recorded as `pending` and the admin approves / denies them. The admin can also
pre-grant access by numeric Telegram ID or by @username.

This module talks to Supabase through its PostgREST endpoint using aiohttp, so
it stays fully async and needs no extra dependency. The bot authenticates with
the project's service_role key, which bypasses Row Level Security — RLS is left
enabled with no public policies, so the publishable/anon key cannot touch this
table.
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# ============================================
# CONFIG
# ============================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
REST_URL = f"{SUPABASE_URL}/rest/v1/bot_users" if SUPABASE_URL else ""
SETTINGS_URL = f"{SUPABASE_URL}/rest/v1/bot_settings" if SUPABASE_URL else ""
RPC_URL = f"{SUPABASE_URL}/rest/v1/rpc" if SUPABASE_URL else ""

# Fallback daily request cap if the backend is unreachable / unseeded.
DEFAULT_RPD = 20


def _parse_admin_ids() -> set:
    raw = os.environ.get("ADMIN_IDS", "")
    ids = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part and part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS = _parse_admin_ids()

# Short-lived cache: telegram_id -> (record, expiry_ts). Keeps the hot path
# (every incoming message) from hitting the database each time.
_CACHE_TTL = 20
_status_cache: Dict[int, Tuple[Dict[str, Any], float]] = {}


def is_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


# ============================================
# CACHE HELPERS
# ============================================
def _cache_get(tid: int) -> Optional[Dict[str, Any]]:
    v = _status_cache.get(tid)
    if v and v[1] > time.time():
        return v[0]
    return None


def _cache_set(tid: int, record: Dict[str, Any]):
    _status_cache[tid] = (record, time.time() + _CACHE_TTL)


def clear_cache(tid: Optional[int] = None):
    if tid is None:
        _status_cache.clear()
    else:
        _status_cache.pop(tid, None)


# ============================================
# LOW-LEVEL REST
# ============================================
async def _get(params: Dict[str, str]) -> List[Dict[str, Any]]:
    async with aiohttp.ClientSession() as s:
        async with s.get(REST_URL, headers=_headers(), params=params, timeout=15) as r:
            r.raise_for_status()
            return await r.json()


async def _post(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            REST_URL,
            headers=_headers({"Prefer": "return=representation"}),
            json=data,
            timeout=15,
        ) as r:
            r.raise_for_status()
            return await r.json()


async def _patch(params: Dict[str, str], data: Dict[str, Any]) -> List[Dict[str, Any]]:
    async with aiohttp.ClientSession() as s:
        async with s.patch(
            REST_URL,
            headers=_headers({"Prefer": "return=representation"}),
            params=params,
            json=data,
            timeout=15,
        ) as r:
            r.raise_for_status()
            return await r.json()


# ============================================
# LOOKUPS
# ============================================
async def get_by_telegram_id(tid: int) -> Optional[Dict[str, Any]]:
    rows = await _get({"telegram_id": f"eq.{tid}", "limit": "1"})
    return rows[0] if rows else None


async def get_by_username(username: str) -> Optional[Dict[str, Any]]:
    username = username.lstrip("@").strip()
    if not username:
        return None
    # ilike without wildcards = case-insensitive exact match.
    rows = await _get({"username": f"ilike.{username}", "limit": "1"})
    return rows[0] if rows else None


async def list_by_status(status: str, limit: int = 50) -> List[Dict[str, Any]]:
    return await _get(
        {"status": f"eq.{status}", "order": "requested_at.desc", "limit": str(limit)}
    )


async def counts() -> Dict[str, int]:
    out = {"pending": 0, "approved": 0, "denied": 0}
    for st in out:
        rows = await _get({"status": f"eq.{st}", "select": "id"})
        out[st] = len(rows)
    return out


# ============================================
# WRITES
# ============================================
async def _create(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = await _post(data)
    return rows[0] if rows else None


async def create_pending(tid: int, username: Optional[str], first_name: Optional[str]):
    return await _create(
        {
            "telegram_id": tid,
            "username": username or None,
            "first_name": first_name,
            "status": "pending",
        }
    )


async def _set_status_by_tid(tid: int, status: str, decided_by: int):
    rows = await _patch(
        {"telegram_id": f"eq.{tid}"},
        {"status": status, "decided_by": decided_by, "decided_at": _now_iso()},
    )
    clear_cache(tid)
    return rows[0] if rows else None


async def _decide(identifier: str, status: str, decided_by: int) -> Optional[Dict[str, Any]]:
    """Approve/deny by numeric Telegram ID or @username, creating a record if
    one does not exist yet (a username-only pre-grant has telegram_id = NULL
    and gets linked when that person first messages the bot)."""
    identifier = identifier.strip()
    if identifier.lstrip("-").isdigit():
        tid = int(identifier)
        existing = await get_by_telegram_id(tid)
        if existing:
            return await _set_status_by_tid(tid, status, decided_by)
        rec = await _create(
            {
                "telegram_id": tid,
                "status": status,
                "decided_by": decided_by,
                "decided_at": _now_iso(),
            }
        )
        clear_cache(tid)
        return rec

    username = identifier.lstrip("@").strip()
    if not username:
        return None
    existing = await get_by_username(username)
    if existing:
        rows = await _patch(
            {"id": f"eq.{existing['id']}"},
            {"status": status, "decided_by": decided_by, "decided_at": _now_iso()},
        )
        if existing.get("telegram_id"):
            clear_cache(existing["telegram_id"])
        return rows[0] if rows else None
    return await _create(
        {
            "username": username,
            "status": status,
            "decided_by": decided_by,
            "decided_at": _now_iso(),
        }
    )


async def approve(identifier: str, decided_by: int) -> Optional[Dict[str, Any]]:
    return await _decide(identifier, "approved", decided_by)


async def deny(identifier: str, decided_by: int) -> Optional[Dict[str, Any]]:
    return await _decide(identifier, "denied", decided_by)


async def _link_username(row_id: int, tid: int, username: Optional[str], first_name: Optional[str]):
    rows = await _patch(
        {"id": f"eq.{row_id}"},
        {"telegram_id": tid, "username": username or None, "first_name": first_name},
    )
    return rows[0] if rows else None


# ============================================
# ADMIN CHECK
# ============================================
async def is_admin(tid: int) -> bool:
    if tid in ADMIN_IDS:
        return True
    if not is_configured():
        return False
    rec = _cache_get(tid)  # warm after the access gate ran this turn
    if rec is None:
        try:
            rec = await get_by_telegram_id(tid)
        except Exception:
            logger.exception("is_admin lookup failed")
            return False
    return bool(rec and rec.get("is_admin"))


# ============================================
# RPD — daily request limit
# ============================================
async def _rpc(fn: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{RPC_URL}/{fn}", headers=_headers(), json=payload, timeout=15
        ) as r:
            r.raise_for_status()
            return await r.json()


async def get_rpd_limit() -> int:
    if not is_configured():
        return DEFAULT_RPD
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                SETTINGS_URL,
                headers=_headers(),
                params={"key": "eq.rpd_limit", "select": "value", "limit": "1"},
                timeout=15,
            ) as r:
                r.raise_for_status()
                rows = await r.json()
        if rows:
            return int(rows[0]["value"])
    except Exception:
        logger.exception("get_rpd_limit failed")
    return DEFAULT_RPD


async def set_rpd_limit(n: int) -> int:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            SETTINGS_URL,
            headers=_headers(
                {"Prefer": "resolution=merge-duplicates,return=representation"}
            ),
            params={"on_conflict": "key"},
            json={"key": "rpd_limit", "value": str(int(n)), "updated_at": _now_iso()},
            timeout=15,
        ) as r:
            r.raise_for_status()
            rows = await r.json()
    return int(rows[0]["value"]) if rows else int(n)


async def bump_usage(tid: int) -> Tuple[bool, int, int]:
    """Consume one daily request. Returns (allowed, used_today, limit).
    Fails open (allowed=True) if the backend errors, so a transient DB issue
    never blocks an approved user."""
    try:
        rows = await _rpc("bump_usage", {"p_telegram_id": tid})
        if rows:
            row = rows[0]
            return (bool(row["allowed"]), int(row["used"]), int(row["lim"]))
    except Exception:
        logger.exception("bump_usage failed for %s (failing open)", tid)
    return (True, 0, DEFAULT_RPD)


def usage_today(rec: Optional[Dict[str, Any]]) -> int:
    """Read a record's request count for the current UTC day (0 if stale)."""
    if not rec:
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    if rec.get("usage_date") == today:
        return int(rec.get("usage_count") or 0)
    return 0


def effective_limit(rec: Optional[Dict[str, Any]], global_limit: int) -> int:
    """A user's personal override if set, otherwise the global limit."""
    if rec and rec.get("rpd_override") is not None:
        return int(rec["rpd_override"])
    return global_limit


async def set_user_rpd(identifier: str, value: Optional[int], decided_by: int) -> Optional[Dict[str, Any]]:
    """Set (or clear, with value=None) a per-user daily limit by Telegram ID or
    @username. Creates an approved record if the user isn't known yet, so you can
    pre-grant a higher limit before they arrive."""
    identifier = identifier.strip()
    val = None if value is None else int(value)

    if identifier.lstrip("-").isdigit():
        tid = int(identifier)
        existing = await get_by_telegram_id(tid)
        if existing:
            rows = await _patch({"telegram_id": f"eq.{tid}"}, {"rpd_override": val})
            clear_cache(tid)
            return rows[0] if rows else None
        rec = await _create(
            {
                "telegram_id": tid,
                "status": "approved",
                "rpd_override": val,
                "decided_by": decided_by,
                "decided_at": _now_iso(),
            }
        )
        clear_cache(tid)
        return rec

    username = identifier.lstrip("@").strip()
    if not username:
        return None
    existing = await get_by_username(username)
    if existing:
        rows = await _patch({"id": f"eq.{existing['id']}"}, {"rpd_override": val})
        if existing.get("telegram_id"):
            clear_cache(existing["telegram_id"])
        return rows[0] if rows else None
    return await _create(
        {
            "username": username,
            "status": "approved",
            "rpd_override": val,
            "decided_by": decided_by,
            "decided_at": _now_iso(),
        }
    )


# ============================================
# THE GATE
# ============================================
async def resolve_access(
    tid: int, username: Optional[str], first_name: Optional[str]
) -> Tuple[str, bool, Optional[Dict[str, Any]]]:
    """Determine whether a user may use the bot.

    Returns (status, is_new_request, record) where status is one of:
      'admin'    -> configured admin (env or DB), always allowed
      'approved' -> allowed
      'pending'  -> awaiting a decision
      'denied'   -> refused
      'error'    -> backend unavailable
    Creates a pending record (and flags is_new_request) the first time an
    unknown, non-admin user is seen.
    """
    if tid in ADMIN_IDS:
        return ("admin", False, None)

    if not is_configured():
        # Access control can't work without a backend; fail closed for
        # everyone except env-configured admins (handled above).
        logger.warning("Supabase not configured; denying non-admin user %s", tid)
        return ("denied", False, None)

    cached = _cache_get(tid)
    if cached is not None:
        status = cached.get("status", "pending")
        if cached.get("is_admin"):
            status = "approved"
        return (status, False, cached)

    try:
        rec = await get_by_telegram_id(tid)
        is_new = False

        if rec is None:
            # Maybe the admin pre-granted this @username before they arrived.
            if username:
                urec = await get_by_username(username)
                if urec and urec.get("telegram_id") is None:
                    rec = await _link_username(urec["id"], tid, username, first_name)
            if rec is None:
                rec = await create_pending(tid, username, first_name)
                is_new = True

        status = rec.get("status", "pending") if rec else "pending"
        if rec and rec.get("is_admin"):
            status = "approved"
        if rec:
            _cache_set(tid, rec)
        return (status, is_new, rec)
    except Exception:
        logger.exception("resolve_access failed for %s", tid)
        return ("error", False, None)


async def peek_status(tid: int) -> Optional[Dict[str, Any]]:
    """Read-only lookup that never creates a record (used by /whoami)."""
    if not is_configured():
        return None
    try:
        return await get_by_telegram_id(tid)
    except Exception:
        logger.exception("peek_status failed for %s", tid)
        return None
