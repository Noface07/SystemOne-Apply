"""Clef-Flash through llama-server's /v1/systemone: the same contract as every other backend."""

from jev_apply import clef_backend, model


def test_clef_answers_cover_every_option_offered_and_keep_the_exits(monkeypatch):
    monkeypatch.setenv("DECISION_BACKEND", "clef")
    monkeypatch.setenv("CLEF_MAX_OPTIONS", "8")
    sent = {}

    def fake_post(url, key, body):
        sent.update(url=url, body=body)
        kept = list(body["questions"]["source"]["criteria"])
        probabilities = {k: (0.9 if k == "fact_3" else 0.1 / (len(kept) - 1)) for k in kept}
        return {"answers": {"source": {"choice": "fact_3", "confidence": 0.9, "probabilities": probabilities}}}

    monkeypatch.setattr(model, "post_json", fake_post)
    criteria = {f"fact_{n}": f"Fact number {n} about python" for n in range(30)}
    criteria.update({k: v for k, v in model.SPECIAL.items()})
    body = {
        "state": {"field": {"label": "Years with Python", "context": "fact_3"}},
        "questions": {"source": {"type": "choice", "criteria": criteria}},
    }
    result, _ = model.systemone(body)
    offered = sent["body"]["questions"]["source"]["criteria"]
    assert sent["url"].endswith("/v1/systemone") and sent["body"]["model"] == "clef-flash"
    assert len(offered) <= 8 and set(model.SPECIAL) <= set(offered)  # exits are never shortlisted away
    answer = model.validate_choice(result["answers"]["source"], criteria)  # every option, dropped ones at 0
    assert answer["choice"] == "fact_3" and set(answer["probabilities"]) == set(criteria)


def test_clef_without_a_server_or_settings_says_what_to_set(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEF_BASE_URL", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.delenv("CLEF_MODEL_PATH", raising=False)
    monkeypatch.delenv("CLEF_SERVER_BIN", raising=False)
    assert "CLEF_MODEL_PATH" in clef_backend.problem()
    monkeypatch.setenv("CLEF_MODEL_PATH", str(tmp_path / "missing.gguf"))
    monkeypatch.setenv("CLEF_SERVER_BIN", str(tmp_path / "llama-server.exe"))
    assert "doesn't exist" in clef_backend.problem()
    command = clef_backend.command(clef_backend.settings())
    assert command[command.index("--port") + 1] == "9" and "-ngl" in command
