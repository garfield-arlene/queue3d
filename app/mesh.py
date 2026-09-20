"""OBJ -> STL conversion, used only at upload time (see routers/user.py's
upload()) - not a slicing concern, so it lives here in app/ rather than in
slicing/ (which stays independent/subprocess-only, see pipeline.py's
docstring). Converting immediately on upload, rather than teaching the
rest of the pipeline to understand a second input format, keeps every
downstream piece (storage.py's job-id-based .stl paths, slicing/
stl_to_3mf.py's parser, the client-side STL preview) working completely
unchanged - there is exactly one on-disk model format past this point,
same as there always has been. `Job.original_filename` still records the
true "vase.obj" for display; what's actually stored and sliced is a
losslessly-converted "5.stl" holding the identical geometry.

No third-party mesh library, matching slicing/stl_to_3mf.py's own
from-scratch STL parser - this is a comparably small, dependency-free
format, not worth a new pip dependency for.
"""

import struct
from pathlib import Path


def parse_obj(path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Returns (vertices, triangles) in the same shape
    slicing/stl_to_3mf.py's parse_stl() does. Handles the handful of OBJ
    face-line shapes actually seen in the wild: bare vertex indices
    ("f 1 2 3"), vertex/texture ("f 1/1 2/2 3/3"), vertex/texture/normal
    ("f 1/1/1 2/2/2 3/3/3"), and vertex//normal ("f 1//1 2//2 3//3") -
    only the first (vertex) index in each group matters here, since we
    only need geometry, not UVs/normals. Negative indices (relative to
    the current vertex count, per the OBJ spec) are resolved the same
    way real slicers do. A face with more than 3 vertices (a quad or
    n-gon) is fan-triangulated around its first vertex - correct for the
    convex polygons any real-world exported model actually uses; OBJ
    exporters that emit genuinely non-convex n-gons are rare enough not
    to special-case here."""
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []

    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("v ") or line.startswith("v\t"):
                parts = line.split()
                vertices.append(tuple(float(p) for p in parts[1:4]))
            elif line.startswith("f ") or line.startswith("f\t"):
                parts = line.split()[1:]
                idxs = []
                for p in parts:
                    v_str = p.split("/")[0]
                    v_idx = int(v_str)
                    # OBJ indices are 1-based; negative means "relative to
                    # the current end of the vertex list so far".
                    idxs.append(v_idx - 1 if v_idx > 0 else len(vertices) + v_idx)
                for i in range(1, len(idxs) - 1):
                    triangles.append((idxs[0], idxs[i], idxs[i + 1]))

    if not vertices:
        raise ValueError("No vertices found - is this a valid OBJ file?")
    if not triangles:
        raise ValueError("No faces found - is this a valid OBJ file?")
    return vertices, triangles


def write_stl_binary(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int]],
    path: Path,
) -> None:
    """Writes a standard binary STL from (vertices, triangles) - the exact
    shape slicing/stl_to_3mf.py's parse_stl() reads back out, so a
    round-trip through this is transparent to everything downstream.
    Per-triangle normals are written as zero vectors rather than computed
    - every actual consumer here (parse_stl, OrcaSlicer itself) derives
    its own normals from vertex winding order and ignores the stored
    ones, so computing real ones would be extra work with no effect on
    the result."""
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)  # header - arbitrary, unused by any reader here
        f.write(struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            f.write(struct.pack("<3f", 0.0, 0.0, 0.0))  # normal (unused, see above)
            for idx in (a, b, c):
                f.write(struct.pack("<3f", *vertices[idx]))
            f.write(struct.pack("<H", 0))  # attribute byte count


def convert_obj_to_stl(obj_path: Path, stl_path: Path) -> None:
    vertices, triangles = parse_obj(obj_path)
    write_stl_binary(vertices, triangles, stl_path)
