"""The Laya backend with a fake checkpoint: no PyTorch, no download."""

from jev_apply import laya_backend, model
from jev_apply.profile import Profile, safe_id

from .test_agent import CURRENT_LPA, PROFILE, action, make_page


def words_tok(text, add_special_tokens=False):
    return {"input_ids": text.split()}


class FakeLaya:
    """Records what Laya would see and answers with the option whose text contains `prefer`."""

    def __init__(self, prefer=None):
        self.prefer, self.calls = prefer, []

    def __call__(self, state, questions):
        self.calls.append((state, questions))
        answers = {}
        for qid, q in questions.items():
            keys = list(q["criteria"])
            pick = next((k for k in keys if self.prefer and self.prefer in str(q["criteria"][k])), keys[0])
            answers[qid] = {
                "choice": pick,
                "probabilities": {k: (0.9 if k == pick else 0.1 / (len(keys) - 1)) for k in keys},
                "confidence": 0.8,
            }
        return {"answers": answers, "usage": {"input_tokens": 1}}


REAL_SYSTEM_ONE = laya_backend.system_one


def use_fake(monkeypatch, fake, tok=words_tok):
    monkeypatch.setenv("DECISION_BACKEND", "laya")
    monkeypatch.setattr(laya_backend, "system_one", lambda body: REAL_SYSTEM_ONE(body, fake, tok, None))


def test_option_text_puts_value_and_unit_first_so_the_48_token_cut_keeps_them():
    profile = Profile(PROFILE)
    lpa = laya_backend.describe_source(profile.by_id[CURRENT_LPA])
    annual = laya_backend.describe_source(profile.by_id[safe_id("compensation.current.total_annual")])
    assert lpa.startswith("value: 17 · in LAKHS per annum")
    assert annual.startswith("value: 1700000 · full annual amount")
    assert "CURRENT CTC" in lpa and "CURRENT CTC" in annual


def test_value_source_shortlists_long_fact_lists_but_keeps_ask_skip_and_draft(monkeypatch):
    monkeypatch.setenv("LAYA_HEAD_MAX_LEN", "120")  # force a shortlist
    monkeypatch.setenv("LAYA_MAX_OPTIONS", "4")
    fake = FakeLaya(prefer="in LAKHS")
    use_fake(monkeypatch, fake)
    monkeypatch.setattr(model.rules, "match", lambda *a, **k: None)  # the model path; rules have their own tests
    profile, page = Profile(PROFILE), make_page()
    result = model.value_source(action(page, "e1"), page, profile, [])
    sent = fake.calls[0][1]["source"]["criteria"]
    assert {"ASK_USER", "SKIP_FIELD", "DRAFT_ANSWER"} <= set(sent)
    assert len(sent) == 4 + 3
    assert all(k.startswith("#") for k in sent if k not in laya_backend.PROTECTED)  # long fact ids are aliased
    # The answer comes back in the caller's ids, with every original option present.
    assert set(result["probabilities"]) == set(profile.sources()) | {"ASK_USER", "SKIP_FIELD", "DRAFT_ANSWER"}
    assert result["choice"].endswith("_lpa") and result["probabilities"][result["choice"]] == 0.9
    assert any(text.startswith("value: 17 · in LAKHS") for text in sent.values())  # current CTC in LPA survived
    assert abs(sum(result["probabilities"].values()) - 1) < 0.02


def test_shortlist_ranks_the_words_the_field_uses():
    options = {
        "a": "value: 22 · in LAKHS per annum (LPA) · EXPECTED CTC: total annual compensation",
        "b": "value: 17 · in LAKHS per annum (LPA) · CURRENT CTC: total CURRENT annual cost",
        "c": "value: Aarav · First name / given name",
        "d": "value: Sharma · Last name / surname",
        "ASK_USER": "ask",
    }
    kept = laya_backend.shortlist("Expected CTC (in LPA)", options, 2)
    assert kept == ["a", "b", "ASK_USER"]


def test_unknown_backend_is_refused_before_anything_runs(monkeypatch):
    monkeypatch.setenv("DECISION_BACKEND", "gpt")
    try:
        model.systemone({"state": {}, "questions": {}})
    except RuntimeError as error:
        assert "DECISION_BACKEND" in str(error)
    else:
        raise AssertionError("expected RuntimeError")
