"""The teacher: Claude's answers are used only when your profile backs them, and each becomes a lesson."""

import json

from jev_apply import model, teacher
from jev_apply.profile import Profile

PROFILE = {
    "personal": {"first_name": "Yuvraj", "city": "Udaipur", "country": "India"},
    "work": {"notice_period_text": "15 days"},
    "preferences": {"willing_to_relocate": True},
    "skill_years": {"PostgreSQL": 2},
}


def profile(tmp_path):
    return Profile(PROFILE, tmp_path, questions_path=tmp_path / "QUESTIONS.md")


def test_only_answers_your_profile_backs_are_used(tmp_path):
    p = profile(tmp_path)
    q = {"question": "When can you join?", "options": []}
    assert teacher.checked(q, {"answer": "15 days", "fact": "work.notice_period_text"}, p) == "15 days"
    assert teacher.checked(q, {"answer": "Immediately", "fact": "work.notice_period_text"}, p) is None  # not its value
    assert teacher.checked(q, {"answer": "15 days", "fact": "work.made_up"}, p) is None  # no such fact
    assert teacher.checked(q, {"answer": None, "fact": None}, p) is None  # didn't know
    relocate = {"question": "Open to relocation?", "options": ["Yes", "No"]}
    assert teacher.checked(relocate, {"answer": "yes", "fact": "preferences.willing_to_relocate"}, p) == "Yes"
    assert teacher.checked(relocate, {"answer": "No", "fact": "preferences.willing_to_relocate"}, p) is None
    assert teacher.checked(relocate, {"answer": "Maybe", "fact": "preferences.willing_to_relocate"}, p) is None
    years = {"question": "Years with Postgres?", "options": []}
    assert teacher.checked(years, {"answer": "2", "fact": "skill_years.PostgreSQL"}, p) == "2"


def test_a_field_of_one_job_or_school_is_never_taught(tmp_path):
    """Xylem: an empty Work Experience 3 got "Job Title" -> the current title, "Location" -> Udaipur as lessons."""
    profile = Profile(
        {"personal": {"email": "me@mail.test"}, "work": {"current_title": "Founding Engineer"},
         "experience": [{"company": "Anexee", "location": "Udaipur, India"}]}, tmp_path,
    )  # fmt: skip
    for question, answer, key in [
        ("Job Title — From*", "Founding Engineer", "work.current_title"),
        ("Job Title", "Founding Engineer", "work.current_title"),
        ("Location", "Udaipur, India", "experience.0.location"),
        ("Tick this box? From (I currently work here)", "Yes", "experience.0.start"),
    ]:
        assert teacher.checked({"question": question, "options": []}, {"answer": answer, "fact": key}, profile) is None
    email = teacher.checked({"question": "Email Address", "options": []},
                            {"answer": "me@mail.test", "fact": "personal.email"}, profile)  # fmt: skip
    assert email == "me@mail.test"


def test_a_stuck_page_gets_claudes_next_step_only_when_it_holds_up(tmp_path, monkeypatch):
    """When a run is about to stop, Claude picks the next step; code refuses a submit, an invented answer, a step
    that just failed and a ticked box, and takes a dropdown to open, a fact's exact value, or "done"."""
    from jev_apply.policy import Policy

    monkeypatch.setenv("TEXT_MODEL_PROVIDER", "claude-code")
    p = profile(tmp_path)
    page = {"url": "https://jobs.example/apply", "text": "", "actions": [
        {"id": "e1", "kind": "click", "role": "button", "label": "Submit application"},
        {"id": "e2", "kind": "fill", "role": "textbox", "label": "First name", "value": ""},
        {"id": "e3", "kind": "click", "role": "button", "label": "Degree Select One Required"},
        {"id": "e4", "kind": "click", "role": "button", "label": "Save and Continue"},
        {"id": "e5", "kind": "click", "role": "checkbox", "label": "I agree", "checked": "true"},
    ]}  # fmt: skip
    sent = {}

    def reply(answer, avoid=(), tried=()):
        def fake(system, payload, **kw):
            sent.update(payload)
            return json.dumps(answer), {}

        monkeypatch.setattr(model, "claude_code", fake)
        return teacher.rescue(page, [], p, "stuck", Policy.load(), tmp_path, avoid, tried)

    assert reply({"id": "e1", "why": "send it"})["refused"]  # never a submit
    assert reply({"id": "e2", "text": "Rahul", "fact": "personal.first_name"})["action"] is None  # not the fact
    step = reply({"id": "e2", "text": "Yuvraj", "fact": "personal.first_name"})
    assert step["action"]["id"] == "e2" and step["text"] == "Yuvraj"
    assert reply({"id": "e3", "why": "open the degree list"})["action"]["id"] == "e3"
    assert reply({"id": "e9"})["refused"]  # not on the page
    assert reply({"id": "e5"})["refused"]  # a ticked box: a click unticks it
    assert reply({"done": True, "why": "degree chosen"})["done"]
    gone = reply({"id": None, "why": "Degree: nothing on file says which degree"})
    assert gone["action"] is None and "Degree" in gone["why"] and not gone.get("refused")

    # Maersk: Claude pressed the Save that had just failed, three times; the banner naming the field never reached it
    page["text"] = ("Application Questions 2 of 2\nErrors Found\nError: The field Degree is required and must have "
                    "a value.\nBack Save and Continue")  # fmt: skip
    assert reply({"id": "e4", "why": "retry"}, avoid=["Save and Continue"], tried=[{"did": "x"}])["refused"]
    assert any("Degree is required" in e for e in sent["page_errors"]) and sent["does_not_work"] == ["Save and Continue"]
    assert sent["steps_this_rescue"] == [{"did": "x"}]


def test_accepted_answers_become_lessons_profiles_load(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_PROVIDER", "claude-code")
    reply = {"answers": [
        {"i": 0, "answer": "Udaipur", "fact": "personal.city", "why": "current city"},
        {"i": 1, "answer": "Yes", "fact": "personal.has_car", "why": "invented"},
        {"i": 2, "answer": None, "fact": None, "why": "not in profile"},
    ]}  # fmt: skip
    seen = {}

    def fake_claude(system, payload, **kw):
        seen.update(payload)
        return json.dumps(reply), {}

    monkeypatch.setattr(model, "claude_code", fake_claude)
    p = profile(tmp_path)
    entries = [{"question": "Which town do you live in?", "options": []},
               {"question": "Do you own a car?", "options": ["Yes", "No"]},
               {"question": "Why this company?", "options": []},
               {"question": "We use cookies to improve your experience", "options": []}]  # fmt: skip
    given = teacher.ask(entries, p, "https://x.test/job")
    assert given == {"Which town do you live in?": "Udaipur"}  # the invented car is dropped
    assert len(seen["questions"]) == 3 and "personal.city" in seen["facts"]  # the cookie banner never went
    (tmp_path / "profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    again = Profile.load(tmp_path / "profile.json", questions_path=tmp_path / "QUESTIONS.md")
    assert again.saved_answer("Which town do you live in?") == "Udaipur"  # next time: no Claude call


def test_no_teacher_without_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_PROVIDER", raising=False)
    monkeypatch.setattr(model, "claude_code", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert teacher.ask([{"question": "City?", "options": []}], profile(tmp_path)) == {}


def test_your_answer_is_found_in_the_lists_own_words_and_kept(tmp_path, monkeypatch):
    """Maersk lists "BS", never B.Tech; a college list has "Manipal University" or only "Other". Claude picks from
    the options shown (checked here), and the pick is kept: the next list offering it needs no Claude call."""
    monkeypatch.setenv("TEXT_MODEL_PROVIDER", "claude-code")
    p = profile(tmp_path)
    calls = []

    def reply(answer):
        def fake(system, payload, **kw):
            calls.append(payload)
            return json.dumps(answer), {}

        monkeypatch.setattr(model, "claude_code", fake)

    degrees = ["Associate", "BS", "MS", "PhD", "Other"]
    reply({"option": "BS", "match": "closest", "why": "B.Tech is a bachelor of science"})
    assert teacher.closest_option("Degree", "education.0.degree", "B.Tech", "degree", degrees, p, "a.test")[0] == "BS"
    assert teacher.closest_option("Degree", "education.0.degree", "B.Tech", "degree", ["BA", "BS"], p, "b.test")[
        :2] == ("BS", "closest") and len(calls) == 1  # kept: no second call
    reply({"option": "Bachelor of Tech", "match": "same"})  # not an option shown
    assert teacher.closest_option("Degree", "education.0.degree", "BE", "degree", degrees, p) is None
    reply({"option": "MS", "match": "other"})  # "other" only for the list's own way out
    assert teacher.closest_option("Degree", "education.0.degree", "BE", "degree", degrees, p) is None
    schools = ["Manipal Academy of Higher Education", "Other"]
    reply({"option": "Other", "match": "other", "why": "no Jaipur campus listed"})
    school = ("School", "education.0.school", "Manipal University Jaipur", "school", schools, p)
    assert teacher.closest_option(*school, "x.test")[0] == "Other"
    calls.clear()
    reply({"option": None, "why": "none"})
    assert teacher.closest_option(*school, "y.test") is None and calls  # an "Other" is never carried to another site


def test_a_lesson_is_used_only_while_it_still_holds_against_your_profile(tmp_path):
    """A lesson keeps the fact it came from: when that fact changes (a new CTC) the old answer is no longer used, and
    a kind of question lessons may not cover (who you are) is dropped even if one was kept before."""
    lessons = {"answers": [
        {"id": "lesson_1", "question": "When can you join?", "answer": "15 days", "fact": "work.notice_period_text"},
        {"id": "lesson_2", "question": "Please specify your Date of Birth", "answer": "Male", "fact": "personal.city"},
    ]}  # fmt: skip
    (tmp_path / "lessons.json").write_text(json.dumps(lessons), encoding="utf-8")
    (tmp_path / "profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    loaded = Profile.load(tmp_path / "profile.json", questions_path=tmp_path / "QUESTIONS.md")
    assert loaded.saved_answer("When can you join?") == "15 days"
    assert loaded.saved_answer("Please specify your Date of Birth") is None  # never a lesson
    changed = {**PROFILE, "work": {"notice_period_text": "30 days"}}
    (tmp_path / "profile.json").write_text(json.dumps(changed), encoding="utf-8")
    again = Profile.load(tmp_path / "profile.json", questions_path=tmp_path / "QUESTIONS.md")
    assert again.saved_answer("When can you join?") != "15 days"  # the fact changed: the old lesson is not used


def test_a_lesson_from_your_own_saved_answer_still_holds(tmp_path):
    """A lesson may come from an answer you gave in QUESTIONS.md ("saved: <question>"): it is checked against it."""
    from jev_apply import inbox

    inbox.write(tmp_path / "QUESTIONS.md", [{"question": "Notice period?", "options": [], "urls": [],
                                            "answer": "15 days"}])  # fmt: skip
    lessons = {"answers": [{"id": "lesson_1", "question": "When could you start?", "answer": "15 days",
                            "fact": "saved: Notice period?"}]}  # fmt: skip
    (tmp_path / "lessons.json").write_text(json.dumps(lessons), encoding="utf-8")
    (tmp_path / "profile.json").write_text(json.dumps({"personal": {"first_name": "Yuvraj"}}), encoding="utf-8")
    loaded = Profile.load(tmp_path / "profile.json", questions_path=tmp_path / "QUESTIONS.md")
    assert loaded.saved_answer("Notice period?") == "15 days"
    assert loaded.saved_answer("When could you start?") == "15 days"  # the lesson holds: kept
