"""The whole loop on the local 3-step fixture: real agent, guards, browser and read-back; the decision model
replaced by a small scripted stand-in (fixture-specific rules, NOT a policy). No API keys, no network.

    uv run --extra playwright python scripts/dry_run.py [--interactive] [--headed] [--chromium PATH]

--interactive answers every question in your terminal, exactly as a real run would ask you.
"""

import argparse
import functools
import http.server
import json
import tempfile
import threading
from pathlib import Path

from jev_apply import model
from jev_apply.agent import Agent
from jev_apply.browser import Browser
from jev_apply.policy import Policy
from jev_apply.profile import Profile, safe_id
from jev_apply.transport import PlaywrightTransport
from jev_apply.ui import TerminalUI

ROOT = Path(__file__).resolve().parent.parent
KEYS = {  # label -> profile key, standing in for the model's value-source question
    "First name": "personal.first_name",
    "Last name": "personal.last_name",
    "Email": "personal.email",
    "Mobile number": "personal.phone",
    "Current location": "personal.city",
    "Current designation": "work.current_title",
    "Total experience": "work.total_experience_years",
    "Current CTC (in LPA)": "compensation.current.total_annual_lpa",
    "Expected CTC": "compensation.expected.total_annual",
    "Current fixed salary": "compensation.current.fixed_annual",
    "Earliest start date": "work.earliest_start_date",
    "Graduation": "education.0.end",
    "Why do you want": "DRAFT_ANSWER",
}
OPTIONS = {"Notice period": "30 days", "Gender": "Prefer not to say", "How did you hear": "Company careers page"}


def scripted_choose(page, goal, history, candidate, exclude=()):
    actions = page["actions"]

    def pick(operation, action, p=0.95):
        return {
            "operation": operation,
            "action": action,
            "target": action and action.get("id"),
            "operation_probability": p,
            "target_probability": p if action else None,
            "latency_ms": 0,
        }

    def first(test):
        return next((a for a in actions if test(a)), None)

    rules = [
        ("OPEN_FORM", lambda a: a["kind"] == "frame"),
        ("CLICK", lambda a: a["label"] == "Only necessary cookies"),
        ("CLICK", lambda a: a.get("role") == "option" and a["label"] == candidate["personal.city"]),
        ("CLICK", lambda a: a.get("role") == "option" and a["label"] in candidate["skills"].split(", ")),
        ("TYPE_TEXT", lambda a: a["kind"] == "fill" and not a["value"]),
        ("TYPE_TEXT", lambda a: a["kind"] == "fill" and a["label"].startswith("Key skills")),  # leftover unmatched item
        (
            "SELECT",
            lambda a: (
                a["kind"] == "select"
                and a["label"].startswith("Preferred locations")
                and a["label"].split(" → ")[1] in candidate["preferences.preferred_locations"].split(", ")
            ),
        ),
        ("UPLOAD", lambda a: a["kind"] == "upload" and not a["value"]),
        (
            "SELECT",
            lambda a: (
                a["kind"] == "select"
                and a["current_value"] == "Select…"
                and any(a["label"].startswith(k) and a["label"].endswith(" → " + v) for k, v in OPTIONS.items())
            ),
        ),
        ("SET_DATE", lambda a: a["kind"] == "setdate" and not a["value"]),
        (
            "CLICK",
            lambda a: (
                a["label"] == "Yes"
                and "relocate" in a.get("context", "")
                and not any(b.get("checked") == "true" for b in actions if "relocate" in b.get("context", ""))
            ),
        ),
        (
            "CLICK",
            lambda a: (
                a["label"] == "No"
                and a.get("pressed") == "false"
                and "visa" in a.get("context", "")
                and not any(b.get("pressed") == "true" for b in actions if "visa" in b.get("context", ""))
            ),
        ),
        ("CLICK", lambda a: a.get("role") == "checkbox" and a.get("checked") == "false"),
        ("SCROLL_DOWN", lambda a: a["id"] == "scroll_down"),
        ("CLICK", lambda a: a["label"] in {"Next", "Save and continue", "Submit application"}),
    ]
    for operation, test in rules:
        action = first(test)
        if action:
            return pick(operation, action)
    return pick("BLOCKED", None)


def scripted_value(action, page, profile, history, dates_only=False, exclude=()):
    if action["label"].startswith("Key skills"):  # next skill not tried in this field yet, then done
        items = [i for i, f in profile.sources(exclude=exclude).items() if f.key.startswith("skills.item_")]
        choice = items[0] if items else "SKIP_FIELD"
        return {"choice": choice, "probabilities": {choice: 0.95}, "margin": 0.9, "ranked": [(choice, 0.95)]}
    key = next((v for k, v in KEYS.items() if action["label"].startswith(k)), "ASK_USER")
    choice = key if key.isupper() else safe_id(key)
    return {"choice": choice, "probabilities": {choice: 0.95}, "margin": 0.9, "ranked": [(choice, 0.95)]}


def scripted_document(action, page, documents):
    choice = "resume" if "Resume" in action.get("context", "") else "SKIP_FIELD"
    return {"choice": choice, "probabilities": {choice: 0.95}, "margin": 0.9, "ranked": [(choice, 0.95)]}


class AutoUI(TerminalUI):
    """Answers like a cooperative candidate so the run completes unattended."""

    def approve(self, what, why=""):
        print(f"  [auto-approve] {what} ({why})")
        return True

    def is_final_submit(self, label):
        print(f"  [auto] '{label}' is the final submit -> stop for review")
        return True

    def pick(self, question, options):
        return options[0][0]

    def ask(self, question, hint=""):
        print(f"  [auto-answer] {question}")
        return "Your payments work matches what I have built for four years.", False

    def edit_draft(self, question, draft):
        return draft

    def handover(self, reason):
        print(f"  [handover] {reason} -> stopping")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--submit", action="store_true", help="auto_submit on: the test form is submitted")
    args = parser.parse_args()
    model.choose, model.value_source, model.document_choice = scripted_choose, scripted_value, scripted_document
    model.draft_text = lambda *a: (None, {"reason": "dry run has no text model"})

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "fixtures"))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    work = Path(tempfile.mkdtemp())
    (work / "resume.pdf").write_bytes(b"%PDF-1.4\n% dry run\n")
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    data["documents"] = {"resume": {"path": str(work / "resume.pdf"), "about": "Résumé / CV"}}
    profile = Profile(data, work)

    transport = PlaywrightTransport(profile_dir=None, headless=not args.headed, executable_path=args.chromium)
    browser = Browser(f"http://localhost:{server.server_address[1]}/careers.html", transport)
    browser.call("Emulation.setDeviceMetricsOverride", width=1200, height=800, deviceScaleFactor=1, mobile=False)
    ui = TerminalUI() if args.interactive else AutoUI()
    try:
        policy = Policy(auto_submit=args.submit)
        report = Agent(browser, profile, policy, ui, run_dir=work / "runs").run()
        submitted = "submitted" in browser.evaluate("location.href")
    finally:
        browser.close()
        server.shutdown()
    print(
        f"\nstatus={report['status']}  fields={len(report['fills'])}  unconfirmed={report['unconfirmed']}  "
        f"not_added={report['not_added']}  submitted={submitted}"
    )
    expected = ("submitted", True) if args.submit else ("review", False)
    ok = (report["status"], submitted) == expected and not report["unconfirmed"]
    raise SystemExit(0 if ok and report["not_added"] == ["Kafka"] else 1)  # the fixture has no "Kafka" option


if __name__ == "__main__":
    main()
