#!/bin/bash
# Downloads the one missing model (Qwen3-VL text encoder as GGUF Q8_0 + its vision mmproj) and checks the rest.
# GGUF, not the Comfy-Org int8_convrot/w4a8 files: int8 matmul has no fast kernel on the RX 6800 (gfx1030).
# The mmproj must be renamed so its name contains the text encoder's name - that's how ComfyUI-GGUF pairs them.
M=/mnt/ssd/ComfyUI/models
B=https://huggingface.co/unsloth/Qwen3-VL-8B-Instruct-GGUF/resolve/main
get() {  # url dest
  [ -f "$2" ] && { echo "have $(basename "$2")"; return; }
  curl -fL -C - -o "$2.part" "$1" && mv "$2.part" "$2" && echo "done $(basename "$2")" || echo "FAIL $(basename "$2")"
}
get $B/Qwen3-VL-8B-Instruct-Q8_0.gguf $M/text_encoders/Qwen3-VL-8B-Instruct-Q8_0.gguf
get $B/mmproj-F16.gguf               $M/text_encoders/Qwen3-VL-8B-Instruct-mmproj-F16.gguf
for f in diffusion_models/qwen-image-2.1-Q8_0.gguf vae/qwen_image_2.1_vae_bf16.safetensors \
         vae/trellis_2_texture_vae_bf16.safetensors vae/trellis_2_shape_vae_bf16.safetensors \
         clip_vision/dino_v3_L_naf_fp32.safetensors; do
  ls $M/$f $M/unet/$(basename $f) 2>/dev/null | head -1 | grep -q . && echo "ok   $f" || echo "MISSING $f"
done
