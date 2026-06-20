"""Background watcher: poll the active account's usage and rotate early.

Run it next to your `claude` session:
    python -m ccswitch.cli daemon --threshold 90

It swaps the shared credentials file, so the *next* request/session picks up
the new account. It cannot abort an in-flight turn -- early rotation is the
mitigation (default threshold 90%).

Each cycle it probes only the active account (cheap). When that account crosses
the threshold it probes the candidates once to pick a target (see selection),
switches, and notifies. A status file and log record what it is doing so other
commands can see it is alive.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from ccswitch import creds, notify, oauth, runstate, selection, vault
from ccswitch.actions import checkpoint_active, switch_to
from ccswitch.usage import is_near_limit, probe


def _stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


def _fmt_reset(reset_at: float | None) -> str:
    if not reset_at:
        return "?"
    dt = datetime.fromtimestamp(reset_at, tz=timezone.utc).astimezone()
    return dt.strftime("%H:%M")


def _emit(line: str) -> None:
    print(line, flush=True)
    runstate.append_log(line)


def _choose_target(data: dict, strategy: str, skip: set[str], threshold: float,
                   active_reset_at):
    """Pick the account to switch to per the strategy. Returns (name, usage, reason)."""
    if strategy == "best":
        # Reset-aware over ALL other accounts (NOT skipping ones seen exhausted),
        # so an all-maxed situation lands on the shortest cooldown - even if that
        # account was the one we rotated off earlier. The ranking already prefers
        # a usable account over any maxed one, so not skipping is safe.
        return selection.pick_best(data, threshold=threshold,
                                   active_reset_at=active_reset_at)
    # next strategy: literal rotation, but prefer accounts not on a known cooldown.
    cooldown_skip = set(skip) | selection.known_cooldowns(data)
    target = vault.next_in_rotation(data, skip=cooldown_skip) \
        or vault.next_in_rotation(data, skip=skip)
    return target, {}, ("next in rotation" if target else "no account to rotate to")


# Cap for the sleep-to-reset path so the loop stays responsive / clock-skew safe.
_RESET_SLEEP_CAP = 3600.0


def _compute_sleep(usage, threshold, interval, max_interval, *, switched,
                   all_exhausted, exhausted_until, now):
    """How long to sleep before the next cycle.

    Pure function (no I/O) so it is unit-testable. The idea: probe rarely when
    usage is far from the limit, tightly when close, and not at all until reset
    when everything is exhausted.
    """
    if all_exhausted:
        future = [t for t in (exhausted_until or {}).values() if t > now]
        if future:
            return max(interval, min(min(future) - now + 30, _RESET_SLEEP_CAP))
        return interval
    if switched:
        return interval  # re-check the freshly switched-in account promptly
    pct = usage.get("percent")
    if pct is None or pct >= threshold - 5:
        return interval  # unknown or near the limit -> tightest cadence
    frac = max(0.0, min(1.0, pct / threshold))
    return interval + (max_interval - interval) * (1 - frac)


def run(
    threshold: float = 90.0,
    interval: float = 60.0,
    skip_exhausted: bool = True,
    strategy: str = "best",
    checkpoint_every: float = 600.0,
    once: bool = False,
    max_interval: float = 600.0,
) -> int:
    max_interval = max(max_interval, interval)  # never below the floor
    started = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    _emit(
        f"[ccswitch] daemon started: threshold={threshold}% interval={interval}-{max_interval}s "
        f"strategy={strategy} skip_exhausted={skip_exhausted} pid={os.getpid()}"
    )
    # name -> epoch when its window resets; skip until then. Seed from the last
    # status file so a restart does not immediately re-pick a maxed account.
    exhausted_until: dict[str, float] = {}
    prior = runstate.read_status()
    if prior and isinstance(prior.get("exhausted_until"), dict):
        exhausted_until = {k: float(v) for k, v in prior["exhausted_until"].items()}
    last_checkpoint = 0.0
    last_switch = prior.get("last_switch") if prior else None
    all_exhausted_notified = False
    backoff = interval

    while True:
        data = vault.load()
        active = data.get("active")
        now = time.time()

        if not active or not vault.get_oauth(data, active):
            _emit(f"[{_stamp()}] no usable active account set; idle")
            runstate.write_status({"pid": os.getpid(), "started": started,
                                   "active": active, "last_check": _stamp(),
                                   "state": "idle", "last_switch": last_switch,
                                   "exhausted_until": exhausted_until})
            if once:
                return 0
            time.sleep(interval)
            continue

        # Periodically sync the active account's rotated refresh token back to
        # the vault so a long-lived daemon does not let it go stale.
        if now - last_checkpoint >= checkpoint_every:
            with vault.transaction() as tdata:
                checkpoint_active(tdata)
            last_checkpoint = now

        usage = probe(vault.get_oauth(data, active), cache=False)

        # Active token expired mid-watch: refresh it once and re-probe so the
        # daemon keeps reading usage instead of being stuck logging 401s.
        # Refresh over the network OUTSIDE the lock, then persist under a lock.
        if usage.get("auth_error"):
            try:
                new_blob = oauth.refresh(vault.get_oauth(data, active))
            except Exception as exc:
                new_blob = None
                _emit(f"[{_stamp()}] active token rejected; refresh skipped: {exc}")
            if new_blob is not None:
                with vault.transaction() as tdata:
                    if active in tdata["accounts"]:
                        tdata["accounts"][active]["claudeAiOauth"] = new_blob
                _emit(f"[{_stamp()}] active token rejected; refreshed")
                data = vault.load()  # pull the refreshed token into the loop copy
                creds.write_live_oauth(vault.get_oauth(data, active),
                                       backups_dir=vault.backups_dir())
                usage = probe(vault.get_oauth(data, active), cache=False)

        # On a network/transport error, back off (capped) instead of hammering.
        if (usage.get("error") or "").startswith("network error"):
            backoff = min(backoff * 2, max(interval, 600))
            _emit(f"[{_stamp()}] probe network error; backing off to {backoff:.0f}s")
            runstate.write_status({"pid": os.getpid(), "started": started,
                                   "active": active, "last_check": _stamp(),
                                   "state": "backoff", "last_switch": last_switch,
                                   "exhausted_until": exhausted_until})
            if once:
                return 0
            time.sleep(backoff)
            continue
        backoff = interval  # recovered

        pct = usage.get("percent")
        pct_s = f"{pct:.0f}%" if pct is not None else "?"
        windows = usage.get("windows") or {}
        win_s = " ".join(f"{w}:{p:.0f}%" for w, p in windows.items())
        line = (
            f"[{_stamp()}] active={active} usage={pct_s} "
            f"[{win_s}] status={usage['status']} reset~{_fmt_reset(usage.get('reset_at'))}"
        )
        if usage.get("error"):
            line += f" err={usage['error']}"
        _emit(line)

        switched_to = None
        all_exhausted = False
        pre_positioned = False
        if is_near_limit(usage, threshold):
            if usage.get("reset_at"):
                exhausted_until[active] = usage["reset_at"]
            skip = {n for n, t in exhausted_until.items() if t > now} if skip_exhausted else set()
            skip.add(active)
            target, tgt_usage, reason = _choose_target(
                data, strategy, skip, threshold, usage.get("reset_at"))
            if target:
                res = switch_to(target)
                switched_to = target
                last_switch = f"{_stamp()} {active} -> {target}"
                all_exhausted_notified = False  # we found headroom again
                note = f" [{res['refresh']}]" if res.get("refresh") else ""
                _emit(f"[{_stamp()}] >>> rotated {active} -> {target} ({reason}){note}")
                notify.notify("ccswitch: account switched",
                              f"{active} was at {pct_s}; now on {target} - {reason}")
                # If we pre-positioned onto a still-maxed (shortest-cooldown)
                # account, sleep until ITS reset rather than re-probing in a
                # cycle. Record its reset so sleep-to-reset targets it.
                tgt_reset = tgt_usage.get("reset_at")
                tgt_maxed = (tgt_usage.get("status") == "rejected"
                             or (tgt_usage.get("percent") or 0) >= threshold)
                if tgt_maxed and tgt_reset:
                    exhausted_until[target] = tgt_reset
                    pre_positioned = True
            else:
                all_exhausted = True
                _emit(f"[{_stamp()}] !!! {reason}; staying on {active}")
                # Toast once per all-exhausted episode, not every cycle.
                if not all_exhausted_notified:
                    notify.notify("ccswitch: all accounts maxed out",
                                  f"every account is near its limit; staying on {active}")
                    all_exhausted_notified = True

        runstate.write_status({
            "pid": os.getpid(), "started": started, "active": switched_to or active,
            "last_check": _stamp(), "state": "switched" if switched_to else "watching",
            "usage_percent": pct, "windows": windows, "last_switch": last_switch,
            "threshold": threshold, "strategy": strategy,
            "exhausted_until": {n: t for n, t in exhausted_until.items() if t > now},
        })

        if once:
            return 0
        sleep_for = _compute_sleep(
            usage, threshold, interval, max_interval,
            switched=bool(switched_to),
            all_exhausted=all_exhausted or pre_positioned,
            exhausted_until=exhausted_until, now=now,
        )
        _emit(f"[{_stamp()}] next check in {sleep_for:.0f}s")
        time.sleep(sleep_for)
