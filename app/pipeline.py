"""Bridge to the slicing/ pipeline. Invoked as a subprocess rather than
imported, deliberately - slicing/ is a sibling directory, not a package
under app/, and it already treats OrcaSlicer and mbotmake as subprocesses
itself (see slicing/README.md); calling slice.py the same way keeps that
boundary consistent instead of reaching across directories with sys.path
hacks. It also means slicing/ stays fully independent and runnable/
testable on its own, which it already was."""

import subprocess
import sys
import tempfile
from pathlib import Path

from supports import write_support_preview

SLICING_DIR = Path(__file__).resolve().parent.parent / "slicing"
SLICE_SCRIPT = SLICING_DIR / "slice.py"


def run_slice(
    stl_path: Path,
    output_makerbot_path: Path,
    enable_supports: bool = False,
    support_style: str | None = None,
    supports_json_path: Path | None = None,
) -> tuple[bool, str]:
    """Returns (success, detail) - detail is the slicer's own output either
    way, useful as slice_error on failure.

    If enable_supports and supports_json_path are both given, also asks
    slice.py to preserve the intermediate gcode (normally thrown away once
    it's converted to .makerbot) and extracts a simplified support-geometry
    preview from it - see supports.py for why that has to come from the
    gcode rather than the final print file.
    """
    with tempfile.TemporaryDirectory(prefix="queue3d-pipeline-") as tmp:
        gcode_path = Path(tmp) / "intermediate.gcode"
        cmd = [sys.executable, str(SLICE_SCRIPT), str(stl_path), str(output_makerbot_path)]
        if enable_supports:
            cmd.append("--enable-supports")
        if support_style:
            cmd += ["--support-style", support_style]
        if supports_json_path is not None:
            cmd += ["--gcode-out", str(gcode_path)]

        # stdin=DEVNULL - a real incident, not foresight: mbotmake (called
        # two subprocess layers down, see slicing/slice.py) has its own
        # internal `input()` call on certain internal errors (a bed-
        # centering sanity check failing for an unusually-shaped model -
        # see app/README.md's "Uploading .obj and .zip files" section for
        # the specific one that surfaced this). Without this, the whole
        # chain inherits whatever stdin the app's own process has - a real
        # terminal in normal dev/deployment use - so that `input()` blocks
        # forever waiting for a keystroke nobody will ever type, instead
        # of raising EOFError immediately the way it does when stdin is
        # already closed/empty. The 600s timeout below still eventually
        # fires either way, but only kills this direct child - the
        # blocked grandchild (mbotmake) would otherwise leak indefinitely
        # rather than exiting with it. Confirmed live: a real job got
        # stuck this way; killing the process tree by hand was the only
        # way to unwedge it before this fix existed.
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=600,
        )
        output = result.stdout or ""
        success = result.returncode == 0 and output_makerbot_path.exists()

        if success and supports_json_path is not None and gcode_path.exists():
            write_support_preview(gcode_path, supports_json_path)

    if success:
        return True, output
    return False, output or f"slice.py exited {result.returncode} with no output"
