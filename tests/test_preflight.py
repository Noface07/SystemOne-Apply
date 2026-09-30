"""Preflight: forms read from their published definitions (mock transport), and who answers each question."""

import json
from pathlib import Path

import httpx

from jev_apply import preflight
from jev_apply.policy import Policy
from jev_apply.profile import Profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = Profile.load(ROOT / "data" / "profile.example.json")


def test_supported_forms_are_recognised_from_their_address():
    assert preflight.detect("https://job-boards.greenhouse.io/acme/jobs/123") == ("greenhouse", "acme", "123")
    lever = "https://jobs.lever.co/Acme/0f8b3c2e-1d2a-4b5c-9e7f-123456789abc/apply"
    assert preflight.detect(lever) == ("lever", "Acme", "0f8b3c2e-1d2a-4b5c-9e7f-123456789abc", "jobs.lever.co")
    ashby = "https://jobs.ashbyhq.com/acme/0f8b3c2e-1d2a-4b5c-9e7f-123456789abc/application"
    assert preflight.detect(ashby)[0] == "ashby" and preflight.detect("https://careers.example.com/1") is None


GREENHOUSE = {
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
        {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file"}]},
        {"label": "Have you ever worked for Acme before?", "required": True,
         "fields": [{"name": "q1", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"label": "Do you hold an active security clearance?", "required": True,
         "fields": [{"name": "q2", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"label": "Why Acme?", "required": False, "fields": [{"name": "q3", "type": "textarea"}]},
    ]
}  # fmt: skip
TEMPLATE = {"text": "Questions", "fields": [
    {"type": "multiple-choice", "text": "Where are you based?", "required": True,
     "options": [{"text": "Pune"}, {"text": "Bengaluru"}]}]}  # fmt: skip
LEVER = (
    '<form><input name="name"><input name="email"><input type="file" name="resume">'
    f'<input type="hidden" name="cards[abc][baseTemplate]" value="{json.dumps(TEMPLATE).replace(chr(34), "&quot;")}">'
    '<input name="urls[LinkedIn]"></form>'
)


def mock():
    def respond(request):
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json=GREENHOUSE)
        return httpx.Response(200, text=LEVER)

    return httpx.Client(transport=httpx.MockTransport(respond))


def test_each_question_gets_who_answers_it_and_unknowns_are_asked():
    urls = [
        "https://job-boards.greenhouse.io/acme/jobs/1",
        "https://jobs.lever.co/Acme/0f8b3c2e-1d2a-4b5c-9e7f-123456789abc/apply",
    ]
    report, problems = preflight.preflight(urls, PROFILE, Policy(), drafting=True, client=mock())
    assert not problems
    greenhouse = {q.label: q.verdict for q in report[urls[0]]}
    assert greenhouse["First Name"] == "profile" and greenhouse["Resume/CV"] == "résumé"
    assert greenhouse["Do you hold an active security clearance?"] == "ASK"  # nothing in the profile says
    assert greenhouse["Why Acme?"] == "skip"  # optional open question: not drafted
    lever = {q.label: q for q in report[urls[1]]}
    assert lever["Where are you based?"].options == ["Pune", "Bengaluru"]
    assert lever["Where are you based?"].verdict == "profile"  # the example profile lives in Bengaluru
    assert lever["LinkedIn URL"].verdict == "profile"
