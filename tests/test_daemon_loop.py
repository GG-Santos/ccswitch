import time

from ccswitch import daemon, oauth, runstate, usage, vault
from helpers import make_oauth


def test_daemon_all_maxed_switches_to_soonest(sandbox, seed_live, monkeypatch):
    _seed(seed_live, active="work", others=("soon", "late"))
    now = time.time()
    monkeypatch.setattr(daemon, "probe", lambda o, cache=True: _usage(95, reset_at=now + 8000))
    monkeypatch.setattr(usage, "probe_all", lambda oauths, names=None: {
        "soon": {"status": "rejected", "percent": 100, "reset_at": now + 1000},
        "late": {"status": "rejected", "percent": 100, "reset_at": now + 7000},
    })
    monkeypatch.setattr(daemon.notify, "notify", lambda *a, **k: True)
    daemon.run(once=True, threshold=90, interval=0, strategy="best")
    assert vault.load()["active"] == "soon"  # pre-positioned on the soonest-reset account


def test_daemon_all_maxed_picks_previously_exhausted_if_soonest(sandbox, seed_live, monkeypatch):
    # The shortest-cooldown account may be one the daemon already rotated off
    # (so it's on the exhausted list). It must still be chosen when all are maxed.
    _seed(seed_live, active="work", others=("early", "late"))
    now = time.time()
    runstate.write_status({"pid": 0, "exhausted_until": {"early": now + 1000}})
    monkeypatch.setattr(daemon, "probe", lambda o, cache=True: _usage(95, reset_at=now + 8000))
    monkeypatch.setattr(usage, "probe_all", lambda oauths, names=None: {
        "early": {"status": "rejected", "percent": 100, "reset_at": now + 1000},
        "late": {"status": "rejected", "percent": 100, "reset_at": now + 6000},
    })
    monkeypatch.setattr(daemon.notify, "notify", lambda *a, **k: True)
    daemon.run(once=True, threshold=90, interval=0, strategy="best")
    assert vault.load()["active"] == "early"  # shortest cooldown wins despite being "exhausted"


def test_daemon_all_maxed_stays_when_active_resets_first(sandbox, seed_live, monkeypatch):
    _seed(seed_live, active="work", others=("soon", "late"))
    now = time.time()
    monkeypatch.setattr(daemon, "probe", lambda o, cache=True: _usage(95, reset_at=now + 500))
    monkeypatch.setattr(usage, "probe_all", lambda oauths, names=None: {
        "soon": {"status": "rejected", "percent": 100, "reset_at": now + 4000},
        "late": {"status": "rejected", "percent": 100, "reset_at": now + 7000},
    })
    monkeypatch.setattr(daemon.notify, "notify", lambda *a, **k: True)
    daemon.run(once=True, threshold=90, interval=0, strategy="best")
    assert vault.load()["active"] == "work"  # active recovers first; no pointless switch


def _seed(seed_live, active="work", others=("home",)):
    seed_live(active)
    data = vault.load()
    vault.add_account(data, active, make_oauth(active))
    for o in others:
        vault.add_account(data, o, make_oauth(o))
    data["active"] = active
    vault.save(data)


def test_compute_sleep_scales_with_usage():
    # Far below the limit -> sleep stretches toward max_interval.
    far = daemon._compute_sleep(_usage(0), 90, 60, 600, switched=False,
                                all_exhausted=False, exhausted_until={}, now=0)
    assert far > 500  # near the ceiling
    # Near the limit -> tightest cadence (the floor).
    near = daemon._compute_sleep(_usage(88), 90, 60, 600, switched=False,
                                 all_exhausted=False, exhausted_until={}, now=0)
    assert near == 60
    # Mid usage -> somewhere in between, monotonic.
    mid = daemon._compute_sleep(_usage(45), 90, 60, 600, switched=False,
                                all_exhausted=False, exhausted_until={}, now=0)
    assert 60 < mid < far
    # Unknown usage -> floor.
    assert daemon._compute_sleep(_usage(None), 90, 60, 600, switched=False,
                                 all_exhausted=False, exhausted_until={}, now=0) == 60


def test_compute_sleep_after_switch_is_floor():
    assert daemon._compute_sleep(_usage(10), 90, 60, 600, switched=True,
                                 all_exhausted=False, exhausted_until={}, now=0) == 60


def test_compute_sleep_to_reset_when_exhausted():
    now = 1000.0
    reset = now + 800  # soonest reset 800s out
    s = daemon._compute_sleep(_usage(100), 90, 60, 600, switched=False,
                              all_exhausted=True, exhausted_until={"x": reset, "y": now + 5000},
                              now=now)
    assert 800 < s <= 800 + 30  # sleeps to the soonest reset (+buffer)
    # Bounded so it never sleeps absurdly long.
    s2 = daemon._compute_sleep(_usage(100), 90, 60, 600, switched=False,
                               all_exhausted=True, exhausted_until={"x": now + 99999},
                               now=now)
    assert s2 <= daemon._RESET_SLEEP_CAP


def _usage(percent, **kw):
    base = {"percent": percent, "windows": {"5h": percent} if percent is not None else {},
            "status": "allowed", "reset_at": None, "http_status": 200, "raw": {},
            "error": None, "auth_error": False}
    base.update(kw)
    return base


def test_switches_when_near_limit(sandbox, seed_live, monkeypatch):
    _seed(seed_live)
    monkeypatch.setattr(daemon, "probe", lambda oauth, cache=True: _usage(95))
    monkeypatch.setattr(daemon.notify, "notify", lambda *a, **k: True)

    daemon.run(once=True, threshold=90, interval=0, strategy="next")

    assert vault.load()["active"] == "home"  # rotated off the maxed account
    status = runstate.read_status()
    assert status and status["last_switch"] and "work -> home" in status["last_switch"]


def test_no_switch_when_below_threshold(sandbox, seed_live, monkeypatch):
    _seed(seed_live)
    monkeypatch.setattr(daemon, "probe", lambda oauth, cache=True: _usage(40))
    daemon.run(once=True, threshold=90, interval=0, strategy="next")
    assert vault.load()["active"] == "work"  # stayed put


def test_self_heals_on_auth_error(sandbox, seed_live, monkeypatch):
    _seed(seed_live, others=())
    calls = {"n": 0}

    def fake_probe(oauth, cache=True):
        calls["n"] += 1
        return _usage(None, auth_error=True) if calls["n"] == 1 else _usage(30)

    def fake_refresh(blob):
        new = dict(blob)
        new["accessToken"] = "AT-work-fresh"
        new["expiresAt"] = int((time.time() + 3600) * 1000)
        return new

    monkeypatch.setattr(daemon, "probe", fake_probe)
    monkeypatch.setattr(oauth, "refresh", fake_refresh)

    daemon.run(once=True, threshold=90, interval=0, strategy="next")

    assert calls["n"] == 2  # probed, hit 401, refreshed, re-probed
    assert vault.load()["accounts"]["work"]["claudeAiOauth"]["accessToken"] == "AT-work-fresh"


def test_seeds_cooldowns_from_status(sandbox, seed_live, monkeypatch):
    _seed(seed_live)  # work active, home present
    # home is recorded as exhausted until the future.
    runstate.write_status({"pid": 0, "exhausted_until": {"home": time.time() + 9999}})
    toasts = []
    monkeypatch.setattr(daemon, "probe", lambda oauth, cache=True: _usage(95))
    monkeypatch.setattr(daemon.notify, "notify", lambda *a, **k: toasts.append(a) or True)

    daemon.run(once=True, threshold=90, interval=0, strategy="next")

    # work is maxed and home is on cooldown -> nowhere to go, stays put + toasts once.
    assert vault.load()["active"] == "work"
    assert toasts  # all-exhausted notification fired
