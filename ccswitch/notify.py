"""Best-effort desktop notification. Silent no-op if it cannot be delivered.

The daemon switches accounts in the background, which is otherwise invisible.
A toast tells the user it happened. We use the OS notifier where we can and
never let a notification failure affect the switch itself.
"""

from __future__ import annotations

import subprocess
import sys

_PS_TOAST = r"""
try {{
  $ErrorActionPreference = 'Stop'
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $texts = $xml.GetElementsByTagName('text')
  $texts.Item(0).AppendChild($xml.CreateTextNode('{title}')) | Out-Null
  $texts.Item(1).AppendChild($xml.CreateTextNode('{message}')) | Out-Null
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('ccswitch').Show($toast)
}} catch {{ exit 1 }}
"""


def _escape(text: str) -> str:
    return text.replace("'", "''").replace("\n", " ")


def notify(title: str, message: str) -> bool:
    """Show a desktop notification. Returns True if it was dispatched."""
    if sys.platform != "win32":
        return False
    script = _PS_TOAST.format(title=_escape(title), message=_escape(message))
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=15,
        )
        return proc.returncode == 0
    except Exception:
        return False
