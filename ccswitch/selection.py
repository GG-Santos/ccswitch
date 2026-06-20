"""Choose which account to switch to.

Blind "next in rotation" can land on an account that is itself nearly full. When
we switch *because* the current account is near its limit, the useful choice is
the account with the most headroom - and when every account is maxed, the one
that *resets soonest* rather than giving up. This module probes the candidates
and ranks them with both signals.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ccswitch import usage, vault

# Don't switch to a sooner-resetting (but still maxed) account unless it beats the
# active account's reset by at least this margin - avoids pointless churn.
SWITCH_MARGIN = 60.0
_INF = float("inf")


def _is_usable(u: dict, threshold: float) -> bool:
    if u.get("status") == "rejected":
        return False
    pct = u.get("percent")
    return pct is None or pct < threshold  # unknown usage counts as usable


def _pct(u: dict) -> float:
    pct = u.get("percent")
    if pct is None:
        return 999.0 if u.get("error") else 50.0  # errored sorts last; unknown = moderate
    return pct


def _reset(u: dict) -> float:
    return u.get("reset_at") or _INF


def _rank_key(u: dict, threshold: float):
    """Sort key (lower = better).

    Tier 0 = usable now, ranked by headroom (lowest usage first), tiebreak
    soonest reset. Tier 1 = maxed, ranked by soonest reset. Any usable account
    therefore beats any maxed one.
    """
    if _is_usable(u, threshold):
        return (0, _pct(u), _reset(u))
    return (1, _reset(u), _pct(u))


def _fmt_reset(reset_at) -> str:
    if not reset_at:
        return "?"
    return datetime.fromtimestamp(reset_at, tz=timezone.utc).astimezone().strftime("%H:%M")


def pick_best(data: dict, skip: set[str] | None = None, threshold: float = 100.0,
              active_reset_at: float | None = None, include_active: bool = False):
    """Return (name, usage, reason) for the best account to switch to.

    Candidates are all accounts except anything in `skip` (and, unless
    `include_active`, the active account).
    - If any candidate is usable now, return the one with the most headroom.
    - If all candidates are maxed, return the soonest-to-reset one *only* when it
      resets meaningfully sooner than the active account (pre-positioning so you
      are unblocked first); otherwise return (None, {}, reason) meaning "stay and
      wait". With no candidates at all, returns (None, {}, reason).

    `include_active=True` keeps the active account in the running, so the manual
    `best` command can answer "you're already on the best one" instead of moving
    you off a usable account onto a worse one.
    """
    skip = set(skip or set())
    active = data.get("active")
    if active and not include_active:
        skip.add(active)
    candidates = [n for n in data.get("accounts", {}) if n not in skip]
    if not candidates:
        return None, {}, "no other account to switch to"

    oauths = {n: vault.get_oauth(data, n) for n in candidates}
    probed = usage.probe_all(oauths, candidates)
    ranked = sorted(probed.items(), key=lambda kv: _rank_key(kv[1], threshold))
    name, u = ranked[0]

    if _is_usable(u, threshold):
        pct = u.get("percent")
        room = f"most headroom ({pct:.0f}%)" if pct is not None else "most headroom"
        return name, u, room

    # All candidates are maxed: switch only if this one frees up sooner than active.
    cand_reset = u.get("reset_at")
    if cand_reset is not None and (active_reset_at is None
                                   or cand_reset < active_reset_at - SWITCH_MARGIN):
        return name, u, f"all near limit; '{name}' resets first at {_fmt_reset(cand_reset)}"
    return None, {}, "all near limit; active resets as soon as any - waiting"


def known_cooldowns(data: dict) -> set[str]:
    """Account names known to be exhausted from already-available info (no probes).

    Combines the daemon's persisted `exhausted_until` (resets still in the future)
    with the on-disk probe cache (recently seen rejected / ~100% accounts). Lets
    `next` skip dead accounts without spending a probe."""
    from ccswitch import runstate

    now = datetime.now(timezone.utc).timestamp()
    cooling: set[str] = set()

    status = runstate.read_status() or {}
    for name, reset_at in (status.get("exhausted_until") or {}).items():
        try:
            if float(reset_at) > now:
                cooling.add(name)
        except (TypeError, ValueError):
            continue

    for name in data.get("accounts", {}):
        blob = vault.get_oauth(data, name)
        if not blob:
            continue
        cached = usage._disk_cache_get(blob.get("accessToken", ""))
        if cached and (cached.get("status") == "rejected"
                       or (cached.get("percent") or 0) >= 99):
            cooling.add(name)
    return cooling
