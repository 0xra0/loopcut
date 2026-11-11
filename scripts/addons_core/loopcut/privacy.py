"""Local privacy: no anonymous usage telemetry and no conversation-log sharing.

What it turns off is exactly those two: telemetry.enabled() and session_logs.start_report() ask here.
It does not choose the model provider; with Strata selected, chats already stay on this computer.
"""

import os

_PRIVACY_ENV_VAR = "LOOPCUT_LOCAL_ONLY"


def is_privacy_mode_enabled() -> bool:
    """The LOOPCUT_LOCAL_ONLY environment switch, else the preference (on by default)."""
    if os.environ.get(_PRIVACY_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        from . import settings
        prefs = settings.preferences()
    except ImportError:  # Outside Blender (tests, harness): no preferences, only the environment.
        return False
    return prefs is not None and prefs.local_privacy_mode
