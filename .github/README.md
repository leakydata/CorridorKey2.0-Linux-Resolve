# CorridorKey for Linux + DaVinci Resolve

A free, community fork of [CorridorKey 2.0](https://github.com/nikopueringer/CorridorKey2.0) that adds a
**DaVinci Resolve Studio integration for Linux**. Select a clip on the timeline, run
`Workspace > Scripts > Utility > CorridorKey - Key selected clip`, and the keyed cutout (straight-alpha ProRes
4444, or linear HDR EXR) lands on a video track above it when the job finishes.

> **Status: early / experimental.** Verified end to end on Resolve Studio 21.2 for ProRes 4444 and EXR output:
> frame-exact trim, inference, import, straight alpha, and placement on the track above the source clip (same
> length, same record position), from 432x240 up to 4K, at 23.976 and 30 fps, including rotated phone clips and
> real green-screen footage. The menu scripts were run inside Resolve; a hand click on every menu entry with a
> GUI selection is still being verified. See [Known limitations](#known-limitations).

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

One command (no sudo; everything goes in one folder, `~/.local/share/corridorkey-resolve`):

```bash
curl -fsSL https://raw.githubusercontent.com/leakydata/CorridorKey2.0-Linux-Resolve/linux-resolve/resolve/install.sh | bash
```

It checks your machine, shows what it will download and asks before starting, then sets up a private Python
environment with PyTorch, downloads the model weights (about 5 GB), and adds the Resolve menu entries and the
`corridorkey-resolve` command. It is safe to run again; it resumes and updates. Prefer to read it first? It is
[`resolve/install.sh`](../resolve/install.sh): download it, read it, run it (`--yes` skips the question,
`--dir` picks another folder). You need an NVIDIA RTX 30-series or newer, `git`, `curl`, `ffmpeg`
(`sudo apt install git curl ffmpeg` on Ubuntu/Kubuntu/Debian) and the NVIDIA driver.

Then, in Resolve: Preferences > System > General > **External scripting using: Local**, and restart Resolve.

**Update:** `corridorkey-resolve update`. **Remove:** `python3 ~/.local/share/corridorkey-resolve/resolve/install.py --uninstall`,
then delete the folder. Check the setup any time with `corridorkey-resolve doctor`.

<details><summary>Manual install (what the script does)</summary>

```bash
git clone https://github.com/leakydata/CorridorKey2.0-Linux-Resolve.git
cd CorridorKey2.0-Linux-Resolve

# environment + weights (about 5 GB), exactly as in the upstream README.md. Python 3.12 recommended:
uv venv -p 3.12 venv
VIRTUAL_ENV=$PWD/venv uv pip install torch==2.9.1 torchvision==0.24.1 --index-url https://download.pytorch.org/whl/cu128
VIRTUAL_ENV=$PWD/venv uv pip install -r requirements.txt
venv/bin/python scripts/download_weights.py

# add the menu entries to Resolve
python3 resolve/install.py
```

Restart Resolve. `python3 resolve/install.py --uninstall` removes the menu entries and the command.
</details>

## Use

1. Put a green/blue-screen clip on the timeline and select it (or park the playhead on it).
2. `Workspace > Scripts > Utility > CorridorKey - Key selected clip` (ProRes 4444) or `... (HDR EXR)`.
   No green screen? See [Footage without a green screen](#footage-without-a-green-screen).
3. Keep working, but leave the same project and timeline open until the job finishes. A desktop notification
   appears when it starts (with a rough time estimate), then updates in place with a progress bar, the current
   step (trimming, loading the model, keying window 3/8, saving, placing) and an estimated time left, and
   again when it finishes. Jobs are queued per machine so two runs never fight for the GPU; a queued job says
   it is waiting. The same progress is available from `corridorkey-resolve status` (below). The percentage is an
   estimate: it moves in steps with the real stages and is most accurate on clips longer than a few seconds.
4. The result is imported into a **CorridorKey** media-pool bin (cutout + alpha matte) and the cutout is placed on
   the first free video track above the clip, aligned to it. Put your background below.

Files and logs: results in `~/Videos/CorridorKey/<project>/...`, job logs in
`~/.local/state/corridorkey-resolve/jobs/<id>/worker.log`.

### Footage without a green screen

`Workspace > Scripts > Utility > CorridorKey - Key person (no green screen)` is for a person filmed in front of an
ordinary background (talking-head, phone or webcam footage). Before keying, it makes a rough matte of the person on
every frame with torchvision's DeepLabV3 segmentation model and passes it to CorridorKey as a hint video
(upstream's `--hint_video`, see "Choosing An Inference Mode" in the upstream [README.md](../README.md)). The hint tells
the keyer *which* subject to keep; CorridorKey still makes the edges. Without it, the model has to guess what the
subject is, and on a normal room it tends to keep nearby objects (a mic arm, a chair) and leave a haze around the
person.

- People only: on a clip with no person the job stops with "no person found" instead of guessing.
- The matte model is torchvision's pretrained DeepLabV3-ResNet101 (about 233 MB; torchvision's code is BSD-3-Clause,
  the weights were trained on a COCO subset and are not covered by this fork's licence; check torchvision's terms
  for your use). `install.py` downloads it once from download.pytorch.org into `weights/torch/` (or on first use;
  `install.py --no-hint-model` skips it). It is never bundled with this repository.
- It adds a few seconds per clip (about 0.1 s per 1080x1920 frame on an RTX 4090).
- The result is named `CK_<clip>_..._person`. It is a key of a normal room, not a green-screen key: expect softer
  hair edges than with a real screen.
- Command line: `corridorkey-resolve key --hint person`. Set `"hint": "person"` in the config to make it the default
  for the other menu entries too (for example for EXR output).

## Command line and AI agents

Everything the menu does is also available without the GUI, for scripts and AI agents. Resolve must be running
(Preferences > System > General > External scripting using: Local).

```bash
corridorkey-resolve doctor                       # check install, GPU, Resolve connection
corridorkey-resolve list                         # clips on the current timeline: id, track, keyable or why not
corridorkey-resolve key --clip "shot1.mov" --wait          # key a clip by name and wait for the result
corridorkey-resolve key --track 1 --mode exr               # key every clip on V1 as EXR, return immediately
corridorkey-resolve key --clip "talk.mov" --hint person    # no green screen: guide the key with a person matte
corridorkey-resolve key --id <timeline-item-id> --wait --json
corridorkey-resolve status [JOB_ID] [--wait]     # progress, e.g. "44%  talk.mov: keying (window 3/8), ~2 min left"
corridorkey-resolve jobs; corridorkey-resolve update
```

Add `--json` to any command for a single machine-readable JSON object (errors included). With no filter, `key`
behaves like the menu: the selection, else the clip under the playhead. Exit codes: `0` ok, `1` finished with
failures or `doctor` found problems, `2` usage/environment error, `3` `--wait` timed out. Each job writes
`result.json` (one line per clip) and `result_detail.json` (status, track, placed item id, output files) next to its
log in `~/.local/state/corridorkey-resolve/jobs/<id>/`.

**MCP server.** `resolve/mcp_server.py` (standard library only) exposes the same operations as MCP tools
(`corridorkey_doctor`, `corridorkey_list_clips`, `corridorkey_key_clips`, `corridorkey_job_status`,
`corridorkey_recent_jobs`). It runs *alongside* Blackmagic's own Resolve MCP, which has a fixed tool list and
cannot be extended. Register it, for example, with
`claude mcp add corridorkey-resolve -- python3 /path/to/resolve/mcp_server.py`, or in a client's JSON config as
`{"command": "python3", "args": ["/path/to/resolve/mcp_server.py"]}`. Keying a few seconds of footage takes about a
minute or more, so `corridorkey_key_clips` returns a job id immediately and `corridorkey_job_status` can wait up to
50 seconds per call.

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
| `hint` | `"none"` | `"person"` = always use the automatic person matte as a hint (the "Key person" entry sets it per job) |
| `hint_quality` | `0.75` | how much the keyer trusts that matte (upstream `--hint_quality`; 0.95 = artist matte, 0.5 = loose) |

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
- The only hint the integration makes is the automatic person matte; your own roto/hint videos are not wired to
  the menu yet (upstream `infer.py --hint_video` takes them directly).

## Roadmap

Linux + DaVinci Resolve only for now. Other hosts are possible far in the future, not planned.

Upstream's own documentation (inference modes, hint video, HDR output, VRAM tuning, CLI flags) is kept in
the original [README.md](../README.md).
