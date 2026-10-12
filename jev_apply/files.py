"""Writing the files a batch, the app and you share (profiles, applied.json, settings, QUESTIONS.md): to a temporary
file first, then swapped in, so nothing ever reads half a file, and a crash mid-write leaves the old one intact."""

import json
import os
import time
from pathlib import Path

SWAP_WAIT_S = 10  # Windows refuses the swap while another program (a reader, antivirus) has the file open


def write_text(path, text):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    deadline, pause = time.monotonic() + SWAP_WAIT_S, 0.02
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if time.monotonic() > deadline:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(pause)
            pause = min(pause * 1.5, 0.25)


def write_json(path, data, ascii_only=False):
    write_text(path, json.dumps(data, indent=2, ensure_ascii=ascii_only) + "\n")
