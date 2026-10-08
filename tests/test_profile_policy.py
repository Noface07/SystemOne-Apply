"""Profile flattening, derived CTC/date variants, and code-level guards. Offline."""

import json

import pytest

from jev_apply.policy import Policy
from jev_apply.profile import MAX_SOURCES, Profile, native_date, safe_id

DATA = {
    "personal": {"first_name": "Aarav", "last_name": "Sharma", "phone": "+91 98765 43210"},
    "work": {"total_experience_years": 4.5, "earliest_start_date": "2026-11-02", "serving_notice": False},
    "compensation": {
        "currency": "INR",
        "current": {"fixed_annual": 1500000, "variable_annual": 200000},
        "expected": {"total_annual": 2200000},
    },
    "education": [{"degree": "B.Tech", "end": "2022-06"}],
    "skills": ["Python", "Go"],
    "custom": {"shirt": {"value": "M", "about": "T-shirt size for joining kit"}},
    "_ignored": "x",
    "documents": {"resume": "missing.pdf"},
}


def facts(profile):
    return {k: f.value for k, f in profile.facts.items()}


def test_current_ctc_is_derived_and_kept_apart_from_expected():
    values = facts(Profile(DATA))
    assert values["compensation.current.total_annual"] == "1700000"
    assert values["compensation.current.total_annual_lpa"] == "17"
    assert values["compensation.current.fixed_annual_lpa"] == "15"
    assert values["compensation.expected.total_annual_lpa"] == "22"
    assert values["compensation.expected.total_annual_monthly"] == "183333"
    profile = Profile(DATA)
    current = profile.facts["compensation.current.total_annual_lpa"].about
    expected = profile.facts["compensation.expected.total_annual_lpa"].about
    assert "CURRENT" in current and "NOT the expected" in current and "LAKHS" in current
    assert "EXPECTED" in expected and "NOT the current" in expected


def test_flattening_dates_lists_booleans_and_custom_leaves():
    profile = Profile(DATA)
    values = facts(profile)
    assert values["personal.full_name"] == "Aarav Sharma"
    assert values["work.total_experience_years"] == "4.5"
    assert values["work.serving_notice"] == "No"
    assert values["skills"] == "Python, Go"
    assert values["education.0.end.year"] == "2022"
    assert values["education.0.end.month_year"] == "June 2022"
    assert values["work.earliest_start_date.dd_mm_yyyy"] == "02/11/2026"
    assert profile.facts["custom.shirt"].about == "T-shirt size for joining kit"
    assert "Education entry 1" in profile.facts["education.0.degree"].about
    assert not any(k.startswith("_") for k in values)
    assert profile.facts["work.earliest_start_date"].kind == "date"
    assert set(profile.sources(dates_only=True)) == {safe_id("work.earliest_start_date"), safe_id("education.0.end")}


def test_documents_must_exist_and_answers_become_facts(tmp_path):
    (tmp_path / "cv.pdf").write_bytes(b"%PDF")
    (tmp_path / "profile.json").write_text(json.dumps({**DATA, "documents": {"resume": "cv.pdf", "cover": "nope.pdf"}}))
    (tmp_path / "answers.json").write_text(
        json.dumps({"answers": [{"id": "why", "question": "Why us?", "answer": "Because."}]})
    )
    profile = Profile.load(tmp_path / "profile.json", tmp_path / "answers.json", tmp_path / "learned.json")
    assert set(profile.documents) == {"resume"} and set(profile.missing_documents) == {"cover"}
    assert profile.facts["answer.why"].value == "Because."
    profile.remember("Notice period negotiable?", "Yes, up to 15 days")
    saved = json.loads((tmp_path / "learned.json").read_text())["answers"]
    assert saved[0]["answer"] == "Yes, up to 15 days"
    assert Profile.load(tmp_path / "profile.json", None, tmp_path / "learned.json").facts["answer.learned_1"]


def test_too_many_facts_is_a_clear_error():
    from jev_apply.profile import MAX_SOURCES_SHORTLISTED

    with pytest.raises(ValueError, match="limit"):
        Profile({"many": {f"k{i}": "v" for i in range(MAX_SOURCES_SHORTLISTED + 1)}})
    assert len(Profile({"many": {f"k{i}": "v" for i in range(MAX_SOURCES + 1)}}).facts) > MAX_SOURCES


@pytest.mark.parametrize(
    "value,kind,expected",
    [
        ("2026-11-02", "date", ("2026-11-02", None)),
        ("2022-06", "month", ("2022-06", None)),
        ("2022-06", "date", ("2022-06-01", "day not in profile; used the 1st")),
        ("June 2022", "date", None),
        ("2022-06-01", "time", None),
    ],
)
def test_native_date(value, kind, expected):
    assert native_date(value, kind) == expected


def test_guards():
    policy = Policy.load()
    assert policy.sensitive({"label": "Gender", "context": "Voluntary self-identification"})
    assert policy.sensitive({"label": "Expected CTC"})
    assert policy.never_fill({"label": "PAN number"})
    assert not policy.never_fill({"label": "Company name"})
    assert not policy.never_fill({"label": "Panel interview availability"})
    assert policy.submit_like({"role": "button", "label": "Submit application"})
    assert policy.submit_like({"role": "link", "label": "Apply now"})
    assert not policy.submit_like({"role": "button", "label": "Save and continue"})
    assert not policy.submit_like({"role": "textbox", "label": "Submit a portfolio link"})


def test_drafts_can_never_be_typed_unseen():
    with pytest.raises(ValueError):
        Policy(drafts="auto")


def test_code_defaults_match_the_documented_example_and_cannot_be_emptied():
    example = json.loads(open("data/policy.example.json").read())
    default = Policy()
    keys = ("confirm_patterns", "never_fill_patterns", "submit_patterns", "min_value_margin", "drafts")
    keys += ("auto_consent", "consent_patterns", "declaration_patterns", "login_wait_s", "submit_drafts")
    keys += ("confirm_wait_s", "per_company", "company_gap_days")
    for key in keys:
        assert getattr(default, key) == example[key]
    with pytest.raises(ValueError):
        Policy(submit_patterns=[])


def test_lists_also_become_one_item_facts_for_tag_fields():
    profile = Profile(DATA)
    values = facts(profile)
    assert values["skills"] == "Python, Go" and "ONE comma-separated" in profile.facts["skills"].about
    assert values["skills.item_1"] == "Python" and values["skills.item_2"] == "Go"
    assert profile.facts["skills.item_2"].kind == "item" and "one at a time" in profile.facts["skills.item_2"].about
    assert "skills.item_1" not in profile.candidate_state()  # the model's page state gets the list once
    tried = {safe_id("skills.item_1")}
    assert safe_id("skills.item_1") not in profile.sources(exclude=tried)


def test_a_dropdown_placeholder_read_with_the_label_is_not_part_of_the_question():
    from jev_apply.profile import same_question

    assert same_question("Current Salary", "Current Salary Select...")
    assert same_question("Notice Period", "Notice Period * Select One")
    assert same_question("Primary skills", "Primary skills 0 items selected")
    assert not same_question("Current Salary", "Expected Salary Select...")


def test_a_step_button_is_not_sensitive_because_of_the_step_it_sits_in():
    from jev_apply.policy import Policy

    policy = Policy()
    step = "4/5 pages Work authorization Not sure how to answer the following questions?"
    assert not policy.sensitive({"kind": "click", "role": "button", "label": "Review", "context": step})
    assert not policy.sensitive({"kind": "click", "role": "button", "label": "Next", "context": step})
    # An answer in that step still is.
    assert policy.sensitive(
        {"kind": "click", "role": "radio", "label": "Yes", "context": "Are you authorized to work?"}
    )
    assert policy.sensitive(
        {"kind": "click", "role": "button", "label": "Yes", "context": "Will you require visa sponsorship?"}
    )
