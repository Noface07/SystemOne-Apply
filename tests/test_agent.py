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
