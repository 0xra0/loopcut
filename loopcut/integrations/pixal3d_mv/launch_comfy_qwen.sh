#!/usr/bin/env bash
set -euo pipefail
# Your normal ComfyUI launch plus --cache-none, which is what stops Qwen filling RAM and
# hanging at "Requested to load QwenImage21" (see README.md). All flags live in
# /mnt/ssd/ComfyUI/launch_best_cpu_vae.sh - keep them there, this stays an env line plus one exec.
# COMFY_PORT is shared with pa.py's URL, so export it once to move the port.
# Real NAF via FlexAttention (FP32); without these Trellis2 falls back to bilinear DummyNAF on AMD.
# TRELLIS2_NAF_STRICT=1 turns every bilinear stand-in (DummyNAF, CPU bilinear projection) into an error;
# pa.py mesh reads this server's environment and refuses to submit without it.
export TRELLIS2_REAL_NAF="${TRELLIS2_REAL_NAF:-1}" TRELLIS2_NATTEN_IMPL="${TRELLIS2_NATTEN_IMPL:-flex}"
export TRELLIS2_NAF_STRICT="${TRELLIS2_NAF_STRICT:-1}"
exec /mnt/ssd/ComfyUI/launch_best_cpu_vae.sh --cache-none --disable-smart-memory "$@"
