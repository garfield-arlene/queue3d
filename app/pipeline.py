"""Bridge to the slicing/ pipeline. Invoked as a subprocess rather than
imported, deliberately - slicing/ is a sibling directory, not a package
under app/, and it already treats OrcaSlicer and mbotmake as subprocesses
itself (see slicing/README.md); calling slice.py the same way keeps that
boundary consistent instead of reaching across directories with sys.path
hacks. It also means slicing/ stays fully independent and runnable/
testable on its own, which it already was."""

import subprocess
import sys
from pathlib import Path

SLICING_DIR = Path(__file__).resolve().parent.parent / "slicing"
SLICE_SCRIPT = SLICING_DIR / "slice.py"


def run_slice(stl_path: Path, output_makerbot_path: Path) -> tuple[bool, str]:
    """Returns (success, detail) - detail is the slicer's own output either
    way, useful as slice_error on failure."""
    result = subprocess.run(
        [sys.executable, str(SLICE_SCRIPT), str(stl_path), str(output_makerbot_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600,
    )
    output = result.stdout or ""
    if result.returncode == 0 and output_makerbot_path.exists():
        return True, output
    return False, output or f"slice.py exited {result.returncode} with no output"
