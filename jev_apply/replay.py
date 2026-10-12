"""Real-page replay tests: the pages runs met, kept so every later change is checked against them.

During a run (JEV_CAPTURE, on unless set to 0) each distinct page layout with fields is kept once, up to MAX_PAGES:
the page frozen into static HTML (freeze.js), what the agent read on it, its last steps, your facts then, and the
planner's decision. `scripts/replay.py promote <run>` copies a good run's pages to data/replay/<site>/ (private, like
all of data/), and from then on:

- `scripts/replay.py check` loads each frozen page in a browser, reads it again with today's snapshot.js and runs
  the planner on it: a page read differently (Maersk's whole-section group gave every field "Date of Birth") or a
  different decision is reported, and `--bless` accepts the new reading when it is the better one;
- tests/test_replay.py replays each kept decision through the planner offline, in the normal test run.

Shadow DOM and frames are not kept (their pages are kept without them)."""

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

FREEZE = Path(__file__).with_name("freeze.js").read_text(encoding="utf-8")
# Run on a frozen page once loaded: typed text back into its boxes, the scroll position it was kept at.
RESTORE = """(() => { for (const e of document.querySelectorAll('[data-jev-value]')) e.value = e.dataset.jevValue;
  const [x, y] = (document.documentElement.dataset.jevScroll || '0,0').split(','); scrollTo(+x, +y);
  return document.documentElement.dataset.jevViewport || '1280,900'; })()"""
MAX_PAGES = 25
# What "reading a page" means for the golden comparison: no values (they change with every applicant), no ids.
READING = ("kind", "role", "label", "context", "required", "multiple", "field_id", "section", "input_type")
FIELD_KINDS = {"fill", "select", "setdate", "upload"}


def enabled():
    return (os.environ.get("JEV_CAPTURE") or "1").strip().lower() not in {"0", "false", "no", "off"}


def reading(actions):
    return [{k: a[k] for k in READING if a.get(k) not in (None, "", False)} for a in actions
            if a["kind"] not in {"wait", "frame", "scroll", "key"} and not a.get("nav")]  # fmt: skip


def has_fields(page):
    return any(a["kind"] in FIELD_KINDS or a.get("role") in {"option", "radio", "checkbox"} for a in page["actions"])


def layout_key(page):
    """The same page drawn again with other values is the same layout: kept once."""
    path = re.sub(r"[?#].*", "", page.get("url") or "")
    shape = [(a["kind"], a.get("role"), a["label"][:60]) for a in page["actions"] if a["kind"] != "wait"]
    return hashlib.sha1(json.dumps([path, shape]).encode()).hexdigest()


def plain(d):
    action = d.get("action") or {}
    return {"operation": d.get("operation"), "label": action.get("label"), "kind": action.get("kind"),
            "p": d.get("target_probability")}  # fmt: skip


class Recorder:
    """Keeps a run's pages under runs/pages/<start>-<id>/ (NNN.html frozen, NNN.json what was read and decided)."""

    def __init__(self, run_dir):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
        self.folder = Path(run_dir) / "pages" / stamp
        self.seen, self.count = set(), 0

    def record(self, browser, page, offered, history, candidate, exclude, planned, final):
        if not enabled() or self.count >= MAX_PAGES or not has_fields(page):
            return
        key = layout_key(page)
        if key in self.seen:
            return
        self.seen.add(key)
        try:
            html = browser.evaluate(FREEZE)
        except Exception:  # noqa: BLE001 - a page that can't be frozen is just not kept
            return
        if not html:
            return
        self.count += 1
        self.folder.mkdir(parents=True, exist_ok=True)
        name = f"{self.count:03d}"
        (self.folder / f"{name}.html").write_text(html, encoding="utf-8")
        secret = {a.get("node") for a in page["actions"] if a.get("secret") or a.get("input_type") == "password"}
        record = {
            "url": page.get("url"), "title": page.get("title"), "text": (page.get("text") or "")[:8000],
            "actions": [{**a, "value": "" if a.get("node") in secret else a.get("value")} for a in page["actions"]],
            "history": [{k: v for k, v in h.items() if k != "typed" or "pass" not in str(h.get("action")).lower()}
                        for h in history[-12:]],
            "offered": [a.get("id") for a in offered["actions"]],  # the rest were tried already or left for you
            "candidate": candidate, "exclude": list(exclude),
            "planned": plain(planned), "final": plain(final),
        }  # fmt: skip
        (self.folder / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


def site_of(url):
    host = re.sub(r"^[a-z]+://", "", url or "").split("/")[0].split(":")[0].lower()  # no port: not a folder name
    host = host[4:] if host.startswith("www.") else host
    return re.sub(r"[^a-z0-9.-]", "_", host) or "local"


def promote(pages, dest):
    """Copy a run's kept pages into dest/<site>/ as goldens: what was read there and what the planner decided."""
    pages, dest, kept = Path(pages), Path(dest), []
    for record in sorted(pages.glob("*.json")):
        data = json.loads(record.read_text(encoding="utf-8"))
        folder = dest / site_of(data["url"])
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{pages.name}-{record.stem}"
        shutil.copyfile(record.with_suffix(".html"), folder / f"{name}.html")
        data["expected"] = {"reading": reading(data["actions"]), "decision": data["planned"]}
        (folder / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        kept.append(folder / f"{name}.json")
    return kept


def goldens(dest):
    return sorted(Path(dest).glob("*/*.json"))


def decide(data, actions=None):
    """The planner's decision on a kept page, offline: the model's picks (Clef) are replaced by the decision kept,
    so only the procedure, the rules and the page reading are under test."""
    from . import planner

    actions = actions or data["actions"]
    if data.get("offered") is not None:
        # What the planner was offered then (the page less what was tried or left): matched by what each control is
        offered = {(a["kind"], a["label"], a.get("context")) for a in data["actions"] if a.get("id") in data["offered"]}
        actions = [a for a in actions if (a["kind"], a["label"], a.get("context")) in offered or a["kind"] == "wait"]
    page = {**{k: data.get(k) for k in ("url", "title", "text")}, "actions": actions}
    kept = data["expected"]["decision"]

    def recorded_pick(question, options, candidate):
        option = next((o for o in options if o.get("label") == kept.get("label")), None)
        return (option, kept.get("p") or 0.9, {}) if option else None

    original = planner.laya_pick
    planner.laya_pick = recorded_pick
    try:
        return plain(planner.choose(page, data["candidate"], tuple(data.get("exclude") or ()), data["history"]))
    finally:
        planner.laya_pick = original


def same_decision(a, b):
    return a.get("operation") == b.get("operation") and (a.get("label") or None) == (b.get("label") or None)


def diff(old, new):
    """What reads differently: fields gone, fields new, and the same field read with other words."""
    key = lambda r: (r["kind"], r.get("role"), r["label"])  # noqa: E731
    before, after = {key(r): r for r in old}, {key(r): r for r in new}
    lines = [f"  gone: {k[0]} {k[2]!r}" for k in before if k not in after]
    lines += [f"  new:  {k[0]} {k[2]!r} {after[k].get('context', '')!r}"[:200] for k in after if k not in before]
    for k in before.keys() & after.keys():
        changed = {f: (before[k].get(f), after[k].get(f)) for f in READING if before[k].get(f) != after[k].get(f)}
        if changed:
            lines.append(f"  read differently: {k[2]!r}: " + "; ".join(f"{f} {a!r} -> {b!r}" for f, (a, b) in
                                                                     changed.items())[:300])  # fmt: skip
    return lines
