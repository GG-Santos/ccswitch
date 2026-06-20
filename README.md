# ccswitch

Juggle multiple Claude accounts without thinking about it. ccswitch keeps your
logged-in Claude accounts in one place and switches between them so you can keep
working when one hits its usage limit.

## The problem it solves

Claude's paid plans cap how much you can use in a rolling 5-hour window (and a
weekly one). If you have more than one Claude account, hitting that cap usually
means stopping what you are doing, logging out, logging into the other account,
and logging back in. Every single time.

ccswitch removes that friction. It remembers your accounts and switches the
active one instantly, with no logging in and out. It can even watch your usage
in the background and move you to a fresh account before you hit the wall, so
your work is not interrupted.

## What it can do

- **Hold several accounts ready** and switch between them with one short command.
- **Show your usage** on each account (both the 5-hour and weekly windows) and
  when each one resets.
- **Pick the smartest account for you:** the one with the most room left, or,
  when everything is busy, the one that frees up soonest.
- **Switch automatically.** A background watcher keeps an eye on your usage and
  moves you to another account before the active one runs out.
- **Keep you signed in.** If an account's login has gone stale, ccswitch renews
  it for you, so you do not have to log in again.
- **Keep your logins private.** On Windows your saved accounts are encrypted so
  only your user can read them.
- **Talk to it in plain language** through an optional Claude Code skill: "how
  much have I used?", "switch me to my work account", "which account has the most
  room?"

## Before you start

You need:

- **Claude Code** installed and working (the `claude` command).
- **Python 3.9 or newer.**
- **Two or more Claude accounts** (it works with one, but switching needs at
  least two).

A note on platforms: account switching and automatic switching work everywhere.
On-disk encryption and desktop pop-up notifications are Windows features. On
macOS and Linux your logins are kept in a folder locked to your user instead of
OS-encrypted, and switches are shown in the terminal rather than as pop-ups.

## Install

### Recommended: pipx (all platforms)

[pipx](https://pipx.pypa.io) installs the `ccswitch` command in its own isolated
space so it never clashes with your other Python tools.

```
pipx install git+https://github.com/GG-Santos/ccswitch.git
```

Do not have pipx yet?

```
python -m pip install --user pipx
python -m pipx ensurepath
```

### With pip

```
pip install git+https://github.com/GG-Santos/ccswitch.git
```

### From a clone (if you want to tinker)

```
git clone https://github.com/GG-Santos/ccswitch.git
cd ccswitch
pip install -e .
```

Then check it works:

```
ccswitch --version
```

## Quick start

**1. Save your first account.** In Claude Code, make sure you are logged into
the account you want, then run:

```
ccswitch add personal
```

**2. Save your other account.** In Claude Code run `/login`, sign in to the
second account, then:

```
ccswitch add work
```

**3. See your accounts and usage:**

```
ccswitch status --all
```

**4. Switch whenever you like:**

```
ccswitch use work
```

A switch takes effect the next time you start Claude or send your next message.

**5. Let it switch for you.** In a spare terminal window, start the watcher:

```
ccswitch daemon
```

It checks your usage and moves you to a fresh account before you hit the limit.
Leave that window open for as long as you want the watcher running.

## Talk to it: the /ccswitch skill (optional)

If you use Claude Code, install the bundled skill to manage accounts just by
asking Claude, no commands to remember:

- "how much have I used?"
- "switch me to my work account"
- "which account has the most room?"
- "my login expired"
- "is the watcher running?"

Install it by copying the skill into your Claude Code skills folder.

Windows (PowerShell), from the project folder:

```
Copy-Item -Recurse skill\ccswitch "$env:USERPROFILE\.claude\skills\ccswitch"
```

macOS / Linux:

```
mkdir -p ~/.claude/skills && cp -r skill/ccswitch ~/.claude/skills/ccswitch
```

Restart Claude Code, then type `/ccswitch` or just ask in plain language.

## Everyday commands

| Command | What it does |
|---|---|
| `ccswitch add <name>` | Save the account you are currently logged into |
| `ccswitch list` | List your saved accounts |
| `ccswitch status` | Show the active account, its usage, and the watcher's state |
| `ccswitch status --all` | Show usage for every account at once |
| `ccswitch use <name>` | Switch to a specific account |
| `ccswitch best` | Switch to the account with the most room left |
| `ccswitch next` | Rotate to the next account |
| `ccswitch refresh` | Renew an account's login without signing in again |
| `ccswitch remove <name>` | Forget a saved account |
| `ccswitch rename <old> <new>` | Rename a saved account |
| `ccswitch doctor` | Check that everything is healthy |
| `ccswitch daemon` | Start the background auto-switch watcher |

## How it chooses an account

When you run `ccswitch best`, or when the watcher needs to move you, ccswitch
looks at how much room each account has left. It prefers the account with the
most room. If every account is busy, it picks the one that resets soonest, so
you are back to work as quickly as possible. If the account you are already on
frees up first, it just leaves you where you are.

## Your logins stay private

Your saved accounts include the keys that grant access to Claude, so ccswitch
handles them carefully:

- On Windows, saved logins and their backups are encrypted so only your Windows
  user can read them, and the folder they live in is locked to you.
- ccswitch never prints your access keys on screen.

Worth knowing honestly: this protects your logins from other people who use the
same computer and from someone copying the files off your disk. It does not
protect against malicious software already running as you. It is still safer than
the plain text file Claude Code keeps by default.

## Good to know

- A switch takes effect on your **next message or next Claude session**, not in
  the middle of a reply that is already underway. The watcher switches early (at
  90% of the limit by default) so a fresh account is ready before you run out.
- Checking usage makes a tiny request to Claude for each account, which counts a
  negligible amount against your limit. The watcher checks less often when you
  are far from the limit and waits quietly when everything is busy.
- The background watcher runs only while its window stays open. For an always-on
  watcher, give it its own terminal.
- Using extra accounts to get around usage limits may go against Anthropic's
  terms. ccswitch is meant for managing your **own** accounts; use it
  responsibly.

## Advanced settings (optional)

Most people never need these.

- `ccswitch daemon --threshold 85` switches at 85% instead of 90%.
- `ccswitch daemon --interval 30 --max-interval 900` controls how often it checks
  (it checks more often near the limit, less often when you are far from it).
- Environment variables: `CCSWITCH_HOME` changes where accounts are stored,
  `CCSWITCH_NO_ENCRYPT=1` stores logins in plain text instead of encrypted.

## Uninstall

```
pipx uninstall ccswitch      # or: pip uninstall ccswitch
```

Your saved accounts live in a folder named `.cc-accounts` in your home
directory. Delete it if you want to remove every saved account.

## Developing

```
git clone https://github.com/GG-Santos/ccswitch.git
cd ccswitch
pip install -e ".[test]"
pytest
```

The tests use throwaway folders and never touch your real accounts or the
network.

## License

MIT. See [LICENSE](LICENSE).
