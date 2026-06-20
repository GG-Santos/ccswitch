"""Probe an account's current usage via Anthropic's rate-limit response headers.

Claude Code shows the "approaching your 5h limit" banner from the
`anthropic-ratelimit-*` headers returned on inference responses; those headers
are NOT persisted anywhere local. So to learn an account's usage we must make
our own minimal request with that account's token and read the headers back.

Why a real (1-token) /v1/messages call and not /v1/messages/count_tokens:
count_tokens was tested live and returns HTTP 200 with the token count but
*no* `anthropic-ratelimit-*` headers, so it cannot report usage. The 1-token
message is the cheapest call that surfaces the headers.

This is best-effort: it relies on replicating Claude Code's request shape, and
the exact header names may change. Everything degrades gracefully -- if no
rate-limit headers come back we report status "unknown".
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"
DEFAULT_PROBE_MODEL = "claude-haiku-4-5-20251001"
# Tried in order if a model is rejected (retired/renamed). The first is the
# verified-working default; the rest are fallbacks so a model rename cannot
# permanently blind the probe.
PROBE_MODELS = [DEFAULT_PROBE_MODEL, "claude-haiku-4-5", "claude-3-5-haiku-latest"]
USER_AGENT = "ccswitch/0.3 (account-usage-probe)"

_REMAINING_RE = re.compile(r"anthropic-ratelimit-(?:unified-)?(.+)-remaining$", re.I)
_UTILIZATION_RE = re.compile(r"anthropic-ratelimit-unified-(.+)-utilization$", re.I)
# A real rolling window looks like 5h / 7d / 1w / 1m. This deliberately excludes
# headers like `overage-period-monthly-utilization`, which are not the rolling
# usage windows the daemon should switch on.
_WINDOW_RE = re.compile(r"^\d+[hdwm]$", re.I)

# In-process probe cache: accessToken -> (monotonic_ts, result). Lets a single
# invocation (e.g. `status --all`, `best`) avoid re-probing the same account.
_CACHE: dict = {}
_CACHE_TTL = 10.0

# Cross-invocation cache on disk so repeated `ccswitch status`/`best` calls (each
# a fresh process) reuse a recent reading instead of re-hitting the API. The
# daemon passes cache=False and never touches this. Only usage numbers + a HASH
# of the token are stored - never the token itself.
_DISK_CACHE_TTL = 30.0
_DISK_FIELDS = ("percent", "windows", "status", "reset_at")


def _disk_cache_path():
    from ccswitch import vault  # lazy: avoid import cost on non-cache paths
    return vault.home() / "probe-cache.json"


def _token_key(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _disk_cache_get(token: str):
    import json as _json
    try:
        data = _json.loads(_disk_cache_path().read_text(encoding="utf-8"))
        entry = data.get(_token_key(token))
        if entry and (time.time() - entry["ts"]) < _DISK_CACHE_TTL:
            r = {"percent": None, "windows": {}, "status": "unknown", "reset_at": None,
                 "http_status": None, "raw": {}, "error": None, "auth_error": False}
            r.update(entry.get("result", {}))
            return r
    except (OSError, ValueError, KeyError, TypeError):
        pass  # fail-open: a missing/corrupt cache is just a miss
    return None


def _disk_cache_put(token: str, result: dict) -> None:
    import json as _json
    path = _disk_cache_path()
    try:
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        now = time.time()
        data[_token_key(token)] = {
            "ts": now,
            "result": {k: result.get(k) for k in _DISK_FIELDS},
        }
        # prune stale keys so the file does not grow without bound
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and now - v.get("ts", 0) < _DISK_CACHE_TTL}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # cache is best-effort


def _headers(access_token: str) -> dict:
    return {
        "authorization": f"Bearer {access_token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": OAUTH_BETA,
        "content-type": "application/json",
        "user-agent": USER_AGENT,
    }


def _parse_reset(value: str) -> float | None:
    """Reset header may be unix seconds, an integer of seconds-until, or HTTP/ISO date."""
    value = value.strip()
    if not value:
        return None
    try:
        num = float(value)
        # Heuristic: large numbers are epoch seconds, small ones are deltas.
        if num > 10_000_000:
            return num
        return datetime.now(timezone.utc).timestamp() + num
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _percent_from_headers(headers: dict) -> tuple[float | None, dict]:
    """Return (highest utilization 0-100 across real windows, per-window percents).

    Anthropic's unified headers report utilization directly as a fraction, e.g.
    `anthropic-ratelimit-unified-5h-utilization: 0.17`. Only genuine rolling
    windows (5h, 7d, ...) are counted; overage/period headers are ignored so
    they cannot inflate the max the daemon reacts to. The remaining/limit style
    is supported as a fallback.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    per_window: dict[str, float] = {}

    for key, value in lower.items():
        m = _UTILIZATION_RE.match(key)
        if not m or not _WINDOW_RE.match(m.group(1)):
            continue
        try:
            per_window[m.group(1)] = max(0.0, min(100.0, float(value) * 100.0))
        except (TypeError, ValueError):
            continue

    # Fallback: remaining/limit pairs.
    for key, remaining in lower.items():
        m = _REMAINING_RE.match(key)
        if not m or not _WINDOW_RE.match(m.group(1)):
            continue
        limit = lower.get(key.replace("-remaining", "-limit"))
        try:
            rem, lim = float(remaining), float(limit)
        except (TypeError, ValueError):
            continue
        if lim <= 0:
            continue
        per_window.setdefault(m.group(1), max(0.0, min(100.0, (1.0 - rem / lim) * 100.0)))

    best = max(per_window.values()) if per_window else None
    return best, per_window


def _request(token: str, model: str, timeout: float):
    """Single probe request. Returns (http_status, ratelimit_headers, error_str,
    is_model_error, is_auth_error)."""
    body = json.dumps(
        {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "."}]}
    ).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers=_headers(token), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            hdrs = {k: v for k, v in resp.headers.items()
                    if k.lower().startswith("anthropic-ratelimit")}
            return resp.status, hdrs, None, False, False
    except urllib.error.HTTPError as exc:
        hdrs = {k: v for k, v in (exc.headers or {}).items()
                if k.lower().startswith("anthropic-ratelimit")}
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        is_model = exc.code in (400, 404) and "model" in detail.lower()
        is_auth = exc.code in (401, 403)
        err = None
        if is_auth:
            err = f"auth rejected ({exc.code}) - token may be expired"
        elif exc.code == 429:
            err = None  # rejected is a usage signal, not an error
        elif not is_model:
            err = f"http {exc.code}: {detail[:120]}"
        return exc.code, hdrs, err, is_model, is_auth
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, {}, f"network error: {exc}", False, False


def probe(oauth: dict, model: str | None = None, timeout: float = 20.0,
          cache: bool = True) -> dict:
    """Make a 1-token request and report usage.

    Returns: {percent, windows, status, reset_at, http_status, raw, error,
              auth_error}
      percent     float 0-100 or None if unknowable
      status      'allowed' | 'allowed_warning' | 'rejected' | 'unknown'
      auth_error  True if the token was rejected (401/403) - caller may refresh
    """
    token = (oauth or {}).get("accessToken")
    result = {"percent": None, "windows": {}, "status": "unknown", "reset_at": None,
              "http_status": None, "raw": {}, "error": None, "auth_error": False}
    if not token:
        result["error"] = "no access token"
        return result

    if cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
            return dict(hit[1])
        disk = _disk_cache_get(token)  # reuse a recent reading from another process
        if disk is not None:
            _CACHE[token] = (time.monotonic(), dict(disk))
            return dict(disk)

    requested = model or os.environ.get("CCSWITCH_PROBE_MODEL")
    models = [requested] if requested else list(PROBE_MODELS)

    code = hdrs = None
    for m in models:
        code, hdrs, err, is_model, is_auth = _request(token, m, timeout)
        result["http_status"] = code
        result["error"] = err
        result["auth_error"] = is_auth
        if err and err.startswith("network error"):
            return result  # not cached; transient
        if is_model:
            continue  # try the next model
        break

    rl = hdrs or {}
    result["raw"] = rl

    status_hdr = next((v for k, v in rl.items() if k.lower().endswith("unified-status")), None)
    if status_hdr:
        result["status"] = status_hdr.strip().lower()
    elif code == 429:
        result["status"] = "rejected"
    elif result["status"] == "unknown" and code == 200:
        result["status"] = "allowed"

    pct, windows = _percent_from_headers(rl)
    result["windows"] = windows
    if pct is not None:
        result["percent"] = pct
    elif result["status"] == "rejected":
        result["percent"] = 100.0

    reset_hdr = next((v for k, v in rl.items() if k.lower().endswith("unified-reset")), None)
    if not reset_hdr:
        reset_hdr = next((v for k, v in rl.items() if k.lower().endswith("-reset")), None)
    if reset_hdr:
        result["reset_at"] = _parse_reset(reset_hdr)

    if cache:
        now = time.monotonic()
        # Evict stale entries so a long-lived daemon doesn't accumulate one per
        # rotated token forever.
        for k in [k for k, (ts, _) in _CACHE.items() if now - ts >= _CACHE_TTL]:
            del _CACHE[k]
        _CACHE[token] = (now, dict(result))
        # Persist usable readings for other processes (skip transient failures).
        if not result.get("error") and not result.get("auth_error"):
            _disk_cache_put(token, result)
    return result


def is_near_limit(usage: dict, threshold: float) -> bool:
    if usage.get("status") in ("allowed_warning", "rejected"):
        return True
    pct = usage.get("percent")
    return pct is not None and pct >= threshold


def probe_all(accounts: dict, names=None, max_workers: int = 8) -> dict:
    """Probe several accounts concurrently. Returns {name: usage_dict}.

    `accounts` maps name -> oauth blob. Probing is concurrent because each probe
    is a network round-trip and we may have several accounts. Each probe is a
    tiny (1-token) request that counts marginally toward that account's usage,
    so this is for on-demand views and switch decisions, not per-cycle polling.
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = list(names) if names is not None else list(accounts)
    targets = [n for n in targets if accounts.get(n)]
    if not targets:
        return {}
    results: dict = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as ex:
        futures = {ex.submit(probe, accounts[n]): n for n in targets}
        for fut in futures:
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as exc:  # never let one probe sink the batch
                results[name] = {"percent": None, "windows": {}, "status": "unknown",
                                 "reset_at": None, "http_status": None, "raw": {},
                                 "error": f"probe crashed: {exc}", "auth_error": False}
    return results
