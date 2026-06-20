"""The account vault: stored credential blobs plus active/rotation state.

Location: ~/.cc-accounts/vault.json  (override with CCSWITCH_HOME).
Holds live access + refresh tokens, so the file is locked to the current user.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ccswitch import crypto
from ccswitch.creds import OAUTH_KEY


class VaultError(Exception):
    """A vault problem worth showing the user as a clean message, not a traceback."""


class VaultDecryptError(VaultError):
    """The vault could not be decrypted (different Windows user or machine)."""


class VaultLockError(VaultError):
    """The vault lock could not be acquired in time."""


def home() -> Path:
    override = os.environ.get("CCSWITCH_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".cc-accounts"
    return base


def vault_path() -> Path:
    return home() / "vault.json"


def lock_path() -> Path:
    return home() / "vault.lock"


def backups_dir() -> Path:
    return home() / "backups"


def is_encrypted() -> bool:
    """True if the on-disk vault stores its token blobs encrypted."""
    p = vault_path()
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    for acct in data.get("accounts", {}).values():
        blob = acct.get(OAUTH_KEY)
        if isinstance(blob, dict) and blob.get("_enc") == crypto.ENC_MARKER:
            return True
    return False


def _encrypt_blobs(data: dict) -> dict:
    """Return a disk copy of `data` with each oauth blob DPAPI-encrypted.

    No-op (deep-ish copy with plaintext blobs) when encryption is unavailable,
    so non-Windows stores plaintext exactly as before."""
    out = dict(data)
    out["accounts"] = {}
    for name, acct in data.get("accounts", {}).items():
        acct_copy = dict(acct)
        blob = acct.get(OAUTH_KEY)
        if crypto.available() and isinstance(blob, dict) and "_enc" not in blob:
            acct_copy[OAUTH_KEY] = {"_enc": crypto.ENC_MARKER,
                                    "v": crypto.protect(json.dumps(blob))}
        out["accounts"][name] = acct_copy
    return out


def _decrypt_blobs(data: dict) -> dict:
    """Transparently decrypt any encrypted oauth blobs read from disk."""
    for name, acct in data.get("accounts", {}).items():
        blob = acct.get(OAUTH_KEY)
        if isinstance(blob, dict) and blob.get("_enc") == crypto.ENC_MARKER:
            try:
                acct[OAUTH_KEY] = json.loads(crypto.unprotect(blob["v"]))
            except Exception as exc:  # DPAPI fails for a different user/machine
                raise VaultDecryptError(
                    f"could not decrypt account '{name}' - the vault was encrypted "
                    f"by a different Windows user or machine. Re-add the account, "
                    f"or set CCSWITCH_NO_ENCRYPT=1 and restore from backups. ({exc})"
                ) from exc
    return data


_LOCK_TIMEOUT = 30.0


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = _LOCK_TIMEOUT):
    """Exclusive cross-process lock held for a brief read-modify-write window.

    Windows `msvcrt.locking(LK_NBLCK)` returns immediately and raises if the
    byte is already locked; we poll it up to `timeout` rather than crashing, so
    a contending command waits instead of failing. POSIX `flock(LOCK_EX)` blocks
    natively. On ultimate failure a clear VaultLockError is raised."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    acquired = False
    try:
        if sys.platform == "win32":
            import msvcrt
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise VaultLockError(
                            "another ccswitch process is holding the vault lock; "
                            "try again in a moment."
                        )
                    time.sleep(0.2)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            acquired = True
        yield
    finally:
        try:
            if acquired:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


@contextlib.contextmanager
def transaction():
    """Lock, load, yield the data for mutation, then save - all under one lock.

    Use this for every read-modify-write so a concurrent process (e.g. the
    daemon and a manual `use`) cannot clobber each other's changes. On an
    exception the save is skipped."""
    _ensure_home_locked()  # lock the dir before the lock file materialises it
    with _file_lock(lock_path()):
        data = load()
        yield data
        save(data)


def _acl_principal() -> str:
    """Best-effort current-user principal for icacls, robust to an empty %USERNAME%."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain and user else user


def _chmod_600(path: Path) -> None:
    """POSIX-perm tighten only. Files in the locked home dir already inherit a
    user-only ACL on Windows, so spawning icacls per file would be redundant."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _lock_down(path: Path, is_dir: bool = False) -> None:
    """Restrict a file or directory to the current user only."""
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            user = _acl_principal()
            if not user:
                return
            # (OI)(CI) makes the grant inherit to dir contents (e.g. backups).
            grant = f"{user}:(OI)(CI)F" if is_dir else f"{user}:F"
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass


def _ensure_home_locked() -> Path:
    """Ensure ~/.cc-accounts is locked to the current user before writing into it.

    Uses a one-time marker so an already-existing (and previously un-locked)
    directory still gets its ACL tightened once, without spawning icacls on
    every save."""
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    marker = h / ".acl-locked"
    if not marker.exists():
        _lock_down(h, is_dir=True)
        try:
            marker.write_text("1", encoding="utf-8")
        except OSError:
            pass
    return h


def load() -> dict:
    p = vault_path()
    if not p.exists():
        return {"accounts": {}, "active": None, "rotation": []}
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise VaultError(
            f"{p} is unreadable ({exc}). Restore a copy from "
            f"{backups_dir()} or remove it to start fresh."
        ) from exc
    data.setdefault("accounts", {})
    data.setdefault("active", None)
    data.setdefault("rotation", [])
    return _decrypt_blobs(data)


def save(data: dict) -> None:
    _ensure_home_locked()
    p = vault_path()
    on_disk = _encrypt_blobs(data)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(on_disk, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    # Files inherit the user-only ACL from the locked home dir; chmod is enough
    # here (no per-file icacls spawn).
    _chmod_600(tmp)
    os.replace(tmp, p)
    _chmod_600(p)


def add_account(data: dict, name: str, oauth: dict, label: str | None = None) -> None:
    data["accounts"][name] = {OAUTH_KEY: oauth, "label": label or name}
    if name not in data["rotation"]:
        data["rotation"].append(name)


def get_oauth(data: dict, name: str) -> dict | None:
    acct = data["accounts"].get(name)
    return acct.get(OAUTH_KEY) if acct else None


def remove_account(data: dict, name: str) -> bool:
    """Drop an account from the vault and rotation. Returns False if unknown.

    If it was the active account, `active` is cleared (the live credentials file
    is left untouched - removing from the vault does not log anyone out)."""
    if name not in data["accounts"]:
        return False
    del data["accounts"][name]
    data["rotation"] = [n for n in data.get("rotation", []) if n != name]
    if data.get("active") == name:
        data["active"] = None
    return True


def rename_account(data: dict, old: str, new: str) -> None:
    """Rename an account, preserving its position in rotation and active state.

    Raises KeyError if `old` is unknown, ValueError if `new` already exists."""
    if old not in data["accounts"]:
        raise KeyError(old)
    if new in data["accounts"]:
        raise ValueError(new)
    data["accounts"][new] = data["accounts"].pop(old)
    data["rotation"] = [new if n == old else n for n in data.get("rotation", [])]
    if data.get("active") == old:
        data["active"] = new


def next_in_rotation(data: dict, skip: set[str] | None = None) -> str | None:
    """Return the rotation entry after the active one, skipping `skip`."""
    skip = skip or set()
    rotation = [n for n in data.get("rotation", []) if n in data["accounts"]]
    if not rotation:
        return None
    active = data.get("active")
    start = rotation.index(active) + 1 if active in rotation else 0
    ordered = rotation[start:] + rotation[:start]
    for name in ordered:
        if name != active and name not in skip:
            return name
    return None
