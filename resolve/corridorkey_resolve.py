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
import threading
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
    "hint": "none",                     # "none" or "person" (auto person matte as a hint, for no-green-screen footage)
    "hint_quality": 0.75,               # infer.py --hint_quality for the automatic matte (rough but useful)
}
HINTS = ("none", "person")
MIN_PERSON_COVERAGE = 0.005             # median share of the frame the person matte must cover

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


def notify(title, body="", urgent=False, key=None, pct=None, icon=None, timeout=8000, log=True):
    """Desktop notification. `key` makes later calls with the same key replace this one in place (one notification
    per job that turns from progress into the result); `pct` draws a progress bar; urgent = critical (stays until
    dismissed on Plasma); timeout in ms (0 = until dismissed)."""
    if log:
        print("[CorridorKey] %s %s" % (title, body), flush=True)
    exe = shutil.which("notify-send")
    if not exe:
        return
    cmd = [exe, "-a", "CorridorKey", "-u", "critical" if urgent else "normal", "-t", str(0 if urgent else timeout)]
    if icon:
        cmd += ["-i", icon]
    if key:
        cmd += ["-h", "string:x-canonical-private-synchronous:corridorkey-" + key]
    if pct is not None:
        cmd += ["-h", "int:value:%d" % int(pct)]
    try:
        subprocess.run(cmd + [title, body], timeout=5, check=False)
    except Exception:
        pass


# --------------------------------------------------------------------------- inside Resolve

class CKError(Exception):
    """A problem to show the user: title + body (becomes a notification from the menu, a message in the CLI)."""
    def __init__(self, title, body=""):
        super().__init__("%s: %s" % (title, body) if body else title)
        self.title, self.body = title, body


def keyable_clips(seq):
    """Drop transitions (they come back alongside clips in a selection; not keyable and not an error)."""
    out = []
    for i in seq or []:
        try:
            if i.GetType() == "transition":
                continue
        except Exception:
            pass
        out.append(i)
    return out


def preflight(cfg=None):
    """Check the environment a job needs. Returns the config; raises CKError with a fix hint."""
    cfg = cfg or load_config()
    if not cfg["ck_dir"] or not os.path.isfile(os.path.join(cfg["ck_dir"], "infer.py")):
        raise CKError("CorridorKey not configured", "Run install.py (ck_dir missing in %s)" % CONFIG_PATH)
    missing = [e for e in ("ffmpeg", "ffprobe", "nvidia-smi") if not shutil.which(e)]
    if missing:
        raise CKError("CorridorKey: missing tools", "%s not found on PATH (Resolve's environment)." % ", ".join(missing))
    venv_py = cfg["python"] or os.path.join(cfg["ck_dir"], "venv", "bin", "python")
    if not os.path.isfile(venv_py):
        raise CKError("CorridorKey not configured", "Python for CorridorKey not found: %s" % venv_py)
    return cfg


def create_job(resolve, mode="video", items=None, hint=None):
    """Write job.json and start the supervised worker. `items` = timeline items to key; None = the timeline's
    selection, else the clip under the playhead (what the menu entries do). `hint` = "none" or "person"
    (None: the config's "hint"). Returns the job dict (with "dir"). Raises CKError for anything the user can fix."""
    cfg = preflight()
    hint = hint or cfg["hint"] or "none"
    if hint not in HINTS:
        raise CKError("CorridorKey", "hint must be one of %s, got %r" % (", ".join(HINTS), hint))
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    if timeline is None:
        raise CKError("CorridorKey", "Open a timeline first.")
    if items is None:
        items = keyable_clips(timeline.GetSelectedClips())
        if not items:
            cur = timeline.GetCurrentVideoItem()
            items = keyable_clips([cur] if cur else [])
        if not items:
            raise CKError("CorridorKey", "Select a clip on the timeline (or park the playhead on one).")
    else:
        items = keyable_clips(items)
        if not items:
            raise CKError("CorridorKey", "No keyable clips given.")
    # A GUI selection also holds the linked audio item (same name); keep each video-track item once.
    video_ids = {it.GetUniqueId() for t in range(1, timeline.GetTrackCount("video") + 1)
                 for it in timeline.GetItemListInTrack("video", t) or []}
    seen, vid = set(), []
    for i in items:
        uid = i.GetUniqueId()
        if uid in video_ids and uid not in seen:
            seen.add(uid)
            vid.append(i)
    if not vid:
        raise CKError("CorridorKey", "Select a video clip (audio-only selections can't be keyed).")
    items = vid

    job_id = time.strftime("%Y%m%d-%H%M%S")
    job_dir = os.path.join(STATE_DIR, "jobs", job_id)
    n = 1
    while os.path.exists(job_dir):  # two jobs in the same second (CLI) must not share a directory
        n += 1
        job_dir = os.path.join(STATE_DIR, "jobs", "%s-%d" % (job_id, n))
    job_id = os.path.basename(job_dir)
    os.makedirs(job_dir)
    job = {
        "id": job_id,
        "mode": mode,
        "hint": hint,
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
    # ResolvePython (bundled CPython, 21.2+) runs in isolated mode and ignores PYTHONPATH, so the module
    # search path is set explicitly inside the worker instead.
    here = os.path.dirname(os.path.abspath(__file__))
    boot = ("import sys; sys.path[:0] = %r; "
            "import corridorkey_resolve as m; m.worker_main(sys.argv[1])" % ([here, RESOLVE_API + "/Modules"],))
    log = open(os.path.join(job_dir, "worker.log"), "ab")
    cmd = [py, "-c", boot, job_path]
    sup = shutil.which("python3")
    if sup:  # a stdlib supervisor reports a failure if the worker dies without writing result.json
        cmd = [sup, "-I", "-c", "import sys; sys.path.insert(0, %r); import corridorkey_resolve as m; "
               "m.supervise(sys.argv[1], sys.argv[2:])" % here, job_path] + cmd
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                     start_new_session=True, close_fds=True)
    job["dir"] = job_dir
    return job


def launch(resolve, mode="video", hint=None):
    """Entry point for the menu stubs. mode: "video" (ProRes 4444) or "exr" (linear half-float EXR);
    hint: "person" for footage without a green screen (None: config default)."""
    try:
        job = create_job(resolve, mode, hint=hint)
        notify("CorridorKey started", "%d clip(s), %s%s. Starting ..." % (
            len(job["items"]), mode, ", person hint" if job["hint"] == "person" else ""),
            key=job["id"], pct=0, icon="applications-multimedia")
    except CKError as ex:
        notify(ex.title, ex.body, True)
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
                if g["cc"] < 8.0:
                    raise RuntimeError("GPU %s (%s) has compute capability %.1f; CorridorKey needs 8.0 or newer "
                                       "(RTX 30-series+). Set \"gpu\" to \"auto\" or another card." %
                                       (g["index"], g["name"], g["cc"]))
                return g
        raise RuntimeError("configured gpu %r not found among %s" % (setting, [g["name"] for g in gpus]))
    ok = [g for g in gpus if g["cc"] >= 8.0]   # upstream: RTX 30-series or newer
    if not ok:
        raise RuntimeError("no CUDA GPU with compute capability >= 8.0 (RTX 30-series+) found")
    return max(ok, key=lambda g: g["mem"])


def run_logged(cmd, env=None, cwd=None, label="", on_line=None):
    print("$ " + " ".join(cmd), flush=True)
    p = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    tail = []
    for line in p.stdout:
        line = line.rstrip("\n")
        print(line, flush=True)
        if on_line:
            try:
                on_line(line)
            except Exception:
                pass
        tail.append(line)
        del tail[:-15]
    rc = p.wait()
    if rc != 0:
        raise RuntimeError("%s failed (exit %d): %s" % (label or cmd[0], rc, " | ".join(tail[-4:])))
    return tail


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
    if not path:
        raise ValueError("compound or nested timeline clips have no source file; keying them is not supported")
    if not os.path.isfile(path):
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
    # GetSourceEndFrame is exclusive in Resolve 21.2 (inclusive in some older builds, per the scripting docs), and
    # for some clips that end on the media's last frame it comes back 2 short while GetSourceEndTime is right.
    # Accept a match from either the frames or the times; the timeline duration is the truth for the count.
    spans = [e - s]
    try:
        spans.append(int(round((float(item.GetSourceEndTime()) - float(item.GetSourceStartTime())) * src_fps)))
    except Exception:
        pass
    if not any(sp in (dur, dur - 1) for sp in spans):
        raise ValueError("source range (%d..%d) doesn't match timeline duration (%d); retimed or in a transition?" % (s, e, dur))
    e = s + dur - 1
    return {"item": item, "mpi": mpi, "track": track, "path": path, "name": item.GetName(),
            "src_start": s, "src_end": e, "rec_start": start, "frames": dur,
            "fusion_comps": item.GetFusionCompCount()}


def estimate_seconds(info):
    """Rough run time: about 0.7 s per megapixel-frame on an RTX 4090 (measured 1080x1920 and 3840x2160)."""
    try:
        w, h = [int(x) for x in str(info["mpi"].GetClipProperty("Resolution")).split("x")[:2]]
    except Exception:
        w, h = 1920, 1080
    return 20 + info["frames"] * w * h / 1e6 * 0.7


def fmt_duration(sec):
    if sec < 90:
        return "about a minute" if sec > 45 else "under a minute"
    if sec < 5400:
        return "about %d min" % round(sec / 60)
    return "about %.1f hours" % (sec / 3600)


def fmt_left(sec):
    """'~2 min left' style, '' when there is little left to say."""
    if sec <= 20:
        return ""
    if sec < 90:
        return ", ~1 min left"
    if sec < 5400:
        return ", ~%d min left" % round(sec / 60)
    return ", ~%.1f h left" % (sec / 3600)


def estimate(info):
    return fmt_duration(estimate_seconds(info))


# --------------------------------------------------------------------------- progress

class Progress:
    """Worker-side progress. Writes progress.json in the job dir (read by `corridorkey-resolve status`, the MCP
    server and read_progress()) and keeps a desktop notification with a progress bar up to date. The file holds the
    state at the last event plus how long the current segment is expected to take, so a reader can interpolate."""

    def __init__(self, job_dir, job_id, clips):
        self.path = os.path.join(job_dir, "progress.json")
        self.job_id = job_id
        self.clips = clips                       # [{"name", "weight", "est"}] in job order
        self.total = sum(c["weight"] for c in clips) or 1
        self.i = 0
        self.last_note = 0.0                     # time of the last notification
        self._stop = threading.Event()
        threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self):
        """Refresh the notification every few seconds from the interpolated progress, so it stays on screen and
        the bar keeps moving between the keyer's coarse stage markers."""
        while not self._stop.wait(4):
            pr = read_progress(os.path.dirname(self.path))
            if pr:
                self._show(pr["pct"], pr["stage"], pr["eta_seconds"], pr["clip_index"], pr["clip_count"])

    def finish(self, failed, results):
        self._stop.set()
        if failed:
            notify("CorridorKey finished with errors", "\n".join(results), urgent=True, key=self.job_id,
                   icon="dialog-error")
        else:
            notify("CorridorKey done  \u2713", "\n".join(results), key=self.job_id, pct=100,
                   icon="dialog-ok-apply", timeout=20000)

    def _show(self, pct, stage, eta, idx, count):
        now = time.time()
        if now - self.last_note < 2:
            return
        self.last_note = now
        body = "%s%s" % (stage, fmt_left(eta))
        if count > 1:
            body = "clip %d/%d: %s" % (idx, count, body)
        notify("CorridorKey %d%%" % int(pct), body, key=self.job_id, pct=pct, icon="applications-multimedia",
               timeout=10000, log=False)

    def clip_start(self, i):
        self.i = i

    def update(self, label, frac, span, expected, clip_eta):
        """frac = share of the current clip done now; span = share it may still advance during this segment
        (taking `expected` seconds); clip_eta = seconds left for this clip."""
        done = sum(c["weight"] for c in self.clips[:self.i])
        w = self.clips[self.i]["weight"] if self.i < len(self.clips) else 0
        pct = 100.0 * (done + w * frac) / self.total
        later = sum(c["est"] for c in self.clips[self.i + 1:])
        d = {"pct": round(pct, 1), "span_pct": round(100.0 * w * span / self.total, 2),
             "segment_seconds": max(1.0, float(expected)), "stage": label,
             "clip": self.clips[self.i]["name"] if self.i < len(self.clips) else "",
             "clip_index": self.i + 1, "clip_count": len(self.clips),
             "eta_seconds": round(clip_eta + later), "updated": time.time()}
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)
        except OSError:
            pass
        self._notify(d)

    def _notify(self, d):
        self._show(d["pct"], d["stage"], d["eta_seconds"], d["clip_index"], d["clip_count"])


_PROG = None


def prog(label, frac, span=0.0, expected=1.0, eta=0.0):
    if _PROG is not None:
        _PROG.update(label, frac, span, expected, eta)


def read_progress(job_dir):
    """Live progress of a running job as a dict (pct, stage, clip, eta_seconds, text), or None."""
    try:
        with open(os.path.join(job_dir, "progress.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    el = max(0.0, time.time() - d["updated"])
    pct = d["pct"] + d["span_pct"] * min(0.95, el / d["segment_seconds"])
    eta = max(0, int(d["eta_seconds"] - el))
    clip = d["clip"] + (" (%d/%d)" % (d["clip_index"], d["clip_count"]) if d["clip_count"] > 1 else "")
    text = "%d%%  %s: %s" % (int(min(pct, 99)), clip, d["stage"])
    text += fmt_left(eta)
    return {"pct": round(min(pct, 99.0), 1), "stage": d["stage"], "clip": d["clip"], "clip_index": d["clip_index"],
            "clip_count": d["clip_count"], "eta_seconds": eta, "text": text}


class _InferProgress:
    """Turns infer.py's stage lines into progress (see the stage timings: load ~30 s, windows ~equal, write)."""

    def __init__(self, base, span, est):
        self.base, self.span, self.est = base, span, est   # this stage's range within the clip, estimated seconds
        self.n = self.k = 0
        self.t_marker = time.time()
        self.per_window = None

    def _set(self, label, f, nxt, expected, eta):
        prog(label, self.base + self.span * f, self.span * (nxt - f), expected, eta)

    def line(self, line):
        now = time.time()
        if line.startswith("Loading CorridorKey"):
            self._set("loading the model", 0.0, 0.10, 30, self.est)
        elif "probed" in line and "frames" in line:
            self._set("analysing the clip", 0.10, 0.17, max(5, self.est * 0.1), self.est * 0.9)
        elif "Hann stage1" in line:
            m = re.search(r"windows=(\d+)", line)
            self.n, self.k, self.t_marker = int(m.group(1)) if m else 1, 0, now
            self._window()
        elif re.match(r"\s*window (\d+)/(\d+)", line):
            m = re.match(r"\s*window (\d+)/(\d+)", line)
            self.k, self.n = int(m.group(1)), int(m.group(2))
            self.per_window, self.t_marker = now - self.t_marker, now
            self._window()
        elif line.strip().startswith("wrote"):
            self._set("saving", 0.97, 1.0, 5, 5)

    def _window(self):
        k, n = self.k, max(self.n, 1)
        f = 0.17 + 0.78 * k / n
        pw = self.per_window or max(5.0, (self.est - 30) / n)
        label = "keying" if n == 1 else "keying (window %d/%d)" % (min(k + 1, n), n)
        self._set(label, f, 0.17 + 0.78 * min(k + 1, n) / n, pw, pw * (n - k) + 10)


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


def run_inference(cfg, job, in_path, out_dir, gpu, stage=(0.0, 1.0, 60.0)):
    ck_dir = cfg["ck_dir"]
    py = cfg["python"] or os.path.join(ck_dir, "venv", "bin", "python")
    tmp = os.path.join(out_dir, "tmp")
    os.makedirs(tmp, exist_ok=True)
    exr = job["mode"] == "exr"
    cmd = [py, os.path.join(ck_dir, "infer.py"), "--input", in_path, "--output_dir", out_dir, "--temp_dir", tmp,
           "--outputs", "cutout", "alpha", "--despill_strength", str(cfg["despill_strength"])]
    if exr:
        cmd += ["--output_format", "exr"]
    hint_dir = None
    if job.get("hint") == "person":
        hint_dir = os.path.join(out_dir, "hint") if cfg["keep_intermediate"] else os.path.join(tmp, "hint")
        cmd += ["--hint_video", hint_dir, "--hint_quality", str(cfg["hint_quality"])]
    cmd += [str(a) for a in cfg["extra_infer_args"]]
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = gpu["uuid"]
    env["PYTHONUNBUFFERED"] = "1"
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "gpu.lock"), "w") as lock:   # queue jobs; Resolve shares this GPU too
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            prog("waiting for another CorridorKey job to finish", stage[0], 0.0, 1, stage[2])
            fcntl.flock(lock, fcntl.LOCK_EX)
        if hint_dir:
            prog("finding the person in every frame", stage[0] - 0.04, 0.04, max(8.0, stage[2] * 0.1), stage[2])
            make_person_hint(py, in_path, hint_dir, env)
        ip = _InferProgress(*stage)
        prog("loading the model", stage[0], stage[1] * 0.1, 30, stage[2])
        run_logged(cmd, env=env, cwd=ck_dir, label="infer.py", on_line=ip.line)
    shutil.rmtree(tmp, ignore_errors=True)


def make_person_hint(py, in_path, hint_dir, env):
    """Write a per-frame person matte (hint_matte.py, torchvision DeepLabV3) for infer.py --hint_video."""
    here = os.path.dirname(os.path.abspath(__file__))
    tail = run_logged([py, os.path.join(here, "hint_matte.py"), "--input", in_path, "--output_dir", hint_dir],
                      env=env, label="person matte")
    summary = {}
    for line in reversed(tail):
        if line.startswith("{"):
            summary = json.loads(line)
            break
    if summary.get("median_coverage", 1.0) < MIN_PERSON_COVERAGE:
        raise RuntimeError("no person found in the clip (the person hint only works on footage with people); "
                           "use the plain 'Key selected clip' entry")


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


def import_clip(mp, ref, name, fps=None):
    if isinstance(ref, tuple):
        d, frames = ref
        m = re.match(r"^(.*?)(\d+)\.exr$", frames[0], re.I)
        pattern = os.path.join(d, "%s%%0%dd.exr" % (m.group(1), len(m.group(2))))
        first, last = int(m.group(2)), int(re.match(r"^(.*?)(\d+)\.exr$", frames[-1], re.I).group(2))
        got = mp.ImportMedia([{"FilePath": pattern, "StartIndex": first, "EndIndex": last}])
        if not got:
            got = mp.ImportMedia([d])
    else:
        got = mp.ImportMedia([ref])  # the {"FilePath": ...} dict form is for sequences only; returns None here
    if not got:
        raise RuntimeError("Resolve refused to import %s" % (ref[0] if isinstance(ref, tuple) else ref))
    clip = got[0]
    try:
        clip.SetClipProperty("Clip Name", name)
        if isinstance(ref, tuple) and fps:
            clip.SetClipProperty("FPS", str(fps))  # image sequences import at Resolve's default 24 fps
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


def _ref_path(ref):
    return ref[0] if isinstance(ref, tuple) else ref


def key_one(resolve, cfg, job, timeline, ref, gpu, detail=None):
    """Key one timeline item. Returns a one-line message; fills `detail` (a dict) with structured facts."""
    detail = detail if detail is not None else {}
    project = resolve.GetProjectManager().GetCurrentProject()
    info = describe_item(timeline, ref)
    hint = job.get("hint", "none")
    detail.update(clip=info["name"], frames=info["frames"], rec_start=info["rec_start"], mode=job["mode"], hint=hint)
    exr = job["mode"] == "exr"
    stem = "%s_f%d-%d%s" % (safe_name(info["name"]), info["src_start"], info["src_end"],
                            "_person" if hint == "person" else "")
    out_dir = os.path.join(cfg["output_root"], safe_name(job["project"]), "%s_%s" % (stem, job["id"]))
    os.makedirs(out_dir, exist_ok=True)
    notify("CorridorKey: keying", "%s (%d frames, %s) on %s" % (info["name"], info["frames"], estimate(info),
                                                                   gpu["name"]))
    if info["fusion_comps"]:
        print("WARNING: %s has Fusion comps; the key is made from the untouched source file." % info["name"])

    in_path = os.path.join(out_dir, stem + ".mov")
    est = estimate_seconds(info)
    prog("trimming the source", 0.0, 0.05, 2 + info["frames"] * 0.02, est)
    rate = make_intermediate(info, in_path)
    base = 0.09 if hint == "person" else 0.05
    run_inference(cfg, job, in_path, out_dir, gpu, stage=(base, 0.93 - base, est))
    prog("importing into Resolve", 0.93, 0.04, 5, 6)
    cutout_ref, alpha_ref = collect_outputs(out_dir, stem, exr)
    detail.update(output_dir=out_dir, cutout=_ref_path(cutout_ref),
                  alpha=_ref_path(alpha_ref) if alpha_ref is not None else os.path.join(out_dir, stem + "_alpha.mp4"))
    if not cfg["keep_intermediate"]:
        os.remove(in_path)

    mp = project.GetMediaPool()
    prev = mp.GetCurrentFolder()
    mp.SetCurrentFolder(ensure_bin(mp))
    try:
        tl_fps = float(timeline.GetSetting("timelineFrameRate") or 0)
        cut = import_clip(mp, cutout_ref, "CK_%s" % stem, tl_fps)
        if alpha_ref is not None:
            import_clip(mp, alpha_ref, "CK_%s_alpha" % stem, tl_fps)
        elif os.path.exists(os.path.join(out_dir, stem + "_alpha.mp4")):
            pass  # alpha preview video is h264 4:4:4; left on disk only
    finally:
        if prev:
            mp.SetCurrentFolder(prev)
    try:
        cut.SetClipProperty("Alpha mode", "Straight")  # already the default for ProRes 4444 on import; kept explicit
    except Exception:
        pass

    cur = project.GetCurrentTimeline()
    if not cfg["place_on_timeline"] or cur is None or cur.GetUniqueId() != job["timeline_id"]:
        return "imported to the %s bin (timeline changed or placement off): %s" % (BIN_NAME, info["name"])
    try:
        info2 = describe_item(cur, ref)  # re-resolve: the user may have moved things during the run
    except ValueError as ex:
        return "imported to the %s bin, not placed (%s): %s" % (BIN_NAME, ex, info["name"])
    if (info2["rec_start"], info2["frames"]) != (info["rec_start"], info["frames"]):
        return "imported to the %s bin; clip was edited while keying, not placed: %s" % (BIN_NAME, info["name"])
    prog("placing on the timeline", 0.97, 0.02, 3, 3)
    t = free_track_above(cur, info2["track"], info2["rec_start"], info2["rec_start"] + info2["frames"])
    n = info["frames"]

    def append(end_frame):
        return mp.AppendToTimeline([{"mediaPoolItem": cut, "startFrame": 0, "endFrame": end_frame,
                                     "trackIndex": t, "recordFrame": info2["rec_start"], "mediaType": 1}])

    # endFrame is exclusive in Resolve 21.2 (inclusive per older docs): try the inclusive form, check the length
    # that actually landed on the timeline, and redo it with the other convention if it is one frame short.
    def length(placed):
        # AppendToTimeline can hand back a truthy list holding an invalid item when nothing was placed
        try:
            return int(placed[0].GetDuration())
        except Exception:
            return -1

    placed = append(n - 1)
    if placed and length(placed) == n - 1:
        cur.DeleteClips(list(placed))
        placed = append(n)
    if not placed or length(placed) != n:
        if placed and length(placed) >= 0:
            cur.DeleteClips([p for p in placed if p])
        return "imported to the %s bin but placement failed: %s" % (BIN_NAME, info["name"])
    detail.update(track=t, placed_item_id=placed[0].GetUniqueId())
    return "keyed %s -> V%d" % (info["name"], t)


def supervise(job_path, worker_cmd):
    """Run the worker; if it dies without writing result.json (crash, kill, import error), say so."""
    job_dir = os.path.dirname(job_path)
    result = os.path.join(job_dir, "result.json")
    try:
        rc = subprocess.call(worker_cmd, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)
    except Exception as ex:
        rc = "could not start worker: %s" % ex
    if not os.path.exists(result):
        msg = "worker exited unexpectedly (%s); see %s" % (rc, os.path.join(job_dir, "worker.log"))
        with open(os.path.join(job_dir, "result_detail.json"), "w") as f:
            json.dump([{"status": "failed", "message": msg}], f, indent=2)
        with open(result, "w") as f:
            json.dump(["FAILED: " + msg], f, indent=2)
        notify("CorridorKey finished with errors", msg, True, key=os.path.basename(job_dir), icon="dialog-error")
        try:  # don't leave an orphaned infer.py (same process group) running on the GPU
            os.killpg(os.getpgrp(), 9)
        except Exception:
            pass


def worker_main(job_path):
    with open(job_path) as f:
        job = json.load(f)
    results, details = [], []
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
        global _PROG
        clips = []
        for ref in job["items"]:
            try:
                inf = describe_item(timeline, ref)
                clips.append({"name": ref["name"], "weight": inf["frames"], "est": estimate_seconds(inf)})
            except Exception:
                clips.append({"name": ref["name"], "weight": 1, "est": 0})
        _PROG = Progress(os.path.dirname(job_path), job["id"], clips)
        for idx, ref in enumerate(job["items"]):
            _PROG.clip_start(idx)
            d = {"clip": ref["name"], "item_id": ref["id"]}
            try:
                msg = key_one(resolve, cfg, job, timeline, ref, gpu, d)
                d["status"] = "keyed" if msg.startswith("keyed") else "imported"
            except Exception as ex:
                traceback.print_exc()
                msg = "FAILED %s: %s" % (ref["name"], ex)
                d["status"] = "failed"
            d["message"] = msg
            results.append(msg)
            details.append(d)
    except Exception as ex:
        traceback.print_exc()
        results.append("FAILED: %s" % ex)
        details.append({"status": "failed", "message": "FAILED: %s" % ex})
    failed = any(r.startswith("FAILED") for r in results)
    out = os.path.dirname(job_path)
    with open(os.path.join(out, "result_detail.json"), "w") as f:   # before result.json, which callers wait on
        json.dump(details, f, indent=2)
    with open(os.path.join(out, "result.json"), "w") as f:
        json.dump(results, f, indent=2)
    if _PROG is not None:
        _PROG.finish(failed, results)
    else:
        notify("CorridorKey " + ("finished with errors" if failed else "done"), "; ".join(results), failed,
               key=job["id"], icon="dialog-error" if failed else "dialog-ok-apply", timeout=20000)
