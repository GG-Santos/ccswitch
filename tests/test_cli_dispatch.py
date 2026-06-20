from ccswitch import cli, oauth, vault


def test_add_list_rename_remove(sandbox, seed_live):
    seed_live("work")
    assert cli.main(["add", "work"]) == 0
    assert vault.load()["active"] == "work"

    assert cli.main(["list"]) == 0
    assert cli.main(["rename", "work", "main"]) == 0
    assert "main" in vault.load()["accounts"]

    assert cli.main(["remove", "main"]) == 0
    assert "main" not in vault.load()["accounts"]


def test_status_and_doctor_no_network(sandbox, seed_live):
    seed_live("work")
    cli.main(["add", "work"])
    assert cli.main(["status", "--no-probe"]) == 0
    assert cli.main(["doctor"]) == 0


def test_use_unknown_account_errors(sandbox, seed_live):
    seed_live("work")
    cli.main(["add", "work"])
    assert cli.main(["use", "ghost"]) == 1  # unknown account -> error code


def test_rename_to_existing_errors(sandbox, seed_live):
    seed_live("work")
    cli.main(["add", "work"])
    data = vault.load()
    vault.add_account(data, "home", data["accounts"]["work"]["claudeAiOauth"])
    vault.save(data)
    assert cli.main(["rename", "work", "home"]) == 1  # target exists


def test_refresh_all_mocked(sandbox, seed_live, monkeypatch):
    seed_live("work")
    cli.main(["add", "work"])

    def fake_refresh(blob):
        new = dict(blob)
        new["accessToken"] = "AT-refreshed"
        return new

    monkeypatch.setattr(oauth, "refresh", fake_refresh)
    assert cli.main(["refresh", "--all"]) == 0
    assert vault.load()["accounts"]["work"]["claudeAiOauth"]["accessToken"] == "AT-refreshed"


def test_next_skips_cooldown(sandbox, seed_live):
    import time
    from ccswitch import runstate
    seed_live("work")
    cli.main(["add", "work"])
    data = vault.load()
    vault.add_account(data, "home", data["accounts"]["work"]["claudeAiOauth"])
    vault.add_account(data, "spare", data["accounts"]["work"]["claudeAiOauth"])
    data["active"] = "work"
    vault.save(data)
    runstate.write_status({"pid": 0, "exhausted_until": {"home": time.time() + 9999}})

    assert cli.main(["next"]) == 0
    assert vault.load()["active"] == "spare"  # skipped 'home' on cooldown


def test_best_switches_to_more_headroom(sandbox, seed_live, monkeypatch, capsys):
    from ccswitch import usage
    seed_live("work")
    cli.main(["add", "work"])
    data = vault.load()
    vault.add_account(data, "ok", data["accounts"]["work"]["claudeAiOauth"])
    vault.save(data)
    # Active 'work' is busier than 'ok' -> best should move to 'ok'.
    monkeypatch.setattr(usage, "probe_all", lambda oauths, names=None: {
        "work": {"status": "allowed", "percent": 85, "windows": {"5h": 85}},
        "ok": {"status": "allowed", "percent": 30, "windows": {"5h": 30}}})
    assert cli.main(["best"]) == 0
    assert vault.load()["active"] == "ok"
    assert "headroom" in capsys.readouterr().out


def test_best_stays_when_active_is_best(sandbox, seed_live, monkeypatch, capsys):
    from ccswitch import usage
    seed_live("work")
    cli.main(["add", "work"])
    data = vault.load()
    vault.add_account(data, "busy", data["accounts"]["work"]["claudeAiOauth"])
    vault.save(data)
    # Active 'work' has the most headroom -> best should NOT switch off it.
    monkeypatch.setattr(usage, "probe_all", lambda oauths, names=None: {
        "work": {"status": "allowed", "percent": 20, "windows": {"5h": 20}},
        "busy": {"status": "allowed", "percent": 90, "windows": {"5h": 90}}})
    assert cli.main(["best"]) == 0
    assert vault.load()["active"] == "work"  # stayed put
    assert "staying on 'work'" in capsys.readouterr().out


def test_corrupt_vault_returns_clean_error(sandbox, seed_live, capsys):
    vault.vault_path().parent.mkdir(parents=True, exist_ok=True)
    vault.vault_path().write_text("{ not json")
    assert cli.main(["list"]) == 1  # caught VaultError, no traceback
    assert "error:" in capsys.readouterr().err


def test_daemon_single_instance_guard(sandbox, seed_live, monkeypatch):
    import os
    from ccswitch import runstate
    seed_live("work")
    cli.main(["add", "work"])
    # Pretend a daemon is already running as this very process (pid is alive).
    runstate.write_status({"pid": os.getpid(), "last_check": "now"})
    assert cli.main(["daemon"]) == 1  # refused without --force
