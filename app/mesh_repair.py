"""Best-effort mesh repair for uploaded models - run once, centrally, in
routers.user._stl_bytes_from_upload, before a model ever reaches the
slicer. OrcaSlicer's CLI has no repair flag of its own (unlike its GUI's
"Fix through Netfabb" menu item, which this closes the gap for) - a
broken mesh otherwise only gets caught as a downstream slice_failed job,
with the user given no real path forward beyond re-exporting elsewhere.

Two tiers, cheapest first:
1. trimesh's own normal/winding fix + small-hole fill - pure numpy/
   scipy/networkx, no compiled mesh library beyond that. Handles the
   common case: a few flipped faces, one or two small gaps.
2. pymeshfix (a dedicated, compiled hole-filling/watertight algorithm)
   for anything trimesh's own lighter repair couldn't close - larger or
   messier holes, the kind a real bad scan or export actually produces.
   Verified directly against a deliberately broken mesh before this
   shipped: pymeshfix recovered the exact original volume of a sphere
   with a multi-triangle hole cut out and some faces' winding flipped,
   a case trimesh's own fill_holes left untouched.

Never raises, and never returns something worse than what came in: a
mesh that's already watertight AND correctly oriented is returned
completely untouched (nothing to fix, no risk of altering valid
geometry just by loading it through a different library); one that's
watertight but inverted (every normal pointing inward - a real,
not hypothetical, case some CAD/export tools produce) is just flipped
back, skipping both repair tiers entirely; and if both tiers fail to
produce a valid, watertight result, the ORIGINAL bytes are returned
unchanged rather than whatever broken intermediate state the attempt
left behind - a model that was already going to fail to slice is no
worse off for having been tried.
"""

import io

import trimesh


def repair_stl_bytes(data: bytes) -> bytes:
    try:
        # process=True (the default) is required, not optional - STL never
        # shares vertex indices between triangles (every triangle stores
        # its own independent copy of each vertex, even along a shared
        # edge), so without trimesh's own merge-duplicate-vertices step
        # here, is_watertight below would read as False for essentially
        # every STL ever loaded this way, regardless of whether the
        # geometry is actually fine - confirmed directly: an already-
        # watertight test cube read back as non-watertight, volume 0,
        # with process=False.
        mesh = trimesh.load(io.BytesIO(data), file_type="stl", process=True)
    except Exception:
        return data  # not even parseable as a mesh - let the slicer report its own error on the original

    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        return data  # empty/degenerate - nothing here to repair

    if mesh.is_watertight:
        try:
            # Watertight alone doesn't mean correctly oriented - a mesh
            # can be a perfectly valid closed surface with every normal
            # pointing inward instead of out (some CAD/export tools
            # produce exactly this). Same real risk as the inverted-
            # volume case below, just reached without needing either
            # repair tier - cheap enough to always check.
            if mesh.volume < 0:
                mesh.invert()
                return trimesh.exchange.stl.export_stl(mesh)
        except Exception:
            return data  # couldn't confirm orientation - ship the original rather than guess
        return data  # genuinely fine - don't touch it

    try:
        mesh.fix_normals()
        trimesh.repair.fill_holes(mesh)
    except Exception:
        pass  # best-effort; fall through to pymeshfix either way

    if not mesh.is_watertight:
        try:
            import pymeshfix

            fixer = pymeshfix.MeshFix(mesh.vertices, mesh.faces)
            fixer.repair()
            mesh = trimesh.Trimesh(vertices=fixer.points, faces=fixer.faces, process=False)
        except Exception:
            return data  # pymeshfix couldn't do it either - ship the original, unrepaired

    if not mesh.is_watertight or len(mesh.faces) == 0:
        return data  # still broken after both tiers - the original is no worse

    # A real bug caught in testing, not a hypothetical: fix_normals() on a
    # still-broken (not yet watertight) mesh doesn't reliably land on a
    # globally outward-facing result - confirmed directly on a
    # deliberately broken sphere, where the final watertight, correctly-
    # shaped mesh still came back with every normal pointing inward
    # (volume exactly negative, same magnitude as the real original).
    # Fatal for 3D printing if shipped: a slicer determines inside vs.
    # outside from this same sign, so an inverted mesh can slice as a
    # shell with material on the wrong side rather than failing loudly.
    # trimesh.volume is signed by convention (negative = inward-facing);
    # mesh.invert() flips every face's winding to correct it.
    try:
        if mesh.volume < 0:
            mesh.invert()
    except Exception:
        return data  # couldn't even determine orientation - don't risk shipping it unchecked

    try:
        return trimesh.exchange.stl.export_stl(mesh)
    except Exception:
        return data
