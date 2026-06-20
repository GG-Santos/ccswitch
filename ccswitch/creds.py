"""Read and write Claude Code's live credentials file safely.

All Claude Code auth lives in <config-dir>/.credentials.json under the
"claudeAiOauth" key. We swap only that object and preserve everything else
(e.g. "mcpOAuth"). Writes are atomic (temp file + os.replace) and a timestamped
backup of the previous file is kept.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ccswitch import crypto

OAUTH_KEY = "claudeAiOauth"
MAX_BACKUPS = 10


def config_dir() -> Path:
    """Claude Code config dir, honoring CLAUDE_CONFIG_DIR like Claude Code does."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def creds_path() -> Path:
    return config_dir() / ".credentials.json"


def _read_raw() -> dict:
    p = creds_path()
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def read_live_oauth() -> dict | None:
    """Return the live claudeAiOauth object, or None if not logged in."""
    data = _read_raw()
    oauth = data.get(OAUTH_KEY)
    return oauth if isinstance(oauth, dict) and oauth.get("accessToken") else None


def _restrict_perms(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _prune_backups(backups_dir: Path, keep: int = MAX_BACKUPS) -> None:
    """Keep only the most recent `keep` backups, by mtime.

    When encryption is available, also delete any legacy *plaintext* backups
    outright - leaving them would defeat the at-rest encryption."""
    try:
        files = sorted(
            (f for f in backups_dir.glob("credentials-*") if f.is_file()),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if crypto.available():
            for f in list(files):
                if f.suffix != ".enc":
                    f.unlink(missing_ok=True)
            files = [f for f in files if f.suffix == ".enc" and f.exists()]
        for stale in files[keep:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def _write_backup(src: Path, backups_dir: Path) -> Path:
    """Back up the live creds file, encrypted at rest where DPAPI is available.

    The live file holds plaintext tokens, so an unencrypted copy here would
    defeat the vault's at-rest encryption. We encrypt the backup with the same
    DPAPI path and prune to a bounded count."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    text = src.read_text(encoding="utf-8")
    if crypto.available():
        backup = backups_dir / f"credentials-{stamp}.json.enc"
        backup.write_text(crypto.protect(text), encoding="utf-8")
    else:
        backup = backups_dir / f"credentials-{stamp}.json"
        backup.write_text(text, encoding="utf-8")
    _restrict_perms(backup)
    _prune_backups(backups_dir)
    return backup


def write_live_oauth(oauth: dict, backups_dir: Path | None = None) -> Path | None:
    """Replace the claudeAiOauth object in the live creds file, preserving other
    top-level keys. Returns the backup path written (or None if no prior file)."""
    p = creds_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    backup = None
    if p.exists() and backups_dir is not None:
        backup = _write_backup(p, backups_dir)

    data = _read_raw()
    data[OAUTH_KEY] = oauth

    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    _restrict_perms(tmp)
    os.replace(tmp, p)  # atomic on same filesystem, works on Windows
    _restrict_perms(p)
    return backup
