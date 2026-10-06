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
    if name == "clef":
        from .clef_backend import problem

        return problem()
    if name == "llm":
        from .llm_backend import settings as llm_settings

        s = llm_settings()
        return None if s["key"] and s["model"] else "set DECISION_MODEL and DECISION_API_KEY (or TEXT_MODEL_*) in .env"
    if name != "laya":
        return f"DECISION_BACKEND must be 'laya', 'clef', 'typesafe' or 'llm', not {name!r}"
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
    if not problem:
        from . import model

        print(f"  steering: {model.steering()} (DECISION_STEERING)")
    if name == "llm" and not problem:
        from .llm_backend import settings as llm_settings

        s = llm_settings()
        print(
            f"  model {s['model']} at {s['base']}"
            + (f", paced {s['min_interval']}s apart" if s["min_interval"] else "")
        )
    if name == "clef" and not problem:
        from .clef_backend import healthy, settings

        s = settings()
        where = "running" if healthy(s["base"]) else f"started on demand from {s['path']}"
        print(f"  {s['model']} at {s['base']} ({where}), shortlist {s['max_options']}")
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
    if (os.environ.get("DECISION_BACKEND", "laya") or "laya").strip().lower() == "clef":
        from .clef_backend import healthy, prepare

        if not healthy():
            print("Starting Clef (llama-server; loading 6 GB onto the GPU takes a minute)...")
        try:
            prepare()
        except Exception as error:  # nothing has been opened yet
            sys.exit(f"Couldn't start Clef: {error}")
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


def plan_batch(urls, loaded, default):
    """Before any form opens: each job's details, its résumé (track), the skills its description will ask about
    that nothing answers (added to QUESTIONS.md now), its form's settled answers and whether it waits because
    you applied to that company recently. Prints the plan."""
    import httpx

    from . import jobinfo, jobplan
    from . import preflight as ahead

    profiles = {folder: profile for folder, (profile, _) in loaded.items()}
    default_profile, default_policy = loaded[default]
    plans, gaps = [], []
    with httpx.Client(timeout=20, follow_redirects=True, headers=jobinfo.UA) as client:
        for url in urls:
            info = jobinfo.fetch(url, client)
            track, scores = jobplan.choose_track(info, profiles, default)
            profile = profiles[track]
            plan = jobplan.Plan(url, info, track, scores, jobplan.skill_gaps(info, profile))
            if ahead.detect(url):
                try:
                    plan.answers = jobplan.form_answers(ahead.questions(url, client), profile)
                except (httpx.HTTPError, ValueError):
                    pass  # the run still reads the page itself
            gaps += plan.skill_gaps
            plans.append(plan)
    folder = default_profile.questions_path.parent if default_profile.questions_path else Path(default)
    jobplan.spread(plans, folder / "applied.json", default_policy)
    print(f"Plan for {len(plans)} job(s):")
    for n, plan in enumerate(plans, 1):
        who = f"{plan.info.company[:22]} · {plan.info.title[:40]}" if plan.info else plan.url[:64]
        track = f" résumé {Path(plan.track).name}" if len(profiles) > 1 else ""
        extra = [f"{len(plan.answers)} answer(s) from the form" if plan.answers else "",
                 f"{len(plan.skill_gaps)} skill question(s)" if plan.skill_gaps else "",
                 f"DEFERRED ({plan.deferred})" if plan.deferred else ""]  # fmt: skip
        print(f"  {n:>2}. {who:<66}{track} {'; '.join(x for x in extra if x)}")
    unique = list({g["question"]: g for g in gaps}.values())
    if unique:
        open_now = default_profile.add_pending(unique)
        print(
            f"\n{len(unique)} skill question(s) from the job descriptions have no answer in your profile; they are in "
            f"{default_profile.questions_path} ({open_now} open there). Answer them so these jobs finish unattended."
        )
    return plans


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
    tracks = [t.strip() for t in (getattr(args, "tracks", "") or "").split(",") if t.strip()]
    folders = tracks or [args.data]
    loaded = {folder: load(argparse.Namespace(**{**vars(args), "data": folder})) for folder in folders}
    profile, policy = loaded[folders[0]]
    if getattr(args, "preflight", False):
        run_preflight(urls, profile, policy, show=False)
        loaded = {folder: load(argparse.Namespace(**{**vars(args), "data": folder})) for folder in folders}
    plans = plan_batch(urls, loaded, folders[0])
    if getattr(args, "plan_only", False):
        return 0
    prepare_model()
    transport = None
    if args.browser == "playwright":
        try:
            transport = PlaywrightTransport(args.browser_profile)
        except ImportError:
            sys.exit("Playwright isn't installed: run `uv sync --extra playwright --extra laya`.")
    results = []
    try:
        opened = 0
        for number, plan in enumerate(plans, 1):
            url = plan.url
            label = f"[{number}/{len(plans)}] "
            print(f"\n{label}{url}")
            ui = UnattendedUI(label)
            if plan.deferred:
                print(f"  · {label}deferred: {plan.deferred}")
                results.append(
                    (url, {"status": "deferred", "fills": [], "left_for_you": [], "error": plan.deferred}, ui)
                )
                continue
            profile, policy = loaded[plan.track]
            if len(loaded) > 1:
                print(f"  · {label}résumé: {plan.track} ({Path(profile.documents['resume'].value).name})")
            try:
                if transport is None:
                    tab = ChromeTransport()  # a new tab in your everyday Chrome, with its logins
                else:
                    tab = transport if opened == 0 else transport.new_tab()
                opened += 1
                browser = Browser(url, tab)
                company = plan.info.company if plan.info else ""
                agent = Agent(
                    browser,
                    profile,
                    policy,
                    ui,
                    note=args.note,
                    run_dir=args.runs,
                    answers=plan.answers,
                    company=company,
                )
                report = agent.run()
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


def scan(args):
    """Find open jobs on Greenhouse, Lever and Ashby boards (forms that need no account) and write their links."""
    from . import scan as scanner

    profile, _ = load(args)
    lines = Path(args.file).read_text(encoding="utf-8").splitlines()
    keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
    max_years = args.max_years
    if max_years is None and "work.total_experience_whole_years" in profile.facts:
        max_years = float(profile.facts["work.total_experience_whole_years"].value)  # completed years, as asked
    folder = profile.questions_path.parent if profile.questions_path else Path(args.data)
    jobs, problems = scanner.scan(
        lines,
        keywords,
        args.location or scanner.INDIA,
        max_years,
        folder / "applied.json",
        include_senior=args.include_senior,
    )
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)
    for number, job in enumerate(jobs, 1):
        print(
            f"{number:>3}. {job.company:<18.18} {job.title[:52]:<52} {job.location[:22]:<22} {'; '.join(job.reasons)}"
        )
        print(f"     {job.url}")
    if args.out and jobs:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(job.url for job in jobs) + "\n", encoding="utf-8")
    print(
        f"\n{len(jobs)} job(s) fit"
        + (f"; links written to {args.out}" if args.out and jobs else "")
        + (f" (at most {max_years:g} years asked)" if max_years is not None else "")
    )
    return 0


def drafting_on(policy):
    if policy.drafts != "confirm":
        return False
    provider = (os.environ.get("TEXT_MODEL_PROVIDER") or "").strip().lower()
    return provider == "claude-code" or bool(os.environ.get("TEXT_MODEL_API_KEY"))


def run_preflight(urls, profile, policy, show=True):
    """Read each Greenhouse / Lever / Ashby form's definition, print who answers each question, and add the
    unanswerable required ones to QUESTIONS.md. Returns how many questions are open there."""
    from . import preflight

    supported = [u for u in urls if preflight.detect(u)]
    if not supported:
        print("Preflight: none of these is a Greenhouse, Lever or Ashby application; nothing to read ahead.")
        return 0
    report, problems = preflight.preflight(supported, profile, policy, drafting_on(policy))
    pending = []
    for url, found in report.items():
        ask = [q for q in found if q.verdict == "ASK"]
        counts = {}
        for q in found:
            counts[q.verdict] = counts.get(q.verdict, 0) + 1
        print(f"\n{url}\n  {len(found)} questions: " + ", ".join(f"{n} {v}" for v, n in sorted(counts.items())))
        for q in found if show else ask:
            mark = "?" if q.verdict == "ASK" else " "
            print(f"  {mark} {q.verdict:<13} {q.label[:70]:<70} {q.detail[:40]}")
        pending += [{"question": q.label, "options": q.options, "urls": [url]} for q in ask]
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)
    open_now = profile.add_pending(pending) if pending else 0
    print(
        f"\nPreflight: {len(pending)} question(s) nothing can answer"
        + (f"; added to {profile.questions_path}. Answer them there before the batch." if pending else ".")
    )
    return open_now


def preflight_cmd(args):
    profile, policy = load(args)
    urls = []
    for item in args.targets:
        path = Path(item)
        if path.is_file():
            urls += [
                u.strip() for u in path.read_text(encoding="utf-8").splitlines() if u.strip() and not u.startswith("#")
            ]
        else:
            urls.append(item)
    run_preflight(urls, profile, policy, show=not args.only_open)
    return 0


def learn_cmd(args):
    """Draft a profile from a résumé into <data>/profile.learned.json; profile.json is never touched."""
    from . import learn

    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    path, draft, notes = learn.learn(args.resume, data, use_claude=not args.no_claude)
    current = data / "profile.json"
    print(f"Draft written to {path} (your profile.json is unchanged).")
    if current.is_file():
        import json

        diffs = learn.differences(draft, json.loads(current.read_text(encoding="utf-8")))
        print()
        print(f"{len(diffs)} value(s) differ from {current}:")
        for key, new, old in diffs[:60]:
            print(f"  {key:<40} learned {str(new)[:45]!r:<48} yours {str(old)[:40]!r}")
    for note in notes:
        print(f"  ! {note}")
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
    finder = sub.add_parser("scan", help="find jobs on Greenhouse, Lever and Ashby boards (no account needed)")
    finder.add_argument("file", help="boards file: greenhouse:<token>, lever:<site>, ashby:<org> or board URLs")
    finder.add_argument("--keywords", default="", help="comma-separated, any of them must appear, e.g. 'c#,.net'")
    finder.add_argument(
        "--location", default="", help="regex the job's location must match (default: India or remote; '.*' for any)"
    )
    finder.add_argument(
        "--max-years", type=float, default=None, help="skip jobs asking more (default: your completed years)"
    )
    finder.add_argument("--include-senior", action="store_true", help="keep senior / lead / staff titles too")
    finder.add_argument("--out", default="", help="write the fitting job links here, ready for `batch`")
    study = sub.add_parser("learn", help="draft a profile from your résumé (writes profile.learned.json)")
    study.add_argument("--resume", required=True, help="the résumé (PDF, .txt or .md)")
    study.add_argument("--no-claude", action="store_true", help="plain code extraction only, no Claude Code step")
    ahead = sub.add_parser("preflight", help="read Greenhouse / Lever / Ashby forms ahead; unknowns go to QUESTIONS.md")
    ahead.add_argument("targets", nargs="+", help="application URLs, or a file of them (one per line)")
    ahead.add_argument("--only-open", action="store_true", help="list only the questions nothing can answer")
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
    many.add_argument("--plan-only", action="store_true", help="print the plan (résumé, questions, deferrals) and stop")
    many.add_argument(
        "--tracks",
        default="",
        help="several profile folders, comma-separated: each job gets the one whose résumé fits it best",
    )
    many.add_argument(
        "--preflight",
        action="store_true",
        help="read Greenhouse / Lever / Ashby forms first; unknowns go to QUESTIONS.md",
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
    if args.command == "scan":
        return scan(args)
    if args.command == "preflight":
        return preflight_cmd(args)
    if args.command == "learn":
        return learn_cmd(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
