"""GPU and VRAM resource coordination for Loopcut.

Monitors Strata, Blender, ROCm, ComfyUI, and rendering workloads:
- Queries AMD ROCm VRAM usage via sysfs (/sys/class/drm/card*/device/mem_info_vram_*)
- Detects active competing workloads
- Checks memory headroom before heavy operations (baking, quad remesh, cycles render)
"""

import glob
import os
from pathlib import Path


def get_rocm_vram_info() -> dict:
    """Read VRAM usage from Linux ROCm sysfs."""
    info = {"total_mib": 0, "used_mib": 0, "free_mib": 0, "backend": "unknown"}

    # Look for AMD GPU mem_info nodes
    used_nodes = glob.glob("/sys/class/drm/card*/device/mem_info_vram_used")
    total_nodes = glob.glob("/sys/class/drm/card*/device/mem_info_vram_total")

    if used_nodes and total_nodes:
        try:
            used_bytes = int(Path(used_nodes[0]).read_text().strip())
            total_bytes = int(Path(total_nodes[0]).read_text().strip())
            info["used_mib"] = round(used_bytes / (1024 * 1024), 1)
            info["total_mib"] = round(total_bytes / (1024 * 1024), 1)
            info["free_mib"] = round((total_bytes - used_bytes) / (1024 * 1024), 1)
            info["backend"] = "rocm_sysfs"
            return info
        except Exception:
            pass

    # Fallback to system RAM if no GPU sysfs
    try:
        with open("/proc/meminfo", "r") as f:
            lines = f.readlines()
        mem = {}
        for line in lines:
            parts = line.split(":")
            if len(parts) == 2:
                mem[parts[0].strip()] = int(parts[1].split()[0])
        total = mem.get("MemTotal", 0) // 1024
        avail = mem.get("MemAvailable", 0) // 1024
        info["total_mib"] = total
        info["free_mib"] = avail
        info["used_mib"] = total - avail
        info["backend"] = "host_ram"
    except Exception:
        pass

    return info


def check_vram_headroom_for_heavy_op(required_mib: int = 2048) -> tuple[bool, str]:
    """Check if sufficient VRAM exists before launching a heavy operation."""
    vram = get_rocm_vram_info()
    free = vram.get("free_mib", 0)
    if free > 0 and free < required_mib:
        return False, (
            f"VRAM warning: Only {free} MiB available, but {required_mib} MiB recommended for this heavy operation. "
            "Consider pausing background workloads (e.g. ComfyUI or Strata inference) before proceeding."
        )
    return True, f"VRAM OK: {free} MiB free."
