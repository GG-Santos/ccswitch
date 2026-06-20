import json

import pytest

from ccswitch import creds, vault
from helpers import make_oauth


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated vault + live-creds dirs, with the slow Windows ACL call stubbed."""
    home = tmp_path / "vault"
    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("CCSWITCH_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(vault, "_lock_down", lambda p, is_dir=False: None)
    return {"home": home, "cfg": cfg}


@pytest.fixture
def seed_live():
    def _seed(tag, extra_keys=None):
        data = {"claudeAiOauth": make_oauth(tag)}
        if extra_keys:
            data.update(extra_keys)
        creds.creds_path().write_text(json.dumps(data))
        return data
    return _seed
