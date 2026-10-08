"""Cloudflare's Clef-Flash (9B, open weights) as the decision model, served by llama-server on this machine.

Clef is a decision model: it reads the state and the questions and returns a probability for every allowed option
in one forward pass, through the same /v1/systemone API as TypeSafe's Jev. llama.cpp serves it from release
b11430 (v0.6.0) on: `llama-server -m Cloudflare_clef-flash-Q4_K_M.gguf`.

With CLEF_MODEL_PATH and CLEF_SERVER_BIN set, `prepare()` starts that server when nothing answers at CLEF_BASE_URL
and stops it when jev-apply exits. Planner.py still decides each step; Clef only answers which option fits.
"""

import atexit
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

DEFAULT_BASE = "http://127.0.0.1:8080"
_server = None


def settings():
    return {
        "base": (os.environ.get("CLEF_BASE_URL") or DEFAULT_BASE).rstrip("/"),
        "model": os.environ.get("CLEF_MODEL") or "clef-flash",
        "path": os.environ.get("CLEF_MODEL_PATH") or "",
        "bin": os.environ.get("CLEF_SERVER_BIN") or "",
        "gpu_layers": os.environ.get("CLEF_GPU_LAYERS") or "99",
        "context": os.environ.get("CLEF_CONTEXT") or "2048",
        "max_options": int(os.environ.get("CLEF_MAX_OPTIONS") or 12),
        "start_wait_s": float(os.environ.get("CLEF_START_WAIT_S") or 240),
    }


def healthy(base=None):
    """Is a server answering at CLEF_BASE_URL (llama-server's /health says ok once the model is loaded)?"""
    import socket

    base = base or settings()["base"]
    url = urlparse(base)
    try:  # Windows takes ~2 s to refuse a closed local port: a short probe first
        socket.create_connection((url.hostname or "127.0.0.1", url.port or 80), timeout=0.3).close()
    except OSError:
        return False
    try:
        response = httpx.get(base + "/health", timeout=2)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def problem():
    """What stops Clef from answering, or None."""
    s = settings()
    if healthy(s["base"]):
        return None
    if not s["path"] or not s["bin"]:
        return (
            f"nothing answers at {s['base']}: start llama-server with the Clef GGUF, or set CLEF_MODEL_PATH and "
            "CLEF_SERVER_BIN in .env so jev-apply starts it"
        )
    for name, value in (("CLEF_MODEL_PATH", s["path"]), ("CLEF_SERVER_BIN", s["bin"])):
        if not Path(value).is_file():
            return f"{name} doesn't exist: {value}"
    return None


def command(s):
    url = urlparse(s["base"])
    return [
        s["bin"],
        "-m",
        s["path"],
        "--host",
        url.hostname or "127.0.0.1",
        "--port",
        str(url.port or 8080),
        "-ngl",
        s["gpu_layers"],
        "-c",
        s["context"],
        # A decision is one prefill of the whole prompt (no generation): the batch must hold all of it. llama-server
        # reserves batch x vocabulary floats of RAM for it (2048 -> about 2 GB), so keep it to what 12 options need.
        "-b",
        s["context"],
        "-ub",
        s["context"],
        "--alias",
        s["model"],
    ]


def commit_free_gb():
    """Memory Windows can still promise to programs (RAM + page file), in GB; None elsewhere."""
    if os.name != "nt":
        return None
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong)
            for name in ("phys", "phys_free", "commit", "commit_free", "virtual", "virtual_free", "extended")
        ]

    status = Status(length=ctypes.sizeof(Status))
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.commit_free / 2**30


def needed_gb(s):
    """Windows charges the whole mapped model file, plus about 1 MB of output buffer per batch token."""
    return Path(s["path"]).stat().st_size / 2**30 + int(s["context"]) / 1024 + 0.6


def prepare():
    """Start llama-server with Clef when nothing answers yet; wait until it has loaded the model."""
    global _server
    s = settings()
    if healthy(s["base"]):
        return
    free, need = commit_free_gb(), needed_gb(s)
    if free is not None and free < need + 1:
        # Starting anyway starves Chrome and Windows itself (tabs fail to open, programs can't start threads).
        raise RuntimeError(
            f"Clef needs about {need:.1f} GB of memory and Windows can only promise {free:.1f} GB: close some "
            "programs (editor windows, browsers, other sessions) or enlarge the page file, then run again"
        )
    log = Path(os.environ.get("CLEF_SERVER_LOG") or Path.cwd() / "runs" / "clef-server.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as sink:
        _server = subprocess.Popen(command(s), stdout=sink, stderr=subprocess.STDOUT)
    atexit.register(stop)
    deadline = time.monotonic() + s["start_wait_s"]
    while time.monotonic() < deadline:
        if _server.poll() is not None:
            raise RuntimeError(f"llama-server stopped while loading Clef (exit {_server.returncode}); see {log}")
        if healthy(s["base"]):
            return
        time.sleep(1)
    stop()
    raise RuntimeError(f"Clef didn't load within {s['start_wait_s']:.0f}s; see {log}")


def stop():
    global _server
    if _server is not None and _server.poll() is None:
        _server.terminate()
        try:
            _server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server.kill()
    _server = None


def system_one(body):
    """One /v1/systemone request, TypeSafe-shaped answers back. Long choice questions are shortlisted (the exits
    ASK_USER, SKIP_FIELD, DRAFT_ANSWER, REVIEW and BLOCKED always stay) and every dropped option is reported with
    probability 0, so the caller's checks see every option it offered."""
    from .model import fit_options, post_json

    s = settings()
    body, dropped = fit_options(body, s["max_options"])
    result = post_json(
        s["base"] + "/v1/systemone", os.environ.get("CLEF_API_KEY") or "none", {"model": s["model"], **body}
    )
    for qid, ids in dropped.items():
        answer = result.get("answers", {}).get(qid)
        if isinstance(answer, dict) and isinstance(answer.get("probabilities"), dict):
            answer["probabilities"].update({k: 0.0 for k in ids})
    return result
