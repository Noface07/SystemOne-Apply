# jev-apply

**Fill job applications from your own data, with a decision model that runs on your machine.**

jev-apply opens a job application in a browser and works through it page by page: text fields, dropdowns, Yes/No
questions, dates, skill tags, résumé upload and "Next". Every value comes from a JSON file describing you, never
from a model's imagination. By default it stops at the final **Submit** button so you can check everything. Turn on
`auto_submit` and it submits complete applications for you, one job at a time or in batches.

- **Rules first.** Common fields (current vs expected CTC, notice period, experience, contact details, education,
  skills, years per technology) are answered by plain code from your profile.
- **Laya for every other choice.** [Laya](https://huggingface.co/convaiinnovations/laya) is an open-weights
  decision model that runs locally. It only ever picks *which* of your facts or options answers a question.
  One line in `.env` switches that role to TypeSafe's [Jev](https://typesafe.ai) instead
  ([Choosing the decision model](#choosing-the-decision-model)).
- **Claude for text only.** Open questions ("Why do you want to join us?") are drafted through the
  [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI on your own Claude login, with no tools and
  no file access. Or leave drafting off and answer them yourself.
- **Safety is code, not prompts.** Sensitive fields, declarations, logins and the final submit are all gated by
  rules you can read in `jev_apply/policy.py` and `jev_apply/agent.py`.

> **Status:** personal project, actively used on LinkedIn Easy Apply and simple company forms. Workday-style
> portals are partly supported (see [Limits](#limits)). Job boards generally prohibit automation in their terms,
> so use it on your own responsibility.

---

## Contents

1. [How it works](#how-it-works)
   - [Choosing the decision model](#choosing-the-decision-model)
2. [Quick start](#quick-start)
3. [Your data](#your-data)
4. [Running applications](#running-applications)
5. [Submitting for you](#submitting-for-you)
6. [Answer once: QUESTIONS.md](#answer-once-questionsmd)
7. [Drafting open answers with Claude Code](#drafting-open-answers-with-claude-code)
8. [Supported fields](#supported-fields)
9. [Configuration](#configuration)
10. [Testing and development](#testing-and-development)
11. [Limits](#limits)
12. [Privacy](#privacy)
13. [Credits and license](#credits-and-license)

---

## How it works

```
 observe page ─► rules: do I know this field? ─► Laya: which option / fact? ─► safety checks ─► act ─► read back
      ▲                                                                                               │
      └───────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **Observe.** `snapshot.js` lists every visible control with its label, the question it belongs to, help text,
   required flag and current value. Styled widgets are read through the element they draw (hidden radios through
   their labels, Workday's 0×0 date inputs through their date box).
2. **Plan.** A fixed procedure (`planner.py`) decides the next step: open an embedded form, dismiss a cookie
   banner, fill the next unanswered field top to bottom, scroll, press Next, stop at Submit.
3. **Choose the value.** `rules.py` maps the wordings forms use to your facts. When no rule knows, Laya picks among
   that one question's options. A model never answers something your profile doesn't hold: those questions go to
   [QUESTIONS.md](#answer-once-questionsmd).
4. **Check.** Low-confidence picks, sensitive fields, declarations and the final submit go through the guards.
5. **Act and read back.** The action runs on the exact element observed, then the page is read again to confirm
   the value stuck.

### Choosing the decision model

Laya is the default. To let TypeSafe's Jev pick the answers instead, set two lines in `.env`:

```bash
DECISION_BACKEND=typesafe
TYPESAFE_API_KEY=...            # from https://console.typesafe.ai/keys
```

Only *who picks the answer* changes. The same procedure walks the form. The same rules, guards, logins and
submit checks apply either way. Long fact lists are shortlisted to TypeSafe's limit of 255 options per question,
as they are for Laya. `TYPESAFE_MODEL` pins a version (default `jev-latest`). `DECISION_STEERING=model` lets the
model also decide each step, which is the older mode and not recommended. Switch back with
`DECISION_BACKEND=laya`. Laya runs locally and costs nothing per decision; Jev is a paid API.

---

## Quick start

You need Python 3.12+ ([uv](https://docs.astral.sh/uv/) installs it for you), about 3 GB of disk for PyTorch
and the Laya checkpoint. You also need your résumé as a PDF. A GPU makes Laya faster; a CPU works.

```bash
git clone https://github.com/<you>/jev-apply.git
cd jev-apply
uv sync --extra playwright --extra laya
uv run playwright install chromium

cp data/profile.example.json data/profile.json      # then fill in your details
cp data/answers.example.json data/answers.json
mkdir -p data/documents && cp ~/Documents/resume.pdf data/documents/resume.pdf
cp .env.example .env                                  # defaults work

uv run jev-apply check                                # shows every fact the agent will use
uv run jev-apply run "https://careers.example.com/jobs/123" --step
```

On Windows use `copy` instead of `cp` and backslashes in paths. Run `set PYTHONUTF8=1` once if you see encoding
errors. The first run downloads the Laya checkpoint from Hugging Face (once).

`--step` asks before every action. Use it until you trust the agent on a new site.

---

## Your data

Everything personal lives in `data/` and is git-ignored. Only the `*.example.json` files are tracked.

| File | What it holds |
|---|---|
| `profile.json` | Facts about you: contact details, work history, education, compensation, preferences, skills |
| `answers.json` | Answers you wrote, typed word for word when a question matches ("Tell us about yourself") |
| `QUESTIONS.md` | Questions a run couldn't answer; fill in the answer once and every later form reuses it |
| `policy.json` | Overrides of the safety defaults (only what you change) |
| `documents/` | Résumé and cover letter; the only files that can ever be uploaded |
| `applied.json` | Jobs submitted with `auto_submit`, so none is applied to twice |

### Starting from your résumé

```bash
uv run jev-apply --data data/dotnet learn --resume data/dotnet/documents/cv.pdf
```

Plain code reads what a pattern can read exactly: name, email, phone, city, links, each job's company, title
and dates, education with its grade and the skills listed under a skills heading. Claude Code (your own
login, no tools) then fills only what code can't: a headline, one summary per job, total and relevant
experience and years per skill, counted from the job dates. It is told to invent nothing and lists anything it
had to judge under `_review`. The result goes to `profile.learned.json` with a list of every value that
differs from your `profile.json`, which is never changed. Copy over what you want. Add `--no-claude` for
the code-only draft. Check experience totals in particular: a résumé rarely separates an internship from the
full-time job it led to.

### Several résumés, several profiles

Keep one folder per track, each with its own profile, answers, policy and résumé. Pick one with `--data`:

```
data/automation/   profile.json  answers.json  policy.json  documents/…Industrial_Automation.pdf
data/dotnet/       profile.json  answers.json  policy.json  documents/…CSharp_DotNET.pdf
data/genai/        profile.json  answers.json  policy.json  documents/…GenAI-ML.pdf
data/QUESTIONS.md  shared by all of them
```

### Writing the profile

| Write it as | Because the agent derives |
|---|---|
| **Money annual, in rupees:** `1500000` | Total CTC, the LPA version (`15`) and the monthly version |
| **Dates in ISO:** `2026-11-02` or `2022-06` | `02/11/2026`, `06/2022`, `June 2022`, `2022` and the right format for pickers |
| **Yes/No as `true` / `false`** | "Yes" / "No" |
| **Lists in priority order** | The whole list for a text box and one item at a time for tag fields |
| **Experience in years:** `2.2` | `26` months, or `2` years + `2` months for a years box next to a months box |
| **Anything custom:** `{"value": "M", "about": "T-shirt size"}` | Your `about` text tells the model what it means |

**Years per technology** go in `skill_years`, so "How many years of experience do you have with C#?" gets a real
number instead of a guess:

```json
"skill_years": { "C#": 1.5, "Python": 2, "OPC UA": 1.5, "LangGraph": 0.5, "Machine learning": 1 }
```

- A skill matches by name, by an abbreviation (".NET" for "ASP.NET Core") or when a question starts with it
  ("C (Programming Language)"). A skill in the middle of another name never matches ("PLC Programming" is not
  "C programming").
- Whole-number boxes (LinkedIn's) get **completed years**: `1.5` is entered as `1`, never rounded up.
- A skill you don't list is **asked** (added to `QUESTIONS.md`), never guessed. There is deliberately no
  default.

Never put Aadhaar, PAN, passport numbers, bank details or passwords in the profile. Those fields are on the
never-fill list anyway.

---

## Running applications

| Command | What it does |
|---|---|
| `uv run jev-apply check` | Lists every fact, document and setting the agent will use |
| `uv run jev-apply run URL` | One application, asking you when it needs you |
| `uv run jev-apply run URL --step` | Ask before every action |
| `uv run jev-apply batch jobs.txt` | Several jobs, one tab each, never asking (unattended) |
| `uv run jev-apply scan boards.txt --keywords "c#,.net" --out jobs.txt` | Find matching jobs on Greenhouse, Lever and Ashby boards (see below) |
| `uv run jev-apply preflight jobs.txt` | Read those forms ahead and send questions nothing can answer to `QUESTIONS.md` |
| `uv run jev-apply learn --resume cv.pdf` | Draft a profile from your résumé into `profile.learned.json` |
| `uv run jev-apply answer` | Answer the open questions of `QUESTIONS.md` in the terminal |

Options go after the command, except `--data`, which goes first:

```bash
uv run jev-apply --data data/dotnet batch jobs/dotnet.txt --submit --browser chrome
```

| Option | Meaning |
|---|---|
| `--data PATH` | Profile folder to use (default `data`) |
| `--browser playwright` | A separate browser with its own profile (default). Log in to portals there once |
| `--browser chrome` | Your everyday Chrome through [Browser Harness](https://pypi.org/project/browser-harness/), with its logins |
| `--submit` | Submit complete applications (see below) |
| `--unattended` | For `run`: never ask; leave what needs you and list it at the end |
| `--note "..."` | Extra instruction, e.g. "apply for the Pune location" |
| `--tracks a,b,c` | For `batch`: several profile folders; each job gets the one whose résumé fits it best |
| `--plan-only` | For `batch`: print the plan (résumé per job, skill questions, deferrals) and stop |

A **batch** file has one job or application URL per line (`#` starts a comment). At the end it prints, per job,
what it filled, what it left and why it stopped. Every run writes a full report to `runs/<date-time>/report.json`.

**Finding jobs with `scan`.** Greenhouse, Lever and Ashby list every open job through a public API. Their
forms take applications without an account. List the company boards you care about in a file
(`data/boards.example.txt` shows the format: `greenhouse:<token>`, `lever:<site>`, `ashby:<org>` or the board's
address) and run:

```bash
uv run jev-apply --data data/dotnet scan data/boards.txt --keywords "c#,.net,asp.net" --out jobs/boards.txt
uv run jev-apply --data data/dotnet batch jobs/boards.txt --submit
```

`scan` keeps a job when a keyword is in its title (or at least two are in the description, as whole words), when
it is in India or remote without another country named (`--location` changes that), when it asks for no more
years than you have (your completed years, or `--max-years`) and when it isn't senior, lead or staff
(`--include-senior` keeps those). Jobs you already applied to are skipped. No model is involved.

**Planning a batch.** Before any form opens, `batch` looks up each job (LinkedIn's public job page, or the
Greenhouse, Lever and Ashby APIs) and prints a plan:

```
uv run jev-apply batch jobs.txt --tracks data/automation,data/dotnet,data/genai --submit
   1. Zuci Systems · Dotnet Developer            résumé dotnet 2 skill question(s)
   2. Zuci Systems · Dotnet Developer            résumé dotnet DEFERRED (Zuci Systems: already 1 application(s) in 14 days)
   4. CoffeeBeans · Generative AI Engineer       résumé genai
  16. Prodigal · Machine Learning Engineer       résumé genai 4 answer(s) from the form
```

- **Résumé per job.** With `--tracks`, each job gets the profile folder whose résumé lists the job's skills,
  each skill weighted by how few of your tracks list it. The job title's skills count five times as much.
- **Skill questions first.** "How many years with X?" is what most LinkedIn runs stop on. The skills a job
  names in its title or at least twice in its description, which your profile has no years for, go to
  `QUESTIONS.md` before the run (at most 5 per job). One answer serves every wording: "React", "React.js" and
  "ReactJS" are one skill.
- **Spread per company.** At most `per_company` applications to one company in any `company_gap_days`
  window (1 in 14 days by default), counting `applied.json` and the batch itself. Extra jobs are deferred and
  listed, not dropped. `"per_company": 0` turns it off.
- **Answers from the form.** For Greenhouse, Lever and Ashby, every option your profile answers exactly (a
  notice period of "15 Days", a salary range of "12 - 15 LPA") is handed to the run in the form's own words, so
  searchable dropdowns get the right option. These answers are used for that job only and never saved.

**Reading forms ahead with `preflight`.** Greenhouse, Lever and Ashby also publish each application form:
every question, its options and whether it is required. `preflight` reads them before any browser opens and
sorts each question by who will answer it: a rule or your profile, a saved answer, your résumé, Laya, a draft,
or nobody yet. The last kind goes to `QUESTIONS.md`, so you answer it once *before* the batch instead of after
it stops. `batch --preflight` does this first on its own. Nothing is filled or sent.

```
https://job-boards.greenhouse.io/acme/jobs/123
  14 questions: 2 ASK, 1 Laya, 9 profile, 1 résumé, 1 skip
    profile       How many years of work experience do you have?        2 years
    Laya          Undergraduate Institution Name                        picks among 49 options
  ? ASK           Are you comfortable working from office 5 days a week? only you can say
```

**Logins.** The agent never types into a sign-in page. In an unattended run it brings the tab to the front, prints
**LOGIN NEEDED** and waits (15 minutes by default) while you sign in or create the account, then carries on.
Portals like Workday keep one account per company. The browser remembers the session afterwards.

---

## Submitting for you

Off by default. Turn it on with `"auto_submit": true` in `policy.json`, or with `--submit` for one run. An
unattended run then clicks the final Submit **only when the application is complete**:

- no required field is empty and nothing is marked invalid;
- nothing was left for you and no question is open;
- every value the agent set was read back from the page;
- no model-written draft is unread (unless `"submit_drafts": true`).

Anything else stops before Submit and says why. A watched run asks "Submit this application now?" first. After
clicking Submit it watches for the site's own confirmation ("Your application was sent", "Thank you for
applying") for up to `confirm_wait_s` seconds, then records the job in `applied.json` and never submitted again, whatever address it comes under
(LinkedIn, Indeed and Naukri job ids, Greenhouse, Lever and Ashby postings).

**Captchas (Lever).** Lever runs an hCaptcha when you click Submit. So does any form carrying one. The
agent never solves a captcha: it fills the whole form, brings the tab to the front, prints **SUBMIT YOURSELF**,
marks the job "ready to submit (captcha)" and moves on to the next job. The tab stays open for you. `scan`
marks Lever jobs so you know in advance.

**Consent boxes.** With `"auto_consent": true`, a required checkbox that only agrees to a privacy notice or the
site's terms is ticked. A box that also declares something about you (criminal record, background check, "the
information is true and accurate", conflicts of interest and similar, see `declaration_patterns`) is never ticked
this way: it goes to `QUESTIONS.md` as "Tick this box? …" and your Yes or No is reused on every later form.

---

## Answer once: QUESTIONS.md

Every question a run couldn't answer is added to `data/QUESTIONS.md` once, with its options and the jobs that
asked it:

```markdown
### 1. How many years of work experience do you have with Angular?
- Asked on: https://www.linkedin.com/jobs/view/...
- Answer: 0
```

Type your answer after `Answer:` (for a question with options, the option or its number) and save. Every later
run fills that question from it on every form: text boxes, dropdowns, radios and Yes/No buttons. After a few
batches most forms need nothing from you.

Questions about what you have done ("Have you deployed ROS 2 on robots?") are never answered by a model. Only a
rule, your profile or your saved answer may answer them.

---

## Drafting open answers with Claude Code

Set this in `.env`:

```bash
TEXT_MODEL_PROVIDER=claude-code
TEXT_MODEL=sonnet
```

Open questions are then drafted with `claude -p` on your own Claude login (no API key). The job page is untrusted
text, so the CLI runs with **no tools and no MCP servers, from an empty temporary folder**: it can only write the
answer. The prompt allows only facts from your profile and saved answers. Anything the job asks for that your
profile doesn't list may appear only as something you want to learn, never as experience.

`house_style()` in `model.py` post-processes every draft (for example to remove em dashes). Edit it to match how
you write.

Drafts are shown to you first. With `auto_submit` and `"submit_drafts": true` they are submitted without review.

The older route still works: leave `TEXT_MODEL_PROVIDER` empty and set `TEXT_MODEL_API_KEY`,
`TEXT_MODEL_BASE_URL` and `TEXT_MODEL` for any OpenAI-compatible endpoint.

---

## Supported fields

| Field type | Support |
|---|---|
| Text, email, phone, number, textarea | Typed like a person; whole-number and length limits respected |
| Autocomplete and search boxes | Types, waits for suggestions, picks the one matching your profile |
| Skill tags ("type, pick, repeat") | One item at a time, never twice; unmatched items reported |
| Native and custom dropdowns, multi-selects | Set or opened and chosen; long lists narrowed to your answer |
| Radios, checkboxes, Yes/No buttons, div choices | Clicked through their label, each with its own question |
| "Select all that apply" | Ticks what your profile lists; never unticks, never guesses |
| Date and month pickers, masked date text ("DD/MM/YYYY") | Your date in the shape asked; masks get key-by-key input |
| Split date boxes (Workday Month + Year) | Filled through the box drawn for them |
| Repeating Work Experience / Education blocks | One block per profile entry; entries a site already lists are left alone |
| Résumé upload, including behind an "Upload" button | From your `documents` only; a file already in the site's list is not uploaded again |
| LinkedIn / Indeed saved résumés | The matching saved résumé is chosen instead of uploading a copy |
| Multi-page forms, scrolling panels, modals | Waits for each step; tall modals are laid out so Submit isn't clipped |
| Embedded forms, same-origin frames, shadow DOM, new tabs | Opened, filled in place, or followed |
| Login, sign-up, CAPTCHA, OTP | Always yours (the run waits for you) |
| Closed shadow roots, canvas-drawn apps | Handed to you |

---

## Configuration

### `.env`

| Variable | Default | Meaning |
|---|---|---|
| `DECISION_BACKEND` | `laya` | Who picks answers: `laya` (local), `typesafe` (Jev via API) or `llm` (chat model via API) |
| `DECISION_STEERING` | `procedure` | `procedure`: the fixed procedure walks the form. `model`: the model also steers |
| `TYPESAFE_API_KEY` / `TYPESAFE_MODEL` | *(empty)* / `jev-latest` | Only for `DECISION_BACKEND=typesafe` |
| `LAYA_MODEL` / `LAYA_CHECKPOINT` / `LAYA_DEVICE` | Hugging Face repo, English, auto | Which Laya and where it runs |
| `LAYA_MAX_LEN` / `LAYA_HEAD_MAX_LEN` / `LAYA_MAX_OPTIONS` | `2048` / `1024` / `20` | Token budgets and shortlist size |
| `TEXT_MODEL_PROVIDER` | *(empty)* | `claude-code` to draft with the Claude Code CLI |
| `TEXT_MODEL` | `sonnet` with Claude Code | Drafting model |
| `TEXT_MODEL_API_KEY` / `TEXT_MODEL_BASE_URL` | *(empty)* | Only for an OpenAI-compatible drafting endpoint |
| `CLAUDE_CODE_BIN` / `CLAUDE_CODE_TIMEOUT` | `claude` on PATH / `180` | Where the CLI is; seconds per draft |
| `JEV_VIEWPORT_HEIGHT` | `1000` | Minimum layout height of the agent's Chrome tabs |
| `JEV_BACKGROUND_TABS` | `0` | `1` keeps the agent's Chrome tabs behind yours |
| `JEV_CHROMIUM` | *(empty)* | A specific Chromium for `--browser playwright` |

### `policy.json`

Copy `data/policy.example.json` and keep only what you change. A test keeps the example identical to the code
defaults in `jev_apply/policy.py`.

| Key | Default | Meaning |
|---|---|---|
| `min_operation_probability` / `min_target_probability` | `0.6` | Below this a model's pick needs your approval (0.85 suggested for Laya) |
| `min_value_margin` | `0.3` | Top two options closer than this: you pick (0.5 suggested for Laya) |
| `drafts` | `"confirm"` | `"confirm"` shows drafts; `"ask"` never drafts |
| `auto_submit` | `false` | Submit complete applications |
| `submit_drafts` | `false` | With `auto_submit`, also submit applications holding unread drafts |
| `auto_consent` | `false` | Tick required privacy / terms consent boxes |
| `consent_patterns` / `declaration_patterns` | privacy notice, terms… / criminal, background, true… | What counts as plain consent; what makes a box a declaration |
| `login_wait_s` | `900` | How long an unattended run waits at a sign-in page |
| `confirm_wait_s` | `15` | How long to watch for the site's confirmation after Submit |
| `per_company` / `company_gap_days` | `1` / `14` | At most this many applications per company in this many days (0: off) |
| `confirm_patterns` | gender, caste, visa, salary, "i confirm"… | Fields that always need your yes |
| `never_fill_patterns` | password, otp, aadhaar, pan card, bank… | Fields the agent never touches |
| `submit_patterns` | submit, apply, send application, finish… | Buttons treated as a final submit. Cannot be empty |
| `naukri_apply` | `"assist"` | Naukri's own Apply: `"assist"`, `"auto"` or `"company_site"` |
| `max_actions` | `200` | Action budget per job |

Patterns match the start of words, ignoring case: `disab` matches "Disability"; `pan card` does not match
"Company".

---

## Testing and development

```bash
uv run pytest                                               # 277 unit tests, offline
uv run --extra playwright python scripts/local_check.py     # 69 real-browser checks on the built-in test forms
uv run --extra playwright python scripts/dry_run.py         # a full application on the test site, nothing sent
uv run ruff check . && uv run ruff format --check .
```

**Benchmark.** `scripts/bench.py` fills real application forms with your profile while every request that
could save or submit is blocked, photographs each form top to bottom and has Claude Code (sandboxed: the
photos go in with the prompt, no tools) grade every question against your profile as right, wrong, missed or
couldn't. The judge never sees the agent's own log, so it grades what a recruiter would see. Results and photos
go to `bench/results/`, which is git-ignored. The list of wrong and missed answers is what the rules get fixed
from.

```bash
uv run --extra playwright --extra laya python scripts/bench.py jobs/boards.txt --data data/dotnet
```

`scripts/probe_fields.py` measures how often Laya is *confidently wrong* on tricky wordings (current vs expected
CTC, LPA vs rupees). `scripts/live_check.py` runs the agent on real public application pages with every request
that could submit or save blocked.

Read [AGENTS.md](AGENTS.md) before changing the loop. It lists the invariants that must not be weakened (values
chosen never generated, no retried actions, freshness before every action, guards in code) and the checks every
change must pass. Any rule change needs a test in `tests/test_rules.py`; any snapshot change needs a fixture case
in `fixtures/` and a check in `scripts/local_check.py`.

```
jev-apply/
├─ jev_apply/
│  ├─ agent.py          the loop, guards, submit, read-back, review report
│  ├─ planner.py        the fixed procedure that decides each step (Laya backend)
│  ├─ rules.py          wordings mapped to profile facts, value shaping (dates, whole years)
│  ├─ snapshot.js       in-page snapshot of controls and their questions
│  ├─ browser.py        executes actions (clicks, typing, dates, uploads)
│  ├─ model.py          decision questions and drafting (Claude Code or an API)
│  ├─ laya_backend.py   runs Laya locally within its token budget
│  ├─ profile.py        profile.json → facts (CTC variants, dates, list items)
│  ├─ policy.py         guard patterns and thresholds
│  ├─ inbox.py          QUESTIONS.md
│  ├─ applied.py        applied.json (never apply twice)
│  ├─ scan.py           finds jobs on Greenhouse, Lever and Ashby boards
│  ├─ preflight.py      reads those forms ahead; unknown questions to QUESTIONS.md
│  ├─ learn.py          drafts a profile from a résumé (code, then Claude Code)
│  ├─ jobinfo.py        a job's company, title and description (LinkedIn guest page, board APIs)
│  ├─ jobplan.py        batch plan: résumé per job, skill questions, form answers, spread per company
│  ├─ skills.py         technology names and aliases found in job descriptions
│  ├─ transport.py      separate browser (Playwright) or your Chrome
│  ├─ ui.py             terminal and unattended interfaces
│  └─ cli.py            jev-apply run / batch / scan / preflight / learn / check / answer
├─ data/                *.example.json (your real files are git-ignored)
├─ fixtures/            built-in test application pages
├─ scripts/             browser checks, dry run, probes, bench.py (screenshot benchmark)
└─ tests/               unit tests
```

---

## Limits

- **Workday** works up to the experience step: sign-in waits, split date boxes and uploads are handled, but its
  skills picker, school search and some date contexts still need work. Start from the job's `/apply/applyManually`
  address.
- A final button labelled only "Next" can't be recognised as a submit, so some sites send the application from
  the last "Next" (LinkedIn does on short forms). The agent only reaches it once everything is filled.
- Enter is pressed only in search boxes outside a `<form>`, since Enter inside a form can submit it.
- Skill names must match the site's options ("Go" vs "Golang"); unmatched ones are reported, not guessed.
- One tab and one application at a time.

---

## Privacy

With Laya, every decision runs on your machine: page text, labels and your profile stay local. Only the
one-time checkpoint download talks to Hugging Face. Drafting sends the question, your profile facts, saved answers
and the job page text to the drafting model (Claude through your own login, or the API you configured). Run
reports and `data/` stay local and are git-ignored. Keep sensitive identifiers out of the profile.

---

## Credits and license

MIT, see [LICENSE](LICENSE). Based on [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) by Browser Use
(MIT). Job discovery, reading forms ahead, résumé learning, the screenshot benchmark and the post-submit
confirmation wait follow ideas from
[TheAdaply/jev-apply](https://github.com/TheAdaply/jev-apply) (MIT). Laya is a model by Convai Innovations
(Apache-2.0). Jev is a model by TypeSafe. Not affiliated with any of
them.
