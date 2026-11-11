"""Native Strata provider support for Loopcut.

Provides automatic localhost discovery, model selection, connection testing,
and live backend status for the local Strata engine and Swift 1.5 checkpoints.
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
STRATA_RUNNER_CONFIG = Path("/mnt/ssd/Strata/strata-swift-abl-iq2_xs.json")
STRATA_RUN_SCRIPT = Path("/mnt/ssd/Strata/run-swift-abl-iq2_xs.sh")


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


def get_local_runner_info() -> dict:
    """Read local Strata configuration and script metadata from disk if available."""
    info = {
        "installed": False,
        "config_path": str(STRATA_RUNNER_CONFIG),
        "run_script_path": str(STRATA_RUN_SCRIPT),
        "run_script_exists": STRATA_RUN_SCRIPT.is_file(),
        "model_name": "swift-1.5-abliterated-iq2_xs",
        "backend": "hip",
        "vision_supported": True,
        "vram_reserve_mib": 3072,
        "max_context": 131072,
    }
    if STRATA_RUNNER_CONFIG.is_file():
        try:
            data = json.loads(STRATA_RUNNER_CONFIG.read_text(encoding="utf-8"))
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
        except Exception:
            pass
    return info


def test_strata_connection(base_url: str = config.STRATA_BASE_URL, timeout: float = 2.5) -> dict:
    """Test connection to Strata, measure latency, and detect backend status."""
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
        "backend": "unknown",
        "vision_ready": False,
        "error": None,
        "runner_info": get_local_runner_info(),
    }

    if not is_port_open(host, port, timeout=1.0):
        # Check if alternative port is up
        alt_port = discover_localhost()
        if alt_port and alt_port != port:
            status["error"] = f"Strata server not running on port {port}, but found active on port {alt_port}."
            status["suggested_base_url"] = f"http://127.0.0.1:{alt_port}/v1"
        else:
            status["error"] = f"Strata server not running on {host}:{port}. Start with {STRATA_RUN_SCRIPT}."
        return status

    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers={"User-Agent": "Loopcut-Blender"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = (time.time() - start_time) * 1000.0
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
            status["ok"] = True
            status["latency_ms"] = round(latency, 1)
            status["models"] = models
            status["active_model"] = models[0] if models else config.STRATA_MODEL
            status["backend"] = status["runner_info"].get("backend", "hip")
            status["vision_ready"] = status["runner_info"].get("vision_supported", False)
    except urllib.error.URLError as err:
        status["error"] = f"Connection error: {err.reason}"
    except Exception as ex:
        status["error"] = f"Unexpected error querying Strata: {ex}"

    return status
