# Working on jev-apply

Read this before changing `agent.py`, `browser.py`, `snapshot.js` or `policy.py`.

## Invariants (do not weaken)

1. **No final submit unless the user turned on `auto_submit`, and then only a complete application.**
   `Policy.submit_like` + `ui.is_final_submit` gate every submit-like click; by default a yes stops the run in
   `review`. With `auto_submit`, `Agent.not_ready` must be empty (nothing required empty or invalid, nothing left,
   no open question, no unconfirmed value, no unread draft unless `submit_drafts`), a watched run asks first, and
   the job is recorded in `data/applied.json` and never submitted twice. Enter is pressed only in a search prompt
   that is not inside a `<form>` (`browser.SAFE_ENTER`): implicit form submission would bypass these checks.
   `submit_patterns` cannot be empty.
2. **Values are chosen, not generated.** Text comes from a profile fact, a saved answer, the candidate's typing,
   or a draft the candidate saw. Files come only from `profile.documents` and must exist.
3. **Targets are observed nodes.** Model output is an index into what the snapshot returned, never a selector,
   URL or path. Unused target heads are never validated or used.
4. **No mutation is retried.** `ExecutionUncertain` and provider errors hand over to the human. Execution is
   logged before the next observation.
5. **Freshness before every action.** Stale pages raise `StalePage` before input. A value resolved for a field
   survives a stale retry only for the identical field (same document, node, label, context).
6. **Node ids are per document.** Anything keyed by node (skips, vetoes, pending values, read-back) is scoped
   by `page["doc"]`.
7. **Guards are code.** Defaults live in `policy.py`; `data/policy.json` only overrides them, and a test keeps
   `data/policy.example.json` identical to the defaults.
8. **The human sees drafts, sensitive values, close calls and low-confidence actions** before they happen.
9. **Tag fields take one list item per entry, never the same item twice** (`Agent.items`, scoped by document).
   An item only counts as added once it appears in the field's context; otherwise the review says *not added*.
10. **Backends share one contract.** `model.systemone` returns TypeSafe-shaped answers whatever the backend. The
    Laya backend may shortlist a single field's facts, but must always keep `laya_backend.PROTECTED` options
    (ASK_USER, SKIP_FIELD, DRAFT_ANSWER, REVIEW, BLOCKED) and must report dropped options with probability 0, so
    `validate_choice` and the margins see every option the caller offered.

11. **Rules before models.** `rules.py` may only name a profile fact or ASK_USER/SKIP_FIELD/DRAFT_ANSWER, and must
    return None when the wording is ambiguous. Every rule change needs a case in `tests/test_rules.py`.
12. **A model never answers what the profile doesn't hold.** A model-picked text fact must share a content word
    with the question (`rules.related`); a choice question goes to Laya only if a rule maps it or it relates to the
    profile (`planner.relates`). Otherwise it is asked (and, unattended, added to `data/QUESTIONS.md`).
13. **Unattended means safe defaults, not approval.** `UnattendedUI.approve` always says no; the agent itself allows
    only confident actions, and sensitive ones only when a rule supplied the value or the option equals the
    profile fact. Submit is never clicked; declarations are never ticked.

14. **The Laya backend doesn't steer.** `planner.py` decides the step (a fixed procedure); Laya is only asked which
    option answers one question, with a handful of options. Keep it that way: Laya's option budget is small.
    The same holds for Clef (`clef_backend.py`, llama-server's `/v1/systemone`): it shortlists like Laya
    (`model.fit_options`, PROTECTED kept, dropped options at probability 0) and never steers.

15. **Claude only writes text and never acts.** Every Claude Code call goes through `model.claude_code`: no tools,
    no MCP servers, an empty working folder, untrusted input (job pages, résumés, photos) passed in as data.
    Claude drafts open answers, fills gaps in a résumé-learned draft and judges the benchmark. It never
    decides a field, clicks or submits: Laya, Clef or Jev (`DECISION_BACKEND`) does that inside
    `planner.py`'s procedure, whichever backend is set.
16. **`learn` never touches `profile.json`.** It writes `profile.learned.json`; code-extracted facts win over
    Claude's additions.
17. **A captcha is never solved.** A form whose Submit runs one (Lever, any hCaptcha) ends at
    "ready to submit (captcha)" with the tab left open; the batch moves on.
18. **Reading ahead sends nothing.** `scan` and `preflight` only read public board APIs and pages; `bench.py`
    blocks every saving or submitting request in the browser.
19. **QUESTIONS.md holds only what the résumé and the job page can't settle.** Before a question is asked, a rule
    maps it to a profile fact (certifications, notice period, a skill's years under another name: "REST APIs" is
    your "REST API"), or, when it asks to list, describe or give examples, it is drafted (DRAFT_ANSWER) from the
    profile with the job page as context; the draft prompt returns null when the facts don't cover it, and only
    then is it asked. Still asked: personal choices (office days, salary you'd accept), dates, years for a skill
    the profile doesn't list, and Yes/No claims (rule 12). A question a later run fills leaves the open list.
    When QUESTIONS.md shows a question this could have handled, fix the rule and add a test, don't just answer it.

## Checks

```bash
uv run pytest
uv run --extra playwright python scripts/local_check.py   # must print 69/69
uv run --extra playwright python scripts/dry_run.py       # must end: status=review ... unconfirmed=0 not_added=['Kafka'] submitted=False
uv run ruff check . && uv run ruff format --check .
```

If you change the snapshot, add a case to `fixtures/` and `scripts/local_check.py`. If you add a profile field
with a special meaning, give it a contrasting description in `profile.KNOWN` and a wording in
`data/probe_fields.example.json`.
