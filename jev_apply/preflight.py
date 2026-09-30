"""Preflight: read an application form's published definition before opening it (Greenhouse, Lever, Ashby).

Each of these boards publishes its form without an account: Greenhouse through its job-board API, Lever in the
server-rendered apply page (each custom question's template is embedded as JSON), Ashby through the GraphQL
query its own page makes. So every question, its options and whether it is required are known before a run.

`jev-apply preflight` sorts each question by who will answer it (a rule or your profile, a saved answer, your
résumé, Laya, a draft) and adds the ones nothing can answer to QUESTIONS.md, so you answer them once *before*
the batch instead of after it stops. Nothing is filled, clicked or sent. The Ashby query follows the one
captured by TheAdaply/jev-apply (MIT).
"""

import html
import json
import re
from dataclasses import dataclass, field

import httpx

from . import planner, rules

TIMEOUT = 20
UA = {"User-Agent": "systemone-apply (+preflight)"}


@dataclass
class Question:
    label: str
    kind: str  # text, long_text, choice, multi, boolean, file, date, number
    required: bool
    options: list = field(default_factory=list)
    section: str = ""
    verdict: str = ""  # who answers it: see classify()
    detail: str = ""


def detect(url):
    """('greenhouse', token, id) | ('lever', site, id, api host) | ('ashby', org, id) | None."""
    found = re.search(r"(?:job-)?boards\.greenhouse\.io/([\w.-]+)/jobs/(\d+)", url or "")
    if found:
        return "greenhouse", found[1], found[2]
    found = re.search(r"jobs(\.eu)?\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", url or "")
    if found:
        return "lever", found[2], found[3], "jobs.eu.lever.co" if found[1] else "jobs.lever.co"
    found = re.search(r"jobs\.ashbyhq\.com/([\w.%-]+)/([0-9a-f-]{36})", url or "")
    if found:
        return "ashby", found[1], found[2]
    return None


def text_of(markup):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup or ""))).strip()


GREENHOUSE_KINDS = {
    "input_text": "text",
    "textarea": "long_text",
    "input_file": "file",
    "multi_value_single_select": "choice",
    "multi_value_multi_select": "multi",
    "input_hidden": "hidden",
}


def greenhouse(token, job, client):
    data = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job}", params={"questions": "true"})
    data.raise_for_status()
    body = data.json()
    out = []
    for group, section in ((body.get("questions"), ""), (body.get("location_questions"), "Location")):
        for q in group or []:
            fields = [f for f in q.get("fields", []) if f.get("type") != "input_hidden"]
            if not fields:
                continue
            main = fields[0]  # "Resume/CV" offers a file or pasted text: the file is the question
            options = [v.get("label", "") for v in main.get("values") or []]
            out.append(Question(q.get("label", ""), GREENHOUSE_KINDS.get(main.get("type"), "text"),
                                bool(q.get("required")), options, section))  # fmt: skip
    for block in body.get("compliance") or []:
        for q in block.get("questions") or []:
            options = [v.get("label", "") for f in q.get("fields", []) for v in f.get("values") or []]
            out.append(Question(q.get("label", ""), "choice", bool(q.get("required")), options, "Voluntary EEO"))
    for q in body.get("demographic_questions", {}).get("questions", []) if body.get("demographic_questions") else []:
        options = [a.get("label", "") for a in q.get("answer_options") or []]
        out.append(Question(q.get("label", ""), "multi" if q.get("type") == "multi_select" else "choice",
                            bool(q.get("required")), options, "Demographic survey"))  # fmt: skip
    return out


LEVER_STANDARD = {
    "name": ("Full name", "text", True),
    "email": ("Email", "text", True),
    "phone": ("Phone", "text", False),
    "org": ("Current company", "text", False),
    "location": ("Current location", "text", False),
    "resume": ("Resume/CV", "file", True),
    "comments": ("Additional information", "long_text", False),
}
LEVER_KINDS = {"text": "text", "textarea": "long_text", "multiple-choice": "choice", "multiple-select": "multi",
               "dropdown": "choice", "file-upload": "file", "date": "date"}  # fmt: skip


def lever(site, job, host, client):
    page = client.get(f"https://{host}/{site}/{job}/apply")
    page.raise_for_status()
    markup = page.text
    names = set(re.findall(r'<(?:input|select|textarea)[^>]*\bname="([^"]+)"', markup))
    out = [Question(label, kind, required) for name, (label, kind, required) in LEVER_STANDARD.items() if name in names]
    out += [Question(f"{link} URL", "text", False) for link in re.findall(r'name="urls\[([^\]]+)\]"', markup)]
    for tag in re.findall(r'<input[^>]*name="cards\[[^\]]+\]\[baseTemplate\]"[^>]*>', markup):
        value = re.search(r'value="([^"]*)"', tag)
        try:
            card = json.loads(html.unescape(value[1])) if value else {}
        except ValueError:
            continue
        for f in card.get("fields") or []:
            options = [o.get("text", "") for o in f.get("options") or []]
            out.append(Question(f.get("text", ""), LEVER_KINDS.get(f.get("type"), "text"), bool(f.get("required")),
                                options, card.get("text", "")))  # fmt: skip
    for eeo in sorted(set(re.findall(r'name="eeo\[(\w+)\]"', markup))):
        out.append(Question(f"EEO: {eeo}", "choice", False, [], "Voluntary EEO"))
    return out


ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id title
    applicationForm { sections { title fieldEntries { id field isRequired isHidden } } }
    surveyForms { sections { title fieldEntries { id field isRequired isHidden } } }
  }
}"""
ASHBY_KINDS = {"String": "text", "Email": "text", "Phone": "text", "Url": "text", "Location": "text",
               "SocialLink": "text", "LongText": "long_text", "File": "file", "ValueSelect": "choice",
               "MultiValueSelect": "multi", "Boolean": "boolean", "Date": "date", "Number": "number",
               "Score": "choice"}  # fmt: skip


def ashby(org, job, client):
    response = client.post(
        "https://jobs.ashbyhq.com/api/non-user-graphql",
        params={"op": "ApiJobPosting"},
        json={
            "operationName": "ApiJobPosting",
            "query": ASHBY_QUERY,
            "variables": {"organizationHostedJobsPageName": org, "jobPostingId": job},
        },  # fmt: skip
    )
    response.raise_for_status()
    posting = (response.json().get("data") or {}).get("jobPosting")
    if not posting:
        raise ValueError("Ashby returned no posting (closed, or not listed)")
    out = []
    forms = [(posting.get("applicationForm"), "")] + [(s, "Survey") for s in posting.get("surveyForms") or []]
    for form, prefix in forms:
        for section in (form or {}).get("sections") or []:
            for entry in section.get("fieldEntries") or []:
                f = entry.get("field") or {}
                if entry.get("isHidden"):
                    continue
                options = [v.get("label", "") for v in f.get("selectableValues") or []]
                title = f.get("title") or ""
                where = " ".join(x for x in (prefix, section.get("title") or "") if x)
                kind = ASHBY_KINDS.get(f.get("type"), "text")
                out.append(Question(title, kind, bool(entry.get("isRequired")), options, where))
    return out


def questions(url, client):
    spec = detect(url)
    if spec is None:
        raise ValueError("not a Greenhouse, Lever or Ashby application")
    provider, *rest = spec
    return {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}[provider](*rest, client)


def classify(q, profile, policy, drafting):
    """Who answers this question, in words: profile, saved answer, résumé, EEO decline, consent, Laya, draft,
    skip (optional and nothing answers it) or ASK (nothing can: it goes to QUESTIONS.md)."""
    candidate = profile.candidate_state()
    facts = planner.Facts(candidate)
    label = q.label.strip()
    saved = profile.saved_answer(label)
    choices = q.kind in {"choice", "multi"} and q.options
    if saved and (not choices or any(planner.norm(saved) in planner.norm(o) or planner.norm(o) == planner.norm(saved)
                                     for o in q.options)):  # fmt: skip
        return "saved answer", saved[:60]  # (a saved answer that is none of this form's options doesn't count)
    if q.kind == "file":
        doc = rules.document({"kind": "upload", "label": label, "required": q.required}, profile.documents)
        if doc and doc in profile.documents:
            return "résumé" if "resume" in doc else "document", doc
        return ("ASK", "no matching document") if q.required else ("skip", "optional file")
    if q.kind in {"choice", "multi", "boolean"}:
        options = q.options or (["Yes", "No"] if q.kind == "boolean" else [])
        group = [{"kind": "select", "label": f"{label} → {o}", "value": o} for o in options]
        exact = planner.profile_answer(label, group, facts) if group else []
        if exact:
            return "profile", planner.option_text(exact[0])[:60]
        if any(planner.eeo_decline(label, o, candidate) for o in options):
            return "EEO decline", "declines to self-identify"
        box = {"kind": "click", "role": "checkbox", "label": label}
        if policy.auto_consent and policy.plain_consent(box):
            return "consent", "privacy / terms consent"
        ruled = rules.match({"label": label, "context": label}, profile)
        if ruled == "ASK_USER" or planner.CLAIM.search(label) or policy.sensitive({"label": label}):
            return ("ASK", "only you can say") if q.required else ("skip", "optional, yours to say")
        if planner.relates(label, candidate):
            return "Laya", f"picks among {len(options)} options"
        return ("ASK", "nothing in your profile") if q.required else ("skip", "optional")
    action = {"kind": "fill", "role": "textbox", "label": label, "required": q.required,
              "input_type": "textarea" if q.kind == "long_text" else q.kind}  # fmt: skip
    key = rules.match(action, profile)
    if key and key not in {"ASK_USER", "SKIP_FIELD", "DRAFT_ANSWER"} and key in profile.by_id:
        return "profile", profile.by_id[key].value[:60]
    if key == "SKIP_FIELD" or (key is None and not q.required and q.kind != "long_text"):
        return "skip", "optional"
    if q.kind == "long_text" or key == "DRAFT_ANSWER":
        if not q.required:
            return "skip", "optional"
        if drafting:
            return "draft", "written from your profile"
        return ("ASK", "open question, no drafting set up") if q.required else ("skip", "optional")
    if key == "ASK_USER":
        return ("ASK", "only you can say") if q.required else ("skip", "optional, yours to say")
    if planner.relates(label, candidate):
        return "Laya", "picks the matching fact"
    return ("ASK", "nothing in your profile") if q.required else ("skip", "optional")


def preflight(urls, profile, policy, drafting=False, client=None):
    """{url: [Question with verdict]} and problems, for every supported URL. Unanswerable required questions are
    returned so the caller can add them to QUESTIONS.md."""
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=UA)
    report, problems = {}, []
    try:
        for url in urls:
            try:
                found = questions(url, client)
            except (httpx.HTTPError, ValueError) as error:
                problems.append(f"{url[:90]}: {str(error).splitlines()[0]}")
                continue
            for q in found:
                q.verdict, q.detail = classify(q, profile, policy, drafting)
            report[url] = found
    finally:
        if own:
            client.close()
    return report, problems
