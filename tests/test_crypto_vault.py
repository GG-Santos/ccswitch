import json

import pytest

from ccswitch import crypto, vault
from helpers import make_oauth


def test_protect_unprotect_roundtrip():
    if not crypto.available():
        pytest.skip("DPAPI not available on this platform")
    token = crypto.protect("hello-secret")
    assert token != "hello-secret"
    assert crypto.unprotect(token) == "hello-secret"


def test_save_load_preserves_tokens(sandbox):
    data = vault.load()
    vault.add_account(data, "work", make_oauth("work"))
    data["active"] = "work"
    vault.save(data)

    loaded = vault.load()
    assert loaded["accounts"]["work"]["claudeAiOauth"]["accessToken"] == "AT-work"
    assert loaded["accounts"]["work"]["claudeAiOauth"]["refreshToken"] == "RT-work"
    assert loaded["active"] == "work"


def test_encrypted_on_disk_when_available(sandbox):
    data = vault.load()
    vault.add_account(data, "work", make_oauth("work"))
    vault.save(data)
    raw = json.loads(vault.vault_path().read_text())
    blob = raw["accounts"]["work"]["claudeAiOauth"]
    if crypto.available():
        assert blob.get("_enc") == crypto.ENC_MARKER
        assert "AT-work" not in json.dumps(raw)  # token not in plaintext on disk
        assert vault.is_encrypted() is True
    else:
        assert blob["accessToken"] == "AT-work"  # plaintext parity off-Windows


def test_plaintext_vault_migrates_on_save(sandbox):
    # Write a legacy plaintext vault by hand.
    vault.vault_path().parent.mkdir(parents=True, exist_ok=True)
    vault.vault_path().write_text(json.dumps({
        "accounts": {"work": {"claudeAiOauth": make_oauth("work"), "label": "work"}},
        "active": "work", "rotation": ["work"],
    }))
    # Loading a plaintext vault must work regardless of platform.
    data = vault.load()
    assert data["accounts"]["work"]["claudeAiOauth"]["accessToken"] == "AT-work"
    # Saving migrates it to encrypted (where available).
    vault.save(data)
    if crypto.available():
        assert vault.is_encrypted() is True
        assert vault.load()["accounts"]["work"]["claudeAiOauth"]["accessToken"] == "AT-work"


def test_decrypt_failure_raises_vaulterror(sandbox, monkeypatch):
    # Hand-write an encrypted-looking vault, then make decryption fail.
    vault.vault_path().parent.mkdir(parents=True, exist_ok=True)
    vault.vault_path().write_text(json.dumps({
        "accounts": {"work": {"claudeAiOauth": {"_enc": crypto.ENC_MARKER, "v": "garbage"},
                              "label": "work"}},
        "active": "work", "rotation": ["work"],
    }))

    def boom(_):
        raise ValueError("wrong user")

    monkeypatch.setattr(crypto, "unprotect", boom)
    with pytest.raises(vault.VaultDecryptError):
        vault.load()


def test_corrupt_vault_raises_vaulterror(sandbox):
    vault.vault_path().parent.mkdir(parents=True, exist_ok=True)
    vault.vault_path().write_text("{ this is not valid json")
    with pytest.raises(vault.VaultError):
        vault.load()


def test_save_does_not_icacls_individual_files(sandbox, monkeypatch):
    # Files inherit the dir's user-only ACL, so save must not spawn per-file
    # icacls (_lock_down with is_dir=False); it should chmod them instead.
    locked = []
    monkeypatch.setattr(vault, "_lock_down", lambda p, is_dir=False: locked.append(is_dir))
    chmods = []
    real_chmod = vault._chmod_600
    monkeypatch.setattr(vault, "_chmod_600", lambda p: chmods.append(p) or real_chmod(p))

    data = vault.load()
    vault.add_account(data, "work", make_oauth("work"))
    vault.save(data)

    assert False not in locked  # no per-file icacls
    assert locked == [True]     # only the one-time directory lock
    assert chmods               # files were chmod-restricted


def test_no_encrypt_env_passthrough(sandbox, monkeypatch):
    monkeypatch.setenv("CCSWITCH_NO_ENCRYPT", "1")
    data = vault.load()
    vault.add_account(data, "work", make_oauth("work"))
    vault.save(data)
    raw = json.loads(vault.vault_path().read_text())
    assert raw["accounts"]["work"]["claudeAiOauth"]["accessToken"] == "AT-work"  # plaintext
    assert vault.is_encrypted() is False
