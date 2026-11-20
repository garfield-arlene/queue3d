"""Bridge to the slicing/ pipeline. Invoked as a subprocess rather than
imported, deliberately - slicing/ is a sibling directory, not a package
under app/, and it already treats OrcaSlicer and mbotmake as subprocesses
itself (see slicing/README.md); calling slice.py the same way keeps that
boundary consistent instead of reaching across directories with sys.path
hacks. It also means slicing/ stays fully independent and runnable/
testable on its own, which it already was."""

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from supports import write_support_preview

SLICING_DIR = Path(__file__).resolve().parent.parent / "slicing"
SLICE_SCRIPT = SLICING_DIR / "slice.py"

# The longest one slice may run before it's stopped. A model's first
# slice gets all of it; the automatic rotation retries in jobs.py pass
# less (see AUTO_ROTATE_BUDGET_S there).
SLICE_TIMEOUT_S = 600
# Starts the detail returned for a slice that ran out of time, so callers
# can tell a timeout from a slicer failure.
TIMEOUT_MESSAGE = "Slicing timed out"


def run_slice(
    stl_path: Path,
    output_makerbot_path: Path,
    enable_supports: bool = False,
    support_style: str | None = None,
    supports_json_path: Path | None = None,
    scale_factor: float = 1.0,
    rotate_x: float = 0.0,
    rotate_y: float = 0.0,
    rotate_z: float = 0.0,
    timeout_s: float = SLICE_TIMEOUT_S,
) -> tuple[bool, str]:
    """Returns (success, detail) - detail is the slicer's own output either
    way, useful as slice_error on failure.

    If enable_supports and supports_json_path are both given, also asks
    slice.py to preserve the intermediate gcode (normally thrown away once
    it's converted to .makerbot) and extracts a simplified support-geometry
    preview from it - see supports.py for why that has to come from the
    gcode rather than the final print file.

    scale_factor: uniform scale (1.0 = original size). rotate_x/y/z:
    degrees, applied in that order about the fixed world axes - see
    models.Job.scale_factor/rotate_x and stl_to_3mf.build_3mf's own
    docstring for the full ordering and why it has to match the client
    preview exactly.

    timeout_s: how long this slice may run before it's stopped. On a
    timeout, detail starts with TIMEOUT_MESSAGE.
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
        if scale_factor != 1.0:
            cmd += ["--scale", str(scale_factor)]
        if rotate_x != 0.0:
            cmd += ["--rotate-x", str(rotate_x)]
        if rotate_y != 0.0:
            cmd += ["--rotate-y", str(rotate_y)]
        if rotate_z != 0.0:
            cmd += ["--rotate-z", str(rotate_z)]

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
        # already closed/empty.
        #
        # start_new_session puts slice.py, OrcaSlicer and mbotmake in one
        # process group of their own, so a timeout kills all three.
        # subprocess.run's own timeout kills only slice.py, then waits
        # for its output pipe to close - which a still-running OrcaSlicer
        # holds open, so the wait never ends and the job stays
        # 'submitted' for good.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            output, _ = proc.communicate()
            return False, f"{TIMEOUT_MESSAGE} after {timeout_s:.0f} seconds and was stopped.\n\n{output or ''}".rstrip()
        output = output or ""
        success = proc.returncode == 0 and output_makerbot_path.exists()

        if success and supports_json_path is not None and gcode_path.exists():
            write_support_preview(gcode_path, supports_json_path)

    if success:
        return True, output
    return False, output or f"slice.py exited {proc.returncode} with no output"
