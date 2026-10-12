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
- **Claude for text, and for getting unstuck.** Open questions ("Why do you want to join us?") are drafted through
  the [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI on your own Claude login, with no tools and
  no file access. The same CLI answers questions from your profile that no rule knows, finds your answer in a
  list that words it differently ("BS" for your B.Tech) and gets a stuck page moving. Code checks every one of
  its answers against your profile ([When a run gets stuck](#when-a-run-gets-stuck-claude-as-teacher-and-rescuer)).
- **Safety is code, not prompts.** Sensitive fields, declarations, logins and the final submit are all gated by
  rules you can read in `jev_apply/policy.py` and `jev_apply/agent.py`.

> **Status:** personal project, actively used on LinkedIn Easy Apply, simple company forms and Workday (sign-up,
> e-mail verification and every step up to Review, tested on several companies). See [Limits](#limits). Job boards
> generally prohibit automation in their terms, so use it on your own responsibility.

---

## Contents

1. [How it works](#how-it-works)
   - [Choosing the decision model](#choosing-the-decision-model)
2. [Quick start](#quick-start)
3. [Your data](#your-data)
4. [Running applications](#running-applications)
5. [Submitting for you](#submitting-for-you)
6. [Answer once: QUESTIONS.md](#answer-once-questionsmd)
7. [When a run gets stuck: Claude as teacher and rescuer](#when-a-run-gets-stuck-claude-as-teacher-and-rescuer)
8. [Drafting open answers with Claude Code](#drafting-open-answers-with-claude-code)
9. [Supported fields](#supported-fields)
10. [Configuration](#configuration)
11. [Testing and development](#testing-and-development)
12. [How the agent is doing: metrics](#how-the-agent-is-doing-metrics)
13. [Limits](#limits)
14. [Privacy](#privacy)
15. [Credits and license](#credits-and-license)

---

## How it works

```
 observe page ─► rules: do I know this field? ─► Laya: which option / fact? ─► safety checks ─► act ─► read back
      ▲                                                                                               │
      └───────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **Observe.** `snapshot.js` lists every visible control with its label, the question it belongs to, help text,
   required flag, current value and, when the site gives one, its stable field name (Workday's `formField-…`).
   Styled widgets are read through the element they draw (hidden radios through their labels, Workday's 0×0 date
   inputs through their date box). A group wrapped around a whole section is never taken as each field's question.
2. **Plan.** A fixed procedure (`planner.py`) decides the next step: open an embedded form, dismiss a cookie
   banner, fill the next unanswered field top to bottom, scroll, press Next, stop at Submit.
3. **Choose the value.** `rules.py` maps the wordings forms use (and Workday's field names) to your facts. When no
   rule knows, the decision model picks among that one question's options. When a list words your answer
   differently, Claude finds it there. A model never answers something your profile doesn't hold: those questions
   go to [QUESTIONS.md](#answer-once-questionsmd).
4. **Check.** Low-confidence picks, sensitive fields, declarations and the final submit go through the guards.
5. **Act and read back.** The action runs on the exact element observed, at a person's pace, then the page is read
   again to confirm the value stuck.
6. **Unstick.** About to stop with nobody watching, Claude gets up to five checked steps to get the page moving,
   or says exactly which field and fact are missing.

### Choosing the decision model

Laya is the default. To let TypeSafe's Jev pick the answers instead, set two lines in `.env`:

```bash
DECISION_BACKEND=typesafe
TYPESAFE_API_KEY=...            # from https://console.typesafe.ai/keys
```

To use Cloudflare's [Clef-Flash](https://huggingface.co/cloudflare/clef-flash), a 9B open-weights decision
model, on your own GPU: get llama.cpp b11430 or newer and a GGUF such as
[bartowski/Cloudflare_clef-flash-GGUF](https://huggingface.co/bartowski/Cloudflare_clef-flash-GGUF) (Q4_K_M,
6 GB, fits an 8 GB GPU), then set:

```bash
DECISION_BACKEND=clef
CLEF_MODEL_PATH=D:/models/Cloudflare_clef-flash-Q4_K_M.gguf
CLEF_SERVER_BIN=D:/llama.cpp/llama-server.exe   # jev-apply starts it when nothing answers on CLEF_BASE_URL
```

Before starting llama-server, jev-apply checks that Windows can spare the memory the model needs, counting the room
a system-managed page file can still grow into on its drive (keeping 5 GB of that drive free). Clef answers through
the same `/v1/systemone` API as Jev, so nothing else changes. Each question keeps its 12
most relevant options (`CLEF_MAX_OPTIONS`); the exits (ask you, skip, draft) always stay. With the rules switched
off, on a real profile, Clef-Flash answered 18 of 26 field wordings right against Laya's 3 (Laya asked about most).

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
| `lessons.json` | Answers Claude gave from your profile, reused without asking again (checked again on every load) |
| `equivalents.json` | Your answer in other lists' words ("BS" for B.Tech), so the same list needs no Claude call |
| `accounts.json` | Portals where an account was created for you |
| `replay/` | Real pages kept from runs, for replay tests (`scripts/replay.py`) |

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
never-fill list anyway. *Holding* a document is a fact like any other: `"has_aadhaar_card": "Yes"`,
`"has_pan_card"`, `"has_driving_licence"` under `personal` answer "Do you have a valid PAN card?". Without them that
question is asked, never guessed, and so are citizenship, nationality and birth details unless your profile states
them.

---

## The web app

```bash
uv run --extra ui jev-apply ui          # opens http://127.0.0.1:8765
```

Everything the commands do, in one place:

- **Overview:** applications sent, per day and per résumé, open questions, the decision model's state.
- **Find jobs:** name the roles you'd take (your résumés suggest some, but don't limit the search), then the
  filters: locations, date posted, on-site / hybrid / remote, years asked, Easy Apply only (off by default), senior
  titles. Sources: LinkedIn (read without logging in) and, when switched on, hiring.cafe (its search pages are read
  in a background tab of your Chrome; see `jev_apply/web/hiringcafe.py`). The same posting found on both is listed
  once. Each job shows the years it asks, the skills it names and the résumé that fits it best. Jobs posted in the
  last 24 hours come first ("Newest first"; "Best match" sorts by score). Results arrive live and only changed
  rows redraw. Hidden: closed jobs, senior titles, too many years, jobs you applied to. Tick the ones you want and
  start applying.
- **Batches:** every batch, newest first, including ones from earlier sessions (rebuilt from runs/batches/).
  Open one to follow it live: each job as a card (résumé used, the step it is on, why it stopped), the log, a
  Stop button and "rerun stopped jobs".
- **Questions:** QUESTIONS.md as cards; options are buttons, answers save straight to the file. When a running
  job reaches a required question nothing on file answers, it puts it here at once and waits (Safety settings:
  "Wait for your answers", 5 minutes by default) with a desktop notification; answer it and the run carries on
  with the same form. No answer in time: the job stops as before. Jobs that stopped on questions you've since
  answered are offered here and in the Inbox: "Re-run them".
- **History:** every application and every run report, field by field, with the model's confidence.
- **Profile:** edit each track's profile.json as a form (jobs and education as cards, skills as tags, years per
  skill as a table). Shared facts (notice period, CTC, years per skill) can be saved to every track at once; a
  track's headline, skills, jobs and résumé stay its own. Each save is checked by loading it, and the previous
  version is kept in data/<track>/.backups/. Add any field (with a description the decision model reads), a
  section, a job or a document; upload a track's résumé (PDF, Word or text); or start a new track for another
  kind of role from your shared facts.
- **Inbox:** everything a run left for you: jobs to finish (with why), drafted answers to read, open questions.
- **Insights:** per résumé track, how many runs end submitted and the commonest reasons the rest stop.
- **Autopilot (System):** every day at a set time, run the saved search you marked "Use for autopilot" (its roles,
  locations, sources and filters), take the good fits newest first, then by score, and apply (`jev-apply
  autopilot`, scheduled with Windows Task Scheduler; `--dry` searches and picks without applying).
- **Safety settings (System):** per track: auto-submit, how sure the model must be, applications per company,
  consent boxes, waits (data/<track>/policy.json, overrides only).
- **Preferences (Find jobs):** blocked companies and words (also stop batches and autopilot), preferred cities,
  remote only, saved searches. Each job shows a match score and the salary when the posting states one.
- **Paste job links (Find jobs):** LinkedIn, Greenhouse, Lever or Ashby links, checked like search results.
- **Keyword coverage and match reasons:** per job, which skills it names your matched résumé covers, has related
  skills for, or misses, and why it scores as it does. Dismiss a job and searches and autopilot never show it again.
- **Answer bank (Questions):** every saved answer, from QUESTIONS.md, each track's answers.json and answers saved
  while applying, with how often runs used it; edit or delete in place.
- **Receipts:** after a submit, what the site showed (its text and a screenshot) is kept with the run report.
- **Signing in:** a sign-in page (a password box, "Continue with Google"-style buttons, Google's or Microsoft's
  own sign-in) makes the run wait for you. With "Sign in with Google for me" on (System → Safety settings), it
  clicks Continue with Google, your profile e-mail's account and Google's basic-sharing confirmation itself.
  With "Sign in / sign up on portals for me" on, it uses your profile e-mail and the portal password on a
  portal's own sign-in or Create Account form (see "Portal accounts" below); otherwise it never types there.
- **Claude usage (Insights):** each Claude call's stage, time, tokens and cost (runs/claude-usage.jsonl).
- **Draft profile from résumé (Profile → Documents):** reads the résumé (code, then Claude for gaps) and shows
  what differs from your profile; only the values you tick are applied.
- **Clef controls (System):** start or stop the model, GPU memory and the memory Windows can spare.
- **System:** switch the decision model (Clef, Laya, Jev, a chat model) and set its keys and paths (saved to
  .env; keys are never shown again, only their last 4 characters), Clef's server and memory, your tracks.

A batch started here is the same `jev-apply batch --submit --browser chrome` process, so every guard applies. The
app listens on 127.0.0.1 only and every API call carries a per-session token, so no other website can drive it.
The API (`jev_apply/web/server.py`) is the contract a hosted version would keep.

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
| `uv run jev-apply metrics` | How runs went: first-run Review rate per site and ATS, why runs stop |

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

**Logins.** Without "Sign in / sign up on portals" on, the agent never types into a sign-in page. In an unattended
run it brings the tab to the front, prints **LOGIN NEEDED** and waits (15 minutes by default) while you sign in or
create the account, then carries on. With it on, it signs in or signs up itself
([Portal accounts](#portal-accounts-and-verification-e-mails)). Portals like Workday keep one account per company.
The browser remembers the session afterwards. Cookie banners are accepted (Decline only when there is no Accept).

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

## Portal accounts and verification e-mails

With **Sign in / sign up on portals** on (Safety settings), a portal's own sign-in (Workday, iCIMS, Oracle...) is
handled for you: your profile's e-mail and the portal password (System settings, `JEV_PORTAL_PASSWORD` in `.env`)
sign in, or create the account first; `data/accounts.json` remembers which portals have one. A refused sign-in is
never retried. With **Read verification e-mails** on and Gmail connected, the codes and verification links portals
send are read from your Gmail (read-only) and typed or opened.

### Gmail for verification codes (once, about 10 minutes)

1. Open https://console.cloud.google.com/, sign in with the Gmail account you apply with, and create a project
   (top bar: project picker, **New project**, name it `jev-apply`).
2. **APIs & Services → Library**: search **Gmail API**, open it, **Enable**.
3. **Google Auth Platform** (or **APIs & Services → OAuth consent screen**): **Get started**. App name `jev-apply`,
   your e-mail as support and developer contact, Audience **External**. Then **Data access → Add or remove
   scopes**: add `https://www.googleapis.com/auth/gmail.readonly`. Under **Audience → Test users**, add your Gmail
   address. (While the app is in *Testing*, Google lets the sign-in last 7 days; **Publish app** on the Audience
   page keeps it, with an "unverified app" notice only you see.)
4. **Clients → Create client**: type **Desktop app**, name `jev-apply desktop`, **Create**, then **Download JSON**.
5. Save that file as `data/google_client.json` in this project.
6. Run `uv run jev-apply gmail-connect`: your browser opens Google's page; choose your account and allow read-only
   access (on the "Google hasn't verified this app" page: **Advanced → Go to jev-apply**). The token is saved in
   `data/google_token.json`; like everything in `data/` it never leaves your machine or reaches git.

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

## When a run gets stuck: Claude as teacher and rescuer

With `TEXT_MODEL_PROVIDER=claude-code` (and `"teacher": true` in `policy.json`, the default), Claude Code steps in
at three points. It always runs on your own login with no tools, and code checks every answer before it is used.

- **Teacher.** Questions no rule answers and nothing on file matches are sent to Claude with your profile facts.
  It must name the fact each answer comes from. Code keeps an answer only when that fact exists and the answer is
  its value (or one of the options shown). Kept answers go to `data/lessons.json` and are reused on every later
  form without a call. Each lesson is checked again against your profile whenever it loads, so one built on a
  value you have since changed (your CTC) stops being used. Questions about who you are (citizenship, nationality,
  birth, marital status, gender) and the fields of one job or school are never taught: the rules answer those.
- **Closest option.** When a dropdown or search list doesn't hold your answer as written, Claude picks the option
  that is your answer in the list's own words: "BS" or "Bachelor of Science" for B.Tech, "Computer Science" for
  Computer Science and Engineering, "Manipal University" (or, only when nothing fits, "Other") for Manipal
  University Jaipur. It must say whether its pick means the same, is the closest equivalent or is the list's way
  out; code keeps only an option actually shown, never a different school, country, level or field. Picks are
  saved to `data/equivalents.json`, so the next list offering "BS" needs no call (an "Other" is kept for its own
  site only). Only lists the rules can tie to one of your facts are looked up.
- **Rescuer.** About to stop with nobody watching (Save pressed three times without effect, nothing left to do),
  Claude gets the page's error messages (Workday's banner at the top too), its controls with their questions and
  field names, the last steps and your facts. It works in up to five steps, seeing after each what changed, and
  says `done` once the field blocking the page is fixed. Code refuses any final submit, password box, sign-out,
  a step that just failed, a ticked box (a click would untick it), text that isn't one of your facts, and a
  sensitive question your profile doesn't answer. When it finds no way, the stop message says why
  ("Claude: Marital Status: nothing on file says"). At most three rescues per run.

Every call and its reply go to `runs/teacher.jsonl`, and tokens and cost to `runs/claude-usage.jsonl`.
`TEXT_MODEL=haiku` makes all of this much cheaper; keep Sonnet for drafts if you prefer.

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
| Workday "Select One" dropdowns and search prompts | Opened, searched (Enter), your option chosen even when worded differently ("Citizen (India)", "BS") |
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
| `JEV_VIEWPORT_HEIGHT` | off | Minimum layout height of the agent's Chrome tabs; by default the window is maximised instead, so pages keep their normal size |
| `JEV_BACKGROUND_TABS` | `0` | `1` keeps the agent's Chrome tabs behind yours |
| `JEV_CHROMIUM` | *(empty)* | A specific Chromium for `--browser playwright` |
| `JEV_PORTAL_PASSWORD` | *(empty)* | The password used to sign in or sign up on portals (never printed or logged) |
| `JEV_CAPTURE` | `1` | Keep each new page layout a run meets for replay tests (`runs/pages/`); `0` turns it off |

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
| `answer_wait_s` | `300` | How long an unattended run waits for your answers before stopping |
| `pace_s` / `pace_after_next_s` | `0.6` / `1.5` | Pause after each action, and after Save / Next, so the site keeps up |
| `teacher` | `true` | Let Claude answer from your profile, find closest options and rescue stuck pages |
| `portal_accounts` / `email_codes` / `google_sign_in` | `false` | Sign in or up on portals; read verification e-mails; Continue with Google |
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
uv run pytest                                               # 442 unit tests, offline
uv run --extra playwright python scripts/local_check.py     # 79 real-browser checks on the built-in test forms
uv run --extra playwright python scripts/dry_run.py         # a full application on the test site, nothing sent
uv run ruff check . && uv run ruff format --check .
```

**Replay tests from real pages.** Every run keeps each new page layout it meets (up to 25, `runs/pages/`): the page
frozen into one static HTML file (`freeze.js`: every element keeps its box, visibility and form state, no site or
script needed), what the agent read on it, its last steps and the planner's decision. Password boxes are never
written down. Keep a good run's pages and check every later change against them:

```bash
uv run python scripts/replay.py promote runs/<report folder>   # its pages become goldens in data/replay/<site>/
uv run --extra playwright python scripts/replay.py check       # read each page again, decide again; --bless accepts
uv run python scripts/replay.py fields                         # Workday field names the map doesn't know yet
```

`check` loads each frozen page in headless Chromium, reads it with today's `snapshot.js` and runs the planner on it:
a field read differently or a different decision is reported. `uv run pytest` replays every kept decision
offline too. Shadow DOM and frames are not kept.

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
│  ├─ agent.py          the loop, guards, submit, read-back, rescues, review report
│  ├─ planner.py        the fixed procedure that decides each step
│  ├─ rules.py          wordings mapped to profile facts, value shaping (dates, whole years)
│  ├─ snapshot.js       in-page snapshot of controls and their questions
│  ├─ freeze.js         a page frozen into static HTML for replay tests
│  ├─ browser.py        executes actions (clicks, typing, dates, uploads)
│  ├─ model.py          decision questions and drafting (Claude Code or an API)
│  ├─ laya_backend.py   runs Laya locally within its token budget
│  ├─ clef_backend.py   starts llama-server with Clef-Flash (memory check included)
│  ├─ teacher.py        Claude as teacher, closest-option finder and rescuer, every answer checked
│  ├─ accounts.py       portal sign-in and sign-up steps
│  ├─ gmail.py          verification codes and links from your Gmail (read-only)
│  ├─ metrics.py        jev-apply metrics: first-run Review rate, stop reasons
│  ├─ replay.py         keeps real pages and replays them
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
│  └─ cli.py            jev-apply run / batch / scan / preflight / learn / check / answer / metrics
├─ data/                *.example.json (your real files are git-ignored)
├─ fixtures/            built-in test application pages
├─ scripts/             browser checks, dry run, probes, bench.py, replay.py
└─ tests/               unit tests
```

---

## How the agent is doing: metrics

Every run report records its site, the applicant tracking system behind it (Workday, Greenhouse, Lever...), one
stop code saying why it ended (`site_errors`, `no_progress`, `login`, `job_closed`...), its rescues and its
closest-option picks. `uv run jev-apply metrics` reads all reports, old ones included:

```
Runs: 204 on 26 sites; reached Review/Submit: 44 (22%)
First run on a new site reached Review: 7 of 26 (27%)
ATS              sites  runs  reached  first run
workday              9    64      14%        11%
Why runs stopped: no_form_found 35, site_errors 30, no_progress 24, resume_mismatch 22, ...
```

The number that matters most is the first run on a company never seen before. `--json` prints the numbers.

---

## Limits

- **Workday** is handled end to end on the companies tested: sign-up and sign-in, e-mail verification, "Select
  One" dropdowns, search prompts, skills, education, split dates, consent boxes and the voluntary disclosures. A new
  company can still show a layout or wording no rule knows; the run then stops and says which field.
- A final button labelled only "Next" can't be recognised as a submit, so some sites send the application from
  the last "Next" (LinkedIn does on short forms). The agent only reaches it once everything is filled.
- Enter is pressed only in search boxes outside a `<form>`, since Enter inside a form can submit it (Workday,
  iCIMS, Oracle and SuccessFactors search boxes excepted: there Enter only searches).
- Aadhaar and PAN *numbers* are never typed; a box asking for one stops the run for you.
- Skill names must match the site's options ("Go" vs "Golang"); unmatched ones are reported, not guessed.
- One tab and one application at a time.

---

## Privacy

With Laya, every decision runs on your machine: page text, labels and your profile stay local. Only the
one-time checkpoint download talks to Hugging Face. Drafting sends the question, your profile facts, saved answers
and the job page text to the drafting model (Claude through your own login, or the API you configured). The
teacher, closest-option finder and rescuer send your profile facts, the question or the page's controls and text
(never a password, never a never-fill field's value) to Claude the same way. Run reports, kept pages
(`runs/pages/`) and `data/` stay local and are git-ignored. Keep sensitive identifiers out of the profile.

---

## Credits and license

MIT, see [LICENSE](LICENSE). Laya is a model by Convai Innovations
(Apache-2.0). Jev is a model by TypeSafe. Not affiliated with any of
them.
