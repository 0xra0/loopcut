#!/bin/bash
# UltraTex (PixelArtistry "Standard 6 views PBR") models, ~20 GB. Node pack: visualbruno/ComfyUI-UltraTex @ 8137da5
# (already cloned; its pip deps trimesh/moderngl/xatlas/scipy are in the env). Run selftest.py after a node update.
# Klein base 9B + Qwen3-8B as GGUF Q8_0 instead of PixelArtistry's fp8 files: closer to bf16, and fp8mixed goes
# through comfy_kitchen like the int8 encoder that failed on gfx1030. Must be Klein *base*, not distilled Klein.
# The two LoRAs are both called dit_lora.safetensors upstream - each is renamed on download.
M=/mnt/ssd/ComfyUI/models
MS=https://www.modelscope.ai/models/Yibo-Zhang/UltraTex/resolve/master
get() {  # url dest
  [ -f "$2" ] && { echo "have $(basename "$2")"; return; }
  mkdir -p "$(dirname "$2")"
  curl -fL -C - -o "$2.part" "$1" && mv "$2.part" "$2" && echo "done $(basename "$2")" || echo "FAIL $(basename "$2")"
}
get https://huggingface.co/unsloth/FLUX.2-klein-base-9B-GGUF/resolve/main/flux-2-klein-base-9b-Q8_0.gguf \
    $M/diffusion_models/flux-2-klein-base-9b-Q8_0.gguf
get https://huggingface.co/unsloth/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q8_0.gguf $M/text_encoders/Qwen3-8B-Q8_0.gguf
get https://huggingface.co/Comfy-Org/flux2-dev/resolve/main/split_files/vae/flux2-vae.safetensors $M/vae/flux2-vae.safetensors
get $MS/flux2/lora/dit_lora.safetensors    $M/loras/ultratex_flux2.safetensors
get $MS/flux2_mr/lora/dit_lora.safetensors $M/loras/ultratex_flux2_mr.safetensors
get $MS/flux2/decoder.pt                   $M/ultratex/ultratex_flux2_decoder.pt
[ -f $M/background_removal/birefnet.safetensors ] && echo "ok   background_removal/birefnet.safetensors" \
  || echo "MISSING background_removal/birefnet.safetensors"
git -C /mnt/ssd/ComfyUI/custom_nodes/ComfyUI-UltraTex log --oneline -1 | grep -q 8137da5 && echo "ok   ComfyUI-UltraTex 8137da5" \
  || echo "CHECK ComfyUI-UltraTex is not at 8137da5 (the commit PixelArtistry's workflows target)"
