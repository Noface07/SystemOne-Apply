"""Model contract and agent guards with fake browser, fake UI and scripted model answers. No paid APIs."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from jev_apply import agent as loop
from jev_apply import model
from jev_apply.browser import ExecutionUncertain, StalePage, fingerprint
from jev_apply.policy import Policy
from jev_apply.profile import Profile, safe_id

PROFILE = {
    "personal": {"first_name": "Aarav", "last_name": "Sharma"},
    "compensation": {
        "current": {"fixed_annual": 1500000, "variable_annual": 200000},
        "expected": {"total_annual": 2200000},
    },
    "work": {"earliest_start_date": "2026-11-02"},
}
CURRENT_LPA = safe_id("compensation.current.total_annual_lpa")
EXPECTED_LPA = safe_id("compensation.expected.total_annual_lpa")


def make_page(values=None, extra=()):
    values = values or {}
    actions = [
        {
            "id": "e1",
            "kind": "fill",
            "role": "textbox",
            "label": "Current CTC (in LPA) *",
            "node": 1,
            "value": values.get(1, ""),
        },
        {"id": "e2", "kind": "fill", "role": "textbox", "label": "PAN number", "node": 2, "value": ""},
        {
            "id": "e3",
            "kind": "select",
            "role": "combobox",
            "label": "Gender → Prefer not to say",
            "node": 3,
            "value": "Prefer not to say",
            "current_value": values.get(3, "Select…"),
        },
        {"id": "e4", "kind": "click", "role": "button", "label": "Submit application", "node": 4, "value": ""},
        {
            "id": "e5",
            "kind": "setdate",
            "role": "date",
            "label": "Earliest start date",
            "node": 5,
            "input_type": "date",
            "value": values.get(5, ""),
        },
        {
            "id": "e6",
            "kind": "fill",
            "role": "textbox",
            "label": "Why do you want to join?",
            "node": 6,
            "input_type": "textarea",
            "value": values.get(6, ""),
        },
        *extra,
        {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
    ]
    state = {
        "url": "https://jobs.example/apply",
        "title": "Apply",
        "text": "Apply",
        "scroll": {},
        "actions": actions,
        "marker": [],
        "page_key": [],
        "guards": {},
    }
    state["fingerprint"] = fingerprint(state)
    return state


class FakeBrowser:
    def __init__(self, pages):
        self.pages, self.acts = list(pages), []
        self.act_errors = []

    def observe(self):
        return deepcopy(self.pages[0] if len(self.pages) == 1 else self.pages.pop(0))

    def fresh(self, page, action=None):
        return True

    def act(self, action, page, **kwargs):
        if self.act_errors:
            raise self.act_errors.pop(0)
        self.acts.append((action["id"], kwargs))

    def show(self):
        pass


class FakeUI:
    step = False

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __getattr__(self, name):
        def respond(*args):
            self.calls.append((name, args))
            value = self.answers.get(name)
            return value(*args) if callable(value) else value

        return respond


def decision(operation, action, p=0.95):
    return {
        "operation": operation,
        "action": action,
        "target": "1",
        "operation_probability": p,
        "target_probability": p if action else None,
        "latency_ms": 5,
    }


def source(choice, ranked=None):
    ranked = ranked or [(choice, 0.9), ("ASK_USER", 0.1)]
    return {"choice": choice, "probabilities": dict(ranked), "margin": ranked[0][1] - ranked[1][1], "ranked": ranked}


@pytest.fixture
def make_agent(tmp_path):
    def build(pages, ui=None, **policy):
        browser = FakeBrowser(pages)
        profile = Profile(PROFILE, tmp_path, learned_path=tmp_path / "learned.json")
        ui = ui or FakeUI(approve=True, handover=True)
        policy = {"pace_s": 0, "pace_after_next_s": 0, **policy}  # no person's pace in tests
        return loop.Agent(browser, profile, replace(Policy.load(), **policy), ui, run_dir=tmp_path / "runs")

    return build


def action(page, action_id):
    return next(a for a in page["actions"] if a["id"] == action_id)


# ---- model contract ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "non_max"])
def test_invalid_choice_is_rejected(mutation):
    answer = {"choice": "a", "confidence": 1.0, "probabilities": {"a": 0.8, "b": 0.2}}
    if mutation == "unknown":
        answer["choice"] = "invented"
    elif mutation == "nan":
        answer["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del answer["probabilities"]["b"]
    else:
        answer["choice"] = "b"
    with pytest.raises(ValueError):
        model.validate_choice(answer, ["a", "b"])


def test_choose_builds_operation_specific_heads_and_uses_only_the_selected_one(monkeypatch):
    monkeypatch.setenv("DECISION_BACKEND", "typesafe")  # one request with every head; Laya is in test_laya.py
    monkeypatch.setenv("DECISION_STEERING", "model")  # the model-steers mode (procedure is the default)
    sent = {}

    def fake(body):
        sent.update(body)
        heads = body["questions"]
        answers = {
            name: {
                "choice": next(iter(q["criteria"])),
                "confidence": 0.9,
                "probabilities": {k: (1.0 if i == 0 else 0.0) for i, k in enumerate(q["criteria"])},
            }
            for name, q in heads.items()
        }
        ops = list(heads["operation"]["criteria"])
        answers["operation"] = {
            "choice": "SET_DATE",
            "confidence": 0.9,
            "probabilities": {k: float(k == "SET_DATE") for k in ops},
        }
        answers["click_target"] = {"choice": "not-a-target"}  # an unused head cannot cause anything
        return {"answers": answers}, 3

    monkeypatch.setattr(model, "systemone", fake)
    page = make_page()
    d = model.choose(page, "goal", [], {"x": "y"})
    assert {"type_text_target", "select_target", "set_date_target", "click_target"} <= set(sent["questions"])
    assert {"REVIEW", "BLOCKED", "WAIT"} <= set(sent["questions"]["operation"]["criteria"])
    assert d["operation"] == "SET_DATE" and d["action"]["label"] == "Earliest start date"
    assert sent["state"]["candidate"] == {"x": "y"}


def test_value_source_offers_facts_and_specials_but_no_drafts_for_dates(monkeypatch, tmp_path):
    monkeypatch.setattr(model.rules, "match", lambda *a, **k: None)  # the model path; rules have their own tests
    seen = []

    def fake(body):
        criteria = body["questions"]["source"]["criteria"]
        seen.append(criteria)
        return {
            "answers": {
                "source": {
                    "choice": "ASK_USER",
                    "confidence": 1.0,
                    "probabilities": {k: float(k == "ASK_USER") for k in criteria},
                }
            }
        }, 2

    monkeypatch.setattr(model, "systemone", fake)
    profile = Profile(PROFILE)
    page = make_page()
    model.value_source(action(page, "e1"), page, profile, [])
    model.value_source(action(page, "e5"), page, profile, [], dates_only=True)
    assert CURRENT_LPA in seen[0] and "DRAFT_ANSWER" in seen[0]
    assert "value: 17" in seen[0][CURRENT_LPA]
    assert "DRAFT_ANSWER" not in seen[1] and set(seen[1]) == {
        safe_id("work.earliest_start_date"),
        "ASK_USER",
        "SKIP_FIELD",
    }


# ---- agent guards --------------------------------------------------------------------------------------------------


def test_fill_from_profile_types_the_fact_and_reads_it_back(make_agent, monkeypatch):
    before, after = make_page(), make_page({1: "17"})
    agent = make_agent([before, after])
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(before, "e1")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source(CURRENT_LPA))
    agent.step()
    assert agent.browser.acts == [("e1", {"text": "17", "value": None, "files": None})]
    assert agent.fills[0]["source"] == "profile" and agent.fills[0]["confirmed"] is True


def test_close_call_between_current_and_expected_goes_to_you(make_agent, monkeypatch):
    page = make_page()
    ui = FakeUI(pick=lambda question, options: options[1][0], approve=True)
    agent = make_agent([page, make_page({1: "17"})], ui)
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(page, "e1")))
    monkeypatch.setattr(
        model,
        "value_source",
        lambda *a, **k: source(EXPECTED_LPA, [(EXPECTED_LPA, 0.52), (CURRENT_LPA, 0.45), ("ASK_USER", 0.03)]),
    )
    agent.step()
    assert ui.calls[0][0] == "pick"
    assert agent.browser.acts[0][1]["text"] == "17"  # you picked the current CTC, not the model's 52% guess


def test_ask_user_and_remember(make_agent, monkeypatch, tmp_path):
    page = make_page()
    ui = FakeUI(ask=("Yes, 15 days", True), approve=True)
    agent = make_agent([page, make_page({6: "Yes, 15 days"})], ui)
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(page, "e6")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source("ASK_USER"))
    agent.step()
    assert agent.browser.acts[0][1]["text"] == "Yes, 15 days" and agent.fills[0]["source"] == "you"
    assert json.loads((tmp_path / "learned.json").read_text())["answers"][0]["answer"] == "Yes, 15 days"


def test_drafts_are_shown_before_typing(make_agent, monkeypatch):
    page = make_page()
    ui = FakeUI(edit_draft=lambda question, draft: draft)
    agent = make_agent([page, make_page({6: "Because payments."})], ui)
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(page, "e6")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source("DRAFT_ANSWER"))
    monkeypatch.setattr(model, "draft_text", lambda *a: ("Because payments.", {}))
    agent.step()
    assert ui.calls[0][0] == "edit_draft"
    assert agent.fills[0]["source"] == "draft" and agent.browser.acts[0][1]["text"] == "Because payments."


def test_final_submit_is_never_clicked(make_agent, monkeypatch):
    page = make_page()
    agent = make_agent([page], FakeUI(is_final_submit=True))
    monkeypatch.setattr(model, "choose", lambda *a: decision("CLICK", action(page, "e4")))
    agent.step()
    assert agent.status == "review" and agent.browser.acts == []


def test_typing_values_already_there_never_stops_the_run_only_a_save_that_does_nothing(make_agent, monkeypatch):
    """MKS kept the start date from an earlier visit: retyping it changed nothing three times, and the run stopped
    instead of going on to Save. A Save that moves nothing among them is what means stuck."""
    page = make_page()
    agent = make_agent([page], UnattendedFakeUI(handover=False))
    agent.page = page
    box = action(page, "e1")
    for _ in range(3):
        agent.execute(box, decision("TYPE_TEXT", box), text="x", expected="x")
    assert not agent.handovers  # unchanged page after typing: carry on
    save = {"id": "s1", "node": 9, "kind": "click", "role": "button", "label": "Save and Continue", "context": ""}
    with pytest.raises(loop.Stop):  # ...but a Save that changed nothing either: stuck, handed over
        agent.execute(save, decision("CLICK", save))


def test_a_split_date_is_typed_whole_from_its_first_box(make_agent):
    """MKS: Month typed alone missed once, the retry spilled into Year ("10//2010"). The whole date goes in from
    the Month box, key by key, as Workday moves on to Day and Year by itself."""
    from jev_apply.profile import Fact

    page = make_page()
    agent = make_agent([page])
    q = "What is your desired start date?* Calendar"
    boxes = [{"id": f"d{i}", "node": 50 + i, "kind": "fill", "role": "spinbutton", "label": label, "context": q,
              "value": "", "rect": {"x": 100 + 60 * i, "y": 10, "w": 40, "h": 30}}
             for i, label in enumerate(("Month", "Day", "Year"))]  # fmt: skip
    agent.page = {**page, "actions": boxes}
    key = "work.earliest_start_date"
    agent.profile.facts[key] = Fact(key, "2026-10-26", "start")
    agent.profile.by_id[safe_id(key)] = agent.profile.facts[key]
    assert agent.whole_date(boxes[0], "10") == "10262026"
    assert agent.whole_date(boxes[1], "26") is None  # only from the first box
    agent.page["actions"][2] = {**boxes[2], "value": "2010"}
    assert agent.whole_date(boxes[0], "10") is None  # a box already holds something: each is set on its own


def test_save_pressed_early_counts_again_once_something_was_filled(make_agent):
    """Motorola: Save was pressed three times before the page was filled, and then banned as "done three times
    without effect" when everything was finally filled in."""
    page = make_page()
    agent = make_agent([page])
    agent.page = page
    save = {"id": "s1", "node": 9, "kind": "click", "role": "button", "label": "Save and Continue", "context": ""}
    press = {"kind": "click", "action": "Save and Continue", "context": "", "url": page["url"], "page_changed": True}
    agent.history += [dict(press, step=i) for i in range(3)]
    assert agent.repeated(save) == 3
    agent.history.append({"kind": "fill", "action": "Phone Number", "context": "Phone", "url": page["url"],
                          "page_changed": True})  # fmt: skip
    assert agent.repeated(save) == 0  # a new attempt after an answer
    # Given up on, then a box is ticked (MKS's terms): Save may be tried again
    agent.vetoed.add((*agent.here(save), save["label"]))
    terms = {"id": "t1", "node": 12, "kind": "click", "role": "checkbox", "label": "I agree", "checked": "false"}
    agent.execute(terms, decision("CLICK", terms))
    assert not agent.excluded(save)


def test_an_action_the_page_keeps_refusing_is_dropped_after_three_tries(make_agent, monkeypatch):
    """Xylem: a chip's × out of view raised "page changed" 450 times in a row, silently, until the budget ran out."""
    page = make_page()
    remove = {**action(page, "e4"), "label": "Remove: OPC UA, OPC DA, Modbus", "role": "button"}
    agent = make_agent([page], UnattendedFakeUI(handover=False))
    agent.page = page
    agent.browser.act_errors = [StalePage("covered")] * 5
    monkeypatch.setattr(model, "choose", lambda *a: decision("CLICK", remove))
    monkeypatch.setattr(agent, "confirm", lambda *a: True)
    for _ in range(3):
        agent.tick()
    assert agent.excluded(remove) and any("Remove: OPC UA" in x["what"] for x in agent.left)


def test_claude_is_asked_before_a_save_that_does_nothing_is_dropped(make_agent, monkeypatch):
    """Maersk: Save and Continue did nothing three times (a dropdown left empty) and was dropped without anyone
    looking at the page."""
    page = make_page()
    save = {**action(page, "e4"), "label": "Save and Continue", "role": "button"}
    agent = make_agent([page], UnattendedFakeUI(handover=False))
    agent.page = page
    press = {"kind": "click", "action": "Save and Continue", "context": save.get("context") or "",
             "url": page["url"], "page_changed": False}  # fmt: skip
    agent.history += [dict(press, step=i) for i in range(3)]
    monkeypatch.setattr(model, "choose", lambda *a: decision("CLICK", save))
    asked = []
    monkeypatch.setattr(agent, "rescue", lambda why, avoid=(): asked.append((why, avoid)) or True)
    agent.tick()
    assert "three times" in asked[0][0] and asked[0][1] == ["Save and Continue"]  # never that Save again
    assert not agent.excluded(save)  # Claude took a step: Save stays
    monkeypatch.setattr(agent, "rescue", lambda why, avoid=(): False)
    agent.tick()
    assert agent.excluded(save)  # Claude saw nothing to do: dropped as before


def test_a_sensitive_workday_dropdown_opens_and_its_profile_answer_is_picked_unattended(make_agent):
    """Xylem: "Will you need support for a work visa...?" is sensitive, so even opening its "Select One" was refused;
    its options carry no question, so "No" couldn't be matched to your profile either."""
    page = make_page()
    agent = make_agent([page], UnattendedFakeUI(handover=False))
    agent.page = page
    agent.profile.note_job_country("India")
    q = "Will you need support for a work visa and residence permit now or in the future?*"
    agent.history.append({"step": 1, "kind": "click", "action": "Select One Required", "context": q,
                          "url": page["url"]})  # fmt: skip
    no = {"id": "o2", "node": 42, "kind": "click", "role": "option", "label": "No", "context": ""}
    yes = {**no, "id": "o1", "node": 41, "label": "Yes"}
    agent.page = {**page, "actions": [yes, no]}
    from jev_apply.profile import Fact

    key = "work_authorization.requires_visa_sponsorship"
    agent.profile.facts[key] = Fact(key, "No", "needs sponsorship")
    agent.profile.by_id[safe_id(key)] = agent.profile.facts[key]
    assert agent.matches_profile(no) and not agent.matches_profile(yes)
    opener = {"id": "b1", "node": 40, "kind": "click", "role": "button", "label": "Select One Required", "context": q}
    assert loop.planner.SHOWS_NOTHING.search(opener["label"])  # opened without a rule's value: it answers nothing


def test_an_open_dropdowns_option_is_judged_with_its_question(make_agent):
    """Maersk: "Yes, I do" for "Please confirm if you have a valid PAN card" sat under the footer's "Follow Us", so
    nothing saw it was about an ID document and the model picked it (92% sure) with nothing on file."""
    page = make_page()
    agent = make_agent([page], UnattendedFakeUI(handover=False))
    pan = "Please confirm if you have a valid PAN card.*"
    opener = {"id": "b1", "node": 40, "kind": "click", "role": "button", "label": "Select One Required", "context": pan}
    yes = {"id": "o1", "node": 41, "kind": "click", "role": "option", "label": "Yes, I do", "context": "",
           "section": "Follow Us"}  # fmt: skip
    no = {**yes, "id": "o2", "node": 42, "label": "No, I don't"}
    agent.page = {**page, "actions": [opener, yes, no]}
    agent.history.append({"step": 1, "kind": "click", "action": "Select One Required", "context": pan,
                          "url": page["url"]})  # fmt: skip
    agent.do_click(yes, decision("CLICK", yes), False)
    assert agent.browser.acts == [] and agent.inbox[-1]["question"].startswith("Please confirm if you have a valid")
    assert agent.inbox[-1]["options"] == ["Yes, I do", "No, I don't"] and agent.excluded(opener)  # not reopened
    # Once you said you hold one, "Yes, I do" is your answer; and "18+ years of age" is your over-18 fact
    from jev_apply.profile import Fact

    for key, value in [("personal.has_pan_card", "Yes"), ("personal.over_18", "Yes")]:
        agent.profile.facts[key] = Fact(key, value, key)
        agent.profile.by_id[safe_id(key)] = agent.profile.facts[key]
    agent.vetoed.clear()
    agent.do_click(yes, decision("CLICK", yes), False)
    assert [a for a, _ in agent.browser.acts] == ["o1"]
    adult = {**opener, "context": "I confirm that I am 18+ years of age*"}
    agent.history.append({"step": 3, "kind": "click", "action": "Select One Required", "context": adult["context"],
                          "url": page["url"]})  # fmt: skip
    assert agent.matches_profile(yes) and not agent.matches_profile(no)
    # Maersk's dropdowns name the question in their own label, with no context: found from the step that opened it
    named = {"id": "b2", "node": 60, "kind": "click", "role": "button", "context": "",
             "label": "Please confirm if you have a valid Aadhar card Select One Required"}  # fmt: skip
    agent.page = {**page, "actions": [named, yes, no]}
    agent.history.append({"step": 4, "kind": "click", "action": named["label"], "context": "", "url": page["url"]})
    agent.do_click(no, decision("CLICK", no), False)
    assert [a for a, _ in agent.browser.acts] == ["o1"] and agent.excluded(named)  # nothing more clicked


def test_maersks_personal_information_dropdowns_are_answered_from_your_profile(make_agent):
    """Maersk: the dropdown's button names the question ("Please specify your Country of Birth Select One") and
    Workday names the country after the answer ("Single (India)", "Citizen (India)"); "India", "Single (India)" were
    left as sensitive questions and the model clicked "Citizen (Belgium)"."""
    from jev_apply.profile import Fact

    page = make_page()
    agent = make_agent([page], UnattendedFakeUI(handover=False))
    for key, value in [("personal.country", "India"), ("personal.country_of_birth", "India"),
                       ("personal.citizenship_country", "India"), ("personal.citizenship_status", "Citizen"),
                       ("personal.marital_status", "Single")]:  # fmt: skip
        agent.profile.facts[key] = Fact(key, value, key)
        agent.profile.by_id[safe_id(key)] = agent.profile.facts[key]

    def option(i, label):
        return {"id": f"o{i}", "node": i, "kind": "click", "role": "option", "label": label, "context": "",
                "section": "Follow Us"}  # fmt: skip

    india, belgium = option(1, "India"), option(2, "Belgium")
    single, married = option(3, "Single (India)"), option(4, "Married (India)")
    agent.page = {**page, "actions": [india, belgium, single, married]}

    def opened(label):
        agent.history.append({"kind": "click", "action": label, "context": "", "url": page["url"]})

    opened("Please specify your Country of Birth Select One")
    assert agent.matches_profile(india) and not agent.matches_profile(belgium)
    opened("Please specify your Marital Status Select One")
    assert agent.matches_profile(single) and not agent.matches_profile(married)
    ours, theirs = option(5, "Citizen (India) not checked"), option(6, "Citizen (Belgium) not checked")
    agent.page = {**page, "actions": [ours, theirs]}
    agent.history.append({"kind": "enter", "action": "Press Enter in 'Please specify your Citizenship Status'",
                          "context": "", "url": page["url"]})  # fmt: skip
    assert agent.matches_profile(ours) and not agent.matches_profile(theirs)
    assert agent.policy.sensitive({**theirs, "context": agent.opened_question()})  # never the model's pick


def test_an_open_list_without_your_answer_as_written_gets_the_closest_option(make_agent, monkeypatch):
    """Maersk's Degree list has "BS" and no B.Tech: the option that is your answer in the list's words is clicked,
    and counts as your profile's answer (a sensitive question's too). Nothing on file: never looked up."""
    from jev_apply import teacher
    from jev_apply.profile import Fact

    page = make_page()
    agent = make_agent([page], UnattendedFakeUI(handover=False))
    key = "education.0.degree"
    agent.profile.facts[key] = Fact(key, "B.Tech", "degree")
    agent.profile.by_id[safe_id(key)] = agent.profile.facts[key]
    options = [{"id": f"o{i}", "node": 50 + i, "kind": "click", "role": "option", "label": label, "context": ""}
               for i, label in enumerate(["BA", "BS", "MS"])]  # fmt: skip
    agent.page = {**page, "actions": options}
    agent.history.append({"kind": "click", "action": "Degree Select One Required", "context": "",
                          "section": "Education 1", "url": page["url"]})  # fmt: skip
    asked = []
    monkeypatch.setattr(loop.rules, "key_for", lambda field, *a, **k: key if field["label"] == "Degree" else None)
    monkeypatch.setattr(teacher, "closest_option",
                        lambda q, k, v, about, texts, *a: asked.append((q, v, texts)) or ("BS", "closest", "B.Tech"))
    escape = {"operation": "KEY", "action": {"id": "escape", "kind": "key", "label": "Press Escape"}}
    d = agent.closest_pick(escape, agent.page)
    assert d["action"]["label"] == "BS" and asked == [("Degree", "B.Tech", ["BA", "BS", "MS"])]
    assert agent.matches_profile(options[1]) and not agent.matches_profile(options[2])
    assert agent.closest_pick(escape, agent.page) is None and len(asked) == 1  # once for these options
    agent.history.append({"kind": "click", "action": "Gender Select One", "context": "", "url": page["url"]})
    assert agent.closest_pick(escape, agent.page) is None and len(asked) == 1  # no fact: yours, never looked up
    # Your answer written exactly as one option ("India", "Male"): picked with no Claude call
    agent.mapped.clear()
    agent.history.append({"kind": "click", "action": "Degree Select One Required", "context": "",
                          "section": "Education 1", "url": page["url"]})  # fmt: skip
    exact = options + [{"id": "o9", "node": 59, "kind": "click", "role": "option", "label": "B.Tech", "context": ""}]
    agent.page = {**page, "actions": exact}
    d = agent.closest_pick(escape, agent.page)
    assert d["action"]["label"] == "B.Tech" and len(asked) == 1  # Claude not asked
    # A plain text box just typed into is no list's question, whatever list is open beside it
    agent.page = {**page, "actions": options + [{"id": "f1", "node": 70, "kind": "fill", "role": "textbox",
                                                 "label": "Degree", "value": "B.Tech"}]}  # fmt: skip
    agent.history.append({"kind": "fill", "action": "Degree", "context": "", "url": page["url"]})
    assert agent.closest_pick(escape, agent.page) is None and len(asked) == 1


def test_a_cookie_banners_decline_is_clicked_though_it_speaks_of_consent(make_agent, monkeypatch):
    """Maersk: "By visiting our website, you consent to ... cookies": Decline was refused as a sensitive question,
    and the banner kept covering Continue Application."""
    page = make_page()
    agent = make_agent([page], UnattendedFakeUI(handover=False))
    agent.page = page
    decline = {"id": "c1", "node": 31, "kind": "click", "role": "button", "label": "Decline",
               "context": "Maersk.com uses cookies ... you consent to our and third party use of cookies."}  # fmt: skip
    agent.do_click(decline, decision("CLICK", decline), False)
    assert [a for a, _ in agent.browser.acts] == ["c1"]


def test_never_fill_fields_go_to_you_and_drop_out(make_agent, monkeypatch):
    page = make_page()
    agent = make_agent([page])
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(page, "e2")))
    agent.step()
    assert agent.browser.acts == [] and agent.handovers and not agent.excluded(action(page, "e1"))
    assert agent.excluded(action(page, "e2"))


def test_sensitive_choice_needs_approval_and_a_no_is_remembered(make_agent, monkeypatch):
    page = make_page()
    ui = FakeUI(approve=False)
    agent = make_agent([page], ui)
    monkeypatch.setattr(model, "choose", lambda *a: decision("SELECT", action(page, "e3")))
    agent.step()
    assert ui.calls[0][0] == "approve" and "sensitive" in ui.calls[0][1][1]
    assert agent.browser.acts == [] and agent.excluded(action(page, "e3"))


def test_low_confidence_needs_approval(make_agent, monkeypatch):
    page = make_page(extra=[{"id": "e7", "kind": "click", "role": "button", "label": "Next", "node": 7}])
    ui = FakeUI(approve=True)
    agent = make_agent([page, make_page()], ui)
    monkeypatch.setattr(model, "choose", lambda *a: decision("CLICK", action(page, "e7"), p=0.4))
    agent.step()
    assert "40% sure" in ui.calls[0][1][1] and agent.browser.acts[0][0] == "e7"


def test_resolved_value_survives_a_stale_retry_for_the_same_field_only(make_agent, monkeypatch):
    page = make_page()
    agent = make_agent([page, page, make_page({1: "17"})])
    agent.browser.act_errors = [StalePage("changed before input")]
    calls = []
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(page, "e1")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: calls.append(1) or source(CURRENT_LPA))
    agent.tick()
    agent.tick()
    assert len(calls) == 1 and agent.browser.acts[0][1]["text"] == "17" and agent.pending is None


def test_uncertain_mutation_is_handed_over_not_retried(make_agent, monkeypatch):
    page = make_page()
    agent = make_agent([page])
    agent.browser.act_errors = [ExecutionUncertain("date not accepted")]
    monkeypatch.setattr(model, "choose", lambda *a: decision("SET_DATE", action(page, "e5")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source(safe_id("work.earliest_start_date")))
    agent.tick()
    assert agent.handovers[0]["reason"] == "date not accepted" and agent.history == [agent.history[0]]
    assert agent.history[0]["kind"] == "handover"


def test_value_that_did_not_stick_is_flagged(make_agent, monkeypatch):
    page = make_page()
    agent = make_agent([page, make_page({1: "1"})])
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(page, "e1")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source(CURRENT_LPA))
    agent.step()
    assert agent.fills[0]["confirmed"] is False


def test_review_stops_and_writes_a_report(make_agent, monkeypatch):
    page = make_page()
    ui = FakeUI()
    agent = make_agent([page], ui)
    monkeypatch.setattr(model, "choose", lambda *a: decision("REVIEW", None))
    report = agent.run()
    assert report["status"] == "review" and ui.calls[-1][0] == "review"
    assert json.loads(open(report["path"]).read())["status"] == "review"


def test_skips_belong_to_one_document_because_node_ids_restart(make_agent, monkeypatch):
    page_one, page_two = make_page(), make_page()
    page_one["doc"], page_two["doc"] = "1000.5", "2000.5"
    agent = make_agent([page_one, page_two])
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(page_one, "e6")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source("SKIP_FIELD"))
    agent.step()
    assert agent.excluded(action(page_one, "e6"))
    agent.page = page_two  # a new document where node 6 is a different field
    assert not agent.excluded(action(page_two, "e6"))


def tag_page(context="", value=""):
    tag = {
        "id": "e8",
        "kind": "fill",
        "role": "combobox",
        "label": "Key skills",
        "node": 8,
        "multiple": True,
        "value": value,
        "context": context,
    }
    return make_page(extra=[tag])


def test_tag_field_gets_one_item_at_a_time_and_never_the_same_one_twice(make_agent, monkeypatch, tmp_path):
    from jev_apply.profile import Profile as P

    first = tag_page()
    agent = make_agent([first, tag_page("Key skills Python ×"), tag_page("Key skills Python × Go ×")])
    agent.profile = P({**PROFILE, "skills": ["Python", "Go"]}, tmp_path)
    offered = []

    def pick_first_item(action_, page, profile, history, dates_only=False, exclude=()):
        items = [i for i, f in profile.sources(exclude=exclude).items() if f.kind == "item"]
        offered.append(items)
        return source(items[0] if items else "SKIP_FIELD")

    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(first, "e8")))
    monkeypatch.setattr(model, "value_source", pick_first_item)
    agent.step()
    agent.step()
    agent.step()
    typed = [kwargs["text"] for _, kwargs in agent.browser.acts]
    assert typed == ["Python", "Go"] and offered[-1] == []
    assert agent.excluded(action(first, "e8"))  # every item tried: the field is done
    assert [f["added"] for f in agent.fills] == [True, True]


def test_item_the_site_does_not_offer_is_reported_not_added(make_agent, monkeypatch, tmp_path):
    from jev_apply.profile import Profile as P

    first = tag_page()
    agent = make_agent([first, tag_page("Key skills", value="Kafka")])
    agent.profile = P({**PROFILE, "skills": ["Kafka", "Go"]}, tmp_path)
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(first, "e8")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source(safe_id("skills.item_1")))
    agent.step()
    assert agent.fills[0]["added"] is False
    agent.status = "review"
    assert agent.finish()["not_added"] == ["Kafka"]


def test_native_multi_select_read_back_accepts_other_selected_options(make_agent, monkeypatch):
    option = {
        "id": "e9",
        "kind": "select",
        "role": "combobox",
        "label": "Locations → Pune",
        "node": 9,
        "value": "Pune",
        "multiple": True,
        "current_value": "Bengaluru",
    }
    after = {
        **option,
        "id": "e9",
        "label": "Locations → Chennai",
        "value": "Chennai",
        "current_value": "Bengaluru, Pune",
    }
    before = make_page(extra=[option])
    agent = make_agent([before, make_page(extra=[after])])
    monkeypatch.setattr(model, "choose", lambda *a: decision("SELECT", action(before, "e9")))
    agent.step()
    assert agent.fills[0]["confirmed"] is True


# ---- form coverage -------------------------------------------------------------------------------------------------


def test_a_required_dropdown_still_empty_keeps_the_agent_from_stopping(make_agent, monkeypatch):
    page = make_page()
    action(page, "e3")["required"] = True  # Gender still shows "Select…"
    agent = make_agent([page], FakeUI(approve=False, handover=True))
    monkeypatch.setattr(model, "choose", lambda *a: decision("REVIEW", None))
    agent.step()
    assert agent.status == "ready" and "Gender" in agent.ui.calls[0][1][1]


def test_the_planner_reason_is_what_the_handover_says(make_agent, monkeypatch):
    ui = FakeUI(handover=True)
    agent = make_agent([make_page()], ui)
    monkeypatch.setattr(model, "choose", lambda *a: {**decision("BLOCKED", None), "reason": "Marked invalid: Email"})
    agent.step()
    assert ("handover", ("Marked invalid: Email",)) in ui.calls


def test_a_date_shaped_text_field_gets_the_date_in_its_shape_or_asks(make_agent, monkeypatch):
    start = {
        "id": "e7",
        "kind": "fill",
        "role": "textbox",
        "label": "Joining date",
        "node": 7,
        "value": "",
        "placeholder": "DD/MM/YYYY",
    }
    page = make_page(extra=[start])
    agent = make_agent([page])
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(page, "e7")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source(safe_id("work.earliest_start_date")))
    agent.step()
    assert agent.browser.acts[-1][1]["text"] == "02/11/2026"
    monthly = {**start, "placeholder": "DD/MM/YYYY", "label": "Graduation"}
    page = make_page(extra=[monthly])
    ui = FakeUI(approve=True, handover=True, ask=("01/06/2022", False))
    agent = make_agent([page], ui)
    agent.profile.by_id[safe_id("education.0.end")] = agent.profile.facts.setdefault(
        "education.0.end", type(agent.profile.facts["work.earliest_start_date"])("education.0.end", "2022-06", "end")
    )
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", action(page, "e7")))
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source(safe_id("education.0.end")))
    agent.step()
    assert [c[0] for c in ui.calls].count("ask") == 1 and agent.browser.acts[-1][1]["text"] == "01/06/2022"


def test_replacing_a_value_the_site_filled_in_needs_your_yes(make_agent, monkeypatch):
    page = make_page()
    ui = FakeUI(approve=False, handover=True)
    agent = make_agent([page], ui)
    prefilled = {**action(page, "e1"), "value": "12", "replaces": "12"}
    monkeypatch.setattr(model, "choose", lambda *a: decision("TYPE_TEXT", prefilled))
    agent.step()
    assert agent.browser.acts == [] and "replacing '12'" in ui.calls[-2][1][0]


def test_a_close_call_between_options_is_confirmed_however_sure_it_sounds(make_agent, monkeypatch):
    page = make_page()
    ui = FakeUI(approve=False, handover=True)
    agent = make_agent([page], ui)
    close = {**decision("SELECT", action(page, "e3"), p=0.9), "target_margin": 0.05}
    monkeypatch.setattr(model, "choose", lambda *a: close)
    agent.step()
    assert agent.browser.acts == [] and ui.calls[0][0] == "approve"


def test_a_skipped_question_does_not_hide_a_later_step_with_the_same_words(make_agent, monkeypatch):
    page = make_page()
    agent = make_agent([page])
    agent.skipped_questions[(page.get("doc"), "Relocate? Yes No")] = {10, 11}
    later = {"id": "r1", "kind": "click", "role": "radio", "label": "Yes", "node": 20, "context": "Relocate? Yes No"}
    assert not agent.excluded(later)
    assert agent.excluded({**later, "node": 10})


def test_a_wait_only_counts_changes_to_the_form(make_agent, monkeypatch):
    first, ticking = make_page(), make_page()
    ticking["text"], ticking["fingerprint"] = "Apply 12:01", "other"
    agent = make_agent([first, ticking])
    monkeypatch.setattr(model, "choose", lambda *a: decision("WAIT", action(first, "wait")))
    agent.step()
    assert agent.history[-1]["page_changed"] is False


def test_a_time_of_day_is_asked_never_taken_from_the_profile(make_agent, monkeypatch):
    slot = {
        "id": "e7",
        "kind": "setdate",
        "role": "date",
        "label": "Interview time",
        "node": 7,
        "value": "",
        "input_type": "time",
    }
    page = make_page(extra=[slot])
    ui = FakeUI(approve=True, handover=True, ask=("10:30", False))
    agent = make_agent([page], ui)
    monkeypatch.setattr(model, "choose", lambda *a: decision("SET_DATE", action(page, "e7")))
    agent.step()
    assert agent.browser.acts[-1][1]["value"] == "10:30"


def test_an_action_the_page_ignores_three_times_is_dropped(make_agent, monkeypatch):
    page = make_page()
    agent = make_agent([page])
    yes = {"id": "y", "kind": "click", "role": "radio", "label": "Yes", "node": 9, "context": "Relocate?"}
    agent.history = [{"kind": "click", "action": "Yes", "context": "Relocate?", "url": page["url"]}] * 3
    monkeypatch.setattr(model, "choose", lambda *a: decision("CLICK", yes))
    agent.step()
    assert agent.browser.acts == [] and agent.excluded(yes) and "didn't take it" in agent.left[-1]["why"]


def test_a_model_fact_unrelated_to_the_question_is_never_typed(make_agent, monkeypatch):
    page = make_page()
    ui = FakeUI(approve=True, handover=True, ask=(None, False))
    agent = make_agent([page], ui)
    monkeypatch.setattr(
        model, "choose", lambda *a: decision("TYPE_TEXT", action(page, "e6"))
    )  # "Why do you want to join?"
    monkeypatch.setattr(model, "value_source", lambda *a, **k: source(safe_id("personal.first_name")))
    agent.step()
    assert agent.browser.acts == [] and ui.calls[0][0] == "ask"


class UnattendedFakeUI(FakeUI):
    unattended = True


def test_the_rescuer_takes_several_checked_steps_and_says_what_is_missing(make_agent, monkeypatch):
    """Maersk: "Primary Nationality" was required and empty. Claude opens its dropdown, clicks "India" for your
    nationality "Indian" (the fact the rules give that question, in the list's words) and says done; a sensitive
    pick nothing on file backs is refused, and when Claude finds no way, the handover says why."""
    from jev_apply import teacher
    from jev_apply.profile import Fact

    monkeypatch.setattr(teacher, "available", lambda: True)
    monkeypatch.setattr(loop.Agent, "show_helper", lambda *a: None)
    opener = {"id": "b1", "node": 40, "kind": "click", "role": "button", "context": "",
              "label": "Please specify your Primary Nationality Select One Required"}  # fmt: skip
    india = {"id": "o1", "node": 41, "kind": "click", "role": "option", "label": "India", "context": ""}
    married = {"id": "o2", "node": 42, "kind": "click", "role": "option", "label": "Married", "context": ""}
    page = {**make_page(), "actions": [opener, india, married]}
    agent = make_agent([page], UnattendedFakeUI(handover=False))
    agent.page = page
    for key, value in [("personal.nationality", "Indian"), ("personal.citizenship_country", "India")]:
        agent.profile.facts[key] = Fact(key, value, key)
        agent.profile.by_id[safe_id(key)] = agent.profile.facts[key]
    replies = iter([
        {"action": opener, "text": None, "fact": None, "why": "open nationality"},
        {"action": india, "text": None, "fact": "personal.nationality", "why": "Indian is India here"},
        {"done": True, "why": "nationality set"},
    ])  # fmt: skip
    seen = []
    monkeypatch.setattr(teacher, "rescue", lambda *a: seen.append(list(a[7])) or next(replies))
    assert agent.rescue("Primary Nationality is required")
    assert [a for a, _ in agent.browser.acts] == ["b1", "o1"]
    assert seen[1][0]["result"] in {"page changed", "nothing changed"}  # Claude saw what its first step did

    agent.history.append({"kind": "click", "action": "Marital Status Select One", "context": "", "url": page["url"]})
    replies = iter([
        {"action": married, "text": None, "fact": None, "why": "guess"},
        {"action": None, "text": None, "fact": None, "why": "Marital Status: nothing on file says"},
    ])  # fmt: skip
    agent.browser.acts.clear()
    assert not agent.rescue("Marital Status is required")
    assert agent.browser.acts == [] and "nothing on file" in agent.rescue_why  # refused, then Claude said why
    replies = iter([{"action": None, "text": None, "fact": None, "why": "Marital Status: nothing on file says"}])
    with pytest.raises(loop.Stop):  # nobody to hand over to: the run stops, with the reason on record
        agent.handover("The site reports problems on this page.")  # tries a rescue first: the same answer
    assert "Claude: Marital Status: nothing on file says" in agent.handovers[-1]["reason"]


JOB = "https://harman.wd3.myworkdayjobs.com/en-US/harman/job/Bangalore/Software-Engineer_R-55937-2026"


class NavigatingBrowser(FakeBrowser):
    def navigate(self, url):
        self.went = getattr(self, "went", []) + [url]


def test_an_empty_last_application_page_opens_apply_manually_once(make_agent, monkeypatch):
    """Harman: a new account's "Use My Last Application" opened on an empty page; the run waited, then gave up.
    With no form there, the same job's "Apply Manually" address is opened instead, once."""
    empty = {"url": JOB + "/apply/useMyLastApplication", "title": "", "text": "Candidate Home", "actions": []}
    agent = make_agent([empty])
    agent.browser = NavigatingBrowser([empty])
    agent.page = empty
    assert agent.start_manually()
    assert agent.browser.went == [JOB + "/apply/applyManually"]
    assert not agent.start_manually()  # once a run
    agent.manual_start, agent.page = False, {**empty, "url": JOB + "/apply/applyManually"}
    assert not agent.start_manually()  # already manual
    # A half-drawn form with the site's own note: straight to Apply Manually, fields or not
    note = "The application didn't autofill because you don't have a previous application. Continue manually."
    half = {**empty, "text": note, "actions": [{"id": "e9", "kind": "fill", "role": "textbox", "label": "How Did You "
            "Hear About Us?", "node": 9, "value": "", "required": True}]}  # fmt: skip
    agent.manual_start, agent.page, agent.browser.went = False, half, []
    assert not agent.start_manually()  # a form is showing: not without the note
    assert loop.NO_PREVIOUS.search(note) and agent.start_manually(said=True)
    assert agent.browser.went == [JOB + "/apply/applyManually"]


def test_the_rescuer_never_leaves_the_job(make_agent, monkeypatch):
    """Harman: Claude clicked Candidate Home, then Search for Jobs, then another job and its Apply. Portal navigation
    is refused, and a step that lands off the job's pages brings the run back to the job and ends the rescue."""
    from jev_apply import teacher

    monkeypatch.setattr(teacher, "available", lambda: True)
    monkeypatch.setattr(loop.Agent, "show_helper", lambda *a: None)
    home = {"id": "h", "node": 1, "kind": "click", "role": "button", "label": "Candidate Home", "context": ""}
    other = {"id": "j", "node": 2, "kind": "click", "role": "link", "label": "Director, Embedded Software",
             "context": ""}  # fmt: skip
    on = {"url": JOB + "/apply/useMyLastApplication", "title": "", "text": "", "actions": [home, other],
          "fingerprint": 1}  # fmt: skip
    off = {**on, "url": "https://harman.wd3.myworkdayjobs.com/en-US/harman/details/Director_R-53670-2026-1",
           "fingerprint": 2}  # fmt: skip
    agent = make_agent([on])
    agent.browser = NavigatingBrowser([on, on, off, on])
    agent.start_url, agent.page = JOB, on
    replies = iter([
        {"action": home, "text": None, "fact": None, "why": "go home"},
        {"action": other, "text": None, "fact": None, "why": "find a job"},
    ])  # fmt: skip
    monkeypatch.setattr(teacher, "rescue", lambda *a: next(replies))
    assert not agent.rescue("Nothing here leads to an application form")
    assert [a for a, _ in agent.browser.acts] == ["j"]  # Candidate Home refused outright
    assert agent.browser.went == [JOB] and "away from this job" in agent.rescue_why
    assert agent.on_job(JOB + "/apply/applyManually") and agent.on_job("https://accounts.google.com/signin")


def test_an_unattended_run_waits_at_a_login_page_then_carries_on(make_agent, monkeypatch):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    login = {**make_page(), "login": True}
    after = make_page()
    ui = UnattendedFakeUI(handover=False)
    agent = make_agent([login, login, after], ui=ui, login_wait_s=60)
    agent.page = agent.browser.observe()
    assert agent.wait_for_login()
    assert not agent.page.get("login") and not agent.handovers  # signing in is not "the run needed you"
    assert any("LOGIN NEEDED" in str(args) for name, args in ui.calls if name == "event")


def test_the_login_wait_gives_up_after_its_time(make_agent, monkeypatch):
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setattr(loop.time, "monotonic", lambda: next(clock))
    login = {**make_page(), "login": True}
    agent = make_agent([login], ui=UnattendedFakeUI(handover=False), login_wait_s=120)
    agent.page = agent.browser.observe()
    assert not agent.wait_for_login()
    assert not make_agent([login], ui=UnattendedFakeUI(), login_wait_s=0).wait_for_login()


def box(label, context="", required=True, checked="false", node=9):
    return {"id": f"b{node}", "kind": "click", "role": "checkbox", "label": label, "context": context,
            "node": node, "value": "", "required": required, "checked": checked}  # fmt: skip


def test_plain_privacy_consent_is_ticked_but_a_declaration_is_asked(make_agent, tmp_path):
    privacy = box("I have read and agree to the Candidate Privacy Notice", node=9)
    criminal = box("I confirm I have no criminal record and that the information given is true", node=10)
    page = make_page(extra=[privacy, criminal])
    agent = make_agent([page], ui=UnattendedFakeUI(), auto_consent=True)
    agent.page = page
    assert agent.consents(privacy) and not agent.consents(criminal)
    asked = [e["question"] for e in agent.unanswered_entries()]
    assert any("criminal record" in q and q.startswith("Tick this box?") for q in asked)
    assert not any("Privacy Notice" in q for q in asked)
    off = make_agent([page], ui=UnattendedFakeUI())  # auto_consent is off unless you turn it on
    assert not off.consents(privacy)


def test_a_declaration_you_answered_yes_is_ticked_on_later_forms(tmp_path):
    from jev_apply import planner

    criminal = box("I confirm I have no criminal record and that the information given is true", node=10)
    question = planner.declaration_question(criminal)
    profile = Profile(PROFILE, tmp_path, answers=[{"id": "md_1", "question": question, "answer": "Yes"}])
    page = make_page(extra=[criminal])
    agent = loop.Agent(FakeBrowser([page]), profile, Policy(), UnattendedFakeUI(), run_dir=tmp_path / "runs")
    agent.page = page
    assert agent.matches_profile(criminal)
    no = Profile(PROFILE, tmp_path, answers=[{"id": "md_1", "question": question, "answer": "No"}])
    agent = loop.Agent(FakeBrowser([page]), no, Policy(), UnattendedFakeUI(), run_dir=tmp_path / "runs")
    agent.page = page
    assert not agent.matches_profile(criminal)


def test_jev_gets_the_same_procedure_and_a_shortlist_within_its_limit(monkeypatch):
    from jev_apply import planner

    monkeypatch.setenv("DECISION_BACKEND", "typesafe")
    monkeypatch.delenv("DECISION_STEERING", raising=False)
    assert model.steering() == "procedure"  # Laya and Jev both answer inside planner.py's procedure
    monkeypatch.setenv("DECISION_STEERING", "model")
    assert model.steering() == "model"
    monkeypatch.delenv("DECISION_STEERING")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sent = {}

    def fake_post(url, key, body):
        sent.update(body)
        ids = list(body["questions"]["source"]["criteria"])
        return {"answers": {"source": {"choice": ids[0], "confidence": 0.9,
                                       "probabilities": {k: (1.0 if k == ids[0] else 0.0) for k in ids}}}}  # fmt: skip

    monkeypatch.setattr(model, "post_json", fake_post)
    criteria = {f"fact_{i}": f"fact number {i}" for i in range(400)} | {"notice_days": "notice period in days"}
    criteria |= {k: v for k, v in model.SPECIAL.items()}
    body = {
        "state": {"field": {"label": "Notice period (days)"}},
        "questions": {"source": {"type": "choice", "criteria": criteria}},
    }
    result, _ = model.systemone(body)
    offered = sent["questions"]["source"]["criteria"]
    assert len(offered) <= model.MAX_TYPESAFE_CHOICES and "notice_days" in offered and "ASK_USER" in offered
    assert set(result["answers"]["source"]["probabilities"]) == set(criteria)  # dropped options reported as 0
    assert planner  # the procedure module stays importable alongside model


def test_the_sites_confirmation_is_waited_for_after_submit(make_agent, monkeypatch):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    before = make_page()
    sent = {**make_page(), "text": "Your application was sent to Example Corp!"}
    agent = make_agent([before, before, sent], ui=UnattendedFakeUI(), confirm_wait_s=10)
    agent.page = agent.browser.observe()
    assert agent.wait_for_confirmation() and "sent" in agent.page["text"]
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(loop.time, "monotonic", lambda: next(clock))
    quiet = make_agent([before], ui=UnattendedFakeUI(), confirm_wait_s=10)
    quiet.page = quiet.browser.observe()
    assert not quiet.wait_for_confirmation()


def test_a_captcha_submit_is_left_for_you_and_the_batch_moves_on(make_agent):
    page = {**make_page(), "url": "https://jobs.lever.co/acme/123/apply"}
    agent = make_agent([page], ui=UnattendedFakeUI(), auto_submit=True)
    agent.page = page
    agent.not_ready = lambda: []
    submit = action(page, "e4")
    assert agent.submit(submit, decision("CLICK", submit))
    assert agent.status == "ready to submit (captcha)" and not agent.browser.acts  # nothing clicked
    assert any("captcha" in x["why"] for x in agent.left)
    plain = make_agent([make_page()], ui=UnattendedFakeUI(), auto_submit=True)
    plain.page = make_page()
    assert not plain.needs_a_person()  # FakeBrowser has no hCaptcha


def test_answers_from_the_form_are_used_for_that_run_only(make_agent):
    agent = make_agent([make_page()], ui=UnattendedFakeUI())
    agent.answers = {"Notice Period": "15 Days"}
    agent.status = "done"  # nothing to fill: only the answers' lifetime is checked
    seen = {}
    real_finish = agent.finish

    def finish():
        seen["during"] = agent.profile.saved_answer("Notice Period")
        return real_finish()

    agent.finish = finish
    agent.run()
    assert agent.profile.saved_answer("Notice Period") is None  # never saved beyond the run


def test_an_unattended_run_waits_for_your_answers_then_carries_on(make_agent, monkeypatch, tmp_path):
    from jev_apply import inbox

    agent = make_agent([make_page()], ui=UnattendedFakeUI(handover=False), answer_wait_s=60)
    question = "What is your notice period in days?"
    agent.inbox = [{"question": question, "kind": "text", "urls": ["https://x.test/job"]}]
    agent.skipped.add(("doc", 1, "Notice period"))

    def you_answer(seconds):  # while the run waits, you answer in the app (it writes QUESTIONS.md)
        entries = inbox.read(tmp_path / "QUESTIONS.md")
        assert [e["question"] for e in entries] == [question]  # it was put there before waiting
        entries[0]["answer"] = "15"
        inbox.write(tmp_path / "QUESTIONS.md", entries)

    monkeypatch.setattr(loop.time, "sleep", you_answer)
    agent.status = "stopped"
    assert agent.wait_for_answers()
    assert agent.status == "ready" and agent.profile.saved_answer(question) == "15"
    assert not agent.skipped and not agent.inbox  # what was set aside for want of the answer is offered again
    assert agent.history[-1]["action"] == "The candidate answered 1 question(s)"


def test_the_answer_wait_gives_up_and_the_job_records_what_it_waits_on(make_agent, monkeypatch, tmp_path):
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setattr(loop.time, "monotonic", lambda: next(clock))
    agent = make_agent([make_page()], ui=UnattendedFakeUI(handover=False), answer_wait_s=120)
    question = "Are you open to night shifts?"
    agent.inbox = [{"question": question, "kind": "choice", "options": ["Yes", "No"], "urls": ["https://x.test/j"]}]
    assert not agent.wait_for_answers()
    agent.status = "stopped"
    assert agent.finish()["waiting_on"] == [question]
    assert not make_agent([make_page()], ui=UnattendedFakeUI(), answer_wait_s=0).wait_for_answers()


def test_a_run_waits_for_answers_a_few_times_at_most(make_agent, monkeypatch):
    agent = make_agent([make_page()], ui=UnattendedFakeUI(handover=False), answer_wait_s=60)
    agent.answer_rounds = loop.ANSWER_ROUNDS
    agent.inbox = [{"question": "Anything?", "kind": "text", "urls": []}]
    monkeypatch.setattr(loop.time, "sleep", lambda s: pytest.fail("no more waiting"))
    assert not agent.wait_for_answers()


def test_a_verification_code_is_typed_by_you_in_the_tab_never_asked(make_agent, monkeypatch, tmp_path):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    box = {"id": "c1", "kind": "fill", "role": "textbox", "label": "Enter verification code digit 1 of six.",
           "node": 7, "value": "", "required": True}  # fmt: skip
    empty, typed = make_page(extra=[box]), make_page(extra=[{**box, "value": "4"}])
    ui = UnattendedFakeUI(handover=False)
    agent = make_agent([empty, empty, typed], ui=ui, login_wait_s=60)
    agent.do_fill(box, None, False)
    assert agent.status == "ready" and not agent.handovers  # you typed it: the run carries on
    assert any("verification code" in str(args) for name, args in ui.calls if name == "event")
    agent.inbox.append({"question": box["label"], "kind": "text", "urls": []})
    agent.status = "stopped"
    report = agent.finish()
    assert report["waiting_on"] == [] and not (tmp_path / "QUESTIONS.md").exists()  # never put in the bank


def test_continue_in_the_app_ends_the_answer_wait(make_agent, monkeypatch, tmp_path):
    agent = make_agent([make_page()], ui=UnattendedFakeUI(handover=False), answer_wait_s=60)
    agent.inbox = [{"question": "What is your expected CTC?", "kind": "text", "urls": []}]
    tokens = []
    monkeypatch.setattr(agent, "continue_token", lambda: tokens.append("abcdef12") or "abcdef12")

    def you_press_continue(seconds):  # you filled it on the page yourself and pressed Continue in the app
        (tmp_path / "runs" / "continue").mkdir(parents=True, exist_ok=True)
        (tmp_path / "runs" / "continue" / tokens[0]).write_text("now")

    monkeypatch.setattr(loop.time, "sleep", you_press_continue)
    agent.status = "stopped"
    assert agent.wait_for_answers() and agent.status == "ready"
    assert "pressed Continue" in agent.history[-1]["action"]
    assert not (tmp_path / "runs" / "continue" / "abcdef12").exists()  # the signal is used up


def test_already_applied_ends_the_job_without_failing_it(make_agent, monkeypatch, tmp_path):
    agent = make_agent([make_page()], ui=UnattendedFakeUI(handover=False))
    monkeypatch.setattr(
        loop.model, "choose", lambda *a, **k: {**decision("BLOCKED", None), "reason": loop.planner.ALREADY_APPLIED}
    )
    agent.step()
    assert agent.status == "already applied" and not agent.handovers
    recorded = json.loads((tmp_path / "applied.json").read_text(encoding="utf-8"))
    assert list(recorded.values())[0]["status"] == "already applied"  # so no later batch tries it again


def with_bubble(agent, *presses):
    """The agent's browser gets a bubble that reports `presses` (lists of events), one list per check."""
    queue, shown = list(presses), []
    agent.browser.helper_events = lambda: queue.pop(0) if queue else []
    agent.browser.helper_state = shown.append
    return shown


def test_pause_in_the_bubble_holds_the_run_until_resume(make_agent, monkeypatch):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    agent = make_agent([make_page()], ui=UnattendedFakeUI(handover=False))
    shown = with_bubble(agent, [{"type": "pause"}], [], [], [{"type": "resume"}])
    assert agent.helper_check() is False and not agent.paused
    assert any(s["paused"] for s in shown)  # it said "Paused" while it held


def test_skip_in_the_bubble_ends_the_job_without_failing_it(make_agent, monkeypatch):
    agent = make_agent([make_page()], ui=UnattendedFakeUI(handover=False))
    with_bubble(agent, [{"type": "skip"}])
    assert agent.run()["status"] == "deferred"


def test_answers_typed_in_the_bubble_end_the_wait(make_agent, monkeypatch, tmp_path):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    agent = make_agent([make_page()], ui=UnattendedFakeUI(handover=False), answer_wait_s=60)
    question = "Expected CTC (LPA)"
    agent.inbox = [{"question": question, "kind": "text", "urls": []}]
    shown = with_bubble(agent, [{"type": "answers", "answers": {question: "18"}}])
    agent.status = "stopped"
    assert agent.wait_for_answers() and agent.profile.saved_answer(question) == "18"
    assert any(s.get("questions") == [{"question": question, "options": []}] for s in shown)  # asked in the bubble


def test_the_teacher_answers_before_you_are_asked(make_agent, monkeypatch):
    from jev_apply import teacher

    agent = make_agent([make_page()], ui=UnattendedFakeUI(handover=False), answer_wait_s=60)
    agent.inbox = [{"question": "Which city are you in?", "kind": "text", "urls": []}]
    monkeypatch.setattr(teacher, "available", lambda: True)
    monkeypatch.setattr(teacher, "ask", lambda entries, profile, url, run_dir: {"Which city are you in?": "Udaipur"})
    agent.status = "stopped"
    assert agent.consult_teacher() and agent.status == "ready"
    assert agent.profile.saved["Which city are you in?"] == "Udaipur" and not agent.inbox
    assert "teacher answered" in str(agent.ui.calls)


def with_email(agent):
    from jev_apply.profile import Fact

    agent.profile.facts["personal.email"] = Fact("personal.email", "me@mail.test", "e-mail")
    return agent


def portal(*actions, text="", login=True, url="https://acme.wd3.myworkdayjobs.com/en-US/careers/login"):
    """A sign-in page as browser.observe returns it: password boxes apart, in "secrets"."""
    secrets = [{**a, "secret": True} for a in actions if a.get("input_type") == "password"]
    return {"url": url, "title": "Sign In", "text": text, "doc": 1, "login": login, "secrets": secrets,
            "actions": [a for a in actions if a.get("input_type") != "password"],
            "page_key": "k", "guards": {}, "marker": "m",
            "fingerprint": json.dumps([text, actions, secrets], sort_keys=True)}  # fmt: skip


def entry_box(n, label, kind="text", value=""):
    return {"id": f"b{n}", "node": n, "kind": "fill", "role": "textbox", "label": label, "input_type": kind,
            "value": value}  # fmt: skip


def btn(n, label, role="button"):
    return {"id": f"c{n}", "node": n, "kind": "click", "role": role, "label": label}


def test_a_portal_sign_in_uses_your_email_and_the_portal_password(make_agent, monkeypatch, tmp_path):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setenv("JEV_PORTAL_PASSWORD", "Agent@123")
    email, pw, go = entry_box(1, "Email Address", "email"), entry_box(2, "Password", "password"), btn(3, "Sign In")
    pages = [portal(email, pw, go), portal({**email, "value": "x"}, pw, go),
             portal({**email, "value": "x"}, {**pw, "value": "*"}, go),
             {**portal(text="My Information", login=False), "actions": []}]  # fmt: skip
    agent = with_email(make_agent(pages, ui=UnattendedFakeUI(handover=False), portal_accounts=True))
    assert agent.sign_in_with_password()
    assert [a for a, _ in agent.browser.acts] == ["b1", "b2", "c3"]
    assert agent.browser.acts[0][1]["text"] == "me@mail.test"
    assert agent.browser.acts[1][1]["text"] == "Agent@123"
    assert "Agent@123" not in json.dumps(agent.history)  # never written down
    book = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    assert "acme.wd3.myworkdayjobs.com" in book


def test_a_new_portal_gets_an_account_created_terms_ticked(make_agent, monkeypatch):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setenv("JEV_PORTAL_PASSWORD", "Agent@123")
    email, pw, again = (
        entry_box(1, "Email Address", "email", "x"),
        entry_box(2, "Password", "password", "*"),
        entry_box(3, "Verify New Password", "password", "*"),
    )
    terms = {
        "id": "t4",
        "node": 4,
        "kind": "click",
        "role": "checkbox",
        "label": "I agree to the terms",
        "checked": "false",
    }
    create = btn(5, "Create Account")
    pages = [portal(email, pw, again, terms, create), portal(email, pw, again, {**terms, "checked": "true"}, create),
             {**portal(text="My Information", login=False), "actions": []}]  # fmt: skip
    agent = with_email(make_agent(pages, ui=UnattendedFakeUI(handover=False), portal_accounts=True))
    assert agent.sign_in_with_password()
    assert [a for a, _ in agent.browser.acts] == ["t4", "c5"]


def test_no_account_on_record_creates_one_before_signing_in(make_agent, monkeypatch):
    """Xylem: the sign-in form (one password box) with a Create Account button, and no account here yet."""
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setenv("JEV_PORTAL_PASSWORD", "Agent@123")
    sign_in = portal(entry_box(1, "Email Address"), entry_box(2, "Password", "password"), btn(3, "Create Account"),
                     btn(4, "Sign In"))  # fmt: skip
    email, pw, again = entry_box(5, "Email Address"), entry_box(6, "Password", "password"), entry_box(
        7, "Verify New Password", "password"
    )
    create, have = btn(8, "Create Account"), "Create Account Already have an account? Sign In"
    pages = [
        sign_in,
        portal(email, pw, again, create, text=have),
        portal({**email, "value": "x"}, pw, again, create, text=have),
        portal({**email, "value": "x"}, {**pw, "value": "*"}, again, create, text=have),
        portal({**email, "value": "x"}, {**pw, "value": "*"}, {**again, "value": "*"}, create, text=have),
        {**portal(text="My Information", login=False), "actions": []},
    ]
    agent = with_email(make_agent(pages, ui=UnattendedFakeUI(handover=False), portal_accounts=True))
    assert agent.sign_in_with_password()
    assert [a for a, _ in agent.browser.acts] == ["c3", "b5", "b6", "b7", "c8"]
    assert [kw.get("text") for _, kw in agent.browser.acts][1:4] == ["me@mail.test", "Agent@123", "Agent@123"]
    assert "Agent@123" not in json.dumps(agent.history)


def test_a_popup_over_verify_password_keeps_it_a_sign_up(make_agent, monkeypatch):
    """Xylem: Workday's Password Requirements panel covers Verify New Password after the password is typed."""
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setenv("JEV_PORTAL_PASSWORD", "Agent@123")
    email, pw, again = entry_box(1, "Email Address", value="x"), entry_box(2, "Password", "password"), entry_box(
        3, "Verify New Password", "password"
    )
    create, sign_in = btn(4, "Create Account"), btn(5, "Sign In")
    pages = [
        portal(email, pw, again, create, sign_in),
        portal(email, {**pw, "value": "*"}, create, sign_in),  # the panel hides the Verify box for a moment
        portal(email, {**pw, "value": "*"}, again, create, sign_in),
        portal(email, {**pw, "value": "*"}, {**again, "value": "*"}, create, sign_in),
        {**portal(text="My Information", login=False), "actions": []},
    ]
    agent = with_email(make_agent(pages, ui=UnattendedFakeUI(handover=False), portal_accounts=True))
    assert agent.sign_in_with_password()
    # The password, then (the panel gone) Verify New Password, then Create Account: never Sign In.
    assert [a for a, _ in agent.browser.acts] == ["b2", "b3", "c4"]


def test_an_account_that_exists_already_is_signed_in_to(make_agent, monkeypatch, tmp_path):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setenv("JEV_PORTAL_PASSWORD", "Agent@123")
    full = (entry_box(1, "Email Address", value="x"), entry_box(2, "Password", "password", "*"),
            entry_box(3, "Verify New Password", "password", "*"))  # fmt: skip
    form = portal(*full, btn(4, "Create Account"), btn(5, "Sign In", role="link"))
    taken = portal(*full, btn(4, "Create Account"), btn(5, "Sign In", role="link"),
                   text="An account with this email already exists.")  # fmt: skip
    def sign_in(mail="", word=""):
        return portal(entry_box(6, "Email Address", value=mail), entry_box(7, "Password", "password", word),
                      btn(8, "Create Account"), btn(9, "Sign In"))  # fmt: skip

    pages = [form, taken, sign_in(), sign_in("x"), sign_in("x", "*"),
             {**portal(text="My Information", login=False), "actions": []}]  # fmt: skip
    agent = with_email(make_agent(pages, ui=UnattendedFakeUI(handover=False), portal_accounts=True))
    assert agent.sign_in_with_password()
    assert [a for a, _ in agent.browser.acts] == ["c4", "c5", "b6", "b7", "c9"]  # one Create Account only
    book = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    assert book["acme.wd3.myworkdayjobs.com"]["created"] == "existing"


def test_a_verify_your_account_sign_in_opens_the_link_from_your_email(make_agent, monkeypatch, tmp_path):
    """Motorola: account created in an earlier run; its sign-in says "Verify your account before you sign in", with
    the e-mail and password boxes still there. The link is read from Gmail (looking back to the account's creation),
    opened, and then the sign-in goes on."""
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setenv("JEV_PORTAL_PASSWORD", "Agent@123")
    url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/login"
    (tmp_path / "accounts.json").write_text(json.dumps({"acme.wd5.myworkdayjobs.com": {
        "email": "me@mail.test", "created": "2026-10-11T19:55:18"}}), encoding="utf-8")  # fmt: skip
    email, pw = entry_box(1, "Email Address", value="x"), entry_box(2, "Password", "password", "*")
    verify = portal(email, pw, btn(3, "Sign In"), url=url,
                    text="Verify your account before you sign in or request a verification email.")  # fmt: skip
    pages = [verify, {**portal(text="Welcome", login=False, url=url), "actions": []}]
    agent = with_email(make_agent(pages, ui=UnattendedFakeUI(handover=False), portal_accounts=True))
    asked = {}

    def mail(want, timeout=150, since=None):
        asked.update(want=want, since=since)
        return "https://acme.wd5.myworkdayjobs.com/careers/activate/abc"

    monkeypatch.setattr(agent, "mail", mail)
    opened = []
    monkeypatch.setattr(agent.browser, "navigate", lambda u: opened.append(u), raising=False)
    # The activation page confirms the token after loading: the agent waits for its answer before leaving
    monkeypatch.setattr(agent.browser, "evaluate", lambda js: "Your account has been verified.", raising=False)
    assert agent.sign_in_with_password()
    assert asked["want"] == "link" and asked["since"] < agent.mail_since  # back to the account's creation
    assert opened[0].endswith("/careers/activate/abc")
    # Blue Yonder: a one-time link opened twice says "invalid token": the same link is never opened again
    book = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))["acme.wd5.myworkdayjobs.com"]
    assert book.get("link_used")
    again = with_email(make_agent([verify], ui=UnattendedFakeUI(handover=False), portal_accounts=True))
    monkeypatch.setattr(again, "mail", mail)
    monkeypatch.setattr(again.browser, "navigate", lambda u: opened.append(u), raising=False)
    assert not again.sign_in_with_password() and len(opened) == 2  # only the first run's link and its way back


def test_a_refused_sign_in_is_never_retried(make_agent, monkeypatch):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setenv("JEV_PORTAL_PASSWORD", "Agent@123")
    email, pw = entry_box(1, "Email Address", "email", "x"), entry_box(2, "Password", "password", "*")
    page = portal(email, pw, btn(3, "Sign In"), text="Invalid email or password")
    agent = with_email(make_agent([page], ui=UnattendedFakeUI(handover=False), portal_accounts=True))
    assert not agent.sign_in_with_password() and agent.browser.acts == []
    # ABB: refused, so the account is yours from before; the next run doesn't try the password on it again
    from jev_apply import accounts

    entry = accounts.book(agent.profile).get(accounts.site(page["url"]), {})
    assert entry.get("created") == "existing" and entry.get("refused")
    fresh = portal(entry_box(1, "Email Address", "email"), entry_box(2, "Password", "password"), btn(3, "Sign In"))
    again = with_email(make_agent([fresh], ui=UnattendedFakeUI(handover=False), portal_accounts=True))
    assert not again.sign_in_with_password() and again.browser.acts == []
    monkeypatch.delenv("JEV_PORTAL_PASSWORD")
    assert not make_agent([page], ui=UnattendedFakeUI(), portal_accounts=True).sign_in_with_password()


def test_a_workday_prompt_is_read_back_from_its_selection_not_its_emptied_box(make_agent):
    """Xylem: typing "LinkedIn" then Enter: the box empties and the field says "1 item selected, LinkedIn"."""
    q = "How Did You Hear About Us?"
    box = {"id": "e1", "node": 7, "kind": "fill", "role": "textbox", "label": q, "placeholder": "Search", "value": ""}
    typed = {**portal(box, login=False), "doc": 1}
    picked = {**portal({**box, "context": "1 item selected, LinkedIn"},
                       {"id": "e2", "node": 8, "kind": "click", "role": "option",
                        "label": "LinkedIn, press delete to clear value."}, login=False), "doc": 1}  # fmt: skip
    agent = make_agent([typed])
    agent.page = typed
    fill = {"label": q, "kind": "fill", "node": 7, "doc": 1, "multiple": False, "chooser": False,
            "expected": "LinkedIn", "url": typed["url"], "confirmed": None}  # fmt: skip
    agent.verify(fill)
    assert fill["confirmed"] is None  # emptied before the pick: not a failure yet
    agent.page = picked
    agent.recheck_prompts({"kind": "enter", "node": 7})
    assert fill["confirmed"] is None  # recheck only looks at agent.fills
    agent.fills.append(fill)
    agent.recheck_prompts({"kind": "enter", "node": 7})
    assert fill["confirmed"] is True


def test_a_job_is_in_your_country_when_its_page_names_it_your_city_or_where_you_would_work(make_agent):
    """A fixed list of Indian cities missed jobs in other cities: your country, city and preferred places count."""
    from jev_apply.profile import Fact

    page = make_page()
    agent = make_agent([page])
    for key, value in [("personal.country", "India"), ("preferences.preferred_locations", "Mysuru, Lucknow")]:
        agent.profile.facts[key] = Fact(key, value, key)
        agent.profile.by_id[safe_id(key)] = agent.profile.facts[key]
    agent.page = {**page, "title": "Software Engineer - Lucknow", "text": ""}
    agent.start_url = "https://acme.example/jobs/1"
    assert agent.job_in_your_country() == "India"
    agent.page = {**page, "title": "Software Engineer - Berlin", "text": "Our team in Berlin"}
    assert agent.job_in_your_country() is None  # a job elsewhere: eligibility there is yours to answer


def test_after_signing_in_a_portal_home_page_leads_back_to_the_job(make_agent, monkeypatch):
    """Blue Yonder: signed in, Workday showed "My Applications" instead of the job; Claude then wandered to Search
    for Jobs. The agent goes back to the job link the run started from."""
    job = "https://jda.wd5.myworkdayjobs.com/jda_careers/job/Hyderabad/Software-Engineer-II_263104"
    home = {**make_page(), "url": "https://jda.wd5.myworkdayjobs.com/en-US/jda_careers/userHome", "actions": [
        {"id": "b1", "node": 1, "kind": "click", "role": "button", "label": "Search for Jobs"}]}  # fmt: skip
    agent = make_agent([home])
    agent.start_url, agent.page = job, home
    opened = []
    monkeypatch.setattr(agent.browser, "navigate", lambda u: opened.append(u), raising=False)
    agent.back_to_job()
    assert opened == [job]
    opened.clear()
    agent.page = {**home, "url": job + "/apply/applyManually"}
    agent.back_to_job()
    assert opened == []  # already on the job's own pages


def test_an_application_from_before_the_record_is_found_in_your_email(make_agent, monkeypatch, tmp_path):
    """ABB: applied in July, before this app kept records; "Have you applied before?" would have been "No"."""
    from jev_apply import gmail

    agent = make_agent([make_page()], email_codes=True)
    agent.start_url = "https://abb.wd3.myworkdayjobs.com/external_career_page/job/x"
    monkeypatch.setattr(agent, "applied_path", lambda: tmp_path / "applied.json")
    monkeypatch.setattr(gmail, "applied_before", lambda folder, url: "abb.wd3" in url)
    assert agent.applied_here()
    agent.start_url = "https://nvidia.wd5.myworkdayjobs.com/x"
    assert not agent.applied_here()


def test_an_unanswered_workday_dropdown_goes_to_questions(make_agent):
    """NCR Atleos: "Are you subject to a non-compete...?" (a required Select One nothing on file answers) never
    reached QUESTIONS.md, so the run just stopped and you were never asked."""
    page = make_page()
    button_ = {"id": "b1", "node": 40, "kind": "click", "role": "button", "label": "Select One Required",
               "context": "Are you subject to a non-compete/non-solicitation or other post-employment restriction?* "
                          "Error : The field is required"}  # fmt: skip
    named = {**button_, "id": "b2", "node": 41, "label": "Do you have any Board of Director commitments? Select One",
             "required": True, "context": ""}  # fmt: skip
    agent = make_agent([{**page, "actions": [button_, named]}])
    agent.page = {**page, "actions": [button_, named]}
    asked = [e["question"] for e in agent.unanswered_entries()]
    assert "Are you subject to a non-compete/non-solicitation or other post-employment restriction?" in asked
    assert "Do you have any Board of Director commitments?" in asked


def test_the_rescuer_may_not_clear_a_settled_pick(make_agent):
    """Harman: Claude tried "Remove: Computer and Information Science", the closest pick for your field of study."""
    agent = make_agent([make_page()])
    agent.mapped.add((loop.norm("Field of Study"), loop.norm("Computer and Information Science")))
    remove = {"id": "r", "kind": "click", "role": "button", "label": "Remove: Computer and Information Science"}
    chip = {"id": "c", "kind": "click", "role": "option",
            "label": "Computer and Information Science, press delete to clear value."}  # fmt: skip
    other = {"id": "o", "kind": "click", "role": "button", "label": "Remove: India (+91)"}
    assert agent.clears_settled(remove) and agent.clears_settled(chip) and not agent.clears_settled(other)


def test_a_page_that_moved_on_is_carried_on_from_not_handed_over(make_agent, monkeypatch):
    """Cisco: after Save, My Experience still showed while Application Questions loaded; the run gave up on the old
    step and sent the new step's dropdowns to you. One more look first: a new step means carry on."""
    old = {**make_page(), "step": "current step 2 of 4 My Experience", "fingerprint": 1}
    new = {**make_page(), "step": "current step 3 of 4 Application Questions", "fingerprint": 2}
    agent = make_agent([new], UnattendedFakeUI(handover=False))
    agent.page = old
    monkeypatch.setattr(loop.Agent, "rescue", lambda *a, **k: pytest.fail("no rescue for a page that moved on"))
    agent.handover("Nothing more the agent can fill here, and no Next/Continue or Submit button.")
    assert agent.page["step"].endswith("Application Questions") and not agent.handovers
    agent.second_looks = 5  # a page that never settles still ends in a handover
    agent.page = old
    monkeypatch.setattr(loop.Agent, "rescue", lambda *a, **k: False)
    with pytest.raises(loop.Stop):
        agent.handover("still nothing")


def test_a_sign_in_wall_inside_the_form_is_waited_out(make_agent, monkeypatch):
    """Deluxe: its Apply Manually form showed "Sign In" for a moment while loading; the run signed in through it,
    landed on the home page, went back, and again. Inside /apply the page is looked at again first."""
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    url = "https://deluxe.wd5.myworkdayjobs.com/usa_can/job/x/apply/applyManually"
    wall = {**make_page(), "url": url, "login": True}
    form = {**make_page(), "url": url}
    agent = make_agent([wall, form])
    agent.page = wall
    assert agent.wall_passes() and not agent.page.get("login")
    agent = make_agent([wall])
    agent.page = wall
    assert not agent.wall_passes()  # still a wall after the wait: a real one
    agent.page = {**wall, "url": "https://jobs.example/login"}
    assert not agent.wall_passes()  # not inside an application: no wait


def test_a_frozen_page_is_opened_again_in_a_new_tab_once(make_agent, monkeypatch):
    """Accenture: the page's script hung (not even document.readyState answered); the run handed over for minutes."""
    page = make_page()

    class Frozen(FakeBrowser):
        frozen, reopened_at = True, None

        def observe(self):
            if self.reopened_at:
                return deepcopy(page)
            raise StalePage("The page didn't answer (busy)")

        def reopen(self, url):
            self.reopened_at, self.frozen = url, False
            return True

    agent = make_agent([page])
    agent.browser, agent.page = Frozen([page]), page
    agent.reobserve()
    assert agent.browser.reopened_at == page["url"] and agent.reopened
