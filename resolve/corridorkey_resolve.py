"""CorridorKey 2.0 <-> DaVinci Resolve Studio (Linux) bridge.

Two halves, both in this one stdlib-only file:

* launch()      runs INSIDE Resolve (called by the tiny Workspace > Scripts stubs).
                It only records which timeline clips are selected, writes a job file and spawns a
                detached worker. It never imports torch and returns immediately, so Resolve stays usable.
* worker_main() runs OUTSIDE Resolve (ResolvePython, external scripting). For every clip it
                trims the exact source range with ffmpeg, runs upstream infer.py in its own venv,
                imports the result into a "CorridorKey" bin and drops it on a video track above the clip.

This is an independent, free integration around the open CorridorKey 2.0 repository
(https://github.com/nikopueringer/CorridorKey2.0). It is not the paid plugin from corridorkey.com and
contains no code or weights from the upstream repo.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

CONFIG_PATH = os.path.expanduser("~/.config/corridorkey-resolve/config.json")
STATE_DIR = os.path.expanduser("~/.local/state/corridorkey-resolve")
BIN_NAME = "CorridorKey"

DEFAULTS = {
    "ck_dir": "",                       # upstream CorridorKey2.0 checkout (has infer.py)
    "python": "",                       # venv python that can import torch; default <ck_dir>/venv/bin/python
    "worker_python": "",                # interpreter for the Resolve-side worker; default ResolvePython
    "output_root": os.path.expanduser("~/Videos/CorridorKey"),
    "gpu": "auto",                      # "auto" (biggest CUDA >= sm_80 card), a GPU UUID, or an index
    "despill_strength": 0.5,
    "extra_infer_args": [],             # e.g. ["--hann_chunk", "24", "--hann_stride", "12"]
    "keep_intermediate": False,
    "place_on_timeline": True,
}

RESOLVE_API = "/opt/resolve/Developer/Scripting"
RESOLVE_LIB = "/opt/resolve/libs/Fusion/fusionscript.so"


# --------------------------------------------------------------------------- shared helpers

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "clip"


def notify(title, body="", urgent=False):
    print("[CorridorKey] %s %s" % (title, body), flush=True)
    exe = shutil.which("notify-send")
    if exe:
        try:
            subprocess.run([exe, "-a", "CorridorKey", "-u", "critical" if urgent else "normal", title, body],
                           timeout=5, check=False)
        except Exception:
            pass


# --------------------------------------------------------------------------- inside Resolve

def launch(resolve, mode="video"):
    """Entry point for the menu stubs. mode: "video" (ProRes 4444) or "exr" (linear half-float EXR)."""
    try:
        cfg = load_config()
        if not cfg["ck_dir"] or not os.path.isfile(os.path.join(cfg["ck_dir"], "infer.py")):
            return notify("CorridorKey not configured", "Run install.py (ck_dir missing in %s)" % CONFIG_PATH, True)
        project = resolve.GetProjectManager().GetCurrentProject()
        timeline = project.GetCurrentTimeline() if project else None
        if timeline is None:
            return notify("CorridorKey", "Open a timeline first.", True)

        items = [i for i in (timeline.GetSelectedClips() or [])]
        if not items:
            cur = timeline.GetCurrentVideoItem()
            items = [cur] if cur else []
        if not items:
            return notify("CorridorKey", "Select a clip on the timeline (or park the playhead on one).", True)

        job_id = time.strftime("%Y%m%d-%H%M%S")
        job_dir = os.path.join(STATE_DIR, "jobs", job_id)
        os.makedirs(job_dir, exist_ok=True)
        job = {
            "id": job_id,
            "mode": mode,
            "project": project.GetName(),
            "timeline_id": timeline.GetUniqueId(),
            "timeline_name": timeline.GetName(),
            "items": [{"id": i.GetUniqueId(), "name": i.GetName()} for i in items],
        }
        job_path = os.path.join(job_dir, "job.json")
        with open(job_path, "w") as f:
            json.dump(job, f, indent=2)

        py = cfg["worker_python"] or ("/opt/resolve/bin/ResolvePython"
                                       if os.path.exists("/opt/resolve/bin/ResolvePython") else "python3")
        env = dict(os.environ)
        env.update(RESOLVE_SCRIPT_API=RESOLVE_API, RESOLVE_SCRIPT_LIB=RESOLVE_LIB)
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.dirname(os.path.abspath(__file__)), RESOLVE_API + "/Modules"]
            + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        log = open(os.path.join(job_dir, "worker.log"), "ab")
        subprocess.Popen([py, "-c", "import corridorkey_resolve as m, sys; m.worker_main(sys.argv[1])", job_path],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                         start_new_session=True, close_fds=True)
        notify("CorridorKey started", "%d clip(s), %s. Log: %s" % (len(items), mode, job_dir))
    except Exception:
        notify("CorridorKey failed to start", traceback.format_exc().splitlines()[-1], True)
        traceback.print_exc()


# --------------------------------------------------------------------------- GPU + subprocess

def pick_gpu(setting):
    """Return the CUDA_VISIBLE_DEVICES value to give infer.py (a UUID is order-proof)."""
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,compute_cap", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True).stdout
    gpus = []
    for line in out.strip().splitlines():
        idx, uuid, name, mem, cc = [x.strip() for x in line.split(",")]
        gpus.append({"index": idx, "uuid": uuid, "name": name, "mem": int(mem), "cc": float(cc)})
    if setting not in ("auto", "", None):
        for g in gpus:
            if setting in (g["uuid"], g["index"]):
                return g
        raise RuntimeError("configured gpu %r not found among %s" % (setting, [g["name"] for g in gpus]))
    ok = [g for g in gpus if g["cc"] >= 8.0]   # upstream: RTX 30-series or newer
    if not ok:
        raise RuntimeError("no CUDA GPU with compute capability >= 8.0 (RTX 30-series+) found")
    return max(ok, key=lambda g: g["mem"])


def run_logged(cmd, env=None, cwd=None, label=""):
    print("$ " + " ".join(cmd), flush=True)
    p = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    tail = []
    for line in p.stdout:
        line = line.rstrip("\n")
        print(line, flush=True)
        tail.append(line)
        del tail[:-15]
    rc = p.wait()
    if rc != 0:
        raise RuntimeError("%s failed (exit %d): %s" % (label or cmd[0], rc, " | ".join(tail[-4:])))


def probe_frames(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames,r_frame_rate", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return int(s["nb_read_frames"]), s["r_frame_rate"]


# --------------------------------------------------------------------------- Resolve-side worker

def _fps(s):
    if "/" in str(s):
        a, b = str(s).split("/")
        return float(a) / float(b)
    return float(s)


def find_item(timeline, uid):
    for t in range(1, timeline.GetTrackCount("video") + 1):
        for it in timeline.GetItemListInTrack("video", t) or []:
            if it.GetUniqueId() == uid:
                return it, t
    return None, None


def describe_item(timeline, ref):
    """Validate one timeline item and return everything needed to key it. Raises ValueError with a reason."""
    item, track = find_item(timeline, ref["id"])
    if item is None:
        raise ValueError("clip no longer on the timeline")
    mpi = item.GetMediaPoolItem()
    if mpi is None:
        raise ValueError("generator/compound/adjustment clips are not supported")
    props = mpi.GetClipProperty() or {}
    path = props.get("File Path", "")
    if not path or not os.path.isfile(path):
        raise ValueError("source is not a single video file (image sequences aren't supported yet): %r" % path)
    src_fps = float(props.get("FPS", 0) or 0)
    tl_fps = float(timeline.GetSetting("timelineFrameRate") or 0)
    if not src_fps or abs(src_fps - tl_fps) > 0.01:
        raise ValueError("clip is %.3f fps on a %.3f fps timeline; conform them first" % (src_fps, tl_fps))
    try:
        pct = float((item.GetSpeed() or {}).get("Percentage", 100.0))
    except Exception:
        pct = 100.0
    if abs(pct - 100.0) > 0.001:
        raise ValueError("retimed clips (%.1f%%) are not supported" % pct)
    s, e = int(item.GetSourceStartFrame()), int(item.GetSourceEndFrame())
    start, dur = int(item.GetStart()), int(item.GetDuration())
    if e - s + 1 != dur:
        raise ValueError("source range (%d frames) != timeline duration (%d); retimed or in a transition?" % (e - s + 1, dur))
    return {"item": item, "mpi": mpi, "track": track, "path": path, "name": item.GetName(),
            "src_start": s, "src_end": e, "rec_start": start, "frames": dur,
            "fusion_comps": item.GetFusionCompCount()}


def make_intermediate(info, out_path):
    """Frame-exact trim of the source range to ProRes 422 HQ (no audio). infer.py can't take a start offset."""
    s, e = info["src_start"], info["src_end"]
    vf = "trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS" % (s, e + 1)
    run_logged(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", info["path"], "-map", "0:v:0",
                "-vf", vf, "-an", "-fps_mode", "passthrough",
                "-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", out_path], label="ffmpeg trim")
    n, rate = probe_frames(out_path)
    if n != info["frames"]:
        raise RuntimeError("trim produced %d frames, expected %d" % (n, info["frames"]))
    return rate


def run_inference(cfg, job, in_path, out_dir, gpu):
    ck_dir = cfg["ck_dir"]
    py = cfg["python"] or os.path.join(ck_dir, "venv", "bin", "python")
    tmp = os.path.join(out_dir, "tmp")
    os.makedirs(tmp, exist_ok=True)
    exr = job["mode"] == "exr"
    cmd = [py, os.path.join(ck_dir, "infer.py"), "--input", in_path, "--output_dir", out_dir, "--temp_dir", tmp,
           "--outputs", "cutout", "alpha", "--despill_strength", str(cfg["despill_strength"])]
    if exr:
        cmd += ["--output_format", "exr"]
    cmd += [str(a) for a in cfg["extra_infer_args"]]
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = gpu["uuid"]
    env["PYTHONUNBUFFERED"] = "1"
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "gpu.lock"), "w") as lock:   # queue jobs; Resolve shares this GPU too
        fcntl.flock(lock, fcntl.LOCK_EX)
        run_logged(cmd, env=env, cwd=ck_dir, label="infer.py")
    shutil.rmtree(tmp, ignore_errors=True)


def collect_outputs(out_dir, stem, exr):
    """Return (cutout_ref, alpha_ref). A ref is a file path or, for EXR, (dir, first, last)."""
    if not exr:
        return (os.path.join(out_dir, stem + "_cutout.mov"), None)
    refs = []
    for name in ("cutout", "alpha"):
        d = os.path.join(out_dir, "%s_%s_exr" % (stem, name))
        frames = sorted(f for f in os.listdir(d) if f.lower().endswith(".exr"))
        refs.append((d, frames))
    return refs[0], refs[1]


def import_clip(mp, ref, name):
    if isinstance(ref, tuple):
        d, frames = ref
        m = re.match(r"^(.*?)(\d+)\.exr$", frames[0], re.I)
        pattern = os.path.join(d, "%s%%0%dd.exr" % (m.group(1), len(m.group(2))))
        first, last = int(m.group(2)), int(re.match(r"^(.*?)(\d+)\.exr$", frames[-1], re.I).group(2))
        got = mp.ImportMedia([{"FilePath": pattern, "StartIndex": first, "EndIndex": last}])
        if not got:
            got = mp.ImportMedia([d])
    else:
        got = mp.ImportMedia([{"FilePath": ref}])
    if not got:
        raise RuntimeError("Resolve refused to import %s" % (ref[0] if isinstance(ref, tuple) else ref))
    clip = got[0]
    try:
        clip.SetClipProperty("Clip Name", name)
    except Exception:
        pass
    return clip


def ensure_bin(mp):
    root = mp.GetRootFolder()
    for f in root.GetSubFolderList() or []:
        if f.GetName() == BIN_NAME:
            return f
    return mp.AddSubFolder(root, BIN_NAME)


def free_track_above(timeline, track, start, end):
    n = timeline.GetTrackCount("video")
    for t in range(track + 1, n + 1):
        busy = any(not (it.GetEnd() <= start or it.GetStart() >= end)
                   for it in timeline.GetItemListInTrack("video", t) or [])
        if not busy:
            return t
    if not timeline.AddTrack("video"):
        raise RuntimeError("could not add a video track")
    return timeline.GetTrackCount("video")


def key_one(resolve, cfg, job, timeline, ref, gpu):
    project = resolve.GetProjectManager().GetCurrentProject()
    info = describe_item(timeline, ref)
    exr = job["mode"] == "exr"
    stem = "%s_f%d-%d" % (safe_name(info["name"]), info["src_start"], info["src_end"])
    out_dir = os.path.join(cfg["output_root"], safe_name(job["project"]), "%s_%s" % (stem, job["id"]))
    os.makedirs(out_dir, exist_ok=True)
    notify("CorridorKey: keying", "%s (%d frames) on %s" % (info["name"], info["frames"], gpu["name"]))
    if info["fusion_comps"]:
        print("WARNING: %s has Fusion comps; the key is made from the untouched source file." % info["name"])

    in_path = os.path.join(out_dir, stem + ".mov")
    rate = make_intermediate(info, in_path)
    run_inference(cfg, job, in_path, out_dir, gpu)
    cutout_ref, alpha_ref = collect_outputs(out_dir, stem, exr)
    if not cfg["keep_intermediate"]:
        os.remove(in_path)

    mp = project.GetMediaPool()
    prev = mp.GetCurrentFolder()
    mp.SetCurrentFolder(ensure_bin(mp))
    try:
        cut = import_clip(mp, cutout_ref, "CK_%s" % stem)
        if alpha_ref is not None:
            import_clip(mp, alpha_ref, "CK_%s_alpha" % stem)
        elif os.path.exists(os.path.join(out_dir, stem + "_alpha.mp4")):
            pass  # alpha preview video is h264 4:4:4; left on disk only
    finally:
        if prev:
            mp.SetCurrentFolder(prev)
    try:
        cut.SetClipProperty("Alpha Mode", "Straight")
    except Exception:
        pass

    cur = project.GetCurrentTimeline()
    if not cfg["place_on_timeline"] or cur is None or cur.GetUniqueId() != job["timeline_id"]:
        return "imported to the %s bin (timeline changed or placement off): %s" % (BIN_NAME, info["name"])
    info2 = describe_item(cur, ref)  # re-resolve: the user may have moved things during the run
    if (info2["rec_start"], info2["frames"]) != (info["rec_start"], info["frames"]):
        return "imported to the %s bin; clip was edited while keying, not placed: %s" % (BIN_NAME, info["name"])
    t = free_track_above(cur, info2["track"], info2["rec_start"], info2["rec_start"] + info2["frames"])
    placed = mp.AppendToTimeline([{"mediaPoolItem": cut, "startFrame": 0, "endFrame": info["frames"] - 1,
                                   "trackIndex": t, "recordFrame": info2["rec_start"], "mediaType": 1}])
    if not placed:
        return "imported to the %s bin but placement failed: %s" % (BIN_NAME, info["name"])
    return "keyed %s -> V%d" % (info["name"], t)


def worker_main(job_path):
    with open(job_path) as f:
        job = json.load(f)
    results = []
    try:
        cfg = load_config()
        import DaVinciResolveScript as dvr
        resolve = dvr.scriptapp("Resolve")
        if resolve is None:
            raise RuntimeError("Resolve not reachable (Preferences > System > General > External scripting = Local)")
        project = resolve.GetProjectManager().GetCurrentProject()
        timeline = project.GetCurrentTimeline() if project else None
        if not timeline or project.GetName() != job["project"] or timeline.GetUniqueId() != job["timeline_id"]:
            raise RuntimeError("project/timeline changed before the worker started")
        gpu = pick_gpu(cfg["gpu"])
        for ref in job["items"]:
            try:
                results.append(key_one(resolve, cfg, job, timeline, ref, gpu))
            except Exception as ex:
                traceback.print_exc()
                results.append("FAILED %s: %s" % (ref["name"], ex))
    except Exception as ex:
        traceback.print_exc()
        results.append("FAILED: %s" % ex)
    failed = any(r.startswith("FAILED") for r in results)
    with open(os.path.join(os.path.dirname(job_path), "result.json"), "w") as f:
        json.dump(results, f, indent=2)
    notify("CorridorKey " + ("finished with errors" if failed else "done"), "; ".join(results), failed)
