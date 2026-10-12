"""QUESTIONS.md: the questions the agent couldn't answer, in a file you answer by typing after "Answer:".

One file for all your profiles (data/QUESTIONS.md when your profiles live in data/<name>/). Every run reads the
answered ones as saved answers, so the next form that asks the same question fills it without asking.

A running batch and the app both change this file (a batch adds questions, you answer them), so every change goes
through update(): under a lock file, read, change, written whole and swapped in at once. Two writers can't lose
each other's work, and a reader never sees half a file.
"""

import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

HEADER = """# Questions the agent couldn't answer

Type your answer after **Answer:** and save the file. Every later run uses it on every form that asks the same
question: typed into text boxes, chosen in dropdowns, radios and Yes/No buttons. For a question with options,
write one of them exactly, or its number. Leave an answer empty to keep the question open.
"""
HEADING = re.compile(r"^###\s+(?:\d+\.\s+)?(.+?)\s*$")
FIELD = re.compile(r"^-\s+\*{0,2}(Options|Asked on|Added|Answer)\*{0,2}:\s?(.*)$")
NUMBERING = re.compile(r"(^|·\s*)\d+\)\s*")  # "1) Yes · 2) No": the numbers only, not digits inside an option
LOCK_WAIT_S = 10
LOCK_STALE_S = 30  # a lock left by a crashed process is taken over after this long


def path_for(profile_dir):
    """data/QUESTIONS.md, shared by data/automation, data/genai...; else next to the profile."""
    profile_dir = Path(profile_dir)
    shared = profile_dir.parent
    return shared / "QUESTIONS.md" if (shared / "profile.example.json").is_file() else profile_dir / "QUESTIONS.md"


def option_for(answer, options):
    """The option an answer names: the option itself, or its number ("2" for the second). A number that is how an
    option starts ("3" for "3 years") means that option, not the third one."""
    if not options or not answer:
        return answer
    if answer in options:
        return answer
    if answer.isdigit():
        starts = [o for o in options if re.match(rf"{answer}(?!\d)", o.strip())]
        if len(starts) == 1:
            return starts[0]
        if 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
    return answer


def parse(text):
    entries = []
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            entries.append({"question": heading.group(1), "options": [], "urls": [], "answer": ""})
            continue
        field = FIELD.match(line.strip())
        if not field or not entries:
            continue
        name, value = field.group(1), field.group(2).strip()
        if name == "Options":
            entries[-1]["options"] = [o.strip() for o in re.split(r"\s*·\s*", NUMBERING.sub(r"\1", value)) if o.strip()]
        elif name == "Asked on":
            entries[-1]["urls"] = [u for u in re.split(r",\s*", value) if u.startswith("http")]
        elif name == "Added":
            entries[-1]["added"] = value
        else:
            entries[-1]["answer"] = value
    for entry in entries:
        entry["answer"] = option_for(entry["answer"], entry["options"])
    return entries


def read(path):
    """The questions in the file: [{question, options, urls, answer, added?}], answer '' while open."""
    path = Path(path) if path else None
    if not path or not path.is_file():
        return []
    deadline = time.monotonic() + 10
    while True:
        try:
            return parse(path.read_text(encoding="utf-8"))
        except PermissionError:  # Windows: the file is being swapped in by a writer this very moment
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def render(entries):
    """Open questions first, answered ones after, numbered."""
    open_ = [e for e in entries if not e.get("answer")]
    done = [e for e in entries if e.get("answer")]
    lines = [HEADER, f"## Open ({len(open_)})", ""]
    for title, group in (("", open_), (f"## Answered ({len(done)})", done)):
        if title:
            lines += [title, ""]
        for number, entry in enumerate(group, 1):
            lines.append(f"### {number}. {' '.join(entry['question'].split())}")
            if entry.get("options"):
                lines.append("- Options: " + " · ".join(f"{i}) {o}" for i, o in enumerate(entry["options"], 1)))
            if entry.get("urls"):
                lines.append("- Asked on: " + ", ".join(entry["urls"][-5:]))
            if entry.get("added"):
                lines.append(f"- Added: {entry['added']}")
            lines += [f"- Answer: {entry.get('answer') or ''}".rstrip(), ""]
    return "\n".join(lines).rstrip() + "\n"


@contextmanager
def locked(path):
    """Hold <file>.lock while changing the file: another process waits its turn (a stale lock is taken over)."""
    lock = Path(f"{path}.lock")
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"{path} is locked by another process (remove {lock} if nothing is running)")
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def save(path, entries):
    """Write the whole file to a temporary one and swap it in (files.write_text): a reader never sees half a file."""
    from .files import write_text

    write_text(path, render(entries))


def write(path, entries):
    """Replace the file's questions with `entries` (under the lock)."""
    with locked(path):
        save(path, entries)


def update(path, change):
    """Read, change and write the file as one step no other writer can interleave with. `change(entries)` edits
    the list in place or returns a new one; update returns the entries written."""
    with locked(path):
        entries = read(path)
        before = render(entries) if Path(path).is_file() else None
        changed = change(entries)
        entries = entries if changed is None else changed
        # Nothing changed: the file is left as it is (and not created for nothing).
        if (render(entries) != before) if before is not None else bool(entries):
            save(path, entries)
        return entries
