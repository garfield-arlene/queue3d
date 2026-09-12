# slicing

STL -> `.makerbot` on Linux, targeting the Replicator+ with a Tough Smart
Extruder+ (see `[[makerbot-hardware]]`/queue3d memory). This is the second
milestone after `test-print/` (which proved the network path); together they
cover the two halves of "upload an STL and have it print."

## Pipeline

```
STL --[stl_to_3mf.py]--> .3mf project --[OrcaSlicer --slice]--> gcode --[mbotmake]--> .makerbot
```

1. **`stl_to_3mf.py`** wraps an arbitrary STL (ASCII or binary) into a
   minimal `.3mf` project alongside our printer profile. This exists because
   OrcaSlicer's CLI `--load-settings` path (loading separate printer/process/
   filament preset files against a raw STL) hits a
   "process not compatible with printer" compatibility-gate failure in
   2.4.2 - reproduced even with 100% stock, unmodified bundled presets, so
   it looks like a CLI-mode limitation rather than anything wrong with our
   profile. A self-contained `.3mf` (geometry + settings bundled together,
   the same shape a human saves from the GUI) sidesteps it entirely.
2. **OrcaSlicer** (`tools/squashfs-root/`, vendored AppImage, extracted so it
   runs without FUSE) slices that project headlessly: `--slice 0` on the
   `.3mf`, gcode lands in `--outputdir` as `plate_1.gcode`.
3. **`mbotmake`** (vendored from `charely6/mbotmake`, one bugfix applied -
   see below) converts that gcode into `.makerbot` - a zip of `meta.json`
   (machine/extruder identity, temps, bounding box) + `print.jsontoolpath`
   (the actual per-move command stream the firmware executes; there's no
   gcode inside a `.makerbot` at all).

   **Important:** call `mbotmake` directly yourself (as `slice.py` does),
   don't rely on OrcaSlicer's `post_process` config hook to chain it
   automatically - that hook is only honored on the GUI export path, and is
   silently never invoked when slicing via `--slice` on the CLI (confirmed:
   no trace of it in `--debug 5` logs, no output file, yet slicing itself
   reports success). The profile file still carries a `post_process` entry
   for anyone who opens it in the actual OrcaSlicer GUI, but our own
   pipeline never depends on it firing.

`slice.py` runs all three steps. `stl_to_3mf.py` and `mbotmake` also work
standalone if you need to debug one stage in isolation.

## Usage

```bash
python3 slice.py models/testcube.stl out/testcube.makerbot
```

Then hand that file to `../test-print/send_print.py` to actually print it.

## Setup (fresh checkout)

The OrcaSlicer AppImage (~140MB) isn't committed - fetch it once:

```bash
mkdir -p tools
curl -sL -o tools/OrcaSlicer.AppImage \
  https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.2/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage
chmod +x tools/OrcaSlicer.AppImage
cd tools && ./OrcaSlicer.AppImage --appimage-extract && cd ..
```

(Extracting avoids needing FUSE at runtime, which headless/server
environments often lack.) For an ARM64 Raspberry Pi, use the aarch64
AppImage asset from the same release instead - OrcaSlicer ships official
Linux aarch64 builds, unlike PrusaSlicer, which currently only publishes
Windows/Mac from its own GitHub releases (Linux arm64 exists via Flathub,
but that's more moving parts for a headless Pi service than a plain
AppImage - see project memory for the fuller comparison).

## Files

- `stl_to_3mf.py` - STL -> minimal `.3mf` project wrapper (reusable module + CLI).
- `mbotmake/mbotmake` - vendored gcode -> `.makerbot` converter, upstream
  `charely6/mbotmake`, with one fix applied (see below). Re-vendor with care
  if upstream changes; re-check the fix still applies.
- `profiles/makerbot-plus-tough-extruder.json` - merged OrcaSlicer
  printer+process+filament profile, adapted from upstream's
  `printerconfigs/MakerbotPlusWithPlusExtruder.3mf`. Bed 295x195mm, height
  165mm, nozzle 0.4mm, `gcode_flavor: marlin2`, origin centered, absolute
  extrusion (`use_relative_e_distances: 0`) - the last two are hard
  requirements of `mbotmake`'s parser, not arbitrary choices.
- `models/testcube.stl` - 20mm calibration cube used to validate the pipeline.
- `slice.py` - orchestrates all three steps.

## The mbotmake bug fix

Upstream `mbotmake` records whichever `M140` (bed temperature) line appears
**last** in the gcode as the job's target bed temperature. Slicer end-gcode
always finishes with a cooldown `M140 S0`, so unpatched this silently bakes
a **0°C bed target** into every `meta.json` - unlike the extruder-temperature
handling a few lines above it in the same script, which correctly keeps only
the first non-zero reading. Our vendored copy keeps only the first non-zero
`M140` too. Confirmed fixed: slicing `models/testcube.stl` now produces
`platform_temperature: 60` in `meta.json`, not `0`.

## Verifying a generated file

```bash
mkdir -p /tmp/check && cd /tmp/check
unzip -o /path/to/file.makerbot
python3 -c "import json; print(json.load(open('meta.json')))" | python3 -m json.tool
```

Check `bot_type` (`replicator_b` = Replicator+), `tool_type` (`mk13_impla` =
Tough Smart Extruder+, `mk13` = plain Smart Extruder+ - mismatching this is
what causes the printer's Error 1048), and `platform_temperature` (should be
your actual bed temp, not 0).
