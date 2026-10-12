"""QUESTIONS.md: answers by option or number, never a placeholder as an option, and no lost changes when a batch
and the app write it at once."""

import threading

from jev_apply import inbox
from jev_apply.profile import Profile, real_options


def test_a_number_that_starts_an_option_means_that_option():
    years = ["0 year", "1 year", "2 years", "3 years", "4 years"]
    assert inbox.option_for("3", years) == "3 years"  # not the third option, "2 years"
    assert inbox.option_for("2", ["Yes", "No"]) == "No"  # the second option
    assert inbox.option_for("15 days", ["30 Days"]) == "15 days"  # not an option: kept as typed
    assert inbox.option_for("12", ["10-12 LPA", "12-15 LPA"]) == "12-15 LPA"


def test_option_numbers_are_stripped_but_digits_inside_options_kept(tmp_path):
    path = tmp_path / "QUESTIONS.md"
    inbox.write(path, [{"question": "Shift?", "options": ["Day (9-6)", "Night 2) late"], "urls": [], "answer": "1"}])
    [entry] = inbox.read(path)
    assert entry["options"] == ["Day (9-6)", "Night 2) late"] and entry["answer"] == "Day (9-6)"


def test_a_dropdowns_placeholder_is_never_an_option(tmp_path):
    assert real_options(["Select One Required", "India (+, press delete to clear value.", "Open Phone", "Yes"]) == [
        "Yes"
    ]
    path = tmp_path / "QUESTIONS.md"
    Profile({}, tmp_path, questions_path=path).add_pending(
        [{"question": "Are you willing to relocate?", "options": ["Select One Required"], "urls": []}]
    )
    assert inbox.read(path)[0]["options"] == []  # a text question, not one button saying "Select One Required"


def test_writers_at_the_same_time_lose_nothing(tmp_path):
    path = tmp_path / "QUESTIONS.md"
    inbox.write(path, [{"question": f"Q{n}?", "options": [], "urls": [], "answer": ""} for n in range(20)])

    def answer(n):  # the app answering while a batch adds questions
        def change(entries):
            next(e for e in entries if e["question"] == f"Q{n}?")["answer"] = str(n)

        inbox.update(path, change)

    def add(n):
        inbox.update(
            path, lambda entries: entries + [{"question": f"New{n}?", "options": [], "urls": [], "answer": ""}]
        )

    threads = [threading.Thread(target=answer, args=(n,)) for n in range(20)]
    threads += [threading.Thread(target=add, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = {e["question"]: e["answer"] for e in inbox.read(path)}
    assert all(entries[f"Q{n}?"] == str(n) for n in range(20))
    assert all(f"New{n}?" in entries for n in range(10))
    assert not (tmp_path / "QUESTIONS.md.lock").exists()
