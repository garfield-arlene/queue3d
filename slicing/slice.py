#!/usr/bin/env python3
"""Slice an STL into a .makerbot file ready for the Replicator+, end to end.

STL -> (wrapped in a .3mf project with our printer profile) -> OrcaSlicer
--slice -> gcode -> mbotmake -> .makerbot

Usage:
    python3 slice.py models/testcube.stl out/testcube.makerbot
    python3 slice.py models/testcube.stl out/testcube.makerbot --enable-supports --gcode-out out/testcube.gcode
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORCASLICER = HERE / "tools/squashfs-root/AppRun"
MBOTMAKE = HERE / "mbotmake/mbotmake"
PROFILE = HERE / "profiles/makerbot-plus-tough-extruder.json"

# support_style -> the support_type it needs to actually take effect.
# OrcaSlicer silently ignores a support_style that isn't compatible with
# the active support_type (no error - ours defaults to "tree(auto)"), so
# picking a style has to co-set the type. Confirmed by direct testing
# against models/overhang_test.stl: each pairing here produces genuinely
# different gcode from the others. "organic" is PrusaSlicer's name for the
# *plain* tree-support algorithm itself (not a further variant alongside
# hybrid/slim) - it correctly matched "tree(auto)" with no style override
# in testing, which is the right answer, not a bug: there's nothing further
# for "organic" to diverge from under that family.
SUPPORT_STYLE_TYPE = {
    "grid": "normal(auto)",
    "snug": "normal(auto)",
    "organic": "tree(auto)",
    "tree_hybrid": "tree(auto)",
    "tree_slim": "tree(auto)",
}

sys.path.insert(0, str(HERE))
from stl_to_3mf import build_3mf  # noqa: E402


def slice_stl(stl_path, output_makerbot_path, enable_supports=False, support_style=None, gcode_out_path=None):
    """gcode_out_path: if given, the intermediate gcode (before mbotmake
    conversion) is copied there - it's otherwise thrown away with the temp
    dir. Needed by callers that want to derive anything from it themselves
    (e.g. app/supports.py extracting support geometry for the 3D preview) -
    mbotmake's own conversion discards feature-type info like "this move is
    a support" entirely, so that has to come from the gcode, not the
    .makerbot.

    support_style: OrcaSlicer's own setting - "default", "grid", "snug",
    "organic", "tree_hybrid", or "tree_slim" (confirmed valid values, see
    app/routers/user.py's SUPPORT_STYLES for the full list with labels).
    Only meaningful when enable_supports is true."""
    stl_path = Path(stl_path)
    output_makerbot_path = Path(output_makerbot_path)
    overrides = {"enable_support": "1" if enable_supports else "0"}
    if support_style:
        overrides["support_style"] = support_style
        if support_style in SUPPORT_STYLE_TYPE:
            overrides["support_type"] = SUPPORT_STYLE_TYPE[support_style]

    with tempfile.TemporaryDirectory(prefix="queue3d-slice-") as tmp:
        tmp = Path(tmp)
        project_3mf = tmp / "project.3mf"
        n_verts, n_tris = build_3mf(str(stl_path), str(PROFILE), str(project_3mf), overrides=overrides)
        print(f"Wrapped {stl_path.name} into project.3mf ({n_verts} vertices, {n_tris} triangles)")

        print("Slicing with OrcaSlicer...")
        result = subprocess.run(
            # --arrange 0 / --orient 0: without these, OrcaSlicer's CLI
            # defaults to "auto" for both and will silently re-place and
            # rotate a single object during --slice (confirmed: a real
            # model's sliced gcode came out rotated ~45 degrees about Z from
            # its raw STL - X/Y extents that were 198mm/37mm in the STL
            # became ~147mm/~147mm, nearly square, the signature of an
            # in-plane rotation - while its Z extent was untouched). The
            # model and its generated supports stay internally consistent
            # with each other either way (both computed in whatever frame
            # OrcaSlicer picks), which is exactly what made this hard to
            # catch: comparing support segments against model geometry
            # pulled from the *same* gcode file always agreed. The mismatch
            # only shows up against app/static/preview.js's raw-STL render,
            # which applies no rotation at all - so the model and its own
            # supports appeared to be in "wildly different orientations" in
            # the browser even though slicing them was internally correct.
            # We already center the model onto the bed origin ourselves (see
            # stl_to_3mf.center_vertices) and every profile'd printer here
            # has one build plate and one object, so there's nothing for
            # auto-arrange/orient to usefully do - forcing both off makes
            # OrcaSlicer slice the exact placement we handed it, matching
            # what the browser preview shows.
            [str(ORCASLICER), "--outputdir", str(tmp), "--arrange", "0", "--orient", "0", "--slice", "0", str(project_3mf)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # OrcaSlicer's AppImage wrapper emits a harmless libexpat version
        # warning on every invocation - filter it out so real errors are
        # visible, but keep everything else.
        noise = "no version information available"
        output = "\n".join(line for line in result.stdout.splitlines() if noise not in line)
        if output.strip():
            print(output)

        gcode_path = tmp / "plate_1.gcode"
        if not gcode_path.exists():
            raise RuntimeError(f"OrcaSlicer didn't produce gcode (exit {result.returncode}). See output above.")

        if gcode_out_path:
            gcode_out_path = Path(gcode_out_path)
            gcode_out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(gcode_path, gcode_out_path)

        print("Converting to .makerbot (mbotmake, Replicator+ / Tough Smart Extruder+)...")
        result = subprocess.run(
            [sys.executable, str(MBOTMAKE), "-RepPlus", "-ToughExt", str(gcode_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        print(result.stdout)

        produced = gcode_path.with_suffix(".makerbot")
        if not produced.exists():
            raise RuntimeError("mbotmake didn't produce a .makerbot file. See output above.")

        output_makerbot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(produced, output_makerbot_path)
        print(f"Done: {output_makerbot_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stl", help="Input STL file")
    parser.add_argument("output", help="Output .makerbot path")
    parser.add_argument("--enable-supports", action="store_true", help="Turn on auto-generated supports for this slice")
    parser.add_argument("--support-style", help="OrcaSlicer support_style override, e.g. grid/snug/organic/tree_hybrid/tree_slim")
    parser.add_argument("--gcode-out", help="Also save the intermediate gcode here")
    args = parser.parse_args()
    slice_stl(
        args.stl,
        args.output,
        enable_supports=args.enable_supports,
        support_style=args.support_style,
        gcode_out_path=args.gcode_out,
    )


if __name__ == "__main__":
    main()
