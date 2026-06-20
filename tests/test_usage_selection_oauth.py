import time

from ccswitch import oauth, selection, usage, vault
from helpers import make_oauth


def test_percent_from_utilization_headers():
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.42",
        "anthropic-ratelimit-unified-7d-utilization": "0.10",
    }
    best, windows = usage._percent_from_headers(headers)
    assert windows["5h"] == 42
    assert windows["7d"] == 10
    assert best == 42  # max across windows


def test_is_near_limit():
    assert usage.is_near_limit({"status": "rejected"}, 90) is True
    assert usage.is_near_limit({"status": "allowed", "percent": 95}, 90) is True
    assert usage.is_near_limit({"status": "allowed", "percent": 50}, 90) is False
    assert usage.is_near_limit({"status": "allowed", "percent": None}, 90) is False


def test_pick_best_chooses_lowest(sandbox, monkeypatch):
    data = vault.load()
    for n in ("work", "home", "spare"):
        vault.add_account(data, n, make_oauth(n))
    data["active"] = "work"
    vault.save(data)
    data = vault.load()

    fake = {
        "home": {"status": "allowed", "percent": 80, "windows": {"5h": 80}},
        "spare": {"status": "allowed", "percent": 20, "windows": {"5h": 20}},
    }
    monkeypatch.setattr(usage, "probe_all", lambda oauths, names=None: fake)
    name, u, reason = selection.pick_best(data)
    assert name == "spare"  # 20% headroom beats 80%
    assert "headroom" in reason


def test_pick_best_all_maxed_stays_when_active_resets_first(sandbox, monkeypatch):
    data = vault.load()
    for n in ("work", "home"):
        vault.add_account(data, n, make_oauth(n))
    data["active"] = "work"
    vault.save(data)
    data = vault.load()
    monkeypatch.setattr(
        usage, "probe_all",
        lambda oauths, names=None: {"home": {"status": "rejected", "percent": 100,
                                             "reset_at": 10_000}})
    # Active resets sooner than the only (maxed) candidate -> stay put.
    name, _, reason = selection.pick_best(data, active_reset_at=5_000)
    assert name is None
    assert "waiting" in reason


def test_pick_best_all_maxed_switches_to_soonest_reset(sandbox, monkeypatch):
    data = vault.load()
    for n in ("work", "soon", "late"):
        vault.add_account(data, n, make_oauth(n))
    data["active"] = "work"
    vault.save(data)
    data = vault.load()
    monkeypatch.setattr(
        usage, "probe_all",
        lambda oauths, names=None: {
            "soon": {"status": "rejected", "percent": 100, "reset_at": 2_000},
            "late": {"status": "rejected", "percent": 100, "reset_at": 9_000},
        })
    # Both candidates maxed; 'soon' resets well before the active (resets at 8000).
    name, u, reason = selection.pick_best(data, active_reset_at=8_000)
    assert name == "soon"  # soonest reset wins among maxed
    assert "resets first" in reason


def test_pick_best_usable_beats_maxed(sandbox, monkeypatch):
    data = vault.load()
    for n in ("work", "maxed", "ok"):
        vault.add_account(data, n, make_oauth(n))
    data["active"] = "work"
    vault.save(data)
    data = vault.load()
    monkeypatch.setattr(
        usage, "probe_all",
        lambda oauths, names=None: {
            "maxed": {"status": "rejected", "percent": 100, "reset_at": 1_000},
            "ok": {"status": "allowed", "percent": 70, "windows": {"5h": 70}},
        })
    name, _, _ = selection.pick_best(data)
    assert name == "ok"  # a usable account beats a sooner-resetting maxed one


def test_known_cooldowns_from_status_and_cache(sandbox, monkeypatch):
    import time as _t
    from ccswitch import runstate
    data = vault.load()
    for n in ("work", "home", "spare"):
        vault.add_account(data, n, make_oauth(n))
    vault.save(data)
    data = vault.load()
    # home: on cooldown via daemon status; spare: via a cached rejected probe.
    runstate.write_status({"pid": 0, "exhausted_until": {"home": _t.time() + 9999}})
    usage._disk_cache_put(vault.get_oauth(data, "spare")["accessToken"],
                          {"status": "rejected", "percent": 100})
    cooling = selection.known_cooldowns(data)
    assert cooling == {"home", "spare"}
    usage._CACHE.clear()


def test_probe_cache_evicts_stale(sandbox, monkeypatch):
    usage._CACHE.clear()
    usage._CACHE["stale-token"] = (time.monotonic() - 1000, {})
    monkeypatch.setattr(
        usage, "_request",
        lambda tok, model, to: (200, {"anthropic-ratelimit-unified-5h-utilization": "0.1"},
                                None, False, False),
    )
    usage.probe({"accessToken": "new-token"})
    assert "stale-token" not in usage._CACHE  # swept on insert
    assert "new-token" in usage._CACHE
    usage._CACHE.clear()


def test_disk_cache_avoids_second_request(sandbox, monkeypatch):
    usage._CACHE.clear()
    calls = {"n": 0}

    def counting_request(tok, model, to):
        calls["n"] += 1
        return (200, {"anthropic-ratelimit-unified-5h-utilization": "0.3"}, None, False, False)

    monkeypatch.setattr(usage, "_request", counting_request)
    usage.probe({"accessToken": "tok-A"})           # first process: real probe + disk write
    usage._CACHE.clear()                             # simulate a new CLI process
    r = usage.probe({"accessToken": "tok-A"})        # should hit the disk cache
    assert calls["n"] == 1                           # no second network request
    assert r["percent"] == 30
    usage._CACHE.clear()


def test_disk_cache_has_no_token(sandbox, monkeypatch):
    usage._CACHE.clear()
    monkeypatch.setattr(
        usage, "_request",
        lambda tok, model, to: (200, {"anthropic-ratelimit-unified-5h-utilization": "0.3"},
                                None, False, False),
    )
    usage.probe({"accessToken": "super-secret-token"})
    cache_text = usage._disk_cache_path().read_text()
    assert "super-secret-token" not in cache_text  # only a hash + usage numbers
    usage._CACHE.clear()


def test_disk_cache_corrupt_is_a_miss(sandbox):
    usage._disk_cache_path().parent.mkdir(parents=True, exist_ok=True)
    usage._disk_cache_path().write_text("{ not json")
    assert usage._disk_cache_get("anything") is None  # fail-open, no crash


def test_is_expired():
    assert oauth.is_expired({}) is True
    assert oauth.is_expired({"accessToken": "x"}) is True  # no expiry
    assert oauth.is_expired({"accessToken": "x", "expiresAt": int((time.time() - 1) * 1000)}) is True
    assert oauth.is_expired({"accessToken": "x", "expiresAt": int((time.time() + 3600) * 1000)}) is False


def test_refresh_maps_fields(monkeypatch):
    import io
    import json as _json

    class FakeResp:
        def __init__(self, payload):
            self._b = _json.dumps(payload).encode()
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        body = _json.loads(req.data.decode())
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "RT-old"
        assert body["client_id"] == oauth.CLIENT_ID
        return FakeResp({"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 3600})

    monkeypatch.setattr(oauth.urllib.request, "urlopen", fake_urlopen)
    new = oauth.refresh({"accessToken": "AT-old", "refreshToken": "RT-old", "subscriptionType": "pro"})
    assert new["accessToken"] == "AT-new"
    assert new["refreshToken"] == "RT-new"
    assert new["subscriptionType"] == "pro"  # preserved
    assert new["expiresAt"] > time.time() * 1000
