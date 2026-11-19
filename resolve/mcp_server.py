#!/usr/bin/env python3
"""MCP server for CorridorKey on DaVinci Resolve Studio (Linux). Standard library only, stdio transport.

It sits next to (not inside) Blackmagic's own Resolve MCP and wraps the same functions as the `corridorkey-resolve`
CLI, so an AI client gets typed tools instead of shelling out:

    corridorkey_doctor        check the install, GPU and Resolve connection
    corridorkey_list_clips    clips on the current timeline (ids, tracks, keyable or why not)
    corridorkey_key_clips     start a keying job (returns a job id at once; jobs take minutes)
    corridorkey_job_status    state/results of a job, optionally waiting a bounded time
    corridorkey_recent_jobs   recent jobs

Register it, for example:
    claude mcp add corridorkey-resolve -- python3 /path/to/resolve/mcp_server.py
or in a client's JSON config: {"command": "python3", "args": ["/path/to/resolve/mcp_server.py"]}
Resolve must be running with Preferences > System > General > External scripting using: Local.
"""
import json
import os
import sys
import time
import traceback

os.environ["CK_REEXEC"] = "1"  # never os.execv from inside a stdio server (would corrupt the protocol stream)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ck  # noqa: E402
from corridorkey_resolve import CKError  # noqa: E402

PROTOCOL = "2024-11-05"
MAX_WAIT = 50  # seconds a single status call may block; clients typically time out around 60 s

TOOLS = [
    {"name": "corridorkey_doctor",
     "description": "Check the CorridorKey install (config, weights, ffmpeg, GPU, torch/CUDA, menu entries) and the "
                    "connection to DaVinci Resolve Studio. Run this first when something does not work.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "corridorkey_list_clips",
     "description": "List the video clips on the timeline open in Resolve: id, name, track, record range in frames, "
                    "and whether CorridorKey can key each one (with the reason if not). Use the ids with "
                    "corridorkey_key_clips.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "corridorkey_key_clips",
     "description": "Remove the green/blue screen from timeline clips with CorridorKey. Starts a background job and "
                    "returns its job_id immediately (a few seconds of footage takes about a minute or more on a "
                    "GPU). The keyed result is placed on the first free video track above each clip, aligned to it, "
                    "and also imported into a 'CorridorKey' bin. Poll with corridorkey_job_status. With no "
                    "filter it keys the selection, else the clip under the playhead. Source clips must be plain video "
                    "files at the timeline frame rate, not retimed.",
     "inputSchema": {"type": "object", "properties": {
         "mode": {"type": "string", "enum": ["video", "exr"], "default": "video",
                  "description": "video = ProRes 4444 straight-alpha cutout; exr = linear half-float EXR sequence"},
         "item_ids": {"type": "array", "items": {"type": "string"},
                      "description": "timeline item ids from corridorkey_list_clips (most precise)"},
         "clip_names": {"type": "array", "items": {"type": "string"}, "description": "exact clip names"},
         "tracks": {"type": "array", "items": {"type": "integer"}, "description": "video track numbers"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "corridorkey_job_status",
     "description": "State of a keying job: running, done or failed, with per-clip results (placed track, output "
                    "files, message). Omit job_id for the newest job. wait_seconds (max %d) blocks until the job "
                    "finishes or the time is up." % MAX_WAIT,
     "inputSchema": {"type": "object", "properties": {
         "job_id": {"type": "string"}, "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "corridorkey_recent_jobs",
     "description": "Recent CorridorKey jobs, newest first.",
     "inputSchema": {"type": "object", "properties": {"n": {"type": "integer", "default": 10}}},
     "annotations": {"readOnlyHint": True}},
]


def call_tool(name, a):
    if name == "corridorkey_doctor":
        return ck.api_doctor()
    if name == "corridorkey_list_clips":
        return ck.api_list(ck.connect())
    if name == "corridorkey_key_clips":
        return ck.api_key(ck.connect(), a.get("mode", "video"), a.get("item_ids") or (),
                          a.get("clip_names") or (), a.get("tracks") or (), wait=False)
    if name == "corridorkey_job_status":
        wait = max(0, min(int(a.get("wait_seconds") or 0), MAX_WAIT))
        return ck.api_status(a.get("job_id"), wait=wait > 0, timeout=wait)
    if name == "corridorkey_recent_jobs":
        return ck.api_jobs(int(a.get("n") or 10))
    raise CKError("Unknown tool", name)


def handle(msg):
    """Return a response dict, or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notification (initialized, cancelled, ...)
        return None
    try:
        if method == "initialize":
            res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "corridorkey-resolve", "version": "1.0"}}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                out = call_tool(params.get("name"), params.get("arguments") or {})
                res = {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "isError": False}
            except CKError as ex:
                res = {"content": [{"type": "text", "text": json.dumps(
                    {"ok": False, "error": ex.title, "detail": ex.body})}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    except Exception as ex:
        traceback.print_exc(file=sys.stderr)
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "internal error: %s" % ex}],
                                                           "isError": True}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(ex)}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}),
                  flush=True)
            continue
        resp = handle(msg)
        if resp is not None:
            print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
