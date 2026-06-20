import json
import time

from ccswitch import actions, creds, oauth, vault
from helpers import make_oauth


def _seed_two(sandbox, seed_live):
    """Vault with work(active)+personal, live creds belong to work."""
    seed_live("work")
    data = vault.load()
    vault.add_account(data, "work", make_oauth("work"))
    vault.add_account(data, "personal", make_oauth("personal"))
    data["active"] = "work"
    vault.save(data)


def test_checkpoint_before_swap(sandbox, seed_live, monkeypatch):
    monkeypatch.setattr(oauth, "refresh", lambda blob: blob)  # no network
    _seed_two(sandbox, seed_live)
    # Claude rotates work's refresh token in the live file.
    live = json.loads(creds.creds_path().read_text())
    live["claudeAiOauth"]["refreshToken"] = "RT-work-rotated"
    creds.creds_path().write_text(json.dumps(live))

    actions.switch_to("personal")

    data = vault.load()
    assert data["accounts"]["work"]["claudeAiOauth"]["refreshToken"] == "RT-work-rotated"
    assert json.loads(creds.creds_path().read_text())["claudeAiOauth"]["accessToken"] == "AT-personal"


def test_switch_refreshes_stale_token(sandbox, seed_live, monkeypatch):
    _seed_two(sandbox, seed_live)
    # Make personal's token already expired.
    data = vault.load()
    data["accounts"]["personal"]["claudeAiOauth"]["expiresAt"] = int((time.time() - 10) * 1000)
    vault.save(data)

    calls = []

    def fake_refresh(blob):
        calls.append(blob["accessToken"])
        new = dict(blob)
        new["accessToken"] = "AT-personal-fresh"
        new["expiresAt"] = int((time.time() + 3600) * 1000)
        return new

    monkeypatch.setattr(oauth, "refresh", fake_refresh)
    res = actions.switch_to("personal")
    assert calls == ["AT-personal"]  # refresh was attempted on the stale token
    assert res["refresh"] == "refreshed"
    assert json.loads(creds.creds_path().read_text())["claudeAiOauth"]["accessToken"] == "AT-personal-fresh"


def test_switch_skips_refresh_when_fresh(sandbox, seed_live, monkeypatch):
    _seed_two(sandbox, seed_live)
    called = []
    monkeypatch.setattr(oauth, "refresh", lambda blob: called.append(1) or blob)
    actions.switch_to("personal")  # personal token is far in the future
    assert called == []  # no refresh needed


def test_refresh_runs_before_lock(sandbox, seed_live, monkeypatch):
    import contextlib

    from ccswitch import actions
    _seed_two(sandbox, seed_live)
    data = vault.load()
    data["accounts"]["personal"]["claudeAiOauth"]["expiresAt"] = int((time.time() - 10) * 1000)
    vault.save(data)

    order = []
    real_tx = vault.transaction

    @contextlib.contextmanager
    def recording_tx():
        order.append("lock")
        with real_tx() as d:
            yield d

    def rec_refresh(blob):
        order.append("refresh")
        new = dict(blob)
        new["expiresAt"] = int((time.time() + 3600) * 1000)
        return new

    monkeypatch.setattr(vault, "transaction", recording_tx)
    monkeypatch.setattr(oauth, "refresh", rec_refresh)
    actions.switch_to("personal")
    # The network refresh must happen before the vault lock is taken.
    assert order == ["refresh", "lock"]


def test_vault_saved_before_live_creds(sandbox, seed_live, monkeypatch):
    from ccswitch import actions, creds
    monkeypatch.setattr(oauth, "refresh", lambda b: b)
    _seed_two(sandbox, seed_live)

    order = []
    real_save = vault.save
    real_write = creds.write_live_oauth
    monkeypatch.setattr(vault, "save", lambda d: order.append("save") or real_save(d))
    monkeypatch.setattr(creds, "write_live_oauth",
                        lambda *a, **k: order.append("live") or real_write(*a, **k))

    actions.switch_to("personal")
    assert order and order[-1] == "live" and "save" in order
    assert order.index("save") < order.index("live")


def test_refresh_failure_is_graceful(sandbox, seed_live, monkeypatch):
    _seed_two(sandbox, seed_live)
    data = vault.load()
    data["accounts"]["personal"]["claudeAiOauth"]["expiresAt"] = int((time.time() - 10) * 1000)
    vault.save(data)

    def boom(blob):
        raise RuntimeError("network down")

    monkeypatch.setattr(oauth, "refresh", boom)
    res = actions.switch_to("personal")
    assert "refresh skipped" in res["refresh"]
    # still switched, using the old (stale) token
    assert json.loads(creds.creds_path().read_text())["claudeAiOauth"]["accessToken"] == "AT-personal"
