"""Native Strata provider support for Loopcut.

Provides automatic localhost discovery, model selection, connection testing,
portable runner discovery, active vision verification, and live backend status
for the local Strata engine and Swift 1.5 checkpoints.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import config

STRATA_DEFAULT_PORT = 8099
STRATA_FALLBACK_PORT = 8080

COMMON_CONFIG_PATHS = [
    Path.home() / "Strata" / "strata-swift-abl-iq2_xs.json",
    Path.home() / "Strata" / "strata-swift.json",
    Path("/mnt/ssd/Strata/strata-swift-abl-iq2_xs.json"),
    Path("/opt/Strata/strata-swift-abl-iq2_xs.json"),
    Path("/opt/Strata/strata.json"),
]

COMMON_RUN_SCRIPTS = [
    Path.home() / "Strata" / "run-swift-abl-iq2_xs.sh",
    Path.home() / "Strata" / "run-swift.sh",
    Path("/mnt/ssd/Strata/run-swift-abl-iq2_xs.sh"),
    Path("/opt/Strata/run-swift-abl-iq2_xs.sh"),
    Path("/opt/Strata/run.sh"),
]

# 16x16 test PNG containing a distinct red square for vision verification
TEST_PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAHElEQVR4nGP4TyJgGOwaGBiwo1EN9NVABBiEGgCoAH2fxYWAGQAAAABJRU5ErkJggg=="
EXPECTED_VISION_SUBJECT = "red"


def is_strata_provider(base_url: str = "") -> bool:
    """Return True if base_url points to a local or configured Strata endpoint."""
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or ""
        port = parsed.port
        if host in ("127.0.0.1", "localhost", "0.0.0.0") and port in (STRATA_DEFAULT_PORT, STRATA_FALLBACK_PORT):
            return True
        return "strata" in base_url.lower()
    except (ValueError, AttributeError):
        return False


def get_strata_config_path(custom_path: str = "") -> Path | None:
    """Find the active Strata config path from custom argument, env var, preferences, or standard paths."""
    if custom_path and custom_path.strip():
        p = Path(custom_path.strip())
        if p.is_file():
            return p
    env_p = os.environ.get("STRATA_CONFIG")
    if env_p and Path(env_p).is_file():
        return Path(env_p)
    try:
        from . import settings
        prefs = settings.preferences()
        pref_p = getattr(prefs, "strata_config_path", "") if prefs else ""
        if pref_p and Path(pref_p).is_file():
            return Path(pref_p)
    except (ImportError, AttributeError):
        pass
    for cand in COMMON_CONFIG_PATHS:
        if cand.is_file():
            return cand
    return None


def get_strata_run_script(custom_path: str = "") -> Path | None:
    """Find the active Strata run script from custom argument, env var, preferences, or standard paths."""
    if custom_path and custom_path.strip():
        p = Path(custom_path.strip())
        if p.is_file():
            return p
    env_p = os.environ.get("STRATA_RUN_SCRIPT")
    if env_p and Path(env_p).is_file():
        return Path(env_p)
    try:
        from . import settings
        prefs = settings.preferences()
        pref_p = getattr(prefs, "strata_run_script", "") if prefs else ""
        if pref_p and Path(pref_p).is_file():
            return Path(pref_p)
    except (ImportError, AttributeError):
        pass
    for cand in COMMON_RUN_SCRIPTS:
        if cand.is_file():
            return cand
    return None


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Check if a local TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def discover_localhost(ports=(STRATA_DEFAULT_PORT, STRATA_FALLBACK_PORT)) -> int | None:
    """Discover a running Strata server on localhost ports. Returns port or None."""
    for port in ports:
        if is_port_open("127.0.0.1", port):
            return port
    return None


def get_local_runner_info(config_path: str = "", run_script_path: str = "") -> dict:
    """Read local Strata configuration and script metadata from disk if available."""
    cfg_file = get_strata_config_path(config_path)
    scr_file = get_strata_run_script(run_script_path)

    info = {
        "installed": cfg_file is not None,
        "config_path": str(cfg_file) if cfg_file else "",
        "run_script_path": str(scr_file) if scr_file else "",
        "run_script_exists": scr_file is not None and scr_file.is_file(),
        "model_name": config.STRATA_MODEL,
        "backend": "hip",
        "vision_supported": False,
        "vram_reserve_mib": 3072,
        "max_context": 131072,
    }
    if cfg_file and cfg_file.is_file():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            info["installed"] = True
            info["model_name"] = data.get("model_name", info["model_name"])
            info["backend"] = data.get("backend", info["backend"])
            info["port"] = data.get("port", STRATA_DEFAULT_PORT)
            if "vision" in data:
                info["vision_supported"] = True
                info["vision_exe"] = data["vision"].get("exe")
                info["vision_model"] = data["vision"].get("model")
            args = data.get("args", [])
            if "--vram-reserve-mib" in args:
                idx = args.index("--vram-reserve-mib")
                if idx + 1 < len(args):
                    info["vram_reserve_mib"] = int(args[idx + 1])
            if "--max-context" in args:
                idx = args.index("--max-context")
                if idx + 1 < len(args):
                    info["max_context"] = int(args[idx + 1])
        except (OSError, json.JSONDecodeError, ValueError, KeyError):
            pass
    return info


def test_strata_connection(base_url: str = config.STRATA_BASE_URL, timeout: float = 2.5) -> dict:
    """Test connection to Strata, discover active model from API, measure latency, and detect backend status."""
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or STRATA_DEFAULT_PORT
    start_time = time.time()

    status = {
        "ok": False,
        "base_url": base_url,
        "host": host,
        "port": port,
        "latency_ms": None,
        "models": [],
        "active_model": None,
        "max_context": 131072,
        "backend": "unknown",
        "vision_ready": False,
        "error": None,
        "runner_info": get_local_runner_info(),
    }

    if not is_port_open(host, port, timeout=1.0):
        alt_port = discover_localhost()
        if alt_port and alt_port != port:
            status["error"] = f"Strata server not running on port {port}, but found active on port {alt_port}."
            status["suggested_base_url"] = f"http://127.0.0.1:{alt_port}/v1"
        else:
            scr = get_strata_run_script()
            status["error"] = f"Strata server not running on {host}:{port}. Start with {scr or 'Strata launch script'}."
        return status

    # Query /health if available on server root
    root_url = f"{parsed.scheme or 'http'}://{host}:{port}"
    try:
        h_req = urllib.request.Request(f"{root_url}/health", headers={"User-Agent": "Loopcut-Blender"})
        with urllib.request.urlopen(h_req, timeout=timeout) as resp:
            h_data = json.loads(resp.read().decode("utf-8"))
            if isinstance(h_data, dict):
                if h_data.get("model"):
                    status["active_model"] = h_data["model"]
                if h_data.get("max_context"):
                    status["max_context"] = int(h_data["max_context"])
                if h_data.get("images"):
                    status["vision_ready"] = True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError, KeyError):
        pass

    # Query /models or /v1/models
    models_url = f"{base_url.rstrip('/')}/models"
    try:
        req = urllib.request.Request(models_url, headers={"User-Agent": "Loopcut-Blender"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = (time.time() - start_time) * 1000.0
            data = json.loads(resp.read().decode("utf-8"))
            models = []
            for m in data.get("data", []):
                if isinstance(m, dict) and m.get("id"):
                    models.append(m["id"])
                    arch = m.get("architecture") or {}
                    modalities = arch.get("input_modalities", [])
                    if "image" in modalities:
                        status["vision_ready"] = True
                    n_ctx = (m.get("meta") or {}).get("n_ctx")
                    if n_ctx:
                        status["max_context"] = int(n_ctx)

            status["ok"] = True
            status["latency_ms"] = round(latency, 1)
            status["models"] = models
            if not status["active_model"]:
                status["active_model"] = models[0] if models else config.STRATA_MODEL
            status["backend"] = status["runner_info"].get("backend", "hip")
            if not status["vision_ready"]:
                status["vision_ready"] = status["runner_info"].get("vision_supported", False)
    except urllib.error.HTTPError as err:
        status["error"] = f"HTTP error {err.code}: {err.reason}"
    except urllib.error.URLError as err:
        status["error"] = f"Connection error: {err.reason}"
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as ex:
        status["error"] = f"Error querying Strata models: {ex}"

    return status


def verify_strata_vision(base_url: str = config.STRATA_BASE_URL, model: str = "", timeout: float = 20.0) -> dict:
    """Actively send a test image to the Strata server to verify real interpretation capability."""
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or STRATA_DEFAULT_PORT

    result = {
        "ok": False,
        "vision_verified": False,
        "active_model": model or config.STRATA_MODEL,
        "reply": "",
        "details": "",
        "error": None,
    }

    if not is_port_open(host, port, timeout=1.0):
        result["error"] = f"Strata server not running on {host}:{port}"
        result["details"] = "Cannot verify vision: server is offline."
        return result

    target_url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model or config.STRATA_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What color is the shape in this image? Reply with just the color name."},
                    {"type": "image_url", "image_url": {"url": TEST_PNG_DATA_URL}},
                ],
            }
        ],
        "max_tokens": 100,
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        target_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Loopcut-Blender"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                content = (msg.get("content") or "").strip()
                result["ok"] = True
                result["reply"] = content
                if EXPECTED_VISION_SUBJECT.lower() in content.lower():
                    result["vision_verified"] = True
                    result["details"] = f"Vision verified: server correctly identified '{EXPECTED_VISION_SUBJECT}' (reply: {content!r})"
                else:
                    result["vision_verified"] = False
                    result["details"] = f"Vision check failed: expected '{EXPECTED_VISION_SUBJECT}', got {content!r}"
            else:
                result["details"] = "Vision request completed but returned no choices."
    except urllib.error.HTTPError as err:
        err_body = ""
        try:
            err_body = err.read().decode("utf-8", errors="replace")
        except OSError:
            pass
        result["error"] = f"HTTP {err.code}: {err.reason} - {err_body}"
        result["details"] = f"Vision verification rejected by server: {result['error']}"
    except urllib.error.URLError as err:
        result["error"] = f"Connection error: {err.reason}"
        result["details"] = f"Could not reach server for vision verification: {err.reason}"
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as ex:
        result["error"] = str(ex)
        result["details"] = f"Failed to process vision verification response: {ex}"

    return result
