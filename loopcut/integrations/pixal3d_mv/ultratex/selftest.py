"""Check UltraTex's two GPU-specific parts on this machine: Triton sparse attention and the moderngl renderer."""
import sys, time
sys.path.insert(0, "/mnt/ssd/ComfyUI/custom_nodes/ComfyUI-UltraTex")
import torch
print("torch", torch.__version__, "gpu", torch.cuda.get_device_name(0))
from ultratex_core import optim
t = time.time(); err = optim.check_sparse_attention()
print("sparse attention:", "OK" if err is None else err, f"({time.time()-t:.1f}s incl. compile)")
if err is None:
    q = torch.randn(1, 24, 8192, 128, device="cuda", dtype=torch.bfloat16)
    sla = optim._sla(0.2)
    for name, f in (("sparse", lambda: sla(q, q, q)),
                    ("dense sdpa", lambda: torch.nn.functional.scaled_dot_product_attention(q, q, q))):
        f(); torch.cuda.synchronize(); t = time.time()
        for _ in range(5): f()
        torch.cuda.synchronize(); print(f"  {name}: {(time.time()-t)/5*1000:.1f} ms")
import moderngl
ctx = moderngl.create_standalone_context(require=330)
print("moderngl:", ctx.info["GL_RENDERER"], ctx.info["GL_VERSION"])
