"""What the app changes besides a profile's fields: documents (a track's résumé), new tracks and the settings in
.env (keys, model paths). Every change is checked first; nothing outside data/ and .env is written."""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import data

# Accepted documents and the bytes a real one starts with.
DOC_TYPES = {".pdf": b"%PDF", ".docx": b"PK", ".doc": None, ".txt": None, ".md": None}
MAX_DOC = 15 * 2**20


def documents(track):
    """{kind: {path, about, exists, size}} for a track's documents (its résumé, a cover letter...)."""
    folder = data.track_folder(track)
    out = {}
    for kind, entry in (data._read(folder / "profile.json").get("documents") or {}).items():
        entry = entry or {}
        path = folder / str(entry.get("path") or "")
        out[kind] = {
            "path": entry.get("path"),
            "about": entry.get("about"),
            "exists": path.is_file(),
            "size": path.stat().st_size if path.is_file() else None,
        }
    return out


def save_document(track, kind, filename, content, about=None):
    """Store an uploaded file in data/<track>/documents/ and make it that track's `kind` document (a résumé is
    `resume`). PDF, Word or text, up to 15 MB; a PDF or .docx must really be one."""
    folder = data.track_folder(track)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", kind or ""):
        raise ValueError("document kind: lower-case letters, digits and _ (e.g. resume, cover_letter)")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename or "").name).strip(" .") or "document.pdf"
    suffix = Path(name).suffix.lower()
    if suffix not in DOC_TYPES:
        raise ValueError("upload a PDF, Word (.docx/.doc) or text (.txt/.md) file")
    if not content or len(content) > MAX_DOC:
        raise ValueError("the file is empty or larger than 15 MB")
    magic = DOC_TYPES[suffix]
    if magic and not content.startswith(magic):
        raise ValueError(f"that isn't a real {suffix} file")
    docs = folder / "documents"
    docs.mkdir(exist_ok=True)
    (docs / name).write_bytes(content)
    profile = data._read(folder / "profile.json")
    entry = (profile.get("documents") or {}).get(kind) or {}
    default = "Résumé / CV" if kind == "resume" else kind.replace("_", " ").capitalize()
    profile.setdefault("documents", {})[kind] = {
        "path": f"documents/{name}",
        "about": about or entry.get("about") or default,
    }
    write_profile(folder, profile)
    return documents(track)


def remove_document(track, kind):
    """Stop using a document (the file stays in documents/)."""
    folder = data.track_folder(track)
    profile = data._read(folder / "profile.json")
    if kind not in (profile.get("documents") or {}):
        raise ValueError("no such document")
    del profile["documents"][kind]
    write_profile(folder, profile)
    return documents(track)


def write_profile(folder, profile):
    """Write a profile, keeping the previous one in .backups/."""
    target = Path(folder) / "profile.json"
    if target.is_file():
        backups = target.parent / ".backups"
        backups.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        (backups / f"profile-{stamp}.json").write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    data._load.cache_clear()


def create_track(name, base=None):
    """A new résumé track, data/<name>/, starting from another track's shared facts (personal details, work,
    compensation, years per skill...); its headline, skills and documents are left for you to fill."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not slug or len(slug) > 30:
        raise ValueError("give the track a short name, e.g. 'embedded' or 'data-engineering'")
    folder = data.data_dir() / slug
    if folder.exists():
        raise ValueError(f"data/{slug} already exists")
    source = data.track_folder(base) if base else next(iter(data.track_folders()), None)
    profile = data._read(source / "profile.json") if source else {"personal": {}}
    profile["headline"] = {"value": "", "about": "One-line title of this résumé, e.g. 'Embedded Software Engineer'"}
    profile["skills"] = []
    profile["documents"] = {}
    started = data.rel(source) if source else "an empty profile"
    profile["_readme"] = f"Track '{slug}', started from {started}: fill in its headline and skills and add its résumé."
    folder.mkdir(parents=True)
    for extra in ("policy.json", "answers.json"):
        if source and (source / extra).is_file():
            shutil.copyfile(source / extra, folder / extra)
    write_profile(folder, profile)
    return data.rel(folder)


# ---- settings in .env -----------------------------------------------------------------------------------------

SETTINGS = [
    # (key, group, label, secret, help)
    ("TYPESAFE_API_KEY", "Jev (TypeSafe)", "API key", True, "From console.typesafe.ai/keys (for Jev)."),
    ("TYPESAFE_BASE_URL", "Jev (TypeSafe)", "API address", False, "Default https://api.typesafe.ai"),
    ("TYPESAFE_MODEL", "Jev (TypeSafe)", "Model", False, "Default jev-latest."),
    ("CLEF_MODEL_PATH", "Clef-Flash", "Model file (.gguf)", False, "The Clef-Flash GGUF on this PC."),
    ("CLEF_SERVER_BIN", "Clef-Flash", "llama-server.exe", False, "llama.cpp b11430 or newer."),
    ("CLEF_BASE_URL", "Clef-Flash", "Server address", False, "Default http://127.0.0.1:8080"),
    ("CLEF_GPU_LAYERS", "Clef-Flash", "Layers on the GPU", False, "Default 99 (all); lower if the GPU runs out."),
    ("CLEF_MAX_OPTIONS", "Clef-Flash", "Options per question", False, "Default 12."),
    ("CLEF_CONTEXT", "Clef-Flash", "Context / batch size", False, "Default 2048. RAM use grows with it."),
    ("LAYA_DEVICE", "Laya", "Device", False, "Empty = auto (CUDA, then CPU)."),
    ("LAYA_CHECKPOINT", "Laya", "Checkpoint", False, "Empty = English; or multilingual, typed-decisions."),
    ("DECISION_MODEL", "Chat model", "Model", False, "For the chat-model backend, e.g. an OpenRouter model id."),
    ("DECISION_API_KEY", "Chat model", "API key", True, "For the chat-model backend."),
    ("DECISION_BASE_URL", "Chat model", "API address", False, "OpenAI-compatible, e.g. https://openrouter.ai/api/v1"),
    ("TEXT_MODEL_PROVIDER", "Drafting open answers", "Provider", False, "claude-code, or empty for an API."),
    ("TEXT_MODEL", "Drafting open answers", "Model", False, "e.g. sonnet with claude-code."),
    ("TEXT_MODEL_API_KEY", "Drafting open answers", "API key", True, "Only for an OpenAI-compatible API."),
    ("TEXT_MODEL_BASE_URL", "Drafting open answers", "API address", False, "Only for an OpenAI-compatible API."),
    ("JEV_BACKGROUND_TABS", "Browser", "Background tabs", False, "1 keeps job tabs behind yours."),
]  # fmt: skip
SETTING_KEYS = {s[0] for s in SETTINGS} | {"DECISION_BACKEND"}


def settings():
    """Every editable setting and its value. A key is never sent back: only whether it is set and its last 4."""
    out = []
    for key, group, label, secret, about in SETTINGS:
        value = os.environ.get(key) or ""
        item = {"key": key, "group": group, "label": label, "secret": secret, "help": about}
        if secret:
            item["set"] = bool(value)
            item["hint"] = f"…{value[-4:]}" if len(value) >= 12 else ("set" if value else "")
        else:
            item["value"] = value
        out.append(item)
    return out


def set_env(updates):
    """Write settings to .env and to this process (the next batch inherits them). An empty value removes a
    setting; None leaves it as it is (a secret you didn't retype)."""
    for key, value in updates.items():
        if key not in SETTING_KEYS:
            raise ValueError(f"{key} isn't a setting the app edits")
        if value is not None and re.search(r"[\r\n]", str(value)):
            raise ValueError(f"{key}: one line only")
    env = data.ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.is_file() else []
    for key, value in updates.items():
        if value is None:
            continue
        value = str(value).strip()
        at = next((i for i, line in enumerate(lines) if re.match(rf"\s*{re.escape(key)}\s*=", line)), None)
        if value:
            if at is None:
                lines.append(f"{key}={value}")
            else:
                lines[at] = f"{key}={value}"
            os.environ[key] = value
        else:
            if at is not None:
                lines.pop(at)
            os.environ.pop(key, None)
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- company boards (data/boards.txt) ----------------------------------------------------------------------------

BOARD_LINKS = {
    "greenhouse": "https://job-boards.greenhouse.io/{}",
    "lever": "https://jobs.lever.co/{}",
    "ashby": "https://jobs.ashbyhq.com/{}",
}


def boards_path():
    return data.data_dir() / "boards.txt"


def boards():
    """The company boards `scan` reads: data/boards.txt, else the example list (until you edit it)."""
    from ..scan import board

    own = boards_path()
    path = own if own.is_file() else data.data_dir() / "boards.example.txt"
    out = []
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    for line in lines:
        found = board(line)
        if found:
            provider, token = found
            out.append({"entry": f"{provider}:{token}", "provider": provider, "token": token,
                        "url": BOARD_LINKS[provider].format(token)})  # fmt: skip
    return {"boards": out, "file": data.rel(path), "own": own.is_file()}


def _write_boards(entries):
    header = "# Company job boards for `jev-apply scan` and the app: one per line, provider:board or a board link.\n"
    boards_path().write_text(header + "".join(f"{e}\n" for e in entries), encoding="utf-8")


def add_board(text):
    """Add a board from 'greenhouse:acme' or a careers-page link, after checking it exists. Returns (entry, jobs)."""
    import httpx

    from ..scan import board, fetch

    found = board(str(text or ""))
    if not found:
        raise ValueError("paste a Greenhouse, Lever or Ashby board link, or provider:name (e.g. greenhouse:acme)")
    provider, token = found
    entry = f"{provider}:{token}"
    current = [b["entry"] for b in boards()["boards"]]
    if entry in current:
        raise ValueError(f"{entry} is already in your list")
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            jobs = fetch(provider, token, client)
    except (httpx.HTTPError, ValueError, KeyError):
        raise ValueError(f"couldn't read {entry}: check the company name in its careers-page link") from None
    _write_boards(current + [entry])
    return entry, len(jobs)


def remove_board(entry):
    current = [b["entry"] for b in boards()["boards"]]
    if entry not in current:
        raise ValueError("no such board")
    _write_boards([e for e in current if e != entry])
