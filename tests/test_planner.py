"""The Laya backend's fixed form-walking procedure, with a fake Laya."""

import pytest

from jev_apply import laya_backend, model, planner

CANDIDATE = {
    "personal.first_name": "Aarav",
    "personal.city": "Udaipur",
    "work.notice_period_text": "15 days",
    "self_identification.gender": "Male",
    "work_authorization.requires_visa_sponsorship": "No",
    "preferences.willing_to_relocate": "Yes",
    "preferences.preferred_locations": "Bengaluru",
    "skills": "OPC UA, DNP3",
}


@pytest.fixture
def laya(monkeypatch):
    calls = []

    def fake(body):
        calls.append(body)
        options = list(body["questions"]["option"]["criteria"])
        return {
            "answers": {
                "option": {
                    "choice": options[-1],
                    "probabilities": {k: 0.7 if k == options[-1] else 0.3 / (len(options) - 1) for k in options},
                    "confidence": 0.5,
                }
            }
        }

    monkeypatch.setattr(laya_backend, "system_one", fake)
    monkeypatch.setenv("DECISION_BACKEND", "laya")
    return calls


def page(*actions):
    return {"url": "https://jobs.example/apply", "title": "Apply", "text": "", "doc": 1, "actions": list(actions)}


def fill(n, label, value="", **extra):
    return {"id": f"f{n}", "node": n, "kind": "fill", "role": "textbox", "label": label, "value": value, **extra}


def select(n, question, options, current="Select…"):
    return [
        {
            "id": f"s{n}_{i}",
            "node": n,
            "kind": "select",
            "role": "combobox",
            "label": f"{question} → {o}",
            "value": o,
            "current_value": current,
        }
        for i, o in enumerate(options)
    ]


def button(n, label, **extra):
    return {"id": f"b{n}", "node": n, "kind": "click", "role": "button", "label": label, "value": "", **extra}


def choose(p):
    return model.choose(p, "goal", [], CANDIDATE)


def test_opens_the_embedded_form_then_dismisses_cookies(laya):
    frame = {"id": "e1", "node": 1, "kind": "frame", "role": "iframe", "label": "Embedded page: Job application form"}
    assert choose(page(frame))["operation"] == "OPEN_FORM"
    d = choose(page(fill(1, "First name *"), button(2, "Only necessary cookies")))
    assert d["operation"] == "CLICK" and d["action"]["label"] == "Only necessary cookies"


def test_fills_fields_top_to_bottom_skipping_filled_ones(laya):
    d = choose(page(fill(1, "First name *", "Aarav"), fill(2, "Email *"), fill(3, "Phone *")))
    assert d["operation"] == "TYPE_TEXT" and d["action"]["label"] == "Email *" and d["operation_probability"] == 1.0


def test_dropdown_matching_the_profile_is_picked_without_asking_laya(laya):
    d = choose(page(*select(4, "Gender", ["Female", "Male", "Prefer not to say"])))
    assert d["operation"] == "SELECT" and d["action"]["value"] == "Male" and d["target_probability"] == 1.0
    d = choose(page(*select(5, "Notice period *", ["Immediate", "15 days", "30 days"])))
    assert d["action"]["value"] == "15 days" and not laya


def test_laya_answers_dropdowns_the_profile_does_not_cover_with_its_own_confidence(laya):
    candidate = {**CANDIDATE, "education.0.degree": "Diploma in Engineering"}
    d = model.choose(
        page(*select(6, "Highest degree level", ["Diploma", "Bachelor's", "Master's"])), "goal", [], candidate
    )
    assert len(laya) == 1 and d["action"]["value"] == "Master's" and d["target_probability"] == 0.7
    assert set(laya[0]["questions"]["option"]["criteria"].values()) == {"Diploma", "Bachelor's", "Master's"}


def test_yes_no_buttons_and_radios_follow_the_profile(laya):
    visa = [
        button(7, "Yes", pressed="false", context="Will you require visa sponsorship?"),
        button(8, "No", pressed="false", context="Will you require visa sponsorship?"),
    ]
    d = choose(page(*visa))
    assert d["action"]["label"] == "No" and not laya
    answered = [dict(visa[0]), dict(visa[1], pressed="true")]
    assert choose(page(*answered, button(9, "Next")))["action"]["label"] == "Next"


def test_suggestions_matching_a_fact_are_clicked(laya):
    typed = fill(1, "Current location *", "Udaipur")
    suggestion = {
        "id": "o1",
        "node": 5,
        "kind": "click",
        "role": "option",
        "label": "Udaipur",
        "context": "Current location",
    }
    assert choose(page(typed, suggestion))["action"]["label"] == "Udaipur"


def test_when_done_it_advances_then_submits_then_scrolls(laya):
    done = fill(1, "First name *", "Aarav")
    assert (
        choose(page(done, button(2, "Back"), button(3, "Save and continue")))["action"]["label"] == "Save and continue"
    )
    assert choose(page(done, button(3, "Submit application")))["action"]["label"] == "Submit application"
    scroll = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down"}
    assert choose(page(done, scroll))["operation"] == "SCROLL_DOWN"
    # Fields and no way on: stuck, never "done" (only reaching the submit button is).
    stuck = choose(page(done))
    assert stuck["operation"] == "BLOCKED" and "Nothing more the agent can fill" in stuck["reason"]
    assert model.choose(page(done), "goal", [], CANDIDATE, ("REVIEW",))["operation"] == "BLOCKED"


def test_cover_letter_upload_never_gets_the_resume():
    from jev_apply import rules
    from jev_apply.profile import Fact

    docs = {"resume": Fact("resume", "r.pdf", "Résumé / CV (PDF)")}
    cover = {"kind": "upload", "label": "Attach", "context": "Cover letter (optional) Attach"}
    resume = {"kind": "upload", "label": "Attach", "context": "Resume/CV * Attach PDF or DOCX", "required": True}
    assert rules.document(cover, docs) == "SKIP_FIELD"
    assert rules.document(resume, docs) == "resume"


def test_workday_dropdowns_open_and_prefix_matches_are_taken(laya):
    state = button(10, "Select One", expanded="false", context="State")
    d = choose(page(fill(1, "City*", "Udaipur"), state))
    assert d["operation"] == "CLICK" and d["action"]["label"] == "Select One"
    code = [
        {
            "id": f"o{i}",
            "node": 20 + i,
            "kind": "click",
            "role": "option",
            "label": o,
            "context": "Country/Territory Phone Code",
        }
        for i, o in enumerate(["Iceland (+354)", "India (+91)", "Indonesia (+62)"])
    ]
    candidate = {**CANDIDATE, "personal.country": "India"}
    d = model.choose(page(*code), "goal", [], candidate)
    assert d["action"]["label"] == "India (+91)" and not laya


def test_an_empty_page_is_waited_for_before_handing_over(laya):
    wait = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}
    loading = page(wait)
    assert model.choose(loading, "goal", [], CANDIDATE)["operation"] == "WAIT"
    waited = [{"kind": "wait"}] * planner.NO_FORM_WAITS  # ~30 s: Workday's first load can take that long
    assert model.choose(loading, "goal", waited, CANDIDATE)["operation"] == "BLOCKED"
    assert model.choose(loading, "goal", [{"kind": "wait"}, {"kind": "fill"}], CANDIDATE)["operation"] == "WAIT"


def test_next_is_not_pressed_twice_when_the_site_refuses_to_move_on(laya):
    done = page(fill(1, "First name *", "Aarav"), button(3, "Save and Continue"))
    clicked = [
        {"kind": "click", "action": "Save and Continue", "url": done["url"], "page_changed": False},
        {"kind": "scroll"},
    ]
    assert model.choose(done, "goal", clicked, CANDIDATE)["operation"] == "BLOCKED"
    # LinkedIn: every step has the same address; a step that changed and needs nothing is pressed on.
    moved = [{**clicked[0], "page_changed": True}]
    assert model.choose(done, "goal", moved, CANDIDATE)["action"]["label"] == "Save and Continue"
    progressed = [clicked[0], {"kind": "fill", "action": "Email *", "url": done["url"]}]
    assert model.choose(done, "goal", progressed, CANDIDATE)["action"]["label"] == "Save and Continue"


def test_it_waits_for_a_new_page_to_stop_changing_before_typing(laya):
    wait = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}
    form = page(fill(1, "Given Name(s)*"), wait)
    assert model.choose(form, "goal", [], CANDIDATE)["operation"] == "WAIT"  # just arrived
    after_next = [{"kind": "click", "page_changed": True}]
    assert model.choose(form, "goal", after_next, CANDIDATE)["operation"] == "WAIT"
    still_drawing = after_next + [{"kind": "wait", "page_changed": True}]
    assert model.choose(form, "goal", still_drawing, CANDIDATE)["operation"] == "WAIT"
    quiet = still_drawing + [{"kind": "wait", "page_changed": False}]
    assert model.choose(form, "goal", quiet, CANDIDATE)["operation"] == "TYPE_TEXT"
    typed = quiet + [{"kind": "fill", "page_changed": True}]
    assert model.choose(form, "goal", typed, CANDIDATE)["operation"] == "TYPE_TEXT"  # no wait between fields


def test_search_prompts_already_showing_a_selection_are_left_alone(laya):
    chosen = fill(1, "How Did You Hear About Us?*", context="How Did You Hear About Us? 1 item selected, LinkedIn")
    empty = fill(2, "How Did You Hear About Us?*", context="How Did You Hear About Us? 0 items selected")
    assert choose(page(chosen, button(3, "Next")))["action"]["label"] == "Next"
    assert choose(page(empty))["operation"] == "TYPE_TEXT"


def test_search_results_ending_with_your_answer_are_clicked(laya):
    result = {
        "id": "o1",
        "node": 5,
        "kind": "click",
        "role": "option",
        "label": "Job Boards > LinkedIn",
        "context": "How did you hear",
    }
    other = {**result, "id": "o2", "node": 6, "label": "Social Media > Twitter"}
    assert (
        model.choose(page(other, result), "goal", [], {**CANDIDATE, "application.how_heard": "LinkedIn"})["action"][
            "id"
        ]
        == "o1"
    )


def test_i_currently_work_here_is_ticked_only_in_the_first_work_experience_block(laya):
    candidate = {**CANDIDATE, "experience.0.company": "Acme Systems"}
    box = {
        "id": "c1",
        "node": 3,
        "kind": "click",
        "role": "checkbox",
        "label": "I currently work here",
        "checked": "false",
        "section": "Work Experience 1",
    }
    second = {**box, "id": "c2", "node": 9, "section": "Work Experience 2"}
    assert model.choose(page(box, second), "goal", [], candidate)["action"]["id"] == "c1"
    assert model.choose(page(second, button(4, "Next")), "goal", [], candidate)["action"]["label"] == "Next"
    ticked = {**box, "checked": "true"}
    assert model.choose(page(ticked, second, button(4, "Next")), "goal", [], candidate)["action"]["label"] == "Next"
    ended = {**candidate, "experience.0.end": "2025-03"}
    assert model.choose(page(box, button(4, "Next")), "goal", [], ended)["action"]["label"] == "Next"


def test_add_another_is_pressed_until_every_job_has_a_block(laya):
    candidate = {**CANDIDATE, "experience.0.company": "Acme Systems", "experience.1.company": "Philips"}
    add = button(7, "Add Another", section="Work Experience 1")
    url = "https://jobs.example/apply"
    assert model.choose(page(add), "goal", [], candidate)["action"]["label"] == "Add Another"
    pressed = [{"kind": "click", "action": "Add Another", "section": "Work Experience 1", "url": url}]
    moved = {**add, "section": "Work Experience 2"}  # the heading above it changed after the new block
    assert model.choose(page(moved, button(8, "Next")), "goal", pressed, candidate)["action"]["label"] == "Next"
    education = button(9, "Add", section="Education")
    with_school = {**candidate, "education.0.institution": "Example Institute of Technology"}
    assert model.choose(page(education), "goal", [], with_school)["action"]["label"] == "Add"
    assert model.choose(page(education, button(8, "Next")), "goal", [], candidate)["action"]["label"] == "Next"


def test_enter_is_pressed_in_a_search_prompt_that_shows_no_results(laya):
    school = fill(1, "School or University*", "Example", role="combobox", placeholder="Search")
    typed = [{"kind": "fill", "action": "School or University*"}]
    d = model.choose(page(school), "goal", typed, CANDIDATE)
    assert d["operation"] == "PRESS_ENTER" and d["action"]["kind"] == "enter" and d["action"]["node"] == 1
    plain = fill(2, "Nickname", "Yuv")
    assert (
        model.choose(page(plain, button(3, "Next")), "goal", [{"kind": "fill", "action": "Nickname"}], CANDIDATE)[
            "operation"
        ]
        != "PRESS_ENTER"
    )


def test_degree_abbreviations_and_ampersands_match_the_names_sites_use(laya):
    degree = [
        {"id": f"d{i}", "node": 40 + i, "kind": "click", "role": "option", "label": o, "context": "Degree"}
        for i, o in enumerate(["Bachelor of Science", "Bachelor of Technology", "Master of Technology"])
    ]
    candidate = {**CANDIDATE, "education.0.degree": "B.Tech"}
    assert model.choose(page(*degree), "goal", [], candidate)["action"]["label"] == "Bachelor of Technology"
    fields = [
        {
            "id": "f1",
            "node": 50,
            "kind": "click",
            "role": "option",
            "label": "Computer Science & Engineering",
            "context": "Field of Study",
        }
    ]
    candidate = {**CANDIDATE, "education.0.field": "Computer Science and Engineering"}
    assert model.choose(page(*fields), "goal", [], candidate)["action"]["id"] == "f1" and not laya


def option(n, label, context=""):
    return {"id": f"o{n}", "node": n, "kind": "click", "role": "option", "label": label, "context": context}


def test_an_open_list_is_answered_for_its_own_question_not_by_any_matching_value(laya):
    # "Yes" is some fact's value (relocate), but nothing in the profile is about non-competes: nothing is clicked,
    # and no model is asked to guess.
    q = "Are you bound by a non-compete agreement?"
    d = choose(page(option(1, "Yes", q), option(2, "No", q), button(3, "Next")))
    assert d["action"]["label"] == "Next" and not laya


def test_a_dropdown_list_without_context_takes_the_question_of_the_control_just_opened(laya):
    opened = [
        {"kind": "click", "action": "Select One", "context": "Notice period*", "url": "https://jobs.example/apply"}
    ]
    listed = page(option(1, "Immediate"), option(2, "15 days"), option(3, "30 days"))
    d = model.choose(listed, "goal", opened, CANDIDATE)
    assert d["action"]["label"] == "15 days" and not laya


def test_suggestions_follow_what_was_typed_and_an_unoffered_item_is_never_swapped(laya):
    typed = [{"kind": "fill", "action": "Current location *", "typed": "Udaipur"}]
    places = page(option(1, "Udaipurwati, Rajasthan"), option(2, "Udaipur, Rajasthan, India"))
    d = model.choose(places, "goal", typed, {**CANDIDATE, "personal.country": "India"})
    assert d["action"]["label"] == "Udaipur, Rajasthan, India"
    skill = [{"kind": "fill", "action": "Key skills", "typed": "Kafka"}]
    d = model.choose(page(option(1, "Java"), option(2, "JavaScript"), button(3, "Next")), "goal", skill, CANDIDATE)
    assert d["action"]["label"] == "Next" and not laya


def test_long_lists_are_narrowed_to_options_sharing_words_with_your_answer(laya):
    degrees = [f"Diploma {i}" for i in range(15)] + ["Bachelor's Degree", "Master's Degree", "Doctorate"]
    candidate = {**CANDIDATE, "education.0.degree": "B.Tech"}
    d = model.choose(page(*select(9, "Highest qualification", degrees)), "goal", [], candidate)
    assert d["action"]["value"] == "Bachelor's Degree" and d["target_probability"] == 0.7 and not laya


def test_lever_style_radio_questions_are_answered_separately(laya):
    def radio(n, label, question):
        return {
            "id": f"r{n}",
            "node": n,
            "kind": "click",
            "role": "radio",
            "label": label,
            "checked": "false",
            "context": f"{question} Yes No",
        }

    relocate = [radio(1, "Yes", "Are you willing to relocate?"), radio(2, "No", "Are you willing to relocate?")]
    visa = [radio(3, "Yes", "Do you require visa sponsorship?"), radio(4, "No", "Do you require visa sponsorship?")]
    assert choose(page(*relocate, *visa))["action"]["id"] == "r1"
    answered = [dict(relocate[0], checked="true"), relocate[1]]
    assert choose(page(*answered, *visa))["action"]["id"] == "r4" and not laya


def test_buttons_without_aria_pressed_answer_a_question_by_their_chosen_state(laya):
    q = "Will you require visa sponsorship? *"
    yes = button(1, "Yes", chosen="false", context=f"{q} Yes No")
    no = button(2, "No", chosen="false", context=f"{q} Yes No")
    assert choose(page(yes, no, button(3, "Next")))["action"]["label"] == "No"
    assert choose(page(yes, dict(no, chosen="true"), button(3, "Next")))["action"]["label"] == "Next"
    # Buttons that do things are never taken for answers.
    tools = [
        button(4, "Upload", chosen="false", context="Resume *"),
        button(5, "Remove", chosen="false", context="Resume *"),
    ]
    assert choose(page(*tools, button(3, "Next")))["action"]["label"] == "Next"


def test_tickbox_groups_get_the_items_your_profile_lists(laya):
    def box(n, label, checked="false"):
        return {
            "id": f"c{n}",
            "node": n,
            "kind": "click",
            "role": "checkbox",
            "label": label,
            "checked": checked,
            "context": "Preferred locations Bengaluru Chennai Pune",
        }

    d = choose(page(box(1, "Bengaluru"), box(2, "Chennai"), box(3, "Pune"), button(4, "Next")))
    assert d["action"]["label"] == "Bengaluru"
    d = choose(page(box(1, "Bengaluru", "true"), box(2, "Chennai"), box(3, "Pune"), button(4, "Next")))
    assert d["action"]["label"] == "Next" and not laya  # never unticks, never guesses


def test_a_value_the_site_filled_in_is_corrected_once(laya):
    wrong = fill(1, "First name *", "Yuv")
    d = choose(page(wrong, button(2, "Next")))
    assert d["operation"] == "TYPE_TEXT" and d["action"]["replaces"] == "Yuv"
    done = [{"kind": "fill", "action": "First name *", "url": "https://jobs.example/apply"}]
    assert model.choose(page(wrong, button(2, "Next")), "goal", done, CANDIDATE)["action"]["label"] == "Next"
    right = fill(1, "First name *", "aarav")  # formatting differences are not wrong
    assert choose(page(right, button(2, "Next")))["action"]["label"] == "Next"
    preset = select(3, "Gender", ["Female", "Male", "Prefer not to say"], current="Female")
    d = choose(page(*preset, button(2, "Next")))
    assert d["action"]["value"] == "Male" and d["action"]["replaces"] == "Female"


def test_invalid_fields_and_unanswered_required_questions_are_named_in_the_handover(laya):
    email = fill(1, "Email *", "x@", invalid=True, error="Enter a valid email")
    # 20 cities, none sharing a word with the profile: nothing to pick, so it stays unanswered.
    city = [dict(s, required=True) for s in select(2, "Office city *", [f"Town {i}" for i in range(20)])]
    d = model.choose(page(email, *city, button(3, "Next")), "goal", [], {})
    assert d["operation"] == "BLOCKED" and not laya
    assert "Email *: Enter a valid email" in d["reason"] and "Office city *" in d["reason"]
    radios = [
        {
            "id": f"r{n}",
            "node": n,
            "kind": "click",
            "role": "radio",
            "label": o,
            "checked": "false",
            "required": True,
            "context": "Do you hold a driving licence? * Yes No",
        }
        for n, o in ((4, "Yes"), (5, "No"))
    ]
    assert planner.unanswered(radios) == ["Do you hold a driving licence?"]
    assert planner.unanswered([dict(radios[0], checked="true"), radios[1]]) == []


def test_a_form_below_the_fold_is_scrolled_to_instead_of_waited_for(laya):
    wait = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}
    scroll = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down"}
    footer = page(button(1, "Save and Continue"), scroll, wait)
    quiet = [{"kind": "wait", "page_changed": False}] * 3
    assert model.choose(footer, "goal", quiet[:1], CANDIDATE)["operation"] == "WAIT"
    assert model.choose(footer, "goal", quiet, CANDIDATE)["operation"] == "SCROLL_DOWN"


def test_range_options_are_chosen_from_your_numbers(laya):
    candidate = {**CANDIDATE, "work.total_experience_years": "4.5", "compensation.current.total_annual_lpa": "17"}
    years = [option(i, o, "Total experience") for i, o in enumerate(["0-2 Years", "2-5 Years", "More than 5 Years"], 1)]
    assert model.choose(page(*years), "goal", [], candidate)["action"]["label"] == "2-5 Years"
    ctc = [option(i, o, "Current CTC") for i, o in enumerate(["Below 10 LPA", "10-16 LPA", "16-20 LPA", "20+ LPA"], 1)]
    assert model.choose(page(*ctc), "goal", [], candidate)["action"]["label"] == "16-20 LPA" and not laya
    assert planner.bounds("5+ years") == (5, float("inf")) and planner.bounds("Less than 1 year") == (0, 1)


def test_eeo_questions_get_the_decline_answer_unless_your_profile_says_otherwise(laya):
    q = "Veteran status"
    opts = [option(1, "I am a protected veteran", q), option(2, "I decline to self-identify", q)]
    assert choose(page(*opts))["action"]["id"] == "o2"
    off = {**CANDIDATE, "self_identification.decline_eeo": "No"}
    assert model.choose(page(*opts, button(3, "Next")), "goal", [], off)["action"]["label"] == "Next"


def test_a_select_like_text_box_is_opened_when_no_rule_has_its_value(laya):
    box = fill(1, "Which team do you prefer? Select...", role="combobox")
    opens = {**box, "id": "f1o", "kind": "click", "label": "Open Which team do you prefer? Select..."}
    assert choose(page(box, opens))["action"]["id"] == "f1o"
    notice = fill(2, "Notice period Select...", role="combobox")
    assert (
        choose(page(notice, {**notice, "id": "f2o", "kind": "click", "label": "Open Notice period Select..."}))[
            "operation"
        ]
        == "TYPE_TEXT"
    )  # a rule has the value: typing finds it in the list


def test_a_field_the_site_clears_twice_is_left(laya):
    typed = [{"kind": "fill", "action": "Current location", "url": "https://jobs.example/apply"}] * 2
    d = model.choose(page(fill(1, "Current location"), button(2, "Next")), "goal", typed, CANDIDATE)
    assert d["action"]["label"] == "Next"


def test_login_walls_and_closed_jobs_are_named(laya):
    wall = {**page(fill(1, "Email address")), "login": True}
    assert "Sign in" in choose(wall)["reason"]
    closed = {**page(button(1, "View all jobs")), "text": "This job is no longer available."}
    assert "no longer open" in choose(closed)["reason"]


def test_questions_about_things_your_profile_never_mentions_are_not_given_to_a_model(laya):
    candidate = {**CANDIDATE, "experience.0.end": "2025-03", "skills": "OPC UA, DNP3, Python"}
    assert not planner.relates("Have you managed end-to-end employee onboarding?", candidate)
    assert not planner.relates("Do you have experience with DarwinBox?", candidate)
    assert planner.relates("Rate your Python skills", candidate)


def test_a_banner_frame_never_beats_the_apply_button_of_the_page(laya):
    banner = {
        "id": "e1",
        "node": 1,
        "kind": "frame",
        "role": "iframe",
        "label": "Embedded page: view.ceros.com",
        "value": "view.ceros.com",
        "rect": {"w": 1280, "h": 326},
    }
    apply = button(2, "Apply for this job online", role="link")
    wait = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}
    waited = [{"kind": "wait"}] * 8
    assert model.choose(page(banner, apply, wait), "goal", waited, CANDIDATE)["action"]["label"].startswith("Apply")
    form = {**banner, "label": "Embedded page: Job application form", "value": "boards.greenhouse.io"}
    assert choose(page(form, apply))["operation"] == "OPEN_FORM"


def test_site_search_boxes_are_not_the_application(laya):
    search = fill(1, "Search", nav=True)
    easy = button(2, "Easy Apply")
    wait = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}
    waited = [{"kind": "wait"}] * 8
    assert model.choose(page(search, easy, wait), "goal", waited, CANDIDATE)["action"]["label"] == "Easy Apply"


def test_linkedin_style_questions(laya):
    candidate = {**CANDIDATE, "personal.phone_country_code": "+91", "personal.country": "India"}
    code = select(3, "Phone country code", ["Afghanistan (+93)", "India (+91)", "Indonesia (+62)"])
    assert model.choose(page(*code), "goal", [], candidate)["action"]["value"] == "India (+91)"
    yes_no = select(4, "Do you have experience with DNP3?", ["Yes", "No"])
    assert choose(page(*yes_no))["action"]["value"] == "Yes"  # DNP3 is one of the skills
    unknown = select(5, "Do you have experience with Kubernetes?", ["Yes", "No"])
    assert choose(page(*unknown, button(6, "Review")))["action"]["label"] == "Review" and not laya


def test_a_short_skill_counts_only_as_a_whole_word(laya):
    candidate = {**CANDIDATE, "skills": "Git, RAG"}
    git = select(4, "Do you have experience with Git?", ["Yes", "No"])
    assert model.choose(page(*git), "goal", [], candidate)["action"]["value"] == "Yes"
    digital = select(5, "Do you have experience with digital twins?", ["Yes", "No"])
    assert model.choose(page(*digital, button(6, "Review")), "goal", [], candidate)["action"]["label"] == "Review"


def test_naukri_chat_answers_are_sent_with_save_and_success_is_seen(laya):
    save = button(9, "Save", chat=True)
    reply = fill(8, "Type message here...", chat=True, context="What is your notice period?")
    typed = [{"kind": "fill", "action": reply["label"], "chat": True, "typed": "15 days", "url": "u"}]
    assert model.choose(page(reply, save), "goal", typed, CANDIDATE)["action"]["label"] == "Save"
    done = {
        **page(button(1, "Similar jobs")),
        "url": "https://www.naukri.com/job-listings-x",
        "text": "You have successfully applied",
    }
    assert model.choose(done, "goal", [{"kind": "click"}], CANDIDATE)["operation"] == "APPLIED"


def test_a_resume_already_on_the_site_is_chosen_not_uploaded_again(laya):
    candidate = {**CANDIDATE, "document.resume": "Aarav_Sharma_Resume.pdf"}
    upload = {"id": "u1", "node": 1, "kind": "upload", "role": "file", "label": "Upload resume", "value": ""}
    card = {
        "id": "c1",
        "node": 2,
        "kind": "click",
        "role": "radio",
        "label": "Aarav_Sharma_Resume.pdf Last used 2 days ago",
        "checked": "false",
        "context": "Resume",
    }
    other = {**card, "id": "c2", "node": 3, "label": "Old_CV.pdf"}
    assert model.choose(page(upload, card, other), "goal", [], candidate)["action"]["id"] == "c1"
    chosen = {**card, "checked": "true"}
    nxt = model.choose(page(upload, chosen, other, button(4, "Next")), "goal", [], candidate)
    assert nxt["action"]["label"] == "Next"


def test_a_job_already_applied_to_is_named(laya):
    naukri = {**page(button(1, "Applied"), button(2, "Send me jobs like this")), "url": "https://www.naukri.com/job-x"}
    assert "already applied" in choose(naukri)["reason"]
    linkedin = {**page(button(1, "Save")), "text": "Applied 3 days ago"}
    assert "already applied" in choose(linkedin)["reason"]


def test_questions_about_what_you_have_done_are_never_guessed(laya):
    q = "Have you personally deployed ROS 2 and C++ teleoperation software on physical robots?"
    radios = [
        {
            "id": f"r{n}",
            "node": n,
            "kind": "click",
            "role": "radio",
            "label": o,
            "checked": "false",
            "context": f"{q} Yes No",
        }
        for n, o in ((1, "Yes"), (2, "No"))
    ]
    candidate = {**CANDIDATE, "skills": "C++, ROS 2, Python"}
    assert model.choose(page(*radios, button(3, "Review")), "goal", [], candidate)["action"]["label"] == "Review"
    assert not laya


def test_quiz_statements_are_never_answered_by_a_model(laya):
    q = "An HTTP 200 response guarantees that the requested business operation succeeded."
    quiz = [
        {
            "id": f"r{n}",
            "node": n,
            "kind": "click",
            "role": "radio",
            "label": o,
            "checked": "false",
            "context": f"{q} Yes No",
        }
        for n, o in ((1, "Yes"), (2, "No"))
    ]
    assert choose(page(*quiz, button(3, "Review")))["action"]["label"] == "Review" and not laya


def test_saved_resume_cards_are_not_a_question_your_resume_is_uploaded(laya):
    candidate = {**CANDIDATE, "document.resume": "Aarav_Sharma_Resume_Industrial_Automation.pdf"}
    cards = [
        {
            "id": f"c{n}",
            "node": n,
            "kind": "click",
            "role": "radio",
            "label": label,
            "checked": checked,
            "context": "Resume* Be sure to include an updated resume",
        }
        for n, label, checked in ((1, "Resume-.pdf", "true"), (2, "PDF v7_Aarav_Sharma_Resume.pdf", "false"))
    ]
    upload = {"id": "u1", "node": 9, "kind": "upload", "role": "file", "label": "Upload resume", "value": ""}
    d = model.choose(page(*cards, upload, button(3, "Next")), "goal", [], candidate)
    assert d["operation"] == "UPLOAD" and not laya


def test_a_job_card_mentioning_easy_apply_is_not_the_apply_button(laya):
    card = {
        "id": "j1",
        "node": 1,
        "kind": "click",
        "role": "button",
        "value": "",
        "label": "PeopleGene Bengaluru (On-site) Promoted · Posted 1 day ago · Easy Apply",
    }
    wait = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}
    d = model.choose(page(card, wait), "goal", [{"kind": "wait"}] * planner.NO_FORM_WAITS, CANDIDATE)
    assert d["operation"] == "BLOCKED"
    assert choose(page(card, button(2, "Easy Apply to this job")))["action"]["label"] == "Easy Apply to this job"
    status = {**page(button(3, "View resume")), "text": "Application status Application submitted 4 minutes ago"}
    assert "already applied" in choose(status)["reason"]


def test_the_saved_resume_that_is_yours_is_selected(laya):
    candidate = {**CANDIDATE, "document.resume": "Aarav_Sharma_Resume_CSharp_DotNET.pdf"}
    cards = [
        {
            "id": f"c{n}",
            "node": n,
            "kind": "click",
            "role": "radio",
            "label": label,
            "checked": checked,
            "context": "Resume*",
        }
        for n, label, checked in (
            (1, "v7_Aarav_Sharma_Resume_Industrial_Automation.pdf", "true"),
            (2, "v4_Aarav_Sharma_Resume_CSharp_DotNET.pdf", "false"),
        )
    ]
    assert model.choose(page(*cards, button(3, "Next")), "goal", [], candidate)["action"]["id"] == "c2"
    other = {**candidate, "document.resume": "Someone_Else.pdf"}
    assert model.choose(page(*cards, button(3, "Next")), "goal", [], other)["operation"] == "BLOCKED"


def test_no_saved_resume_is_yours_so_the_upload_button_is_used(laya):
    candidate = {**CANDIDATE, "document.resume": "AaravSharma_GenAI-ML_Resume.pdf"}
    cards = [
        {
            "id": "c1",
            "node": 1,
            "kind": "click",
            "role": "radio",
            "label": "v4_Aarav_Sharma_Resume_CSharp_DotNET.pdf",
            "checked": "true",
            "context": "Resume*",
        },
        {
            "id": "c2",
            "node": 2,
            "kind": "click",
            "role": "radio",
            "label": "Resume-.pdf",
            "checked": "false",
            "context": "Resume*",
        },
    ]
    d = model.choose(page(*cards, button(3, "Upload resume"), button(4, "Next")), "goal", [], candidate)
    assert d["operation"] == "UPLOAD" and d["action"]["chooser"] and d["action"]["node"] == 3


def test_a_section_that_already_lists_entries_gets_none_added(laya):
    candidate = {**CANDIDATE, "experience.0.company": "Acme Systems", "experience.1.company": "Philips"}
    listed = button(90, "Edit, Work Experience", context="Your title Founding Engineer Company Acme Systems")
    add = button(91, "Add work experience", context="Your title Founding Engineer Company Acme Systems")
    d = model.choose(page(listed, add, button(60, "Next")), "goal", [], candidate)
    assert d["action"]["label"] == "Next"
    empty = button(91, "Add work experience", context="Work experience")
    assert model.choose(page(empty, button(60, "Next")), "goal", [], candidate)["action"]["label"] == (
        "Add work experience"
    )


def test_decimal_years_pick_the_whole_year_option_never_a_look_alike():
    facts = planner.Facts({"work.total_experience_years": "2.2", "work.total_experience_extra_months": "2"})
    question = "Please select your total years of professional experience:*"
    years = select(1, question, [f"{n} year" if n < 2 else f"{n} years" for n in range(0, 25)])
    assert [planner.option_text(o) for o in planner.profile_answer(question, years, facts)] == ["2 years"]
    question = "Please select your total additional months of experience:*"
    months = select(2, question, ["0 month", "1 month", "2 months", "3 months"])
    assert [planner.option_text(o) for o in planner.profile_answer(question, months, facts)] == ["2 months"]


def test_an_add_button_next_to_an_entry_of_yours_is_not_pressed(laya):
    candidate = {**CANDIDATE, "education.0.institution": "Example Institute of Technology"}
    add = button(
        91, "Add education", context="Education School Example Institute of Technology Degree Bachelor of Technology"
    )
    assert model.choose(page(add, button(60, "Next")), "goal", [], candidate)["action"]["label"] == "Next"


def test_workdays_sign_in_step_is_a_login_wall(laya):
    header = [button(1, "Sign In"), button(2, "Search Careers")]
    workday = {
        **page(*header),
        "text": "Custom Software Engineer current step 1 of 6 Create Account/Sign In step 2 of 6",
    }
    assert planner.sign_in_wall(workday)
    assert model.choose(workday, "goal", [], CANDIDATE)["operation"] == "BLOCKED"
    later = {**page(*header), "text": "step 1 of 6 Create Account/Sign In current step 2 of 6 My Information"}
    assert not planner.sign_in_wall(later)


def test_a_form_that_is_still_loading_gets_more_time(laya):
    footer = [button(1, "Save and Continue")]
    loading = {**page(*footer, {"id": "wait", "kind": "wait", "label": "Wait for the page to update"})}
    loading["text"] = "My Information Loading Loading Legal Name Loading"
    waited = [{"kind": "wait", "url": loading["url"]}] * 30  # past the usual limit
    assert model.choose(loading, "goal", waited, CANDIDATE)["operation"] == "WAIT"


def test_a_file_already_in_the_list_is_not_uploaded_again(laya):
    candidate = {**CANDIDATE, "document.resume": "Aarav_Sharma_Resume_CSharp_DotNET.pdf"}
    upload = {"id": "u1", "node": 40, "kind": "upload", "role": "file", "label": "Drop files here or Select files",
              "value": "", "context": "Resume/CV"}  # fmt: skip
    listed = {
        **page(upload, button(41, "Delete Aarav_Sharma_Resume_CSharp_DotNET.pdf"), button(60, "Save and Continue"))
    }
    listed["text"] = "Resume/CV Aarav_Sharma_Resume_CSharp_DotNET.pdf successfully uploaded"
    assert model.choose(listed, "goal", [], candidate)["action"]["label"] != upload["label"]
    fresh = page(upload, button(60, "Save and Continue"))
    assert model.choose(fresh, "goal", [], candidate)["operation"] == "UPLOAD"
    done = [{"kind": "upload", "action": upload["label"], "url": fresh["url"]}]
    assert model.choose(fresh, "goal", done, candidate)["action"]["label"] != upload["label"]


def test_a_label_that_says_type_to_add_is_a_search_prompt():
    assert planner.is_prompt({"kind": "fill", "role": "textbox", "label": "Type to Add Skills"})
    assert not planner.is_prompt({"kind": "fill", "role": "textbox", "label": "Skills you would like to learn"})


def test_a_saved_resume_that_does_not_stay_selected_stops_the_run(laya):
    candidate = {**CANDIDATE, "document.resume": "Aarav_Sharma_Resume_GenAI.pdf"}
    cards = [
        {
            "id": f"c{n}",
            "node": n,
            "kind": "click",
            "role": "radio",
            "label": label,
            "checked": checked,
            "context": "Resume*",
        }  # fmt: skip
        for n, label, checked in (
            (1, "v4_Aarav_Sharma_Resume_CSharp_DotNET.pdf", "true"),
            (2, "Aarav_Sharma_Resume_GenAI.pdf", "false"),
        )
    ]
    url = "https://www.linkedin.com/jobs/view/1/"
    clicked = [{"kind": "click", "action": cards[1]["label"], "url": url, "context": "Resume*"}]
    d = model.choose({**page(*cards, button(3, "Next")), "url": url}, "goal", clicked, candidate)
    assert d["operation"] == "BLOCKED" and "didn't stay selected" in d["reason"]


def test_a_saved_resume_scrolled_out_of_view_is_scrolled_back_to_never_uploaded_again(laya):
    # LinkedIn moves the chosen résumé to the top of its list; after scrolling down only the others are on screen,
    # though the list still names it.
    candidate = {**CANDIDATE, "document.resume": "Aarav_Sharma_Resume_CSharp_DotNET.pdf"}
    listing = (
        "PDF Aarav_Sharma_GenAI.pdf 10/1/2026 PDF v4_Aarav_Sharma_Resume_CSharp_DotNET.pdf 9/25/2026 PDF Resume-.pdf"
    )
    cards = [
        {
            "id": f"c{n}",
            "node": n,
            "kind": "click",
            "role": "radio",
            "label": label,
            "checked": "false",
            "context": listing,
        }  # fmt: skip
        for n, label in ((1, "Aarav_Sharma_GenAI.pdf"), (2, "Resume-.pdf"))
    ]
    upload = {"id": "u1", "node": 9, "kind": "click", "role": "button", "label": "Upload resume", "context": listing}
    up = {"id": "scroll_up", "kind": "scroll", "label": "Scroll up inside the form panel", "delta": -400}
    url = "https://www.linkedin.com/jobs/view/1/"
    shown = {**page(*cards, upload, up, button(3, "Next")), "url": url}
    d = model.choose(shown, "goal", [], candidate)
    assert d["operation"] == "SCROLL_UP" and d["action"]["id"] == "scroll_up"
    # Clicked, then seen again after scrolling up: on to Next, no second copy uploaded.
    seen = [
        {"kind": "click", "action": "v4_Aarav_Sharma_Resume_CSharp_DotNET.pdf", "url": url, "context": listing},
        {"kind": "scroll", "action": "Scroll up inside the form panel", "url": url},
    ]
    d = model.choose(shown, "goal", seen, candidate)
    assert d["operation"] != "UPLOAD" and d["action"]["label"] == "Next"
    # Never picked and never in view after scrolling: yours to choose, not an upload.
    tried = [{"kind": "scroll", "action": "Scroll up inside the form panel", "url": url}] * 4
    d = model.choose(shown, "goal", tried, candidate)
    assert d["operation"] == "BLOCKED" and "saved list" in d["reason"]
