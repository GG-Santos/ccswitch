"""At-rest encryption for stored tokens, via Windows DPAPI.

The vault aggregates every account's access + refresh tokens in one file - a
higher-value target than Claude Code's single live credentials file. On Windows
we encrypt each token blob with DPAPI (CryptProtectData), so the data on disk is
only usable by the same Windows user.

Elsewhere - or when CCSWITCH_NO_ENCRYPT is set - this is a transparent
passthrough that stores plaintext, matching the parity of the live creds file.
`unprotect` always handles both forms so an encrypted vault degrades gracefully
and a plaintext one keeps working.
"""

from __future__ import annotations

import base64
import os
import sys

ENC_MARKER = "dpapi"


def available() -> bool:
    return sys.platform == "win32" and not os.environ.get("CCSWITCH_NO_ENCRYPT")


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes) -> "_DATA_BLOB":
        buf = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _from_blob(blob: "_DATA_BLOB") -> bytes:
        size = blob.cbData
        out = ctypes.create_string_buffer(size)
        ctypes.memmove(out, blob.pbData, size)
        return out.raw

    def _protect_bytes(data: bytes) -> bytes:
        out = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(_to_blob(data)), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise ctypes.WinError()
        try:
            return _from_blob(out)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)

    def _unprotect_bytes(data: bytes) -> bytes:
        out = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(_to_blob(data)), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise ctypes.WinError()
        try:
            return _from_blob(out)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(text: str) -> str:
    """Encrypt a string to a base64 token (DPAPI). Caller checks available() first."""
    if not available():
        raise RuntimeError("encryption not available on this platform")
    return base64.b64encode(_protect_bytes(text.encode("utf-8"))).decode("ascii")


def unprotect(token: str) -> str:
    """Decrypt a base64 token produced by protect()."""
    return _unprotect_bytes(base64.b64decode(token)).decode("utf-8")
