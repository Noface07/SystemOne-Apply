"""Replay: every page kept from real runs (data/replay, private) is decided again by the planner, offline.
The page reading itself is checked in a browser by scripts/replay.py check."""

import json
from pathlib import Path

import pytest

from jev_apply import replay

KEPT = replay.goldens(Path(__file__).resolve().parents[1] / "data" / "replay")


@pytest.mark.parametrize("path", KEPT, ids=[f"{p.parent.name}/{p.stem}" for p in KEPT])
def test_a_kept_page_is_decided_as_before(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    decided = replay.decide(data)
    assert replay.same_decision(decided, data["expected"]["decision"]), (decided, data["expected"]["decision"])


def test_pages_are_kept_once_per_layout_and_promoted_as_goldens(tmp_path):
    class Browser:
        def evaluate(self, expression):
            return "<!doctype html><html><body>frozen</body></html>"

    field = {"id": "e1", "node": 1, "kind": "fill", "role": "textbox", "label": "City", "value": "Udaipur"}
    secret = {"id": "e2", "node": 2, "kind": "fill", "role": "textbox", "label": "Password", "value": "x",
              "input_type": "password"}  # fmt: skip
    page = {"url": "https://acme.wd1.myworkdayjobs.com/apply", "text": "", "actions": [field, secret]}
    decided = {"operation": "TYPE_TEXT", "action": field, "target_probability": 1.0}
    recorder = replay.Recorder(tmp_path)
    for value in ("Udaipur", "Jaipur"):  # the same layout with another value: kept once
        page["actions"][0] = {**field, "value": value}
        recorder.record(Browser(), page, page, [{"kind": "fill", "action": "Password", "typed": "x"}],
                        {"personal.city": "Udaipur"}, (), decided, decided)  # fmt: skip
    assert recorder.count == 1
    kept = json.loads((recorder.folder / "001.json").read_text(encoding="utf-8"))
    assert kept["actions"][1]["value"] == "" and "typed" not in kept["history"][0]  # no password written down
    golden = replay.promote(recorder.folder, tmp_path / "replay")
    assert golden[0].parent.name == "acme.wd1.myworkdayjobs.com" and golden[0].with_suffix(".html").is_file()
    data = json.loads(golden[0].read_text(encoding="utf-8"))
    assert data["expected"]["decision"]["label"] == "City" and data["expected"]["reading"][0]["label"] == "City"
    assert replay.diff(data["expected"]["reading"], [{**data["expected"]["reading"][0], "context": "Date of Birth"},
                                                     data["expected"]["reading"][1]])  # read differently: reported
