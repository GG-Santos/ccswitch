"""ccswitch command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from ccswitch import __version__, creds, crypto, daemon, oauth, runstate, selection, vault
from ccswitch.actions import checkpoint_active, switch_to
from ccswitch.usage import probe, probe_all
from ccswitch.vault import VaultError


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _fmt_reset(reset_at) -> str:
    if not reset_at:
        return "-"
    dt = datetime.fromtimestamp(reset_at, tz=timezone.utc).astimezone()
    return dt.strftime("%m-%d %H:%M")


def _windows_str(u: dict) -> str:
    w = u.get("windows") or {}
    return " ".join(f"{k}:{v:.0f}%" for k, v in w.items()) or "-"


# --------------------------------------------------------------------------- #
# pool


def cmd_add(args) -> int:
    live = creds.read_live_oauth()
    if not live:
        return _err(
            "no logged-in account found in the live credentials file.\n"
            "Run `claude` and `/login` first, then re-run `ccswitch add`."
        )
    with vault.transaction() as data:
        existed = args.name in data["accounts"]
        vault.add_account(data, args.name, live, label=args.label)
        # The live credentials file now belongs to this account, so it becomes
        # the active one. Keeping active == owner-of-live-file is what makes the
        # checkpoint-before-swap logic correct on the next switch.
        data["active"] = args.name
    sub = live.get("subscriptionType", "?")
    verb = "updated" if existed else "added"
    print(f"{verb} account '{args.name}' (subscription: {sub}); it is now active.")
    print(f"vault: {vault.vault_path()}")
    return 0


def cmd_remove(args) -> int:
    with vault.transaction() as data:
        was_active = data.get("active") == args.name
        removed = vault.remove_account(data, args.name)
    if not removed:
        return _err(f"unknown account '{args.name}'. See `ccswitch list`.")
    print(f"removed '{args.name}' from the vault.")
    if was_active:
        print("note: it was the active account; the live credentials file is "
              "unchanged (you are still logged in as it until you switch).")
    return 0


def cmd_rename(args) -> int:
    try:
        with vault.transaction() as data:
            vault.rename_account(data, args.old, args.new)
    except KeyError:
        return _err(f"unknown account '{args.old}'. See `ccswitch list`.")
    except ValueError:
        return _err(f"'{args.new}' already exists; pick another name.")
    print(f"renamed '{args.old}' -> '{args.new}'.")
    return 0


def cmd_list(args) -> int:
    data = vault.load()
    if not data["accounts"]:
        print("no accounts stored. Add one with: ccswitch add <name>")
        return 0
    active = data.get("active")
    probed = {}
    if getattr(args, "usage", False):
        probed = probe_all({n: vault.get_oauth(data, n) for n in data["accounts"]})

    if probed:
        print(f"{'':2}{'NAME':<16}{'SUB':<7}{'WINDOWS':<18}{'STATUS':<10}{'RESET'}")
        for name, acct in data["accounts"].items():
            sub = acct.get("claudeAiOauth", {}).get("subscriptionType", "?")
            u = probed.get(name, {})
            mark = "*" if name == active else " "
            print(f"{mark:2}{name:<16}{sub:<7}{_windows_str(u):<18}"
                  f"{u.get('status', '?'):<10}{_fmt_reset(u.get('reset_at'))}")
    else:
        print(f"{'':2}{'NAME':<16}{'SUB':<8}{'LABEL'}")
        for name, acct in data["accounts"].items():
            sub = acct.get("claudeAiOauth", {}).get("subscriptionType", "?")
            mark = "*" if name == active else " "
            print(f"{mark:2}{name:<16}{sub:<8}{acct.get('label', '')}")
    print(f"\nrotation: {' -> '.join(data.get('rotation', [])) or '(none)'}")
    return 0


# --------------------------------------------------------------------------- #
# switching


def _report_switch(name: str, res: dict) -> None:
    print(f"switched to '{name}'")
    if res.get("refresh"):
        print(f"token: {res['refresh']}")
    if res.get("backup"):
        print(f"backup: {res['backup']}")
    print("note: open or restart `claude` for the new account to take effect.")


def cmd_use(args) -> int:
    data = vault.load()
    if args.name not in data["accounts"]:
        return _err(f"unknown account '{args.name}'. See `ccswitch list`.")
    res = switch_to(args.name)
    _report_switch(args.name, res)
    return 0


def cmd_next(args) -> int:
    data = vault.load()
    # Prefer accounts not on a known cooldown; fall back to literal rotation.
    cooldowns = selection.known_cooldowns(data)
    target = vault.next_in_rotation(data, skip=cooldowns) or vault.next_in_rotation(data)
    if not target:
        return _err("no other account available to rotate to.")
    if target in cooldowns:
        print("note: all other accounts are on cooldown; rotating anyway.")
    res = switch_to(target)
    _report_switch(target, res)
    return 0


def cmd_best(args) -> int:
    data = vault.load()
    active = data.get("active")
    if len([n for n in data["accounts"] if n != active]) == 0:
        return _err("no other account to switch to.")
    print("probing accounts for the best one (headroom, then soonest reset)...")
    # include_active so we can recommend staying when the active account is best,
    # rather than moving off a usable account onto a worse one.
    name, u, reason = selection.pick_best(data, include_active=True)
    if not name or name == active:
        # Already on the best account (or nothing better to move to).
        print(f"staying on '{active}' ({reason})" if name == active
              else f"staying on '{active}' - {reason}.")
        return 0
    res = switch_to(name)
    print(f"best account: '{name}' ({reason})")
    _report_switch(name, res)
    return 0


# --------------------------------------------------------------------------- #
# status / refresh / doctor


def _probe_active(data: dict) -> dict:
    active = data.get("active")
    oauth_blob = vault.get_oauth(data, active) if active else None
    return probe(oauth_blob) if oauth_blob else {}


def cmd_status(args) -> int:
    data = vault.load()
    active = data.get("active")

    if args.json:
        out = {"active": active, "accounts": list(data["accounts"])}
        running = runstate.running_status()
        out["daemon"] = running or None
        if args.all:
            out["usage"] = probe_all({n: vault.get_oauth(data, n) for n in data["accounts"]}) \
                if not args.no_probe else {}
        elif active and not args.no_probe:
            out["usage"] = {active: _probe_active(data)}
        print(json.dumps(out, indent=2, default=str))
        return 0

    if not active and not data["accounts"]:
        print("no accounts stored. Add one with: ccswitch add <name>")
        return 0

    if args.all:
        return cmd_list(argparse.Namespace(usage=not args.no_probe))

    print(f"active: {active or '(none)'}")
    if active and not args.no_probe:
        u = _probe_active(data)
        pct = f"{u['percent']:.0f}%" if u.get("percent") is not None else "?"
        print(f"usage: {pct}  status: {u.get('status', '?')}")
        if u.get("windows"):
            print(f"windows: {_windows_str(u)}")
        if u.get("reset_at"):
            print(f"resets: {_fmt_reset(u['reset_at'])}")
        if u.get("error"):
            print(f"probe note: {u['error']}")

    running = runstate.running_status()
    if running:
        print(f"daemon: running (pid {running['pid']}), last check {running.get('last_check', '?')}"
              + (f", last switch {running['last_switch']}" if running.get("last_switch") else ""))
    else:
        print("daemon: not running")
    return 0


def cmd_checkpoint(args) -> int:
    with vault.transaction() as data:
        ok = checkpoint_active(data)
        active = data.get("active")
    if ok:
        print(f"checkpointed live credentials into '{active}'")
    else:
        print("nothing to checkpoint (no active account or no live credentials).")
    return 0


def cmd_refresh(args) -> int:
    data = vault.load()
    if args.all:
        targets = list(data["accounts"])
    elif args.name:
        targets = [args.name]
    else:
        targets = [data.get("active")] if data.get("active") else []
    targets = [t for t in targets if t]
    if not targets:
        return _err("nothing to refresh (no account specified and none active).")

    # Refresh over the network OUTSIDE the lock, then persist under a short lock.
    new_blobs: dict = {}
    for name in targets:
        blob = vault.get_oauth(data, name)
        if not blob:
            print(f"{name}: unknown account")
            continue
        try:
            new_blobs[name] = oauth.refresh(blob)
            print(f"{name}: refreshed")
        except Exception as exc:
            print(f"{name}: refresh failed: {exc}")

    if not new_blobs:
        return 0

    with vault.transaction() as data:
        active = data.get("active")
        for name, blob in new_blobs.items():
            if name in data["accounts"]:
                data["accounts"][name]["claudeAiOauth"] = blob
        active_oauth = vault.get_oauth(data, active) if active in new_blobs else None

    # If we refreshed the live account, push the new token into the live file too.
    if active_oauth:
        creds.write_live_oauth(active_oauth, backups_dir=vault.backups_dir())
        print("updated live credentials with the refreshed active token.")
    return 0


def cmd_doctor(args) -> int:
    data = vault.load()
    print("ccswitch doctor")
    enc = "encrypted (DPAPI)" if vault.is_encrypted() else (
        "plaintext (encryption available, will encrypt on next save)"
        if crypto.available() else "plaintext")
    print(f"  vault: {vault.vault_path()} ({'exists' if vault.vault_path().exists() else 'MISSING'}) - {enc}")
    print(f"  accounts: {len(data['accounts'])}  active: {data.get('active') or '(none)'}")

    live = creds.read_live_oauth()
    print(f"  live credentials: {'present' if live else 'none (not logged in)'}")

    for name in data["accounts"]:
        blob = vault.get_oauth(data, name) or {}
        exp = blob.get("expiresAt")
        if not exp:
            state = "no expiry recorded (will refresh on switch)"
        else:
            secs = exp / 1000.0 - datetime.now(timezone.utc).timestamp()
            state = f"expires in {secs/3600:.1f}h" if secs > 0 else f"EXPIRED {-secs/3600:.1f}h ago"
        has_refresh = "ok" if blob.get("refreshToken") else "NO refresh token (re-login needed)"
        print(f"    - {name}: {state}; refresh token: {has_refresh}")

    running = runstate.running_status()
    print(f"  daemon: {'running pid ' + str(running['pid']) if running else 'not running'}")
    return 0


# --------------------------------------------------------------------------- #
# daemon


def cmd_daemon(args) -> int:
    running = runstate.running_status()
    if running and not args.force:
        return _err(
            f"a daemon is already running (pid {running['pid']}, last check "
            f"{running.get('last_check', '?')}). Use --force to start another."
        )
    try:
        return daemon.run(
            threshold=args.threshold,
            interval=args.interval,
            max_interval=args.max_interval,
            skip_exhausted=not args.no_skip_exhausted,
            strategy=args.strategy,
            once=args.once,
        )
    except KeyboardInterrupt:
        print("\n[ccswitch] daemon stopped")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccswitch",
        description="Manage and rotate Claude Code accounts by swapping credentials.",
    )
    p.add_argument("--version", action="version", version=f"ccswitch {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="store the currently logged-in account")
    a.add_argument("name")
    a.add_argument("--label", default=None, help="optional human label")
    a.set_defaults(func=cmd_add)

    rm = sub.add_parser("remove", help="remove an account from the vault")
    rm.add_argument("name")
    rm.set_defaults(func=cmd_remove)

    rn = sub.add_parser("rename", help="rename a stored account")
    rn.add_argument("old")
    rn.add_argument("new")
    rn.set_defaults(func=cmd_rename)

    l = sub.add_parser("list", help="list stored accounts")
    l.add_argument("--usage", action="store_true", help="probe and show each account's usage")
    l.set_defaults(func=cmd_list)

    u = sub.add_parser("use", help="switch the active account")
    u.add_argument("name")
    u.set_defaults(func=cmd_use)

    n = sub.add_parser("next", help="rotate to the next account in rotation")
    n.set_defaults(func=cmd_next)

    b = sub.add_parser("best", help="switch to the account with the most headroom")
    b.set_defaults(func=cmd_best)

    st = sub.add_parser("status", help="show the active account, usage, and daemon state")
    st.add_argument("--all", action="store_true", help="show every account's usage")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    st.add_argument("--no-probe", action="store_true", help="skip the usage probe(s)")
    st.set_defaults(func=cmd_status)

    # `current` kept as a familiar alias for the single-account status view.
    c = sub.add_parser("current", help="alias for `status`")
    c.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    c.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    c.add_argument("--no-probe", action="store_true", help="skip the usage probe")
    c.set_defaults(func=cmd_status)

    cp = sub.add_parser("checkpoint", help="sync live credentials back into the vault")
    cp.set_defaults(func=cmd_checkpoint)

    rf = sub.add_parser("refresh", help="refresh stored token(s) via the refresh token")
    rf.add_argument("name", nargs="?", default=None, help="account (default: active)")
    rf.add_argument("--all", action="store_true", help="refresh every account")
    rf.set_defaults(func=cmd_refresh)

    doc = sub.add_parser("doctor", help="diagnose vault, tokens, and daemon")
    doc.set_defaults(func=cmd_doctor)

    d = sub.add_parser("daemon", help="watch usage and rotate before the limit")
    d.add_argument("--threshold", type=float, default=90.0, help="rotate at this %% (default 90)")
    d.add_argument("--interval", type=float, default=60.0,
                   help="near-limit poll floor in seconds (default 60)")
    d.add_argument("--max-interval", type=float, default=600.0,
                   help="idle poll ceiling in seconds; polling stretches toward this "
                        "when usage is low (default 600)")
    d.add_argument("--strategy", choices=("best", "next"), default="best",
                   help="how to choose the target account (default best=most headroom)")
    d.add_argument("--no-skip-exhausted", action="store_true", help="do not skip exhausted accounts")
    d.add_argument("--once", action="store_true", help="run a single check and exit")
    d.add_argument("--force", action="store_true", help="start even if a daemon is already running")
    d.set_defaults(func=cmd_daemon)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except VaultError as exc:
        # Vault problems (corrupt file, undecryptable, lock contention) should
        # read as a clean message, not a traceback.
        return _err(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
