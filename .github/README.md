# CorridorKey for Linux + DaVinci Resolve

A free, community fork of [CorridorKey 2.0](https://github.com/nikopueringer/CorridorKey2.0) that adds a
**DaVinci Resolve Studio integration for Linux**. Select a clip on the timeline, run
`Workspace > Scripts > Utility > CorridorKey - Key selected clip`, and the keyed cutout (straight-alpha ProRes
4444, or linear HDR EXR) lands on a video track above it when the job finishes.

> **Status: early / experimental.** Verified end to end on Resolve Studio 21.2 for both ProRes 4444 and EXR
> output: frame-exact trim, inference, import, straight alpha, and placement on the track above the source clip
> (same length, same record position). The jobs were started through the same `launch()` entry point the menu
> entries call; clicking the entries from Resolve's Workspace > Scripts menu is still being verified. Only
> low-resolution, non-green-screen footage has been used so far, so keying quality is untested. See
> [Known limitations](#known-limitations).

## Acknowledgements

All of the hard work here is not mine:

- **The CorridorKey 2.0 model and software** were designed and trained by **Zachary Harris** in collaboration with
  **Niko Pueringer and Corridor Digital**. This fork exists only because they released it openly.
  Upstream: <https://github.com/nikopueringer/CorridorKey2.0>. Weights: <https://huggingface.co/nikopueringer/CorridorKey2.0>.
- The frozen foundation models it builds on (NVIDIA C-RADIOv4, Microsoft MoGe-2, Google DeepMind RVM) remain under
  their own licences; see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
- **This is not the official CorridorKey plugin.** The official, paid plugins for many editing/compositing apps are
  made by Ale Alvaro and Corridor Digital at <https://corridorkey.com>. If you use Windows/macOS, or want a
  supported product, or want to support the people who made the model, buy that. This fork only adds an
  unofficial, free Linux + Resolve path using the open repository and has no affiliation with, or endorsement
  from, Corridor Digital.

## Licence

Same as upstream: the **Corridor Key Licence 2.0** (CC BY-NC-SA 4.0 plus additional terms), see [LICENSE](../LICENSE).
In short, and without replacing the licence text:

- Free to use, including on commercial projects; this fork is and will stay free. No repackaging or resale.
- Forks/derivatives must be free, share-alike under the same licence, and keep the "CorridorKey" name.
- Companies with > US $20M revenue need a seat of the official plugin (licence section 3).
- Incorporating this into a commercial software package needs a separate agreement (section 2d).

## Requirements

- Linux, DaVinci Resolve **Studio** (external scripting is a Studio feature), tested on 21.2.
- NVIDIA GPU, RTX 30-series or newer, 4+ GB VRAM (see upstream README for VRAM guidance).
  With several GPUs the integration automatically picks the largest CUDA card (>= compute 8.0) and pins
  `CUDA_VISIBLE_DEVICES` to its UUID for the inference process only.
- `ffmpeg` / `ffprobe` on `PATH`, `uv` or Python 3.10-3.12, `notify-send` (optional, for desktop notifications).
- Resolve: Preferences > System > General > **External scripting using: Local**.

## Install

```bash
git clone https://github.com/leakydata/CorridorKey2.0-Linux-Resolve.git
cd CorridorKey2.0-Linux-Resolve

# environment + weights (about 5 GB), exactly as in the upstream [README.md](../README.md). Python 3.12 recommended:
uv venv -p 3.12 venv
VIRTUAL_ENV=$PWD/venv uv pip install torch==2.9.1 torchvision==0.24.1 --index-url https://download.pytorch.org/whl/cu128
VIRTUAL_ENV=$PWD/venv uv pip install -r requirements.txt
venv/bin/python scripts/download_weights.py

# add the menu entries to Resolve
python3 resolve/install.py
```

Restart Resolve. `python3 resolve/install.py --uninstall` removes the menu entries.

## Use

1. Put a green/blue-screen clip on the timeline and select it (or park the playhead on it).
2. `Workspace > Scripts > Utility > CorridorKey - Key selected clip` (ProRes 4444) or `... (HDR EXR)`.
3. Keep working. A desktop notification appears when it starts and finishes. Jobs are queued per machine so two
   runs never fight for the GPU.
4. The result is imported into a **CorridorKey** media-pool bin (cutout + alpha matte) and the cutout is placed on
   the first free video track above the clip, aligned to it. Put your background below.

Files and logs: results in `~/Videos/CorridorKey/<project>/...`, job logs in
`~/.local/state/corridorkey-resolve/jobs/<id>/worker.log`.

## Configuration

`~/.config/corridorkey-resolve/config.json` (written by the installer; every key is optional):

| key | default | meaning |
|---|---|---|
| `ck_dir`, `python` | set by installer | CorridorKey checkout and its venv python |
| `output_root` | `~/Videos/CorridorKey` | where results are written |
| `gpu` | `"auto"` | `"auto"`, a GPU UUID (`nvidia-smi -L`) or an index |
| `despill_strength` | `0.5` | 0.0-1.0, applied to the cutout |
| `extra_infer_args` | `[]` | passed to `infer.py`, e.g. `["--hann_chunk","24","--hann_stride","12"]` |
| `keep_intermediate` | `false` | keep the trimmed ProRes source copy |
| `place_on_timeline` | `true` | `false` = only import to the bin |

## How it works

Out-of-process "render and replace": a stdlib-only stub runs inside Resolve, records the selection, writes a job
file and spawns a detached worker (so Resolve never freezes). The worker (Resolve external scripting) trims the
clip's exact source range with ffmpeg, runs the unmodified upstream `infer.py` in its venv, imports the results and
places them on the timeline. All integration code is in [`resolve/`](../resolve/); upstream files are untouched.

## Known limitations

- The key is made from the **original source file**: Resolve grades, crops, transforms, Fusion edits and speed
  changes on the clip are not baked in. Retimed clips and clips whose fps differs from the timeline are refused.
- Video files only (no image sequences / compound / generator clips) in this version.
- Resolve and CorridorKey share the GPU. Scrubbing heavily in Resolve during a job can run either out of VRAM.
- Resolve on Linux needs a Plasma/X11 session for working mouse input; this is a Resolve issue, not this fork's.
- No hint-matte workflow yet (upstream supports first-frame and full hint videos).

## Roadmap

Linux + DaVinci Resolve only for now. Other hosts are possible far in the future, not planned.

Upstream's own documentation (inference modes, hint video, HDR output, VRAM tuning, CLI flags) is kept in
the original [README.md](../README.md).
