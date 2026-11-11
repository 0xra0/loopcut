"""Small, dependency-free UV triangle overlap check (touching edges are allowed)."""
import math


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def area(points):
    # Work relative to the first corner in Python double precision. Absolute
    # float32 shoelace products can cancel valid, small UV triangles to zero.
    if len(points) < 3:
        return 0.0
    return abs(sum(cross(points[0], points[i], points[i + 1]) for i in range(1, len(points) - 1))) / 2


def intersection_area(subject, clip):
    polygon = list(subject)
    sign = 1 if cross(*clip) >= 0 else -1
    for i, a in enumerate(clip):
        b = clip[(i + 1) % 3]
        result = []
        for j, end in enumerate(polygon):
            start = polygon[j - 1]
            ds, de = sign * cross(a, b, start), sign * cross(a, b, end)
            if (ds >= 0) != (de >= 0):
                t = ds / (ds - de)
                result.append((start[0] + t * (end[0] - start[0]), start[1] + t * (end[1] - start[1])))
            if de >= 0:
                result.append(end)
        polygon = result
        if len(polygon) < 3:
            return 0.0
    return area(polygon)


def overlaps(triangles, max_tests=2000000):
    """Spatial grid broad phase + convex polygon clipping; bounded for pathological UVs."""
    size = min(256, max(8, int(math.sqrt(len(triangles) or 1))))
    grid, boxes, samples = {}, [], []
    tests, count, affected = 0, 0, set()
    for i, tri in enumerate(triangles):
        box = (min(p[0] for p in tri), min(p[1] for p in tri), max(p[0] for p in tri), max(p[1] for p in tri))
        boxes.append(box)
        cells = [(x, y) for x in range(max(0, int(box[0] * size)), min(size - 1, int(box[2] * size)) + 1)
                 for y in range(max(0, int(box[1] * size)), min(size - 1, int(box[3] * size)) + 1)]
        candidates = set()
        for cell in cells:
            candidates.update(grid.get(cell, ()))
        for j in candidates:
            other = boxes[j]
            if min(box[2], other[2]) <= max(box[0], other[0]) or min(box[3], other[3]) <= max(box[1], other[1]):
                continue
            tests += 1
            if tests > max_tests:
                return {"uv_overlap_check": "budget_exceeded", "uv_overlap_pairs": count,
                        "uv_overlap_samples": samples, "uv_overlap_faces": sorted(affected)}
            if intersection_area(tri, triangles[j]) > 1e-12:
                count += 1
                affected.update((i, j))
                if len(samples) < 20:
                    samples.append([j, i])
        for cell in cells:
            grid.setdefault(cell, []).append(i)
    return {"uv_overlap_check": "complete", "uv_overlap_pairs": count,
            "uv_overlap_samples": samples, "uv_overlap_faces": sorted(affected)}
