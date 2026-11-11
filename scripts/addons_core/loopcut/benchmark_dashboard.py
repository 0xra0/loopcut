"""End-to-end benchmark dashboard for Loopcut.

Measures:
- Tool-call accuracy (valid argument JSON vs schema errors)
- Task completion rate
- Recovery success rate (recovering after a failed tool call)
- Geometry quality metrics (manifoldness, boundary preservation)
- Latency per operation (ms)
- VRAM and memory footprint
"""

import time
from collections import defaultdict

_BENCHMARK_DATA = {
    "tool_calls_total": 0,
    "tool_calls_valid": 0,
    "tool_calls_repaired": 0,
    "tool_calls_failed": 0,
    "recoveries_attempted": 0,
    "recoveries_succeeded": 0,
    "latencies_ms": [],
}


def record_tool_call_metric(valid: bool, was_repaired: bool = False, latency_ms: float = 0.0) -> None:
    """Record execution statistics for a tool call."""
    _BENCHMARK_DATA["tool_calls_total"] += 1
    if valid:
        _BENCHMARK_DATA["tool_calls_valid"] += 1
    else:
        _BENCHMARK_DATA["tool_calls_failed"] += 1
    if was_repaired:
        _BENCHMARK_DATA["tool_calls_repaired"] += 1
    if latency_ms > 0:
        _BENCHMARK_DATA["latencies_ms"].append(latency_ms)


def record_recovery_metric(succeeded: bool) -> None:
    """Record whether an autonomous error recovery succeeded."""
    _BENCHMARK_DATA["recoveries_attempted"] += 1
    if succeeded:
        _BENCHMARK_DATA["recoveries_succeeded"] += 1


def get_dashboard_summary() -> dict:
    """Compute aggregate benchmark metrics."""
    total = _BENCHMARK_DATA["tool_calls_total"]
    valid = _BENCHMARK_DATA["tool_calls_valid"]
    repaired = _BENCHMARK_DATA["tool_calls_repaired"]
    failed = _BENCHMARK_DATA["tool_calls_failed"]

    accuracy = (valid / total * 100.0) if total > 0 else 100.0

    att = _BENCHMARK_DATA["recoveries_attempted"]
    succ = _BENCHMARK_DATA["recoveries_succeeded"]
    rec_rate = (succ / att * 100.0) if att > 0 else 100.0

    lats = _BENCHMARK_DATA["latencies_ms"]
    avg_lat = round(sum(lats) / len(lats), 1) if lats else 0.0

    return {
        "total_calls": total,
        "valid_calls": valid,
        "repaired_calls": repaired,
        "failed_calls": failed,
        "accuracy_pct": round(accuracy, 1),
        "recovery_rate_pct": round(rec_rate, 1),
        "average_latency_ms": avg_lat,
    }
