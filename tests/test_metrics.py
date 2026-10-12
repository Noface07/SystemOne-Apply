"""Metrics: every run's site, ATS and stop code, and the first-run Review rate per site."""

import json

from jev_apply import metrics


def test_stop_codes_name_the_cause_not_the_catch_all():
    assert metrics.stop_code("review") == "review"
    assert metrics.stop_code("submitted (no confirmation seen)") == "submitted"
    reason = "Nothing here leads to an application form the agent recognises: maybe a CAPTCHA, a login, or ..."
    assert metrics.stop_code("stopped", [{"reason": reason}]) == "no_form_found"  # not "login"
    assert metrics.stop_code("stopped", [{"reason": "The site reports problems on this page."}]) == "site_errors"
    assert metrics.stop_code("stopped", [{"reason": "'Next' didn't move the form on."}]) == "no_progress"
    assert metrics.tags("https://maersk.wd3.myworkdayjobs.com/x", "review")["ats"] == "workday"


def test_the_first_run_on_each_site_and_runs_until_review_are_counted(tmp_path):
    runs = [("1", "https://a.wd1.myworkdayjobs.com/j", "stopped"), ("2", "https://a.wd1.myworkdayjobs.com/j", "review"),
            ("3", "https://job-boards.greenhouse.io/b/1", "review"), ("4", "http://localhost:9/apply", "review")]
    for name, url, status in runs:
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.json").write_text(json.dumps(
            {"start_url": url, "status": status, "handovers": [],
             "rescues": [{"helped": status == "review"}]}), encoding="utf-8")  # fmt: skip
    s = metrics.summary(metrics.load(tmp_path))
    assert s["runs"] == 3 and s["sites"] == 2  # the local fixture isn't a site
    assert s["first_contact"] == {"sites": 2, "reached": 1}
    assert s["runs_until_reached"] == {"a.wd1.myworkdayjobs.com": 2, "job-boards.greenhouse.io": 1}
    assert s["per_ats"]["workday"]["first_reached"] == 0 and s["rescues"] == {**s["rescues"], "tried": 3, "helped": 2}
    assert "First run on a new site reached Review: 1 of 2" in metrics.render(s)
