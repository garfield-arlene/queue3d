"""OBJ/3MF -> STL conversion, used only at upload time (see
routers/user.py's upload()) - not a slicing concern, so it lives here in
app/ rather than in slicing/ (which stays independent/subprocess-only,
see pipeline.py's docstring). Converting immediately on upload, rather
than teaching the rest of the pipeline to understand a second input
format, keeps every downstream piece (storage.py's job-id-based .stl
paths, slicing/stl_to_3mf.py's parser, the client-side STL preview)
working completely unchanged - there is exactly one on-disk model format
past this point, same as there always has been. `Job.original_filename`
still records the true "vase.obj"/"hinge.3mf" for display; what's
actually stored and sliced is a losslessly-converted "5.stl" holding the
same geometry (already merged into one piece for a multi-object .3mf -
see parse_3mf's own docstring for why that's the right behavior, not a
limitation).

No third-party mesh library, matching slicing/stl_to_3mf.py's own
from-scratch STL parser - both formats here are small and well enough
specified to parse directly (3MF's own XML via the standard library's
`xml.etree.ElementTree`, its container via `zipfile` - both already
stdlib, nothing new to vendor or stage for offline install), not worth a
new pip dependency for parsing itself.

One dependency *is* worth it, though: the actual XML parse call
(`_3mfPackage.objects_in` below) runs on a user-uploaded, untrusted
`.3mf`'s own XML content, and plain `xml.etree.ElementTree.fromstring`
is documented as vulnerable to entity-expansion ("billion laughs")
denial of service regardless - a crafted part could exhaust memory on
upload (classic external-entity file disclosure doesn't apply here;
stock ElementTree never fetches external entities to begin with, but
expat still expands entities defined *within* the same document by
default, which is the actual residual risk). `defusedxml.ElementTree.
fromstring` is the same expat-backed parser, just with exactly that
expansion disabled - fetched the same offline-installable way as every
other pip dependency here (see deploy/fetch_bundle_assets.sh/
remote_install.sh's pre-staged-wheels flow), not a runtime network
fetch, so it costs nothing deployment-constraint-wise despite being new.
"""

import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException


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


# 3MF's own declared units, converted to millimeters - every coordinate
# and every transform's translation in the file is consistently in
# whatever unit the <model> root declares (default "millimeter" if
# unspecified, per the core spec), so the conversion factor is applied
# once, uniformly, to the final merged output rather than needing to be
# threaded through every intermediate transform.
_3MF_UNIT_TO_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}

# 3MF objects can carry a "support"/"solidsupport" type alongside real
# printable parts (a downloaded file that's actually a pre-sliced
# export, rather than a raw model, might include its own generated
# supports as separate objects) - skipped here so they can never get
# merged into the geometry this app slices, which would double up
# against the supports this app generates itself. Real models default
# to type "model" when unspecified, and "surface"/"other" are also
# real, printable geometry - only these two are ever excluded.
_3MF_NON_PRINTABLE_TYPES = {"support", "solidsupport"}


def _3mf_local_tag(elem: ET.Element) -> str:
    """Strips the namespace prefix ElementTree keeps on every tag
    ("{http://schemas.../2015/02}mesh" -> "mesh") - 3MF's XML always
    carries a namespace, and the exact URI has varied slightly across
    spec revisions and producers, but the element names this actually
    needs (model/resources/object/mesh/vertices/vertex/triangles/
    triangle/components/component/build/item) are stable across all of
    them."""
    return elem.tag.rsplit("}", 1)[-1]


def _3mf_path_attr(elem: ET.Element) -> str | None:
    """The Production Extension's "path" attribute (real key:
    "{http://schemas.../production/2015/06}path", namespace-stripped the
    same way _3mf_local_tag strips tag namespaces) on a <component> or
    <build><item> - present when that reference points at an object
    defined in a *different* part of the package, not the one currently
    being read. None (use the current part) when absent, which is the
    common case for a file with everything in one part."""
    for key, value in elem.attrib.items():
        if key.rsplit("}", 1)[-1] == "path":
            return value
    return None


def _3mf_parse_transform(raw: str | None) -> tuple[float, ...]:
    """The 3MF "transform" attribute on a <build><item> or <component> -
    12 space-separated numbers: a 4x3 affine matrix in row-major order
    (three basis-vector rows, then a translation row) - see the 3MF core
    spec. Identity (no change at all) when the attribute is absent
    entirely, since neither element is required to carry one."""
    if not raw:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    values = tuple(float(x) for x in raw.split())
    if len(values) != 12:
        raise ValueError(f"malformed transform (expected 12 numbers, got {len(values)})")
    return values


def _3mf_apply_transform(v: tuple[float, float, float], m: tuple[float, ...]) -> tuple[float, float, float]:
    x, y, z = v
    return (
        x * m[0] + y * m[3] + z * m[6] + m[9],
        x * m[1] + y * m[4] + z * m[7] + m[10],
        x * m[2] + y * m[5] + z * m[8] + m[11],
    )


def _3mf_compose_transform(outer: tuple[float, ...], inner: tuple[float, ...]) -> tuple[float, ...]:
    """The single transform equivalent to applying `inner` first, then
    `outer` - needed for nested <components> (an object placed on the
    plate by a <build> item's own transform, built out of other objects
    each positioned by a further transform relative to that parent).
    Composed as real 4x4 affine matrices (row-vector convention: a point
    transforms as v' = v . M), even though only the 4x3 part is ever
    non-trivial here - simplest way to get the composition right without
    hand-deriving the 4x3-only shortcut."""
    def to_4x4(m):
        return (
            (m[0], m[1], m[2], 0.0),
            (m[3], m[4], m[5], 0.0),
            (m[6], m[7], m[8], 0.0),
            (m[9], m[10], m[11], 1.0),
        )
    a, b = to_4x4(inner), to_4x4(outer)
    r = [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
    return (
        r[0][0], r[0][1], r[0][2],
        r[1][0], r[1][1], r[1][2],
        r[2][0], r[2][1], r[2][2],
        r[3][0], r[3][1], r[3][2],
    )


def parse_3mf(path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Returns (vertices, triangles) - same shape as parse_obj() above -
    for the *combined*, already-placed geometry of every object actually
    referenced from the file's own <build> section, not just the first
    object found.

    A single straightforward part exports as one object and this is a
    plain format conversion, nothing more. A multi-part assembly -
    hinges, gears, anything meant to be printed "in place" as one
    interlocking piece - exports as several objects, each positioned by
    its own <build> item's transform. Unlike a zip's several independent
    files (see storage.extract_model_files, and app/README.md's
    "Uploading .obj and .zip files" section for why *those* become
    separate jobs), these pieces are meant to be printed *together*, at
    the exact relative positions the file itself already specifies -
    splitting them into separate jobs the way a zip does would silently
    break the one thing an interlocking multi-part model actually needs.
    Flattening every referenced object into one merged mesh here, at
    upload time, means the rest of this app's pipeline (exactly one
    object per job - see slicing/stl_to_3mf.py) never has to know the
    source was ever an assembly at all: it's just a single STL from this
    point on, like any other upload.

    Nested <components> (an object built entirely out of other objects,
    each with its own transform relative to the parent) are resolved
    recursively, composing transforms down to world space - real for
    grouped/instanced parts some CAD tools export this way, not just a
    theoretical case a flat reading of the spec suggests.

    A real, common case, not just Production-Extension trivia: a
    slicer-authored project .3mf (Bambu Studio/OrcaSlicer/Creality Print
    all do this) routinely splits each real object out into its *own*
    `.model` part under `3D/Objects/`, with the root `3D/3dmodel.model`
    holding no mesh data of its own at all - just thin wrapper objects
    whose <component>/<item> reference the real geometry in another part
    via the Production Extension's `p:path` attribute (a real object id
    is only unique *within* the part that defines it, never globally
    across parts, since two different parts can each reuse the same
    small integer ids independently). A first version of this only ever
    read the root part, so an object reference elsewhere in the same
    file resolved to nothing at all: no crash, just an empty merged mesh
    and "no mesh geometry found" - wrong, and confusing, for a file that
    plainly has real geometry in it. Every part this actually needs is
    now loaded lazily, on first reference, and cached (`_3mfPackage`
    below) rather than assumed to all live in the one root file."""

    class _3mfPackage:
        """Lazily loads and caches each part (`.model` file) this .3mf
        package actually references, keyed by its own path inside the
        zip - a part is only ever read and parsed once, however many
        times it's referenced (a real assembly can reuse the same part
        for several placed instances)."""

        def __init__(self, zf: zipfile.ZipFile):
            self.zf = zf
            self.names = set(zf.namelist())
            self._roots: dict[str, ET.Element] = {}
            self._objects: dict[str, dict[str, ET.Element]] = {}

        def objects_in(self, part_path: str) -> dict[str, ET.Element]:
            normalized = part_path.lstrip("/")
            if normalized not in self._objects:
                if normalized not in self.names:
                    raise ValueError(f"referenced part {part_path!r} not found in this .3mf file")
                try:
                    # defusedxml, not plain ET.fromstring - see this
                    # module's own docstring for why: this is parsing a
                    # user-uploaded file's own XML content, not
                    # something this app wrote itself.
                    root = DET.fromstring(self.zf.read(normalized))
                except (ET.ParseError, DefusedXmlException) as e:
                    raise ValueError(f"couldn't parse {part_path!r} ({e})")
                self._roots[normalized] = root
                objs: dict[str, ET.Element] = {}
                for section in root:
                    if _3mf_local_tag(section) != "resources":
                        continue
                    for obj in section:
                        if _3mf_local_tag(obj) == "object" and obj.get("id") is not None:
                            objs[obj.get("id")] = obj
                self._objects[normalized] = objs
            return self._objects[normalized]

        def root_of(self, part_path: str) -> ET.Element:
            self.objects_in(part_path)  # ensures it's loaded
            return self._roots[part_path.lstrip("/")]

    # A .3mf is a zip container (the OPC package format) - a file that
    # isn't one at all (or claims the extension but is actually
    # something else entirely) raises zipfile.BadZipFile, not a
    # ValueError, on its own - normalized here so every failure this
    # function can raise is consistently a ValueError, same contract
    # parse_obj() above already has.
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError("not a valid .3mf file (not a zip container)")
    with zf:
        pkg = _3mfPackage(zf)
        root_model_name = "3D/3dmodel.model" if "3D/3dmodel.model" in pkg.names else next(
            (n for n in pkg.names if n.lower().endswith(".model")), None
        )
        if root_model_name is None:
            raise ValueError("no 3D model found inside this .3mf file")

        pkg.objects_in(root_model_name)  # loads and validates the root part
        root = pkg.root_of(root_model_name)
        scale = _3MF_UNIT_TO_MM.get(root.get("unit", "millimeter"), 1.0)

        build = next((c for c in root if _3mf_local_tag(c) == "build"), None)
        if build is None:
            raise ValueError("no <build> section found in this .3mf file")

        merged_vertices: list[tuple[float, float, float]] = []
        merged_triangles: list[tuple[int, int, int]] = []

        def flatten_mesh(mesh_elem: ET.Element, transform: tuple[float, ...]) -> None:
            base = len(merged_vertices)
            added_vertices: list[tuple[float, float, float]] = []
            for part in mesh_elem:
                part_tag = _3mf_local_tag(part)
                if part_tag == "vertices":
                    for v in part:
                        if _3mf_local_tag(v) != "vertex":
                            continue
                        raw = (float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))
                        added_vertices.append(_3mf_apply_transform(raw, transform))
                elif part_tag == "triangles":
                    for t in part:
                        if _3mf_local_tag(t) != "triangle":
                            continue
                        merged_triangles.append(
                            (base + int(t.get("v1")), base + int(t.get("v2")), base + int(t.get("v3")))
                        )
            merged_vertices.extend(added_vertices)

        def resolve_object(part_path: str, obj: ET.Element, transform: tuple[float, ...], depth: int = 0) -> None:
            if depth > 8:
                raise ValueError("object references nested too deeply (a reference cycle?)")
            if obj.get("type", "model") in _3MF_NON_PRINTABLE_TYPES:
                return
            for child in obj:
                tag = _3mf_local_tag(child)
                if tag == "mesh":
                    flatten_mesh(child, transform)
                elif tag == "components":
                    for comp in child:
                        if _3mf_local_tag(comp) != "component":
                            continue
                        target_part = _3mf_path_attr(comp) or part_path
                        ref = pkg.objects_in(target_part).get(comp.get("objectid"))
                        if ref is None:
                            continue
                        combined = _3mf_compose_transform(transform, _3mf_parse_transform(comp.get("transform")))
                        resolve_object(target_part, ref, combined, depth + 1)

        items_resolved = 0
        for item in build:
            if _3mf_local_tag(item) != "item":
                continue
            target_part = _3mf_path_attr(item) or root_model_name
            obj = pkg.objects_in(target_part).get(item.get("objectid"))
            if obj is None:
                continue
            resolve_object(target_part, obj, _3mf_parse_transform(item.get("transform")))
            items_resolved += 1

    if items_resolved == 0:
        raise ValueError("no printable objects found in this .3mf file's <build> section")
    if not merged_vertices or not merged_triangles:
        raise ValueError("no mesh geometry found in this .3mf file")

    if scale != 1.0:
        merged_vertices = [(x * scale, y * scale, z * scale) for x, y, z in merged_vertices]
    return merged_vertices, merged_triangles


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


def convert_3mf_to_stl(threemf_path: Path, stl_path: Path) -> None:
    vertices, triangles = parse_3mf(threemf_path)
    write_stl_binary(vertices, triangles, stl_path)
