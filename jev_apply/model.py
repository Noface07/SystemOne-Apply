"""The decision model makes every choice. A small text model only drafts open-ended answers, which you see first.

Decision backends (DECISION_BACKEND in .env):
  laya      default. Laya, open weights, runs locally in this process (see laya_backend.py). No key, no network
            after the first download.
  typesafe  Jev through the TypeSafe API (or a compatible gateway). Needs TYPESAFE_API_KEY.
  clef      Cloudflare's Clef-Flash (9B, open weights) on this machine, served by llama-server (clef_backend.py).
  llm       a general chat model through any OpenAI-compatible API (e.g. OpenRouter); see llm_backend.py.

Whatever the backend, rules.py answers the common fields (CTC, notice period, experience...) first.
"""

import json
import math
import os
import re
import time

import httpx

from . import clef_backend, laya_backend, llm_backend, planner, rules
from .questions import DOCUMENT, DRAFT, NEXT_ACTION, SPECIAL, TARGET, VALUE_SOURCE

CLIENT = httpx.Client(http2=True, timeout=25)
# A provider saying a request option isn't supported by this model (JSON mode, reasoning switch...).
UNSUPPORTED = re.compile(r"structured.?output|response.?format|json.?mode|json_object|reasoning|not support", re.I)
OPERATIONS = {
    "click": "CLICK",
    "fill": "TYPE_TEXT",
    "select": "SELECT",
    "setdate": "SET_DATE",
    "upload": "UPLOAD",
    "frame": "OPEN_FORM",
}
LABELS = {
    "CLICK": "Click a button, link, checkbox, radio, Yes/No button, dropdown, dropdown option or Next/Continue.",
    "TYPE_TEXT": "Type into a text field. The value comes from the candidate's facts, saved answers, or the candidate.",
    "SELECT": "Choose the option of a native dropdown that matches the candidate facts.",
    "SET_DATE": "Set a date or month field from the candidate facts.",
    "UPLOAD": "Attach a candidate document (résumé/CV, cover letter) to a file field.",
    "OPEN_FORM": "Open an embedded application form (iframe) in this tab so it can be filled.",
}
FIELD_KEYS = (
    "label",
    "context",
    "role",
    "input_type",
    "placeholder",
    "help",
    "required",
    "value",
    "current_value",
    "checked",
    "selected",
    "expanded",
    "multiple",
    "pressed",
    "chosen",
    "accept",
    "invalid",
    "error",
    "maxlength",
)


def post_json(url, key, body):
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(
                f"Model provider returned HTTP {response.status_code}: {llm_backend.provider_message(response)} "
                "No action executed."
            )
        return response.json()
    raise RuntimeError("Model unavailable")


BACKENDS = ("laya", "clef", "typesafe", "llm")


def backend():
    name = os.environ.get("DECISION_BACKEND", "laya").strip().lower() or "laya"
    if name not in BACKENDS:
        raise RuntimeError(f"DECISION_BACKEND must be one of {', '.join(BACKENDS)}, not {name!r}.")
    return name


def steering():
    """Who decides the next step on a page. `procedure` (default for Laya and Jev): planner.py walks the form and
    the model only picks answers. `model`: the model also steers (the default for the llm backend)."""
    name = (os.environ.get("DECISION_STEERING") or "").strip().lower()
    if name and name not in {"procedure", "model"}:
        raise RuntimeError(f"DECISION_STEERING must be 'procedure' or 'model', not {name!r}.")
    return name or ("model" if backend() == "llm" else "procedure")


# TypeSafe answers at most 255 options per choice question (256 is "Too many choices").
MAX_TYPESAFE_CHOICES = 255


def fit_for_typesafe(body):
    return fit_options(body, MAX_TYPESAFE_CHOICES)


def fit_options(body, limit):
    """Shortlist choice questions longer than `limit` options, like the Laya backend does: the options sharing the
    field's words are kept, ASK_USER and the other exits always. Returns (body, dropped option ids per question)."""
    questions, dropped = {}, {}
    for qid, question in body["questions"].items():
        criteria = question.get("criteria") or {}
        if question.get("type") == "choice" and len(criteria) > limit:
            state = body.get("state") or {}
            query = laya_backend.query_text(state) if isinstance(state.get("field"), dict) else json.dumps(question)
            protected = sum(1 for k in criteria if k in laya_backend.PROTECTED)
            kept = laya_backend.shortlist(query, {k: str(v) for k, v in criteria.items()}, limit - protected)
            dropped[qid] = [k for k in criteria if k not in kept]
            question = {**question, "criteria": {k: criteria[k] for k in kept}}
        questions[qid] = question
    return {**body, "questions": questions}, dropped


def systemone(body):
    if backend() in {"laya", "llm"}:
        started = time.perf_counter()
        result = (laya_backend if backend() == "laya" else llm_backend).system_one(body)
        return result, round((time.perf_counter() - started) * 1000)
    if backend() == "clef":
        started = time.perf_counter()
        result = clef_backend.system_one(body)
        return result, round((time.perf_counter() - started) * 1000)
    base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("Set TYPESAFE_API_KEY in .env")
    body, dropped = fit_for_typesafe(body)
    started = time.perf_counter()
    result = post_json(base + "/v1/systemone", key, {"model": os.environ.get("TYPESAFE_MODEL", "jev-latest"), **body})
    for qid, ids in dropped.items():  # every option the caller offered is accounted for, as with Laya
        answer = result.get("answers", {}).get(qid)
        if isinstance(answer, dict) and isinstance(answer.get("probabilities"), dict):
            answer["probabilities"].update({k: 0.0 for k in ids})
    return result, round((time.perf_counter() - started) * 1000)


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid decision-model response; no action executed.")
    return answer


def ranked(answer):
    return sorted(answer["probabilities"].items(), key=lambda item: -item[1])


def margin(answer):
    top = ranked(answer)
    return top[0][1] - (top[1][1] if len(top) > 1 else 0.0)


def field(action):
    return {k: action[k] for k in FIELD_KEYS if action.get(k) not in (None, "")}


def action_space(actions):
    """One index per observed element; each operation gets its own valid targets."""
    elements, indices, targets, controls = [], {}, {}, {}
    for action in actions:
        kind = action["kind"]
        if kind not in OPERATIONS:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            indices[node] = str(len(elements) + 1)
            element = {k: v for k, v in field(action).items() if k not in {"current_value"}}
            element.update(index=indices[node], label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        element = elements[int(index) - 1]
        operation = OPERATIONS[kind]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"].split(" → ", 1)[-1]})
        targets.setdefault(operation, {})[target] = action
    return elements, targets, controls


def choose(page, goal, history, candidate, exclude=()):
    if steering() == "procedure":
        # A fixed procedure walks the form (planner.py) and asks the rules, then the model (Laya or Jev), only
        # which answer fits. The same guards and fixes apply whichever model answers.
        return planner.choose(page, candidate, exclude, history)
    elements, targets, controls = action_space(page["actions"])
    operations = {key: LABELS[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(
        REVIEW="The application is filled and the final submit button (or a final review page) is reached. "
        "Stop so the candidate reviews and submits.",
        BLOCKED="The candidate must act: login, sign-up, CAPTCHA, OTP, or a required choice the facts cannot answer.",
    )
    for name in exclude:  # e.g. REVIEW while required fields on this page are still empty
        operations.pop(name, None)
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {index: {"element": f"[{index}] {a['label']}", **field(a)} for index, a in candidates.items()},
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    state = {
        "page": {k: page[k] for k in ("url", "title", "text")},
        "elements": elements,
        "candidate": candidate,
        "recent_actions": [
            {k: h.get(k) for k in ("action", "context", "kind", "source", "page_changed")} for h in history[-12:]
        ],
    }
    result, latency = systemone({"state": state, "questions": questions})
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    action, target, target_answer = None, None, None
    if operation in targets:
        # Unused target heads cannot cause an action. Validate only the head the operation selected.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        action = targets[operation][target]
    elif operation in controls:
        action = controls[operation]
    return {
        "operation": operation,
        "action": action,
        "target": target,
        "operation_probability": operation_answer["probabilities"][operation],
        "target_probability": target_answer["probabilities"][target] if target_answer else None,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "usage": result.get("usage", {}),
        "latency_ms": latency,
    }


def value_source(action, page, profile, history, dates_only=False, exclude=()):
    """Which fact, saved answer, draft, question to the candidate, or skip fills this one field."""
    sources = profile.sources(dates_only, exclude)
    fixed = rules.match(action, profile, dates_only, exclude, page.get("text", ""))
    if fixed is not None:
        ids = [*sources, *(k for k in SPECIAL if not (dates_only and k == "DRAFT_ANSWER"))]
        probabilities = {k: float(k == fixed) for k in ids}
        return {
            "choice": fixed,
            "probabilities": probabilities,
            "confidence": 1.0,
            "margin": 1.0,
            "ranked": [(fixed, 1.0)],
            "latency_ms": 0,
            "usage": {},
            "rule": True,
        }
    if backend() == "laya":
        criteria = {i: laya_backend.describe_source(f) for i, f in sources.items()}
    else:
        criteria = {i: f"{f.about} | value: {f.value[:160]}" for i, f in sources.items()}
    criteria.update({k: v for k, v in SPECIAL.items() if not (dates_only and k == "DRAFT_ANSWER")})
    body = {
        "state": {
            "field": field(action),
            "page": {"url": page["url"], "title": page["title"], "text": page["text"][:3000]},
            "recent_actions": [{k: h.get(k) for k in ("action", "context", "source")} for h in history[-6:]],
        },
        "questions": {"source": {"type": "choice", "criteria": criteria, "instructions": {"rules": VALUE_SOURCE}}},
    }
    result, latency = systemone(body)
    answer = validate_choice(result["answers"].get("source", {}), criteria)
    return {
        **answer,
        "margin": margin(answer),
        "ranked": ranked(answer)[:4],
        "latency_ms": latency,
        "usage": result.get("usage", {}),
    }


def document_choice(action, page, documents):
    fixed = rules.document(action, documents)
    if fixed is not None:
        ids = [*documents, "ASK_USER", "SKIP_FIELD"]
        return {
            "choice": fixed,
            "probabilities": {k: float(k == fixed) for k in ids},
            "confidence": 1.0,
            "margin": 1.0,
            "ranked": [(fixed, 1.0)],
            "latency_ms": 0,
            "rule": True,
        }
    criteria = {name: f"{doc.about} ({doc.value.rsplit('/', 1)[-1]})" for name, doc in documents.items()}
    criteria.update({k: SPECIAL[k] for k in ("ASK_USER", "SKIP_FIELD")})
    body = {
        "state": {"field": field(action), "page": {"title": page["title"], "text": page["text"][:1500]}},
        "questions": {"document": {"type": "choice", "criteria": criteria, "instructions": {"rules": DOCUMENT}}},
    }
    result, latency = systemone(body)
    answer = validate_choice(result["answers"].get("document", {}), criteria)
    return {**answer, "margin": margin(answer), "ranked": ranked(answer)[:3], "latency_ms": latency}


def draft_text(action, page, profile):
    """Draft an open-ended answer. Returns (text or None, meta). The caller always shows it to the candidate."""
    saved = {f.about: f.value for f in profile.facts.values() if f.key.startswith("answer.")}
    context = {
        "question": field(action),
        "candidate": profile.candidate_state(),
        "saved_answers": saved,
        "job_page": {"title": page["title"], "text": page["text"][:5000]},
    }
    if (os.environ.get("TEXT_MODEL_PROVIDER") or "").strip().lower() == "claude-code":
        return draft_with_claude_code(context)
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        return None, {"model": None, "reason": "TEXT_MODEL_API_KEY not set"}
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "inception/mercury-2.5")
    reasoning = {"reasoning": {"enabled": False}} if os.environ.get("TEXT_MODEL_REASONING") == "none" else {}
    started = time.perf_counter()
    body = {
        "model": model,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
        **reasoning,
        "messages": [{"role": "system", "content": DRAFT}, {"role": "user", "content": json.dumps(context)}],
    }
    try:
        result = post_json(base + "/chat/completions", key, body)
    except RuntimeError as error:
        if not UNSUPPORTED.search(str(error)):
            raise
        # Some models reject JSON mode or the reasoning switch: ask again without them; the prompt still asks
        # for JSON and the reply is read leniently below.
        body = {k: v for k, v in body.items() if k not in {"response_format", "reasoning"}}
        result = post_json(base + "/chat/completions", key, body)
    meta = {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
    try:
        content = result["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return None, {**meta, "reason": "the drafting model returned no message"}
    text = read_draft(content)
    if text is None:
        return None, {**meta, "reason": f"couldn't read the drafting model's reply: {content.strip()[:120]!r}"}
    return house_style(text) or None, meta


def claude_code(system, payload, model=None, stdin=None, timeout=None):
    """One answer from the Claude Code CLI (`claude -p`) on the candidate's own Claude login, no API key. It runs
    with no tools and no MCP servers, from an empty folder: whatever the input says (job pages, résumés and
    screenshots are untrusted), Claude can only write text back, never read or run anything.
    Returns (text or None, meta). `stdin` replaces the JSON payload, e.g. stream-json messages with images."""
    import shutil
    import subprocess
    import tempfile

    exe = os.environ.get("CLAUDE_CODE_BIN") or shutil.which("claude")
    if not exe:
        return None, {"model": None, "reason": "the claude CLI (Claude Code) isn't installed or on PATH"}
    model = model or os.environ.get("TEXT_MODEL") or "sonnet"
    command = [exe, "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--model", model]
    command += ["--output-format", "json", "--system-prompt", system]
    if stdin is not None:
        command += ["--input-format", "stream-json", "--verbose", "--output-format", "stream-json"]
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="jev-claude-") as empty:
            done = subprocess.run(
                command,
                input=stdin if stdin is not None else json.dumps(payload),
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=empty,
                timeout=timeout or int(os.environ.get("CLAUDE_CODE_TIMEOUT") or 180),
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, {"model": f"claude-code/{model}", "reason": f"claude CLI failed: {error}"}
    meta = {"model": f"claude-code/{model}", "latency_ms": round((time.perf_counter() - started) * 1000)}
    reply = None
    for line in reversed((done.stdout or "").strip().splitlines() or [""]):  # stream-json: the last "result" line
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and ("result" in candidate or candidate.get("type") == "result"):
            reply = candidate
            break
    if reply is None:
        return None, {**meta, "reason": f"claude CLI gave no JSON: {(done.stdout or done.stderr).strip()[:160]!r}"}
    if reply.get("is_error") or done.returncode:
        return None, {**meta, "reason": f"claude CLI error: {str(reply.get('result'))[:160]!r}"}
    return str(reply.get("result") or ""), meta


def draft_with_claude_code(context):
    """Draft an open answer through the Claude Code CLI (see claude_code: no tools, empty folder)."""
    content, meta = claude_code(DRAFT, context)
    if content is None:
        return None, meta
    text = read_draft(content)
    if text is None:
        return None, {**meta, "reason": f"couldn't read Claude's reply: {content[:120]!r}"}
    return house_style(text) or None, meta


def house_style(text):
    """The candidate's writing rules, whatever the model wrote: no em or en dashes, no comma before "and"."""
    text = re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1-\2", text)  # 50–70% -> 50-70%
    text = re.sub(r"\s*[—–]\s*", ", ", text)  # a dash between clauses reads as a comma
    text = re.sub(r",\s*,", ",", text)
    return re.sub(r",(\s+and\b)", r"\1", text, flags=re.I)


def read_draft(content):
    """The answer text from a drafting reply: {"text": ...} JSON if the model sent it, otherwise plain prose
    (models without JSON mode often just answer). Returns '' for 'facts insufficient', None if unreadable."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()  # models that show their reasoning
    try:
        output = llm_backend.parse(content)
    except ValueError:
        output = None
    if isinstance(output, dict) and "text" in output:
        text = output["text"]
        return "" if text is None else (text.strip() if isinstance(text, str) and len(text) <= 4000 else None)
    if "{" in content or not content or len(content) > 4000:
        return None  # half-formed JSON or nothing: don't type it
    text = re.sub(r"^```\w*|```$", "", content, flags=re.M).strip().strip('"').strip()
    text = re.sub(r"^(here(\'s| is) (a |the |my )?(draft|answer)[^:\n]*:|answer:|draft:)\s*", "", text, flags=re.I)
    return text.strip()
