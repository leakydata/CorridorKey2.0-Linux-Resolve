#!/usr/bin/env python3
"""corridorkey-resolve: drive CorridorKey on the clips of the timeline open in DaVinci Resolve Studio (Linux).

For scripts, AI agents and MCP wrappers; the menu entries do the same thing for people. Every command takes
--json and then prints one JSON object on stdout (errors too: {"ok": false, "error": ...}).

    ck.py doctor                          check the install, the GPU and the connection to Resolve
    ck.py list                            clips on the current timeline: id, track, range, keyable or why not
    ck.py key [--mode video|exr] [--clip NAME]... [--track N]... [--id UID]... [--wait]
                                          key clips (no filter = the selection, else the clip under the playhead)
    ck.py status [JOB_ID] [--wait]        a job's state/results (default: newest job)
    ck.py jobs [-n 10]                    recent jobs

Exit codes: 0 ok, 1 finished with failures / doctor found problems, 2 usage or environment error, 3 --wait timed out.
Needs Resolve running with Preferences > System > General > External scripting using: Local.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import corridorkey_resolve as cr  # noqa: E402
from corridorkey_resolve import CKError  # noqa: E402

RESOLVE_PYTHON = "/opt/resolve/bin/ResolvePython"


# --------------------------------------------------------------------------- connection

def connect():
    """Return the Resolve scripting object, or raise CKError with a hint."""
    os.environ.setdefault("RESOLVE_SCRIPT_API", cr.RESOLVE_API)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", cr.RESOLVE_LIB)
    mods = os.path.join(cr.RESOLVE_API, "Modules")
    if mods not in sys.path:
        sys.path.insert(0, mods)
    try:
        import DaVinciResolveScript as dvr
        resolve = dvr.scriptapp("Resolve")
    except Exception as ex:
        me = os.path.realpath(sys.executable)
        if os.path.exists(RESOLVE_PYTHON) and me != os.path.realpath(RESOLVE_PYTHON) and not os.environ.get("CK_REEXEC"):
            # this python can't load fusionscript.so (version mismatch): retry under Resolve's own interpreter
            os.environ["CK_REEXEC"] = "1"
            os.execv(RESOLVE_PYTHON, [RESOLVE_PYTHON, os.path.abspath(__file__)] + sys.argv[1:])
        raise CKError("Cannot load Resolve's scripting module", str(ex))
    if resolve is None:
        raise CKError("Resolve not reachable",
                      "Start DaVinci Resolve Studio and set Preferences > System > General > External scripting "
                      "using: Local.")
    return resolve


def current(resolve):
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        raise CKError("No project open in Resolve")
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise CKError("Open a timeline first")
    return project, timeline


def video_items(timeline):
    out = []
    for track in range(1, int(timeline.GetTrackCount("video")) + 1):
        for it in timeline.GetItemListInTrack("video", track) or []:
            out.append((track, it))
    return out


# --------------------------------------------------------------------------- commands (return JSON-able dicts)

def api_list(resolve):
    project, timeline = current(resolve)
    clips = []
    for track, it in video_items(timeline):
        d = {"id": it.GetUniqueId(), "name": it.GetName(), "track": track, "type": it.GetType(),
             "rec_start": int(it.GetStart()), "rec_end": int(it.GetEnd()), "frames": int(it.GetDuration())}
        if d["type"] == "transition":
            d.update(keyable=False, reason="transition")
        else:
            try:
                info = cr.describe_item(timeline, {"id": d["id"]})
                d.update(keyable=True, source=info["path"], src_start=info["src_start"], src_end=info["src_end"])
            except ValueError as ex:
                d.update(keyable=False, reason=str(ex))
        clips.append(d)
    return {"ok": True, "project": project.GetName(), "timeline": timeline.GetName(),
            "fps": float(timeline.GetSetting("timelineFrameRate") or 0), "clips": clips}


def pick_items(timeline, ids=(), names=(), tracks=()):
    """Resolve filters to timeline items. ids OR'd; names and tracks AND'd together."""
    pool = [(t, it) for t, it in video_items(timeline) if it.GetType() != "transition"]
    if ids:
        chosen = [it for _, it in pool if it.GetUniqueId() in set(ids)]
        missing = [i for i in ids if i not in {it.GetUniqueId() for it in chosen}]
        if missing:
            raise CKError("Clip not found", "no timeline item with id: %s" % ", ".join(missing))
        return chosen
    chosen = [(t, it) for t, it in pool
              if (not names or it.GetName() in names) and (not tracks or t in tracks)]
    if not chosen:
        raise CKError("Clip not found", "no clip matches name=%s track=%s" % (list(names) or "any", list(tracks) or "any"))
    missing = [n for n in names if not any(it.GetName() == n for _, it in chosen)]
    if missing:
        raise CKError("Clip not found", "no clip named: %s" % ", ".join(missing))
    return [it for _, it in chosen]


def job_state(job_dir):
    """running | done | failed, plus parsed results."""
    res_path = os.path.join(job_dir, "result.json")
    if not os.path.exists(res_path):
        return "running", None, None
    with open(res_path) as f:
        results = json.load(f)
    details = None
    dp = os.path.join(job_dir, "result_detail.json")
    if os.path.exists(dp):
        with open(dp) as f:
            details = json.load(f)
    failed = any(str(r).startswith("FAILED") for r in results)
    return ("failed" if failed else "done"), results, details


def log_tail(job_dir, n=6):
    try:
        with open(os.path.join(job_dir, "worker.log"), errors="ignore") as f:
            lines = [l.rstrip() for l in f.read().splitlines() if l.strip() and not l.startswith("[CorridorKey]")]
    except FileNotFoundError:
        return []
    return [l[:200] for l in lines[-n:]]


def api_status(job_id=None, wait=False, timeout=3600):
    jobs_dir = os.path.join(cr.STATE_DIR, "jobs")
    if job_id is None:
        ids = sorted(os.listdir(jobs_dir)) if os.path.isdir(jobs_dir) else []
        if not ids:
            raise CKError("No jobs yet")
        job_id = ids[-1]
    job_dir = os.path.join(jobs_dir, job_id)
    if not os.path.isfile(os.path.join(job_dir, "job.json")):
        raise CKError("Unknown job", job_id)
    t0 = time.time()
    state, results, details = job_state(job_dir)
    while wait and state == "running" and time.time() - t0 < timeout:
        time.sleep(2)
        state, results, details = job_state(job_dir)
    with open(os.path.join(job_dir, "job.json")) as f:
        job = json.load(f)
    out = {"ok": state != "failed", "job_id": job_id, "state": state, "mode": job["mode"],
           "project": job["project"], "timeline": job["timeline_name"], "clips": [i["name"] for i in job["items"]],
           "dir": job_dir, "log": os.path.join(job_dir, "worker.log")}
    if state == "running":
        out["log_tail"] = log_tail(job_dir)
    else:
        out["results"] = details if details is not None else results
    return out


def api_key(resolve, mode="video", ids=(), names=(), tracks=(), wait=False, timeout=3600):
    if mode not in ("video", "exr"):
        raise CKError("Bad mode", "mode must be 'video' or 'exr'")
    items = None
    if ids or names or tracks:
        _, timeline = current(resolve)
        items = pick_items(timeline, ids, names, tracks)
    job = cr.create_job(resolve, mode, items)
    out = {"ok": True, "job_id": job["id"], "dir": job["dir"], "mode": mode,
           "clips": [i["name"] for i in job["items"]]}
    if wait:
        out = api_status(job["id"], wait=True, timeout=timeout)
        if out["state"] == "running":
            out["timed_out"] = True
    return out


def api_jobs(n=10):
    jobs_dir = os.path.join(cr.STATE_DIR, "jobs")
    ids = sorted(os.listdir(jobs_dir))[-n:] if os.path.isdir(jobs_dir) else []
    rows = []
    for jid in reversed(ids):
        jd = os.path.join(jobs_dir, jid)
        try:
            with open(os.path.join(jd, "job.json")) as f:
                job = json.load(f)
        except Exception:
            continue
        state, results, _ = job_state(jd)
        rows.append({"job_id": jid, "state": state, "mode": job["mode"], "project": job["project"],
                     "clips": [i["name"] for i in job["items"]], "results": results})
    return {"ok": True, "jobs": rows}


def api_doctor():
    checks = []

    def add(name, ok, detail="", required=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})

    cfg = cr.load_config()
    add("config file", os.path.exists(cr.CONFIG_PATH), cr.CONFIG_PATH + ("" if os.path.exists(cr.CONFIG_PATH)
                                                                      else "  (run install.py)"))
    ck = cfg["ck_dir"]
    add("CorridorKey checkout", ck and os.path.isfile(os.path.join(ck, "infer.py")), ck or "ck_dir not set")
    py = cfg["python"] or (os.path.join(ck, "venv", "bin", "python") if ck else "")
    add("CorridorKey python", py and os.path.isfile(py), py)
    w = os.path.join(ck, "weights", "corridorkey_v2.safetensors") if ck else ""
    add("model weights", w and os.path.isfile(w), w if w and os.path.isfile(w) else "missing: run scripts/download_weights.py")
    from shutil import which
    for exe in ("ffmpeg", "ffprobe", "nvidia-smi"):
        add(exe, which(exe), which(exe) or "not on PATH")
    add("notify-send", which("notify-send"), which("notify-send") or "optional: desktop notifications",
        required=False)
    gpu = None
    try:
        gpu = cr.pick_gpu(cfg["gpu"])
        add("GPU", True, "%s (%d MB, compute %.1f)" % (gpu["name"], gpu["mem"], gpu["cc"]))
    except Exception as ex:
        add("GPU", False, str(ex))
    if gpu and py and os.path.isfile(py):
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=gpu["uuid"])
        r = subprocess.run([py, "-c", "import torch; print(torch.cuda.is_available(), torch.version.cuda)"],
                           capture_output=True, text=True, env=env)
        out = r.stdout.strip()
        add("torch sees the GPU", r.returncode == 0 and out.startswith("True"), out or r.stderr.strip()[-200:])
    stubs = [os.path.join(os.path.expanduser("~/.local/share/DaVinciResolve/Fusion/Scripts/Utility"), n)
             for n in ("CorridorKey - Key selected clip.py", "CorridorKey - Key selected clip (HDR EXR).py")]
    add("Resolve menu entries", all(os.path.exists(p) for p in stubs),
        "Workspace > Scripts > Utility" if all(os.path.exists(p) for p in stubs) else "missing (run install.py)",
        required=False)
    try:
        resolve = connect()
        name = resolve.GetProductName() if hasattr(resolve, "GetProductName") else "?"
        ver = resolve.GetVersionString() if hasattr(resolve, "GetVersionString") else "?"
        add("Resolve scripting", True, "%s %s" % (name, ver))
        add("Resolve Studio", "Studio" in str(name), str(name))
        project = resolve.GetProjectManager().GetCurrentProject()
        add("project open", bool(project), project.GetName() if project else "open a project", required=False)
    except CKError as ex:
        add("Resolve scripting", False, "%s: %s" % (ex.title, ex.body))
    ok = all(c["ok"] for c in checks if c["required"])
    return {"ok": ok, "checks": checks}


# --------------------------------------------------------------------------- output

def human(cmd, out):
    if cmd == "doctor":
        for c in out["checks"]:
            mark = "ok  " if c["ok"] else ("FAIL" if c["required"] else "warn")
            print("[%s] %-24s %s" % (mark, c["name"], c["detail"]))
        print("\nAll required checks passed." if out["ok"] else "\nSome required checks failed.")
    elif cmd == "list":
        print("%s / %s  (%.3f fps)" % (out["project"], out["timeline"], out["fps"]))
        for c in out["clips"]:
            tail = "keyable" if c.get("keyable") else "NOT keyable: " + c.get("reason", "")
            print("  V%d  %-36s %7d frames  id=%s  %s" % (c["track"], c["name"][:36], c["frames"], c["id"], tail))
    elif cmd == "jobs":
        for j in out["jobs"]:
            print("%-18s %-8s %-5s %s" % (j["job_id"], j["state"], j["mode"], ", ".join(j["clips"])))
    else:  # key / status
        print("job %s: %s  (%s, %s)" % (out["job_id"], out.get("state", "started"), out["mode"],
                                        ", ".join(out["clips"])))
        for r in out.get("results") or []:
            print("  " + (r["message"] if isinstance(r, dict) else str(r)))
        if out.get("state") == "running" and out.get("log_tail"):
            print("  ... " + out["log_tail"][-1])
        print("  log: " + out.get("log", os.path.join(out.get("dir", ""), "worker.log")))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="corridorkey-resolve", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true", help="print one JSON object on stdout")
        return p

    add("doctor", "check install, GPU and the Resolve connection")
    add("list", "list clips on the current timeline")
    p = add("key", "key clips")
    p.add_argument("--mode", choices=("video", "exr"), default="video", help="ProRes 4444 (default) or linear EXR")
    p.add_argument("--clip", action="append", default=[], metavar="NAME", help="timeline clip name (repeatable)")
    p.add_argument("--track", action="append", default=[], type=int, metavar="N", help="video track (repeatable)")
    p.add_argument("--id", action="append", default=[], metavar="UID", help="timeline item id from `list`")
    p.add_argument("--wait", action="store_true", help="wait for the job and print its results")
    p.add_argument("--timeout", type=int, default=3600, help="seconds to wait (default 3600)")
    p = add("status", "show a job")
    p.add_argument("job_id", nargs="?")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=int, default=3600)
    p = add("jobs", "recent jobs")
    p.add_argument("-n", type=int, default=10)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "doctor":
            out = api_doctor()
        elif a.cmd == "list":
            out = api_list(connect())
        elif a.cmd == "key":
            out = api_key(connect(), a.mode, a.id, a.clip, a.track, a.wait, a.timeout)
        elif a.cmd == "status":
            out = api_status(a.job_id, a.wait, a.timeout)
        else:
            out = api_jobs(a.n)
    except CKError as ex:
        err = {"ok": False, "error": ex.title, "detail": ex.body}
        if a.json:
            print(json.dumps(err))
        else:
            print("error: %s%s" % (ex.title, (": " + ex.body) if ex.body else ""), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        human(a.cmd, out)
    if out.get("timed_out"):
        return 3
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
