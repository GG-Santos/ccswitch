"""Core actions shared by the CLI and the daemon."""

from __future__ import annotations

from ccswitch import creds, oauth, vault
from ccswitch.creds import OAUTH_KEY


def checkpoint_active(data: dict) -> bool:
    """Sync the live credentials back into the active account's vault entry.

    Claude Code rotates the refreshToken on every token refresh and rewrites the
    live file, so the vault copy of the active account goes stale. Syncing it
    back before we overwrite the live file is what prevents a stale refresh
    token (which would force a manual /login). Returns True if anything synced.
    """
    active = data.get("active")
    if not active or active not in data["accounts"]:
        return False
    live = creds.read_live_oauth()
    if not live:
        return False
    data["accounts"][active][OAUTH_KEY] = live
    return True


def refresh_account(data: dict, name: str) -> tuple[bool, str]:
    """Refresh the stored tokens for `name` in `data` (in place, not saved).

    Returns (changed, message). `changed` is True only when the vault entry was
    updated, so callers know whether to persist. Failures are reported, not
    raised, so a refresh problem never blocks the surrounding operation.
    """
    blob = data["accounts"].get(name, {}).get(OAUTH_KEY)
    if not blob:
        return False, f"no stored credentials for '{name}'"
    try:
        new = oauth.refresh(blob)
    except Exception as exc:
        return False, str(exc)
    data["accounts"][name][OAUTH_KEY] = new
    return True, "refreshed"


def switch_to(name: str, refresh_if_stale: bool = True) -> dict:
    """Checkpoint the active account, then make `name` the live + active account.

    If the target's access token is expired or about to expire, refresh it first
    so the user lands on a working session instead of a token Claude has to
    refresh on the next call. Returns the (reloaded) vault dict plus a 'refresh'
    note. Raises KeyError if `name` is unknown.
    """
    # Do the network refresh BEFORE taking the lock - holding the vault lock
    # across a network call can make a concurrent command time out and fail.
    snapshot = vault.load()
    if name not in snapshot["accounts"]:
        raise KeyError(name)
    refreshed_blob = None
    refreshed = None
    if refresh_if_stale and oauth.is_expired(vault.get_oauth(snapshot, name)):
        try:
            refreshed_blob = oauth.refresh(vault.get_oauth(snapshot, name))
            refreshed = "refreshed"
        except Exception as exc:
            refreshed = f"refresh skipped: {exc}"

    # Short, lock-held critical section: checkpoint, apply refreshed token, set
    # active, and persist the vault. Live creds are written AFTER the vault is
    # saved so a crash cannot leave the live file ahead of the vault.
    with vault.transaction() as data:
        if name not in data["accounts"]:
            raise KeyError(name)
        checkpoint_active(data)
        if refreshed_blob is not None:
            data["accounts"][name][OAUTH_KEY] = refreshed_blob
        blob = vault.get_oauth(data, name)
        data["active"] = name

    backup = creds.write_live_oauth(blob, backups_dir=vault.backups_dir())
    return {"vault": data, "backup": backup, "refresh": refreshed}
