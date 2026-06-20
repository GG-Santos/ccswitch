---
name: ccswitch
description: Manage and rotate Claude Code accounts. Use whenever the user wants to switch/swap Claude accounts, check usage or how close they are to the 5h or weekly limit, jump to the account with the most headroom or the one that resets/frees up soonest, add/remove/rename a stored login, refresh an expired token, diagnose login/account problems, or start the auto-switch daemon. Trigger even when the user does not say "ccswitch" by name, e.g. "bump me to my other login", "how much have I used", "am I about to hit my limit", "which account has the most room", "which account resets first", "get me on whatever frees up soonest", "my login expired", "save this account", "is the watcher running". Invoked as /ccswitch.
user-invocable: true
---

# ccswitch - Claude account manager

A CLI that stores multiple logged-in Claude accounts and swaps the active one by
rewriting `~/.claude/.credentials.json` in place, plus a daemon that rotates
accounts before the usage limit is hit. Stored tokens are encrypted at rest
(DPAPI) on Windows.

- Vault: `~/.cc-accounts/vault.json` (stores account credentials; treat as secret)
- The `ccswitch` command should be on PATH (installed via pipx or pip). If a run
  fails with "command not found", fall back to `python -m ccswitch <args>`, or
  reinstall per the project README.

## How to handle the invocation

Read the argument after `/ccswitch` (or infer intent from a natural-language
request), run the matching command, then report the result in plain language.
With no argument, run the **status** action. Match intent even when the wording
differs - the user rarely uses the exact command name.

| Intent | Command | Example phrasings |
|---|---|---|
| status + usage + daemon state | `ccswitch status` | "how much have I used", "am I near my limit", (no arg) |
| usage for every account | `ccswitch status --all` | "show all my accounts' usage", "which has room" |
| list the pool | `ccswitch list` | "what accounts do I have", "which one am I on" |
| switch to a named account | `ccswitch use <name>` | "switch to work", "bump me to my other login" |
| switch to the best account | `ccswitch best` | "put me on the freshest one", "whichever has the most room", "whatever frees up soonest" |
| rotate to the next account | `ccswitch next` | "rotate", "next account", "get me off this one" |
| capture the logged-in account | `ccswitch add <name>` | "save this account", "add my personal login" |
| remove an account | `ccswitch remove <name>` | "forget this login", "delete the old account" |
| rename an account | `ccswitch rename <old> <new>` | "rename work to work-pro" |
| refresh stored token(s) | `ccswitch refresh [name\|--all]` | "my login expired", "refresh my tokens" |
| diagnose problems | `ccswitch doctor` | "something's wrong with my login", "check token expiry", "is the watcher running" |
| auto-switch watcher | `ccswitch daemon ...` | "auto switch before I run out", "watch my usage" |
| sync creds after a refresh | `ccswitch checkpoint` | "checkpoint", "sync my tokens" |

If a switch or status shows an auth/expired-token error, suggest `ccswitch
refresh` (or `ccswitch doctor` to see which accounts are expired).

## What to tell the user for each action

These notes exist because each action has a non-obvious consequence the user
needs to know - leaving them out leads to confusion or lost accounts.

**status** - Reports usage for both the 5h and 7d (weekly) windows plus the
reset time, and whether the daemon is running. Report both windows: either can
be the binding limit. `--all` probes every account (one tiny request each);
plain `status` probes only the active one. Interactive probes are cached ~30s, so
repeated checks are cheap.

**use / next / best** - Switching rewrites the global credentials file, so it
takes effect on the user's **next message or next `claude` session**, not in the
current turn. Always say this. If the target's token is stale, `use`/`best`
refresh it first, so the user lands on a working session (no re-`/login` needed).

- `best` is **reset-aware**: it picks the usable account with the most headroom;
  if every account is maxed it moves to the one that **resets soonest** (so the
  user is unblocked first), or stays put if the current account already resets as
  soon as any. It reports the reason it chose. Use it for "which should I be on".
- `next` follows rotation order but now **skips accounts on a known cooldown**
  (using cached info, no extra probes), falling back to plain rotation if every
  other account is cooling down.

**add `<name>`** - Captures whatever account is *currently logged in* to the live
credentials file. Workflow: `/login` that account in a `claude` session first,
then `/ccswitch add <name>`. Tell the user the freshly added account becomes the
**active** one (it owns the live credentials you just captured), so a later
switch moves them off it.

**remove / rename** - Vault edits. `remove` of the active account does not log
the user out (the live credentials file is untouched); it just drops the stored
copy. `rename` keeps rotation position and active state.

**refresh** - Renews stored tokens via their refresh token (no `/login`). With no
argument it refreshes the active account; `--all` does every account. If a
refresh fails (revoked token), tell the user to `/login` + `ccswitch add` that
account again.

**doctor** - Triage: shows the vault location and whether it's encrypted, each
account's token expiry and whether it still has a refresh token, and whether the
daemon is running. Use this first when something seems off.

**checkpoint** - Syncs the live credentials back into the active account's vault
entry. `use`/`next`/`best` already checkpoint automatically before swapping; only
suggest this for a manual sync without switching.

**daemon** - The watcher. It polls the active account and, when usage crosses the
threshold, switches to the account with the **most headroom** (default
`--strategy best`; `--strategy next` for plain rotation). Make clear when you
start it:

- It switches *early* (default 90%) so a fresh account is ready before the limit;
  it cannot abort a turn already in flight. It rotates on the max of the 5h/7d
  windows. When every account is maxed it moves to (or waits on) the account that
  **resets soonest** rather than giving up.
- **Adaptive cadence:** `--interval` (default 60s) is the near-limit floor and
  `--max-interval` (default 600s) the idle ceiling - it polls rarely when usage
  is low and tightens near the limit, to save quota.
- It only lives as long as the process you start it in (a session-bound
  background task dies with the session). For an always-on watcher, run it in its
  own terminal (or `launch.bat work /d`).
- Only one runs at a time: it refuses to start if one is already active (pass
  `--force` to override). `ccswitch status`/`doctor` show whether it's running.

Start it as a background process and report that it's watching, e.g.
`ccswitch daemon --threshold 90`. For a quick demo use a low threshold like
`--threshold 15`, or `--once` to run a single check and exit. To stop a
session-bound daemon, stop that background task; for a terminal one, Ctrl+C in
its window (there is no `daemon stop` subcommand).

## Guardrails

- Never print `accessToken` / `refreshToken` values from the vault or the
  credentials file, even if asked - they grant account access.
- `use`/`next`/`best` only work between accounts already captured with `add`. If
  the user asks to switch to a name not in `ccswitch list`, do not error out or
  invent it: tell them to `/login` that account and run `/ccswitch add <name>`
  first. Running `ccswitch list` to check first is a good habit.
- Rotating accounts to extend usage may conflict with Anthropic's terms; this
  tool manages the user's own accounts.
