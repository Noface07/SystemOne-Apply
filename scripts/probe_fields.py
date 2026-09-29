"""Measure how the decision model maps tricky field wordings (current vs expected CTC, LPA vs annual...)
to your profile.

    uv run python scripts/probe_fields.py [--fields data/probe_fields.json] [--profile data/profile.json]

One decision per field, on the backend set by DECISION_BACKEND (laya by default: local and free; typesafe
for Jev). What matters most is the last line: CONFIDENTLY WRONG answers are the dangerous ones. Close calls
are safe because the agent asks you whenever the margin is below the policy's.
"""

import argparse
import json
from pathlib import Path

from jev_apply import model
from jev_apply.cli import load_environment
from jev_apply.policy import Policy
from jev_apply.profile import Profile, safe_id


def main():
    load_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("--fields", default="data/probe_fields.json")
    parser.add_argument("--profile", default="data/profile.json")
    args = parser.parse_args()
    fields_path = Path(args.fields) if Path(args.fields).exists() else Path("data/probe_fields.example.json")
    profile_path = Path(args.profile) if Path(args.profile).exists() else Path("data/profile.example.json")
    # Judge close calls with the thresholds the real run will use: the policy.json next to the profile.
    policy_path = profile_path.parent / "policy.json"
    profile, policy = Profile.load(profile_path), Policy.load(policy_path)
    fields = json.loads(fields_path.read_text(encoding="utf-8"))["fields"]
    print(f"{len(fields)} fields from {fields_path}, facts from {profile_path}, backend {model.backend()}")
    source = policy_path if policy_path.exists() else "defaults"
    print(f"close call = margin below {policy.min_value_margin} (from {source})\n")

    tally = {"right": 0, "asks": 0, "wrong": 0}
    for number, spec in enumerate(fields, 1):
        action = {"kind": "fill", "role": "textbox", "node": number, "value": "", **spec}  # role may be overridden
        action.pop("expect")
        dates_only = spec.get("input_type") in {"date", "month"}
        page = {"url": "https://example.test/apply", "title": "Job application", "text": spec.get("context", "")}
        result = model.value_source(action, page, profile, [], dates_only)
        wanted = spec["expect"] if isinstance(spec["expect"], list) else [spec["expect"]]
        expect = {w if w.isupper() else safe_id(w) for w in wanted}
        chosen, p = result["choice"], result["probabilities"][result["choice"]]
        if result["margin"] < policy.min_value_margin and chosen not in expect:
            verdict = "asks you (close call)"
            tally["asks"] += 1
        elif chosen in expect:
            verdict = "right"
            tally["right"] += 1
        else:
            verdict = "CONFIDENTLY WRONG"
            tally["wrong"] += 1
        fact = profile.by_id.get(chosen)
        shown = f"{fact.key} = {fact.value}" if fact else chosen
        print(f"{spec['label'][:40]:<40} -> {shown[:58]:<58} p={p:.2f} margin={result['margin']:.2f}  {verdict}")
    print(f"\nright {tally['right']}, would ask you {tally['asks']}, confidently wrong {tally['wrong']}")


if __name__ == "__main__":
    main()
