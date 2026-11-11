"""Split a turnaround sheet (front|left|back|right in one row) into 4 RGBA views on
equal HxH canvases, or (--views) cut out four separately generated views.
Background removed with rembg isnet; sheet panels are found from the mask's column occupancy
(not a blind equal split). Then every view is defringed (white halo un-mixed and a 1 px rim
trimmed). Sheet views keep the sheet's pixel scale (one canvas, one camera); separately generated
views are rescaled to the front view's height and baseline, since each has its own framing.
views_qa.json records the mirror/scale checks of view_qa.py before and after normalising.
usage: split_sheet.py sheet.png outdir
       split_sheet.py --views outdir front.png left.png back.png right.png"""
import json, os, sys
import numpy as np
from PIL import Image
os.environ.setdefault("U2NET_HOME", "/mnt/ssd/ComfyUI/models/background_removal")
from rembg import new_session, remove
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_qa


def finish(views, out, size, rescale=False):
    """Defringe, record raw consistency, (rescale) normalise scale/baseline/centre, save, record final consistency."""
    os.makedirs(out, exist_ok=True)
    views = {v: view_qa.defringe(views[v]) for v in view_qa.VIEWS}
    raw = view_qa.consistency({v: view_qa.mask(views[v]) for v in view_qa.VIEWS})
    if rescale and raw.get("height_px"):
        views = view_qa.normalize(views, size)
    final = view_qa.consistency({v: view_qa.mask(views[v]) for v in view_qa.VIEWS})
    for name, image in views.items():
        image.save(f"{out}/{name}.png")
        coverage = final.get("coverage", {}).get(name, 0)
        print(f"  {name} alpha coverage {coverage:.4f}" +
              ("  WARNING: very low coverage - mesh will likely be broken" if coverage < view_qa.MIN_SIDE_COVERAGE else ""))
    if rescale and raw.get("height_spread", 0) > view_qa.MAX_HEIGHT_SPREAD:
        print(f"  rescaled views to the front's height (raw spread {raw['height_spread']:.1%})")
    with open(f"{out}/views_qa.json", "w") as stream:
        json.dump({"raw": raw, "normalized": final}, stream, indent=1)
    for issue in final["issues"]:
        print("  VIEW CHECK:", issue)
    print("  views consistent" if final["passed"] else "  views INCONSISTENT - regenerate or fix before meshing",
          "(views_qa.json)")
    return final


def separate_views(out, paths):
    session = new_session("isnet-general-use", providers=["CPUExecutionProvider"])
    views = {}
    for name, path in zip(view_qa.VIEWS, paths):
        image = Image.open(path)
        views[name] = image if view_qa.is_cut_out(image) else remove(image.convert("RGB"), session=session)
    size = max(max(i.size) for i in views.values())
    # every view onto the same square canvas first, so normalize() works in one pixel space
    for name, image in views.items():
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
        views[name] = canvas
    return finish(views, out, size, rescale=True)


if len(sys.argv) == 7 and sys.argv[1] == "--views":
    result = separate_views(sys.argv[2], sys.argv[3:7])
    sys.exit(0)
if len(sys.argv) != 3:
    sys.exit(__doc__)
sheet = Image.open(sys.argv[1]).convert("RGB"); out = sys.argv[2]
W, H = sheet.size
rgba = remove(sheet, session=new_session("isnet-general-use", providers=["CPUExecutionProvider"]))
a = np.asarray(rgba)[..., 3]
cols = (a > 32).sum(0) > 2
runs, x = [], 0
while x < W:
    if cols[x]:
        s = x
        while x < W and cols[x]: x += 1
        runs.append([s, x])
    x += 1
# merge runs separated by small gaps (thin parts can break a view into pieces)
merged = []
for r in runs:
    if merged and r[0] - merged[-1][1] < W // 80: merged[-1][1] = r[1]
    else: merged.append(r)
merged = [r for r in merged if r[1] - r[0] > 3]
print("panels found:", merged)

# Re-split oversized panels that likely contain two merged views
expected_w = W // 4
col_density = (a > 32).sum(0)  # per-column pixel count (for finding split points)
refined = []
for r in merged:
    pw = r[1] - r[0]
    if pw > expected_w * 1.4 and len(merged) < 4:
        # Find the lowest-density column in the central 60% of this panel
        margin = pw // 5
        search_start = r[0] + margin
        search_end = r[1] - margin
        if search_end > search_start + 10:
            window = 5  # smooth over a small window to avoid single-pixel noise
            best_col = search_start
            best_score = float('inf')
            for c in range(search_start, search_end):
                lo = max(0, c - window)
                hi = min(W, c + window + 1)
                score = col_density[lo:hi].sum()
                if score < best_score:
                    best_score = score
                    best_col = c
            print(f"  re-splitting oversized panel [{r[0]},{r[1]}] (width {pw}) at column {best_col}")
            refined.append([r[0], best_col])
            refined.append([best_col, r[1]])
            continue
    refined.append(r)
merged = refined

if len(merged) != 4:
    # Try splitting the widest panel iteratively
    attempts = 0
    while len(merged) < 4 and attempts < 3:
        attempts += 1
        widths = [r[1] - r[0] for r in merged]
        widest_idx = widths.index(max(widths))
        r = merged[widest_idx]
        pw = r[1] - r[0]
        margin = pw // 5
        search_start = r[0] + margin
        search_end = r[1] - margin
        if search_end <= search_start + 10:
            break
        window = 5
        best_col = (search_start + search_end) // 2
        best_score = float('inf')
        for c in range(search_start, search_end):
            lo = max(0, c - window)
            hi = min(W, c + window + 1)
            score = col_density[lo:hi].sum()
            if score < best_score:
                best_score = score
                best_col = c
        print(f"  fallback: splitting widest panel [{r[0]},{r[1]}] at column {best_col}")
        merged[widest_idx:widest_idx+1] = [[r[0], best_col], [best_col, r[1]]]
    if len(merged) != 4:
        print("expected 4 views; falling back to equal split")
        merged = [[i * W // 4, (i + 1) * W // 4] for i in range(4)]

print("final panels:", merged)
views = {}
for name, (x0, x1) in zip(view_qa.VIEWS, merged):
    crop = rgba.crop((max(x0 - 8, 0), 0, min(x1 + 8, W), H))
    canvas = Image.new("RGBA", (H, H), (0, 0, 0, 0))
    canvas.paste(crop, ((H - crop.width) // 2, 0))
    views[name] = canvas
    print(f"  {name} width {x1 - x0}")
finish(views, out, H)
