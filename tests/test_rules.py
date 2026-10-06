"""Fixed field rules: the wordings Indian forms use, answered the same way every time."""

import json
from pathlib import Path

import pytest

from jev_apply import model, rules
from jev_apply.profile import Profile, safe_id

ROOT = Path(__file__).resolve().parents[1]
PROFILE = Profile.load(ROOT / "data" / "profile.example.json")
PROBES = json.loads((ROOT / "data" / "probe_fields.example.json").read_text(encoding="utf-8"))["fields"]


def field(label, **extra):
    return {"kind": "fill", "role": "textbox", "node": 1, "value": "", "label": label, **extra}


@pytest.mark.parametrize("spec", PROBES, ids=[p["label"] for p in PROBES])
def test_every_probe_wording_is_answered_right_or_left_to_the_model(spec):
    action = {k: v for k, v in field(spec["label"]).items()} | {k: v for k, v in spec.items() if k != "expect"}
    wanted = spec["expect"] if isinstance(spec["expect"], list) else [spec["expect"]]
    wanted = {w if w.isupper() else safe_id(w) for w in wanted}
    got = rules.match(action, PROFILE, spec.get("input_type") in {"date", "month"}, ())
    assert got in wanted | {None}, f"{spec['label']!r} -> {got}"


@pytest.mark.parametrize(
    "label, extra, expected",
    [
        ("Current CTC (in LPA) *", {}, "compensation.current.total_annual_lpa"),
        ("Expected CTC *", {"help": "Annual, in INR"}, "compensation.expected.total_annual"),
        ("Expected CTC per month", {}, "compensation.expected.total_annual_monthly"),
        ("Current fixed CTC (Lakhs)", {}, "compensation.current.fixed_annual_lpa"),
        ("Total experience (years) *", {"input_type": "number"}, "work.total_experience_years"),
        ("Notice period", {"input_type": "number"}, "work.notice_period_days"),
        ("Notice period", {}, "work.notice_period_text"),
        ("Are you currently serving notice?", {}, "work.serving_notice"),
        ("Graduation year", {}, "education.0.end.year"),  # "2022", not "2022-06"
        ("Current designation *", {}, "work.current_title"),
        ("Mobile number *", {}, "personal.phone"),
        ("Current location *", {}, "personal.city"),
        ("Email *", {}, "personal.email"),
        ("LinkedIn profile", {}, "links.linkedin"),
        ("Earliest start date", {"input_type": "date"}, "work.earliest_start_date"),
    ],
)
def test_common_fields_map_to_the_right_fact(label, extra, expected):
    dates_only = extra.get("input_type") in {"date", "month"}
    assert rules.match(field(label, **extra), PROFILE, dates_only, ()) == safe_id(expected)


@pytest.mark.parametrize(
    "label, extra",
    [
        ("Expected CTC", {}),  # LPA or rupees? nothing says: the model (or you) decides
        ("Salary", {}),  # current or expected? nothing says
        ("Years of experience with Kubernetes", {}),  # one tool, not total experience
        ("Graduation (month and year)", {"input_type": "month"}),  # no month in the profile
        ("Notice period (months)", {}),
        ("Expected hike %", {}),
        ("Alternate mobile number", {}),
        ("Upload your portfolio", {}),
    ],
)
def test_unclear_fields_are_not_guessed(label, extra):
    got = rules.match(field(label, **extra), PROFILE, extra.get("input_type") in {"date", "month"}, ())
    assert got in {None, "ASK_USER", "SKIP_FIELD"}  # never a fact


def test_open_questions_draft_and_unknowns_ask():
    assert rules.match(field("Why do you want to join Acme? *"), PROFILE) == "DRAFT_ANSWER"
    assert rules.match(field("How did you hear about us?"), PROFILE) == "ASK_USER"
    assert rules.match(field("Current take-home per month"), PROFILE) == "ASK_USER"
    assert rules.match(field("Middle name", required=True), PROFILE) is None


def test_tag_fields_take_the_next_item_not_already_added_then_skip():
    action = field("Key skills", multiple=True, context="Key skills Python × Go ×")
    first = rules.match(action, PROFILE, False, ())
    assert first == safe_id("skills.item_3")
    assert rules.match(action, PROFILE, False, (first,)) == safe_id("skills.item_4")
    everything = {safe_id(f"skills.item_{n}") for n in range(1, 7)}
    assert rules.match(action, PROFILE, False, everything) == "SKIP_FIELD"


def test_a_rule_answers_value_source_without_calling_any_model(monkeypatch):
    monkeypatch.setattr(model, "systemone", lambda body: pytest.fail("a model was called"))
    page = {"url": "https://jobs.example/apply", "title": "Apply", "text": ""}
    result = model.value_source(field("Current CTC (in LPA) *"), page, PROFILE, [])
    assert result["choice"] == safe_id("compensation.current.total_annual_lpa") and result["rule"]
    assert result["margin"] == 1.0 and abs(sum(result["probabilities"].values()) - 1) < 1e-9
    model.validate_choice(result, list(result["probabilities"]))


def test_tag_fields_stop_at_the_limit_they_state():
    action = field("Key skills (add up to 2)", multiple=True, context="")
    tried = {safe_id("skills.item_1"), safe_id("skills.item_2")}
    assert rules.match(action, PROFILE, False, tried) == "SKIP_FIELD"


WORKDAY = Profile(
    {
        **json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8")),
        "personal": {
            "first_name": "Aarav",
            "last_name": "Sharma",
            "phone": "+91 98765 43210",
            "phone_local": "9876543210",
            "phone_country_code": "+91",
            "phone_type": "Mobile",
            "address": "12 Example Road",
            "full_address": "12 Example Road, Udaipur, Rajasthan 560034",
            "city": "Udaipur",
            "state": "Rajasthan",
            "country": "India",
            "pincode": "560034",
        },
        "work": {"worked_for_this_company_before": False},
        "experience": [{"company": "Acme Systems"}, {"company": "Philips"}],
    }
)


@pytest.mark.parametrize(
    "label, extra, page_text, expected",
    [
        ("Given Name(s)*", {"required": True}, "", "personal.first_name"),
        ("Family Name*", {"required": True}, "", "personal.last_name"),
        ("Local Given Name(s)", {}, "", "SKIP_FIELD"),
        ("Middle Name", {}, "", "SKIP_FIELD"),
        ("Address Line 1*", {"required": True}, "", "personal.address"),
        ("Address Line 2", {}, "", "SKIP_FIELD"),
        ("Current Address*", {"required": True}, "", "personal.full_address"),
        ("Street address", {}, "", "personal.address"),
        ("City*", {"required": True}, "", "personal.city"),
        ("Postal Code*", {"required": True}, "", "personal.pincode"),
        ("State", {}, "", "personal.state"),
        ("Country/Territory*", {}, "", "personal.country"),
        ("Phone Number*", {"required": True}, "Country/Territory Phone Code India (+91)", "personal.phone_local"),
        ("Phone Number*", {"required": True}, "", "personal.phone"),
        ("Phone Extension", {}, "", "SKIP_FIELD"),
        ("Phone Device Type*", {}, "", "personal.phone_type"),
        (
            "Have you ever worked for Accenture or any of its affiliates?*",
            {},
            "",
            "work.worked_for_this_company_before",
        ),
        ("Have you previously worked at Philips?", {}, "", "ASK_USER"),
    ],
)
def test_workday_style_fields(label, extra, page_text, expected):
    got = rules.match(field(label, **extra), WORKDAY, False, (), page_text)
    assert got == (expected if expected.isupper() else safe_id(expected))


def test_how_did_you_hear_uses_your_answer_or_asks():
    question = field("How Did You Hear About Us?*")
    assert rules.match(question, PROFILE) == "ASK_USER"
    with_answer = Profile(
        {
            **json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8")),
            "application": {"how_heard": "LinkedIn"},
        }
    )
    assert rules.match(question, with_answer) == safe_id("application.how_heard")


JOBS = Profile(
    {
        "experience": [
            {"company": "Acme Systems", "title": "Founding Engineer", "start": "2025-04", "end": ""},
            {"company": "Philips", "title": "Software Engineer", "start": "2023-08", "end": "2025-03"},
        ]
    }
)
ONE_BLOCK = "My Experience Work Experience Work Experience 1 Job Title Company"


@pytest.mark.parametrize(
    "label, extra, page_text, expected",
    [
        ("Job Title*", {}, ONE_BLOCK, "experience.0.title"),
        ("Company*", {}, ONE_BLOCK, "experience.0.company"),
        ("From*", {"placeholder": "MM/YYYY"}, ONE_BLOCK, "experience.0.start.mm_yyyy"),
        ("To*", {"placeholder": "MM/YYYY", "context": "Work Experience 2 To"}, ONE_BLOCK, "experience.1.end.mm_yyyy"),
        ("Month", {"context": "From* current value is MM/YYYY"}, ONE_BLOCK, "experience.0.start.mm"),
        ("Year", {"context": "From* current value is MM/YYYY"}, ONE_BLOCK, "experience.0.start.year"),
    ],
)
def test_work_experience_blocks(label, extra, page_text, expected):
    assert rules.match(field(label, **extra), JOBS, False, (), page_text) == safe_id(expected)


def test_experience_fields_are_not_guessed_when_several_blocks_show_without_numbers():
    two = ONE_BLOCK + " Work Experience 2"
    assert rules.match(field("Job Title*"), JOBS, False, (), two) is None
    assert rules.match(field("To*"), JOBS, False, (), ONE_BLOCK) is None  # current job: no end date


def test_a_required_upload_gets_the_resume_but_never_a_cover_letter_slot():
    from jev_apply.profile import Fact

    docs = {"resume": Fact("resume", "r.pdf", "Résumé / CV (PDF)")}
    upload = {
        "kind": "upload",
        "label": "Select files",
        "context": "Upload a file (5MB max)* Drop files here or",
        "required": True,
    }
    assert rules.document(upload, docs) == "resume"
    assert rules.document({**upload, "required": False}, docs) == "resume"  # Muvi: "Attach your files here"
    other = {**upload, "label": "Other documents (optional)", "context": "Upload any additional documents"}
    assert rules.document(other, docs) is None


def test_blocks_are_known_from_the_heading_above_the_field():
    title = field("Job Title*", section="Work Experience 2")
    page_text = "Work Experience 1 Work Experience 2"
    assert rules.match(title, JOBS, False, (), page_text) == safe_id("experience.1.title")
    school = field("School or University*", section="Education 1")
    edu = Profile(
        {"education": [{"institution": "Example Institute of Technology", "degree": "B.Tech", "end": "2024"}]}
    )
    assert rules.match(school, edu, False, (), "Education 1") == safe_id("education.0.institution")
    assert rules.match(field("Degree*", section="Education 1"), edu, False, (), "") == safe_id("education.0.degree")


def test_address_goes_over_two_lines_only_when_the_form_has_two():
    home = Profile(
        {
            "personal": {
                "address": "7, Lake View Colony, Main Circle, Station Road",
                "address_line1": "7, Lake View Colony",
                "address_line2": "Main Circle, Station Road",
            }
        }
    )
    line1 = field("Address Line 1*", required=True)
    assert rules.match(line1, home, False, (), "Address Line 1 Address Line 2") == safe_id("personal.address_line1")
    assert rules.match(line1, home, False, (), "Address Line 1 City") == safe_id("personal.address")
    assert rules.match(field("Address Line 2"), home, False, (), "") == safe_id("personal.address_line2")


SCHOOLED = Profile(
    {
        **json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8")),
        "school": {"class_10": {"percentage": "92%", "year": 2014}, "class_12": {"percentage": "88%", "year": 2016}},
        "education": [{"degree": "B.Tech", "end": "2022-06", "grade": "8.4 CGPA"}],
        "experience": [{"company": "Acme Systems", "title": "Engineer"}, {"company": "Philips", "title": "Intern"}],
    },
    ROOT / "data",
)


@pytest.mark.parametrize(
    "label, extra, expected",
    [
        ("10th Percentage", {}, "school.class_10.percentage"),
        ("Class XII marks (%)", {}, "school.class_12.percentage"),
        ("12th passing year", {}, "school.class_12.year"),
        ("Higher secondary percentage", {}, "school.class_12.percentage"),
        ("Graduation percentage", {}, "education.0.grade"),
        ("Year of passing", {}, "education.0.end.year"),
        ("Total experience (in months)", {}, "work.total_experience_months"),
        ("Years", {"context": "Total experience"}, "work.total_experience_whole_years"),
        ("Months", {"context": "Total experience"}, "work.total_experience_extra_months"),
        ("Total experience in IT", {}, "work.total_experience_years"),
        ("Company", {"section": "Employment 2"}, "experience.1.company"),
        ("Company", {"section": "Previous employer"}, "experience.1.company"),
        ("Job title", {"section": "Experience #1"}, "experience.0.title"),
        ("Earliest start date", {"placeholder": "DD/MM/YYYY"}, "work.earliest_start_date"),
    ],
)
def test_indian_form_wordings_map_to_their_own_facts(label, extra, expected):
    assert rules.match(field(label, **extra), SCHOOLED, False, ()) == safe_id(expected)


@pytest.mark.parametrize(
    "label",
    ["Years of experience in Python", "Experience with Kubernetes (years)", "Total experience (years and months)"],
)
def test_experience_in_one_skill_or_in_mixed_units_is_not_guessed(label):
    # Years with one skill are asked (once, then saved) unless skill_years has them; mixed units go to the model.
    got = rules.match(field(label), SCHOOLED, False, ())
    assert got in {None, "ASK_USER"} and got != safe_id("work.total_experience_years")


@pytest.mark.parametrize("label", ["Date of birth", "Father's name", "Mother's name", "SSC board", "Marital status"])
def test_facts_nothing_else_can_stand_in_for_are_asked_when_missing(label):
    assert rules.match(field(label), SCHOOLED, False, ()) == "ASK_USER"


def test_values_are_fitted_to_the_field_or_left_to_you():
    start = SCHOOLED.facts["work.earliest_start_date"]  # 2026-11-02
    assert rules.fit(start, field("Start", placeholder="DD/MM/YYYY"), SCHOOLED) == "02/11/2026"
    assert rules.fit(start, field("Start", placeholder="mm-dd-yyyy"), SCHOOLED) == "11-02-2026"
    assert rules.fit(start, field("Start", placeholder="Month YYYY"), SCHOOLED) == "November 2026"
    end = SCHOOLED.facts["education.0.end"]  # 2022-06: no day
    assert rules.fit(end, field("End", placeholder="DD/MM/YYYY"), SCHOOLED) is None
    assert rules.fit(end, field("End", placeholder="MM/YYYY"), SCHOOLED) == "06/2022"
    prose = Profile({"work": {"availability": "Within 15 days"}}, ROOT).facts["work.availability"]
    assert rules.fit(prose, field("Start", placeholder="DD/MM/YYYY"), SCHOOLED) is None
    years = SCHOOLED.facts["work.total_experience_years"]  # 4.5
    assert rules.fit(years, field("Exp", input_type="number", step="0.5"), SCHOOLED) == "4.5"
    assert rules.fit(years, field("Exp", input_type="number"), SCHOOLED) == "4"  # whole years in a step-1 box
    phone = SCHOOLED.facts["personal.phone"]  # +91 98765 43210
    assert rules.fit(phone, field("Mobile", maxlength=10), SCHOOLED) == "9876543210"
    assert rules.fit(SCHOOLED.facts["personal.email"], field("Email", maxlength=5), SCHOOLED) is None


def test_new_personal_facts_are_answered_from_the_profile():
    assert rules.match(field("Nationality"), SCHOOLED, False, ()) == safe_id("personal.nationality")
    backlogs = rules.match(field("Any active backlogs?"), SCHOOLED, False, ())
    assert backlogs == safe_id("academics.has_active_backlogs") and SCHOOLED.by_id[backlogs].value == "No"
    count = rules.match(field("Number of backlogs"), SCHOOLED, False, ())
    assert SCHOOLED.by_id[count].value == "0"


def test_a_model_pick_must_share_a_word_with_the_question():
    degree = SCHOOLED.facts["education.0.degree"]
    assert not rules.related(degree, field("Have you used AI tools? If yes, which ones?"))
    assert rules.related(SCHOOLED.facts["links.github"], field("GitHub username"))


@pytest.mark.parametrize(
    "label, extra, expected",
    [
        ("Do you have an experience within a tech startup?", {}, None),
        ("Tell us about a time you solved a hard problem", {"input_type": "textarea"}, "DRAFT_ANSWER"),
        ("What country are you based in?", {}, "personal.country"),
        ("Location (City)", {}, "personal.city"),
        ("Do you have a relative working at Groww?", {}, "work.has_relative_at_company"),
        ("Father's name", {}, "ASK_USER"),
    ],
)
def test_live_site_wordings(label, extra, expected):
    got = rules.match(field(label, **extra), SCHOOLED, False, ())
    assert got == (safe_id(expected) if expected and not expected.isupper() else expected)


def test_a_yes_no_experience_question_behind_a_generic_label_is_not_a_duration():
    lever = field("Type your response", context="Do you have an experience within a tech startup? ✱")
    assert rules.match(lever, SCHOOLED, False, ()) is None
    assert rules.match(field("How many years of experience do you have?"), SCHOOLED, False, ()) == safe_id(
        "work.total_experience_years"
    )


def test_years_with_a_skill_come_from_skill_years():
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    profile = Profile({**data, "skill_years": {"Python": 3, "PLC programming": 2}}, ROOT / "data")
    linkedin = field("How many years of work experience do you have with Python?")
    assert profile.by_id[rules.match(linkedin, profile, False, ())].value == "3"
    plc = field("Years of experience in PLC Programming *")
    assert profile.by_id[rules.match(plc, profile, False, ())].value == "2"
    k8s = field("How many years of experience do you have with Kubernetes?")
    assert rules.match(k8s, profile, False, ()) == "ASK_USER"


def test_years_with_an_apostrophe_and_a_default_for_unlisted_skills():
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    profile = Profile({**data, "skill_years": {"Python": 3, "default": 2}}, ROOT / "data")
    servo = field("How many year's of hands-on experience do you have with Servo/Stepper systems*")
    assert profile.by_id[rules.match(servo, profile, False, ())].value == "2"


def test_years_with_a_skill_pick_the_right_listed_skill():
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    years = {"Git": 2, "BACnet": 1.5, "ASP.NET Core": 1.5, "RAG": 0.5, "PLC integration": 0.5, "Industrial IoT": 1.5}
    profile = Profile({**data, "skill_years": years}, ROOT / "data")

    def got(question):
        key = rules.match(field(question), profile, False, ())
        return key if key == "ASK_USER" else profile.by_id[key].key

    assert got("How many years of work experience do you have with Git?") == "skill_years.Git"
    # A short skill is a whole word, never part of another one.
    assert got("How many years of experience do you have with digital twins?") == "ASK_USER"
    assert got("How many years of experience do you have with storage systems?") == "ASK_USER"
    # ".NET" abbreviates ASP.NET Core, not BACnet; the longest listed skill inside the question wins.
    assert got("How many years of experience do you have with .NET?") == "skill_years.ASP.NET Core"
    assert got("How many years of work experience do you have with Industrial IoT?") == "skill_years.Industrial IoT"
    assert got("How many years of experience do you have with PLC?") == "skill_years.PLC integration"
    assert got("How many years of experience do you have with PLC programming?") == "ASK_USER"
    # A skill counts where a part of the question starts, never in the middle of another skill's name.
    years = {**years, "C programming": 1.5, "Programming": 2, "Python": 2, "IIoT": 1.5}
    profile = Profile({**data, "skill_years": years}, ROOT / "data")
    assert got("How many years of work experience do you have with PLC Programming?") == "ASK_USER"
    assert got("How many years of work experience do you have with C (Programming Language)?") == (
        "skill_years.C programming"
    )
    assert got("How many years of work experience do you have with Python (Programming Language)?") == (
        "skill_years.Python"
    )
    assert got("How many years of experience you have in IIoT/IoT/Industrial Automation?") == "skill_years.IIoT"


def test_decimal_skill_years_in_a_whole_number_box_are_completed_years():
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    profile = Profile({**data, "skill_years": {"C#": 1.5}}, ROOT / "data")
    csharp = profile.facts["skill_years.C#"]
    assert rules.fit(csharp, field("Years with C#", input_type="number"), profile) == "1"
    assert rules.fit(csharp, field("Years with C#", input_type="number", step="0.5"), profile) == "1.5"
    # A text box too: LinkedIn rejects "1.5" as "Invalid input".
    assert rules.fit(csharp, field("How many years of work experience do you have with C#?"), profile) == "1"
    total = profile.facts["work.total_experience_years"]  # 4.5
    assert rules.fit(total, field("How many years of work experience do you have?"), profile) == "4"
    assert rules.fit(total, field("Total experience"), profile) == "4.5"


def test_a_months_box_beside_a_years_box_gets_the_months_beyond_whole_years():
    months = field("Please select your total additional months of experience:*")
    assert rules.match(months, PROFILE, False, ()) == safe_id("work.total_experience_extra_months")


def test_worked_on_a_technology_is_not_a_previous_employer_question():
    servo = field("HAVE YOU WORKED ON SIEMENS SERVO DRIVES, SERVO MOTORS ETC?*")
    assert rules.match(servo, PROFILE, False, ()) != safe_id("work.worked_for_this_company_before")
    before = field("Have you ever worked for Example Corp before?")
    assert rules.match(before, PROFILE, False, ()) in {safe_id("work.worked_for_this_company_before"), None}


def test_a_generic_word_is_not_read_as_a_longer_listed_skill():
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    profile = Profile({**data, "skill_years": {"Prompt engineering": 0.5, "ASP.NET Core": 1.5}}, ROOT / "data")
    engineering = field("How many years of Engineering experience do you have?")
    assert rules.match(engineering, profile, False, ()) != safe_id("skill_years.Prompt engineering")
    dotnet = field("How many years of work experience do you have with .NET?")
    assert rules.match(dotnet, profile, False, ()) == safe_id("skill_years.ASP.NET Core")
    aspnet = field("How many years of work experience do you have with ASP.NET?")
    assert rules.match(aspnet, profile, False, ()) == safe_id("skill_years.ASP.NET Core")  # the start of it


def test_list_and_example_questions_are_drafted_not_asked():
    # Written from your résumé facts by the drafting model (which returns nothing, so it's asked, when they don't
    # cover it). Short labels stay with the other rules.
    for question in (
        "Have you worked on both small-scale and large-scale projects/products? Please provide examples.",
        "Please list the primary technologies you've worked with and mention any advanced or latest versions used",
        "Have you led a team? If so, how many people and what did you own?",
    ):
        assert rules.match(field(question, input_type="textarea"), PROFILE) == "DRAFT_ANSWER", question
    assert rules.match(field("Please mention your notice period"), PROFILE) == safe_id("work.notice_period_text")


def test_certifications_come_from_the_profile_fact():
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    certs = {"value": "AWS Cloud Practitioner (2025)", "about": "Certifications / courses completed"}
    profile = Profile({**data, "certifications": certs}, ROOT / "data")
    asked = field("Have you completed any relevant certifications or training programs? Please list them.")
    assert rules.match(asked, profile) == safe_id("certifications")
    assert rules.match(field("Certification number"), profile) != safe_id("certifications")
    assert rules.match(field("Upload your certifications", kind="upload"), profile) != safe_id("certifications")
    # Without the fact, nothing stands in for it: drafted (and asked when the facts don't cover it).
    assert rules.match(asked, PROFILE) == "DRAFT_ANSWER"


def test_last_working_day_when_not_serving_notice():
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    free = Profile({**data, "work": {**data["work"], "serving_notice": False}}, ROOT / "data")
    serving = Profile({**data, "work": {**data["work"], "serving_notice": True}}, ROOT / "data")
    conditional = field("If you are serving notice, what will be your official last working day?")
    assert rules.match(conditional, free) == "DRAFT_ANSWER"  # "Not serving notice; I can join in N days."
    assert rules.match(conditional, serving) == "ASK_USER"  # the date is yours
    assert rules.match(field("Last working day", input_type="date"), free, True) == "ASK_USER"
    both = field("Could you please confirm your official notice period/last working day?")
    assert rules.match(both, free) == safe_id("work.notice_period_text")
    assert rules.match(both, serving) == "ASK_USER"


def test_years_with_a_skill_match_other_wordings_of_a_listed_skill():
    data = json.loads((ROOT / "data" / "profile.example.json").read_text(encoding="utf-8"))
    profile = Profile({**data, "skill_years": {"REST API": 1.5, "Agentic AI": 0.5}}, ROOT / "data")
    rest = rules.match(field("How many years of work experience do you have with REST APIs?"), profile)
    assert profile.by_id[rest].key == "skill_years.REST API"
    agents = rules.match(field("How many years of work experience do you have with AI Agents?"), profile)
    assert profile.by_id[agents].key == "skill_years.Agentic AI"
    assert rules.match(field("How many years of work experience do you have with SQL Server?"), profile) == ("ASK_USER")
