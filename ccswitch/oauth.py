"""Refresh a Claude account's access token using its stored refresh token.

Stored accounts go stale: an idle account's accessToken expires, and only the
account that is currently live gets refreshed by Claude Code itself. Without
this, switching to a long-idle account lands on an expired token until Claude
refreshes it. We refresh proactively using the same OAuth flow Claude Code uses.

Endpoint, client id, and request shape were taken from the installed Claude Code
binary (not guessed):
  POST https://platform.claude.com/v1/oauth/token
  JSON {grant_type: "refresh_token", refresh_token, client_id}
  -> {access_token, refresh_token, expires_in, ...}
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = "ccswitch/0.2 (token-refresh)"

# Refresh when the token expires within this many seconds (or is already past).
EXPIRY_BUFFER_S = 300


def is_expired(oauth: dict, buffer_s: int = EXPIRY_BUFFER_S) -> bool:
    """True if the access token is missing, has no expiry, or expires soon.

    expiresAt is stored in milliseconds (Claude Code's convention). When there
    is no expiry recorded we conservatively report stale so a refresh is tried.
    """
    if not oauth.get("accessToken"):
        return True
    expires_at = oauth.get("expiresAt")
    if not expires_at:
        return True
    return (expires_at / 1000.0) - time.time() < buffer_s


def refresh(oauth: dict, timeout: float = 30.0) -> dict:
    """Return a new oauth blob with refreshed tokens. Raises on failure.

    The returned dict is a copy of `oauth` with accessToken/refreshToken/
    expiresAt updated; all other fields (scopes, subscriptionType, ...) are kept
    so the live credentials file stays well-formed.
    """
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise ValueError("account has no refreshToken; re-login required")

    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"content-type": "application/json", "user-agent": USER_AGENT},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise RuntimeError(f"refresh failed (HTTP {exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"refresh failed (network): {exc}") from exc

    access = payload.get("access_token")
    if not access:
        raise RuntimeError(f"refresh response missing access_token: {payload}")

    new = dict(oauth)
    new["accessToken"] = access
    # The server rotates the refresh token; keep the old one only if not returned.
    new["refreshToken"] = payload.get("refresh_token", refresh_token)
    expires_in = payload.get("expires_in")
    if expires_in:
        new["expiresAt"] = int((time.time() + float(expires_in)) * 1000)
    return new
