import json

from ccswitch import creds, crypto, vault
from helpers import make_oauth


def _read_backup(path):
    """Decrypt a backup if it was written encrypted, else read it plainly."""
    text = path.read_text()
    if path.suffix == ".enc":
        text = crypto.unprotect(text)
    return json.loads(text)


def test_write_live_preserves_other_keys(sandbox, seed_live):
    seed_live("a", extra_keys={"mcpOAuth": ["keep-me"]})
    creds.write_live_oauth(make_oauth("b"), backups_dir=vault.backups_dir())
    data = json.loads(creds.creds_path().read_text())
    assert data["claudeAiOauth"]["accessToken"] == "AT-b"
    assert data["mcpOAuth"] == ["keep-me"]  # non-oauth key survives the swap


def test_write_live_makes_backup(sandbox, seed_live):
    seed_live("a")
    backup = creds.write_live_oauth(make_oauth("b"), backups_dir=vault.backups_dir())
    assert backup and backup.exists()
    assert _read_backup(backup)["claudeAiOauth"]["accessToken"] == "AT-a"
    if crypto.available():
        assert backup.suffix == ".enc"
        assert "AT-a" not in backup.read_text()  # not plaintext on disk


def test_backups_are_pruned(sandbox, seed_live):
    seed_live("a")
    for i in range(creds.MAX_BACKUPS + 5):
        creds.write_live_oauth(make_oauth(f"x{i}"), backups_dir=vault.backups_dir())
    backups = list(vault.backups_dir().glob("credentials-*"))
    assert len(backups) <= creds.MAX_BACKUPS


def test_vault_add_get_remove_rename(sandbox):
    data = vault.load()
    vault.add_account(data, "work", make_oauth("work"))
    vault.add_account(data, "home", make_oauth("home"))
    assert vault.get_oauth(data, "work")["accessToken"] == "AT-work"
    assert data["rotation"] == ["work", "home"]

    vault.rename_account(data, "home", "personal")
    assert "personal" in data["accounts"] and "home" not in data["accounts"]
    assert data["rotation"] == ["work", "personal"]

    assert vault.remove_account(data, "work") is True
    assert "work" not in data["accounts"]
    assert data["rotation"] == ["personal"]
    assert vault.remove_account(data, "nope") is False


def test_remove_active_clears_active(sandbox):
    data = vault.load()
    vault.add_account(data, "work", make_oauth("work"))
    data["active"] = "work"
    vault.remove_account(data, "work")
    assert data["active"] is None


def test_next_in_rotation_skips(sandbox):
    data = vault.load()
    for n in ("a", "b", "c"):
        vault.add_account(data, n, make_oauth(n))
    data["active"] = "a"
    assert vault.next_in_rotation(data) == "b"
    assert vault.next_in_rotation(data, skip={"b"}) == "c"
    assert vault.next_in_rotation(data, skip={"b", "c"}) is None
