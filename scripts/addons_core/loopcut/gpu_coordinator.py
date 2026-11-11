"""GPU and VRAM resource coordination for Loopcut.

Monitors Strata, Blender, ROCm, ComfyUI, and rendering workloads:
- Queries AMD ROCm VRAM usage via sysfs (/sys/class/drm/card*/device/mem_info_vram_*)
- Detects active competing workloads
- Checks memory headroom before heavy operations (baking, quad remesh, cycles render)
- Coordinates and queues competing GPU-heavy operations
"""

import glob
import os
import time
from pathlib import Path

# In-process coordination of competing heavy GPU tasks
_active_heavy_job: dict | None = None
_job_queue: list[dict] = []


def get_rocm_vram_info() -> dict:
    """Read VRAM usage from Linux ROCm sysfs."""
    info = {"total_mib": 0, "used_mib": 0, "free_mib": 0, "backend": "unknown"}

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
        except (OSError, ValueError, IndexError):
            pass

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
    except (OSError, ValueError, KeyError):
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


def get_vram_status() -> dict:
    """Return live VRAM statistics and active/queued GPU jobs."""
    info = get_rocm_vram_info()
    info["active_job"] = dict(_active_heavy_job) if _active_heavy_job else None
    info["queued_jobs"] = [dict(j) for j in _job_queue]
    return info


def acquire_gpu_stage(job_id: str, kind: str, required_vram_mib: int = 2048) -> tuple[bool, str]:
    """Acquire exclusive GPU stage lease or queue behind an active heavy job."""
    global _active_heavy_job, _job_queue
    vram = get_rocm_vram_info()
    free = vram.get("free_mib", 0)

    if _active_heavy_job is not None and _active_heavy_job.get("job_id") != job_id:
        entry = {"job_id": job_id, "kind": kind, "required_mib": required_vram_mib, "queued_at": time.time()}
        if not any(j.get("job_id") == job_id for j in _job_queue):
            _job_queue.append(entry)
        pos = len(_job_queue)
        return False, f"Queued (position {pos}) behind active GPU stage {_active_heavy_job.get('kind')} ({_active_heavy_job.get('job_id')}). Free VRAM: {free} MiB."

    ok, headroom_msg = check_vram_headroom_for_heavy_op(required_vram_mib)
    if not ok:
        entry = {"job_id": job_id, "kind": kind, "required_mib": required_vram_mib, "queued_at": time.time()}
        if not any(j.get("job_id") == job_id for j in _job_queue):
            _job_queue.append(entry)
        pos = len(_job_queue)
        return False, f"Insufficient VRAM headroom for {kind}: {headroom_msg} Queued at position {pos}."

    _active_heavy_job = {"job_id": job_id, "kind": kind, "required_mib": required_vram_mib, "started_at": time.time()}
    _job_queue = [j for j in _job_queue if j.get("job_id") != job_id]
    return True, f"Acquired GPU stage for {kind} with {free} MiB free. ({headroom_msg})"


def release_gpu_stage(job_id: str) -> dict | None:
    """Release active GPU stage lease and promote next queued job."""
    global _active_heavy_job, _job_queue
    if _active_heavy_job and _active_heavy_job.get("job_id") == job_id:
        if _job_queue:
            next_job = _job_queue.pop(0)
            next_job["started_at"] = time.time()
            _active_heavy_job = next_job
            return next_job
        _active_heavy_job = None
    else:
        _job_queue = [j for j in _job_queue if j.get("job_id") != job_id]
    return None
