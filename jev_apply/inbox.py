"""QUESTIONS.md: the questions the agent couldn't answer, in a file you answer by typing after "Answer:".

One file for all your profiles (data/QUESTIONS.md when your profiles live in data/<name>/). Every run reads the
answered ones as saved answers, so the next form that asks the same question fills it without asking.
"""

import re
from pathlib import Path

HEADER = """# Questions the agent couldn't answer

Type your answer after **Answer:** and save the file. Every later run uses it on every form that asks the same
question: typed into text boxes, chosen in dropdowns, radios and Yes/No buttons. For a question with options,
write one of them exactly, or its number. Leave an answer empty to keep the question open.
"""
HEADING = re.compile(r"^###\s+(?:\d+\.\s+)?(.+?)\s*$")
FIELD = re.compile(r"^-\s+\*{0,2}(Options|Asked on|Answer)\*{0,2}:\s?(.*)$")


def path_for(profile_dir):
    """data/QUESTIONS.md, shared by data/automation, data/genai...; else next to the profile."""
    profile_dir = Path(profile_dir)
    shared = profile_dir.parent
    return shared / "QUESTIONS.md" if (shared / "profile.example.json").is_file() else profile_dir / "QUESTIONS.md"


def read(path):
    """The questions in the file: [{question, options, urls, answer}], answer '' while open."""
    path = Path(path) if path else None
    if not path or not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = HEADING.match(line)
        if heading:
            entries.append({"question": heading.group(1), "options": [], "urls": [], "answer": ""})
            continue
        field = FIELD.match(line.strip())
        if not field or not entries:
            continue
        name, value = field.group(1), field.group(2).strip()
        if name == "Options":
            entries[-1]["options"] = [o.strip() for o in re.split(r"\s*·\s*", re.sub(r"\d+\)\s*", "", value)) if o]
        elif name == "Asked on":
            entries[-1]["urls"] = [u for u in re.split(r",\s*", value) if u.startswith("http")]
        else:
            entries[-1]["answer"] = value
    for entry in entries:
        answer, options = entry["answer"], entry["options"]
        if answer.isdigit() and options and 1 <= int(answer) <= len(options):
            entry["answer"] = options[int(answer) - 1]  # "2" for the second option
    return entries


def write(path, entries):
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
            lines += [f"- Answer: {entry.get('answer') or ''}".rstrip(), ""]
    Path(path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
