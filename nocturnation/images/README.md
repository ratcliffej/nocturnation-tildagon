# Tildagon Lume background images

Pre-rendered RGB565 image files for the Lume-mode LCD background
layer (Epic 13 Phase 2A). Loaded by `nocturnation/images.py` and
composed under the orchestrator-driven text overlay.

## Filename convention

Each image is keyed by the Director's persisted Performance-range
source id (see [Section 4.7 of the user manual](../../../Docs/manuals/user-manual.md)
in the docs repo for the DirID setup workflow):

| Filename | Shown when... |
|---|---|
| `dirid_<hex>.raw` | The Tildagon's TOFU lock names that DirID. e.g. `dirid_d0.raw` for stage D. Hex is lowercase, two digits, no `0x` prefix. |
| `default.raw` | Fallback when the locked DirID has no specific file, OR when no lock is held. Always loaded if present. |

A missing image silently falls back to the pre-Epic-13 solid wash
background. No image files means no behaviour change.

## File format

Flat little-endian RGB565 binary:

- 240 × 240 pixels (`DISPLAY_W` × `DISPLAY_H` in `images.py`)
- 2 bytes per pixel = **115,200 bytes per file**
- Row-major, no header, no palette

Convert authoring PNGs with `Docs/tools/png_to_rgb565.py` in the
nocturnation-docs repo:

```bash
./png_to_rgb565.py stage-d-logo.png --out nocturnation/images/dirid_d0.raw
./png_to_rgb565.py default-logo.png --out nocturnation/images/default.raw
```

The tool resizes / crops to 240×240 by default (`--fit cover` is the
default, designed for centred logos). Each conversion drops a
`.meta.txt` sidecar next to the `.raw` recording the source filename,
dimensions, and SHA-256 - so the provenance of a deployed image can
be checked against its source PNG.

## Deployment

`deploy.sh` runs `mpremote cp -r nocturnation :apps/nocturnation/`
which copies this directory recursively, picking up any `.raw` files
present. No deploy script changes are needed when adding a new image -
just drop the file in and redeploy.
