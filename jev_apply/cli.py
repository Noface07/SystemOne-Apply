"""jev-apply run <job-url> | jev-apply batch <file of urls> | jev-apply check"""

import argparse
import os
import sys
from pathlib import Path

from .policy import Policy
from .profile import Profile, source_limit


def load_environment(path=Path(".env")):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def load(args):
    data = Path(args.data)
    policy = Policy.load(data / "policy.json")
    if getattr(args, "submit", False):
        from dataclasses import replace

        policy = replace(policy, auto_submit=True)
    learned = data / policy.learned_answers
    profile_path = data / "profile.json"
    if not profile_path.exists():
        sys.exit(f"No {profile_path}. Copy data/profile.example.json to it and fill in your details.")
    profile = Profile.load(profile_path, data / "answers.json", learned)
    email = profile.facts.get("personal.email")
    if email and email.value.lower().endswith("@example.com"):
        # The copied example, never edited: every form would get the example person's details.
        print(
            f"WARNING: {profile_path} still holds the example profile ({email.value}). Put your own details in it, "
            "or point --data at the folder with your real profile.",
            file=sys.stderr,
        )
    return profile, policy


def backend_problem():
    """What stops the configured decision model from running, or None."""
    name = (os.environ.get("DECISION_BACKEND", "laya") or "laya").strip().lower()
    if name == "typesafe":
        return None if os.environ.get("TYPESAFE_API_KEY") else "set TYPESAFE_API_KEY in .env (see .env.example)"
    if name == "llm":
        from .llm_backend import settings as llm_settings

        s = llm_settings()
        return None if s["key"] and s["model"] else "set DECISION_MODEL and DECISION_API_KEY (or TEXT_MODEL_*) in .env"
    if name != "laya":
        return f"DECISION_BACKEND must be 'laya', 'typesafe' or 'llm', not {name!r}"
    from .laya_backend import installed

    return None if installed() else "Laya isn't installed: run `uv sync --extra laya`"


def check(args):
    profile, policy = load(args)
    print(f"{len(profile.facts)} facts and saved answers (limit {source_limit()}):\n")
    for fact in profile.facts.values():
        print(f"  {fact.key:<48} {fact.value[:34]!r:<36} {fact.about[:70]}")
    print("\nDocuments:")
    for name, doc in profile.documents.items():
        print(f"  ok       {name:<16} {doc.value}")
    for name, doc in profile.missing_documents.items():
        print(f"  MISSING  {name:<16} {doc.value}")
    print(f"\nPolicy: drafts={policy.drafts}, value margin {policy.min_value_margin}, max {policy.max_actions} actions")
    problem = backend_problem()
    name = os.environ.get("DECISION_BACKEND", "laya") or "laya"
    print(f"Decision model: {name}", f"- {problem}" if problem else "- ready")
    if name == "llm" and not problem:
        from .llm_backend import settings as llm_settings

        s = llm_settings()
        print(
            f"  model {s['model']} at {s['base']}"
            + (f", paced {s['min_interval']}s apart" if s["min_interval"] else "")
        )
    if name == "laya" and not problem:
        from .laya_backend import settings

        s = settings()
        checkpoint = s["repo"] + (f"/{s['checkpoint']}" if s["checkpoint"] else "")
        print(
            f"  checkpoint {checkpoint}, device {s['device'] or 'auto'}, max_len {s['max_len']},"
            f" head_max_len {s['head_max_len']}, shortlist {s['max_options']}"
        )
        if policy.min_value_margin <= 0.3 and policy.min_operation_probability <= 0.6:
            print("  Tip: Laya is over-confident as shipped; stricter data/policy.json thresholds are safer (README).")
    if (os.environ.get("TEXT_MODEL_PROVIDER") or "").strip().lower() == "claude-code":
        import shutil

        found = os.environ.get("CLAUDE_CODE_BIN") or shutil.which("claude")
        print(
            "Drafting: Claude Code CLI,", f"model {os.environ.get('TEXT_MODEL') or 'sonnet'}" if found else "NOT FOUND"
        )
        return
    print(
        "TEXT_MODEL_API_KEY:",
        "set" if os.environ.get("TEXT_MODEL_API_KEY") else "not set (open questions will be asked)",
    )


def prepare_model():
    problem = backend_problem()
    if problem:
        sys.exit(problem[0].upper() + problem[1:] + ".")
    if (os.environ.get("DECISION_BACKEND", "laya") or "laya").strip().lower() == "laya":
        from .laya_backend import agent as load_laya

        print("Loading Laya (the first run downloads the checkpoint from Hugging Face and can take a few minutes)...")
        try:
            load_laya()
        except Exception as error:  # download, disk or device problems: nothing has been opened yet
            sys.exit(f"Couldn't load Laya: {error}")


def run(args):
    from .agent import Agent
    from .browser import Browser
    from .ui import TerminalUI, UnattendedUI

    profile, policy = load(args)
    prepare_model()
    from .transport import ChromeTransport, PlaywrightTransport

    try:
        transport = PlaywrightTransport(args.browser_profile) if args.browser == "playwright" else ChromeTransport()
    except ImportError:
        sys.exit(
            "Playwright isn't installed: run `uv sync --extra playwright` and `uv run playwright install chromium`, "
            "or use --browser chrome."
        )
    browser = Browser(args.url, transport)
    ui = UnattendedUI() if args.unattended else TerminalUI(step=args.step)
    try:
        report = Agent(browser, profile, policy, ui, note=args.note, run_dir=args.runs).run()
    except KeyboardInterrupt:
        print("\nStopped. Nothing was submitted.")
        report = {"status": "interrupted"}
    if args.browser == "playwright":
        input("\nThe browser stays open for your review. Press Enter here to close it once you're done: ")
        browser.close()
    return 0 if report["status"] == "review" else 1


def batch(args):
    """Fill several jobs, one tab each, without asking anything. Nothing is submitted: every tab stays open for
    you to review and submit, and a summary lists what each one still needs from you."""
    from .agent import Agent
    from .browser import Browser
    from .transport import ChromeTransport, PlaywrightTransport
    from .ui import UnattendedUI

    lines = Path(args.file).read_text(encoding="utf-8").splitlines()
    urls = [u.strip() for u in lines if u.strip() and not u.strip().startswith("#")]
    if not urls:
        sys.exit(f"No URLs in {args.file} (one per line; lines starting with # are ignored).")
    profile, policy = load(args)
    prepare_model()
    transport = None
    if args.browser == "playwright":
        try:
            transport = PlaywrightTransport(args.browser_profile)
        except ImportError:
            sys.exit("Playwright isn't installed: run `uv sync --extra playwright --extra laya`.")
    results = []
    try:
        for number, url in enumerate(urls, 1):
            label = f"[{number}/{len(urls)}] "
            print(f"\n{label}{url}")
            ui = UnattendedUI(label)
            try:
                if transport is None:
                    tab = ChromeTransport()  # a new tab in your everyday Chrome, with its logins
                else:
                    tab = transport if number == 1 else transport.new_tab()
                browser = Browser(url, tab)
                report = Agent(browser, profile, policy, ui, note=args.note, run_dir=args.runs).run()
            except KeyboardInterrupt:
                raise
            except Exception as error:  # one broken site must not stop the rest
                print(f"  · {label}failed: {error}")
                report = {"status": "failed", "fills": [], "left_for_you": [], "error": str(error)}
            results.append((url, report, ui))
    except KeyboardInterrupt:
        print("\nStopped the batch.")
    print("\n" + "=" * 72)
    sent = [u for u, r, _ in results if str(r["status"]).startswith(("submitted", "applied"))]
    print(
        f"Batch done. {len(sent)} submitted (auto_submit); the others were not. Each job is in its own tab:\n"
        if sent
        else "Batch done. Nothing was submitted. Each job is in its own tab, in this order:\n"
    )
    for number, (url, report, ui) in enumerate(results, 1):
        drafts = [f["label"] for f in report["fills"] if f["source"] == "draft"]
        left = ui.needs + [f"{x['what']} ({x['why']})" for x in report.get("left_for_you", [])]
        ready = report["status"] == "review" and not left
        state = "READY TO CHECK" if ready else report["status"].upper()
        print(f"{number}. {state}  {url}")
        print(
            f"   {len(report['fills'])} fields filled" + (f"; report: {report['path']}" if report.get("path") else "")
        )
        for label in drafts:
            print(f"   - read the draft for: {label[:80]}")
        for item in left:
            print(f"   - fill yourself: {item[:100]}")
        if ui.stopped or report.get("error"):
            print(f"   - stopped early: {(ui.stopped or report.get('error'))[:100]}")
    pending = max((r.get("pending_total") or 0 for _, r, _ in results), default=0)
    if pending:
        print(
            f"\n{pending} open question(s) are in QUESTIONS.md. Type your answers there (or run `jev-apply answer`):"
            " every later form fills them itself."
        )
    if transport is None:
        print("\nReview each tab in Chrome, fix what's listed and submit the ones you want. Your Chrome stays open.")
        return 0
    input("\nReview each tab, fix what's listed, submit the ones you want. Press Enter here to close the browser: ")
    transport.close()
    return 0


def answer(args):
    """Answer, in the terminal, the open questions of QUESTIONS.md (or edit that file directly)."""
    from . import inbox

    profile, _ = load(args)
    path = profile.questions_path
    questions = inbox.read(path)
    open_ = [q for q in questions if not q["answer"]]
    if not open_:
        print(f"Nothing open in {path}. Unattended runs add the questions they couldn't answer there.")
        return 0
    print(f"{len(open_)} open question(s) in {path}. Enter = skip for now, 'q' = stop.\n")
    for number, entry in enumerate(open_, 1):
        print(f"{number}. {entry['question']}")
        options = entry.get("options") or []
        for i, option in enumerate(options, 1):
            print(f"     {i}. {option}")
        if entry.get("urls"):
            print(f"   (asked on {entry['urls'][0][:90]})")
        reply = input("   Your answer" + (" (number or text)" if options else "") + ": ").strip()
        if reply.lower() in {"q", "quit"}:
            break
        if options and reply.isdigit() and 1 <= int(reply) <= len(options):
            reply = options[int(reply) - 1]
        entry["answer"] = reply
        if reply:
            print(f"   saved: {reply}\n")
    inbox.write(path, questions)
    left = sum(1 for q in questions if not q["answer"])
    print(f"\nSaved in {path}. {left} still open. Later runs fill these on their own.")
    return 0


def main(argv=None):
    load_environment()
    parser = argparse.ArgumentParser(prog="jev-apply", description="Fill job applications from your own data.")
    parser.add_argument("--data", default="data", help="folder with profile.json, answers.json, policy.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="show what the agent knows about you and what's missing")
    sub.add_parser("answer", help="answer, once, the questions unattended runs left for you (reused on every form)")
    start = sub.add_parser("run", help="fill one application and stop before submit")
    start.add_argument("url", help="job posting or application form URL")
    start.add_argument("--note", default="", help="extra instruction, e.g. 'apply for the Pune location'")
    start.add_argument("--step", action="store_true", help="approve every action")
    start.add_argument("--submit", action="store_true", help="submit when complete (asks first unless --unattended)")
    start.add_argument(
        "--unattended", action="store_true", help="never ask: skip what needs you and list it at the end (no submit)"
    )
    start.add_argument(
        "--browser",
        choices=["playwright", "chrome"],
        default="playwright",
        help="playwright: a dedicated profile (default, recommended). chrome: your everyday Chrome",
    )
    start.add_argument("--browser-profile", default="~/.jev-apply/browser")
    start.add_argument("--runs", default="runs", help="where run reports are written")
    many = sub.add_parser("batch", help="fill several jobs unattended, one tab each, and stop before every submit")
    many.add_argument("file", help="text file with one job or application URL per line")
    many.add_argument("--note", default="", help="extra instruction for every job")
    many.add_argument(
        "--submit", action="store_true", help="submit each application that is complete (never twice; see README)"
    )
    many.add_argument(
        "--browser",
        choices=["playwright", "chrome"],
        default="playwright",
        help="playwright: the separate browser (default). chrome: new tabs in your everyday Chrome",
    )
    many.add_argument("--browser-profile", default="~/.jev-apply/browser")
    many.add_argument("--runs", default="runs", help="where run reports are written")
    args = parser.parse_args(argv)
    if args.command == "batch":
        return batch(args)
    if args.command == "check":
        check(args)
        return 0
    if args.command == "answer":
        return answer(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
