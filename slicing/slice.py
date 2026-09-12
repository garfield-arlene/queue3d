#!/usr/bin/env python3
"""Slice an STL into a .makerbot file ready for the Replicator+, end to end.

STL -> (wrapped in a .3mf project with our printer profile) -> OrcaSlicer
--slice -> gcode -> mbotmake -> .makerbot

Usage:
    python3 slice.py models/testcube.stl out/testcube.makerbot
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

sys.path.insert(0, str(HERE))
from stl_to_3mf import build_3mf  # noqa: E402


def slice_stl(stl_path, output_makerbot_path):
    stl_path = Path(stl_path)
    output_makerbot_path = Path(output_makerbot_path)

    with tempfile.TemporaryDirectory(prefix="queue3d-slice-") as tmp:
        tmp = Path(tmp)
        project_3mf = tmp / "project.3mf"
        n_verts, n_tris = build_3mf(str(stl_path), str(PROFILE), str(project_3mf))
        print(f"Wrapped {stl_path.name} into project.3mf ({n_verts} vertices, {n_tris} triangles)")

        print("Slicing with OrcaSlicer...")
        result = subprocess.run(
            [str(ORCASLICER), "--outputdir", str(tmp), "--slice", "0", str(project_3mf)],
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
    args = parser.parse_args()
    slice_stl(args.stl, args.output)


if __name__ == "__main__":
    main()
