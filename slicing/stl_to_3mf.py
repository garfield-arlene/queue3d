#!/usr/bin/env python3
"""Wrap an STL file + an OrcaSlicer project_settings.config into a minimal
.3mf project OrcaSlicer can slice directly.

Why this exists: OrcaSlicer's CLI --load-settings path (loading separate
printer/process/filament preset files against a raw STL) hits a
"process not compatible with printer" compatibility-gate failure in 2.4.2,
reproducible even with 100%% stock, unmodified bundled presets - looks like
a CLI-mode bug/limitation, not something wrong with our profile. Slicing a
self-contained .3mf project (geometry + settings bundled together, which is
how a human using the GUI would save/share this printer's profile) sidesteps
it entirely and is the actually-documented, working path.

A .3mf is just a zip (the OPC/OOXML package format) with:
  [Content_Types].xml         - generic, declares known file extensions
  _rels/.rels                 - points at the root 3D model part
  3D/3dmodel.model            - the mesh(es) + build item(s), core 3MF XML
  Metadata/project_settings.config - the merged printer+process+filament
                                      settings OrcaSlicer applies on load
"""

import argparse
import json
import struct
import sys
import zipfile

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

MODEL_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
  <object id="1" type="model">
   <mesh>
    <vertices>
{vertices}
    </vertices>
    <triangles>
{triangles}
    </triangles>
   </mesh>
  </object>
 </resources>
 <build>
  <item objectid="1" printable="1"/>
 </build>
</model>
"""


def parse_stl(path):
    """Return (vertices, triangles) - vertices deduplicated, triangles as
    index triples. Handles both ASCII and binary STL."""
    with open(path, "rb") as f:
        head = f.read(80)
        rest = f.read()

    is_ascii = head.lstrip()[:5].lower() == b"solid" and b"facet" in (head + rest[:200])
    if is_ascii:
        with open(path, "r", errors="replace") as f:
            text = f.read()
        return _parse_stl_ascii(text)
    return _parse_stl_binary(head, rest)


def _parse_stl_ascii(text):
    vertex_index = {}
    vertices = []
    triangles = []
    tri = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            xyz = tuple(float(p) for p in parts[1:4])
            idx = vertex_index.get(xyz)
            if idx is None:
                idx = len(vertices)
                vertex_index[xyz] = idx
                vertices.append(xyz)
            tri.append(idx)
            if len(tri) == 3:
                triangles.append(tuple(tri))
                tri = []
    if not triangles:
        raise ValueError("No triangles found - is this a valid ASCII STL?")
    return vertices, triangles


def _parse_stl_binary(head, rest):
    (count,) = struct.unpack("<I", rest[:4])
    body = rest[4:]
    vertex_index = {}
    vertices = []
    triangles = []
    record_size = 50  # 12 bytes normal + 3x12 bytes vertices + 2 bytes attr
    for i in range(count):
        record = body[i * record_size : (i + 1) * record_size]
        tri = []
        for v in range(3):
            offset = 12 + v * 12
            xyz = struct.unpack("<fff", record[offset : offset + 12])
            idx = vertex_index.get(xyz)
            if idx is None:
                idx = len(vertices)
                vertex_index[xyz] = idx
                vertices.append(xyz)
            tri.append(idx)
        triangles.append(tuple(tri))
    if not triangles:
        raise ValueError("No triangles found - is this a valid binary STL?")
    return vertices, triangles


def center_vertices(vertices):
    """Center X/Y on the bounding-box center and drop Z so the lowest point
    sits at 0 - matching app/static/preview.js's showModel() exactly.

    Why this has to happen here, not left to OrcaSlicer: confirmed (by
    slicing a real, off-center-authored model and comparing its sliced
    gcode's own bounding box to the raw STL's) that OrcaSlicer sometimes
    repositions an object during slicing and sometimes doesn't - a smaller
    synthetic test model was sliced at its native coordinates untouched,
    while a large real-world model with an off-center authored origin
    (Y-center ~9.8mm, not 0) came out of slicing centered near Y~0 instead.
    Meanwhile the client-side preview always centers the raw STL to its own
    bounding box for display, independent of whatever OrcaSlicer decides.
    Two independently-arrived-at placements agreeing by luck for a
    centered-ish model, and visibly disagreeing for an off-center one, is
    exactly the "supports render solid now but are floating disconnected
    from the model" bug reported after the tube-rendering fix. Centering
    here ourselves - the same way, in the same place, every time - removes
    the guesswork: OrcaSlicer slices already-centered geometry, so there's
    nothing left for it to reposition differently than what's displayed.
    """
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    z_min = min(zs)
    return [(x - cx, y - cy, z - z_min) for x, y, z in vertices]


def build_model_xml(vertices, triangles):
    vlines = "\n".join(
        f'     <vertex x="{x:.6g}" y="{y:.6g}" z="{z:.6g}"/>' for x, y, z in vertices
    )
    tlines = "\n".join(
        f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in triangles
    )
    return MODEL_TEMPLATE.format(vertices=vlines, triangles=tlines)


def build_3mf(stl_path, settings_path, output_path, overrides=None):
    """overrides: optional dict of settings keys to override in the loaded
    profile before embedding it - e.g. {"enable_support": "1"} to turn
    supports on for one job without needing a second profile file."""
    vertices, triangles = parse_stl(stl_path)
    vertices = center_vertices(vertices)
    model_xml = build_model_xml(vertices, triangles)

    with open(settings_path, "r") as f:
        settings = json.load(f)
    if overrides:
        settings.update(overrides)
    settings_text = json.dumps(settings, indent=4)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("3D/3dmodel.model", model_xml)
        z.writestr("Metadata/project_settings.config", settings_text)

    return len(vertices), len(triangles)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stl", help="Input STL file (ASCII or binary)")
    parser.add_argument("settings", help="project_settings.config (merged OrcaSlicer profile)")
    parser.add_argument("output", help="Output .3mf path")
    args = parser.parse_args()

    n_verts, n_tris = build_3mf(args.stl, args.settings, args.output)
    print(f"Wrote {args.output} ({n_verts} vertices, {n_tris} triangles)")


if __name__ == "__main__":
    sys.exit(main())
