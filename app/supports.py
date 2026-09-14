"""Extract a simplified visual representation of generated support material
from the intermediate gcode (see slicing/slice.py's gcode_out_path) for the
3D preview. Deliberately not a full toolpath/layer viewer - see README.md's
To do list: just enough line segments to show roughly where supports are,
not the exact print path or a per-layer scrub control.

How: OrcaSlicer's gcode marks feature types with ";TYPE:X" comments as it
switches between them - confirmed by slicing a real overhang test model,
`;TYPE:Support` and `;TYPE:Support interface` do appear. mbotmake's
conversion to .makerbot throws this away entirely (every move becomes an
undifferentiated "move" command - see slicing/mbotmake/mbotmake), so this
has to come from the gcode, not the final print file.
"""

import json
from pathlib import Path

SUPPORT_TYPES = {"Support", "Support interface"}

# Show every real layer whenever the total fits under this budget - only
# thin things out as a last resort for a pathologically support-dense
# model. This matters more than it sounds: each rendered tube's radius
# (see preview.js's SUPPORT_TUBE_RADIUS) is comparable to a real layer's
# height, so consecutive *real* layers naturally overlap into what reads as
# a continuous, gap-free surface - the moment layers get skipped, visible
# gaps reappear, and worse, a skipped span can jump between two entirely
# different, unrelated support towers rather than following one leaning
# branch (confirmed against a real multi-tower model - see git history for
# both bugs this constant used to cause: too small a budget kept as few as
# 3 layers total, and even after raising it, starting the search already
# thinned out - rather than at full density - kept it artificially sparse
# even when the full set would've fit the budget easily).
MAX_SEGMENTS = 500_000


def _parse_gcode_axis_value(token: str) -> float:
    return float(token[1:])


def extract_support_segments(gcode_path) -> list[list[float]]:
    """Returns a list of [x1, y1, z1, x2, y2, z2] segments for every
    extrusion move gcode tagged as support material or support interface."""
    pos = {"X": 0.0, "Y": 0.0, "Z": 0.0, "E": 0.0}
    in_support = False
    layers: dict[float, list] = {}

    with open(gcode_path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            if line.startswith(";TYPE:"):
                in_support = line[len(";TYPE:") :].strip() in SUPPORT_TYPES
                continue

            code = line.split(";", 1)[0].strip()
            if not code:
                continue
            parts = code.split()
            if not parts or parts[0] not in ("G0", "G1"):
                continue

            prev = dict(pos)
            extruding = False
            for token in parts[1:]:
                axis = token[0]
                if axis not in pos:
                    continue
                value = _parse_gcode_axis_value(token)
                if axis == "E":
                    extruding = value > pos["E"]
                pos[axis] = value

            if in_support and extruding:
                # Group by layer (Z), not just appended to one flat list -
                # downsampling has to drop whole layers, not individual
                # segments picked from anywhere in the chronological
                # sequence, or what survives is disconnected fragments
                # scattered across many different layers: each one too
                # short to read as a line at normal zoom (renders as
                # isolated dots), and with no visual relationship to its
                # neighbors, since they were never adjacent in the same
                # cross-section to begin with.
                layer_key = round(prev["Z"], 3)
                layers.setdefault(layer_key, []).append(
                    [prev["X"], prev["Y"], prev["Z"], pos["X"], pos["Y"], pos["Z"]]
                )

    return _select_whole_layers(layers)


def _select_whole_layers(layers: dict) -> list[list[float]]:
    """Keeps entire layers (all their segments, so each stays a coherent,
    connected cross-section) rather than sampling individual segments.
    Starts at full density (every real layer) and only increases the
    stride if that's too much data - never thins pre-emptively."""
    ordered_zs = sorted(layers)
    stride = 1

    while stride < len(ordered_zs):
        kept = ordered_zs[::stride]
        if sum(len(layers[z]) for z in kept) <= MAX_SEGMENTS:
            break
        stride += 1
    else:
        kept = ordered_zs[::stride] if ordered_zs else []

    segments = []
    for z in kept:
        segments.extend(layers[z])
    return segments


def write_support_preview(gcode_path, output_json_path) -> int:
    """Writes the extracted segments to output_json_path. Returns the
    segment count (0 means no supports were generated for this job - the
    file is still written, as an empty list, so callers don't need to
    special-case "no supports" vs "preview not generated yet")."""
    segments = extract_support_segments(gcode_path)
    Path(output_json_path).write_text(json.dumps(segments))
    return len(segments)
