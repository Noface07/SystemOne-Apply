"""How the agent does, from the run reports it already writes (runs/<time>/report.json).

Every report carries its site, the applicant tracking system behind it, and one stop code saying why it ended
(tags, written by Agent.finish). `jev-apply metrics` reads them all, old ones too, and shows what matters most:
how often the first run on a company never seen before reaches Review, per ATS, and the reasons runs stop."""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

# The platform behind a job's host: one fix for Workday helps every company on Workday.
ATS = [
    ("workday", r"myworkdayjobs\.com|myworkdaysite\.com|workday\.com"),
    ("greenhouse", r"greenhouse\.io"),
    ("lever", r"lever\.co"),
    ("ashby", r"ashbyhq\.com"),
    ("smartrecruiters", r"smartrecruiters\.com"),
    ("icims", r"icims\.com"),
    ("successfactors", r"successfactors|sapsf"),
    ("taleo", r"taleo\.net"),
    ("oracle", r"oraclecloud\.com"),
    ("workable", r"workable\.com"),
    ("linkedin", r"linkedin\.com"),
    ("naukri", r"naukri\.com"),
    ("local", r"^(localhost|127\.0\.0\.1)$"),
]

# Why a run ended, first match wins: the status first, then the words of its last handover.
STOP_CODES = [
    ("review", r"^review$"),
    ("submitted", r"^submitted"),
    ("already_applied", r"^already applied$|already applied"),
    ("budget", r"^budget$"),
    ("job_closed", r"no longer open|has closed|expired|no longer accepting"),
    # The agent's own catch-alls name possible causes ("maybe a CAPTCHA, a login"): matched before those causes
    ("all_tried", r"everything the agent could do on this page"),
    ("no_form_found", r"nothing here leads to an application form"),
    ("no_way_forward", r"nothing more the agent can fill here"),
    ("model_error", r"rate limit|http \d{3}|model provider|decision model"),
    ("resume_mismatch", r"r[ée]sum[ée]"),
    ("login", r"\blog ?in\b|sign ?in|captcha|\botp\b|verification code|password"),
    ("site_errors", r"site reports problems|marked invalid|site says"),
    ("no_progress", r"changed nothing|three times|keeps coming back|wouldn't take it|kept changing|didn't move"),
    ("needs_answer", r"question|never-fill|sensitive|answer"),
]


def host_of(url):
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def ats_of(host):
    return next((name for name, pattern in ATS if re.search(pattern, host or "")), "other")


def stop_code(status, handovers=(), left=()):
    """One word for why a run ended: its status when that says it, else its last handover's reason."""
    texts = [status or ""] + [h.get("reason") or "" for h in reversed(list(handovers))]
    texts += [x.get("why") or "" for x in left]
    for text in texts:
        for code, pattern in STOP_CODES:
            if re.search(pattern, text, re.I):
                return code
    return "other"


def tags(url, status, handovers=(), left=()):
    host = host_of(url)
    return {"host": host, "ats": ats_of(host), "stop_code": stop_code(status, handovers, left)}


def reached(report):
    return report.get("status") == "review" or str(report.get("status") or "").startswith("submitted")


def load(run_dir, read=None):
    """Every report, oldest first, each with its tags (older reports get them computed here). `read` parses one
    report.json (the web app passes its cached reader); None by default."""
    reports = []
    for path in sorted(Path(run_dir).glob("*/report.json")):
        try:
            report = dict(read(path) or {}) if read else json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not report:
            continue
        if "stop_code" not in report:
            report.update(tags(report.get("start_url"), report.get("status"), report.get("handovers") or [],
                               report.get("left_for_you") or []))  # fmt: skip
        report["_run"] = path.parent.name
        reports.append(report)
    return reports


def summary(reports):
    """The numbers: overall and per ATS, the first run on each site, runs until a site first reached Review, stop
    codes, and how the rescuer and closest-option picks did."""
    by_host = defaultdict(list)
    for r in reports:
        if r.get("ats") != "local":  # the fixtures aren't a site
            by_host[r["host"]].append(r)
    firsts = {host: runs[0] for host, runs in by_host.items()}
    until = {}
    for host, runs in by_host.items():
        n = next((i + 1 for i, r in enumerate(runs) if reached(r)), None)
        if n:
            until[host] = n
    per_ats = defaultdict(lambda: Counter())
    for host, runs in by_host.items():
        ats = runs[0]["ats"]
        per_ats[ats]["sites"] += 1
        per_ats[ats]["runs"] += len(runs)
        per_ats[ats]["reached"] += sum(reached(r) for r in runs)
        per_ats[ats]["first_reached"] += reached(firsts[host])
    real = [r for runs in by_host.values() for r in runs]
    rescues = [x for r in real for x in r.get("rescues") or []]
    closest = [x for r in real for x in r.get("closest") or []]
    return {
        "runs": len(real),
        "sites": len(by_host),
        "reached": sum(reached(r) for r in real),
        "first_contact": {"sites": len(firsts), "reached": sum(reached(r) for r in firsts.values())},
        "runs_until_reached": until,
        "per_ats": {k: dict(v) for k, v in per_ats.items()},
        "stop_codes": Counter(r["stop_code"] for r in real if not reached(r)).most_common(),
        "rescues": {"tried": len(rescues), "helped": sum(bool(x.get("helped")) for x in rescues),
                    "why_not": Counter(x.get("why") or "" for x in rescues if not x.get("helped")).most_common(5)},
        "closest": Counter(x.get("match") for x in closest),
    }  # fmt: skip


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def render(s):
    lines = [
        f"Runs: {s['runs']} on {s['sites']} sites; reached Review/Submit: {s['reached']} ({pct(s['reached'], s['runs'])})",
        f"First run on a new site reached Review: {s['first_contact']['reached']} of {s['first_contact']['sites']}"
        f" ({pct(s['first_contact']['reached'], s['first_contact']['sites'])})",
    ]
    if s["runs_until_reached"]:
        slow = sorted(s["runs_until_reached"].items(), key=lambda x: -x[1])[:8]
        lines.append("Runs until a site first reached Review: " + ", ".join(f"{h} {n}" for h, n in slow))
    lines.append("")
    lines.append(f"{'ATS':<16}{'sites':>6}{'runs':>6}{'reached':>9}{'first run':>11}")
    for ats, c in sorted(s["per_ats"].items(), key=lambda x: -x[1]["runs"]):
        lines.append(f"{ats:<16}{c['sites']:>6}{c['runs']:>6}{pct(c['reached'], c['runs']):>9}"
                     f"{pct(c['first_reached'], c['sites']):>11}")  # fmt: skip
    lines.append("")
    lines.append("Why runs stopped: " + ", ".join(f"{code} {n}" for code, n in s["stop_codes"]))
    r = s["rescues"]
    if r["tried"]:
        lines.append(f"Rescues: {r['helped']} of {r['tried']} got the page moving")
        lines += [f"  not: {why[:100]} ({n})" for why, n in r["why_not"] if why]
    if s["closest"]:
        lines.append("Closest-option picks: " + ", ".join(f"{k} {n}" for k, n in s["closest"].items()))
    return "\n".join(lines)
