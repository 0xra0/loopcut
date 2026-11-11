"""Strata's Fast, Balanced and Deep thinking modes, as the reasoning effort each request carries.

Strata's server (serve/server.py) reads `reasoning_effort` from every request and sizes the
model's thinking by it; with none it falls back to its config's level.
"""

EFFORTS = {"fast": "low", "balanced": "medium", "deep": "high"}


def reasoning_effort(profile: str) -> str:
    """The reasoning effort for a thinking mode; Balanced's for an unknown one."""
    return EFFORTS.get(profile, EFFORTS["balanced"])
