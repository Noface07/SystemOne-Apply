"""Laya (Convai Innovations, Apache-2.0): an open-weights System One decision model, run locally.

It answers the same typed `choice` questions as Jev and returns the same shape (choice, probabilities,
confidence), so everything above `model.systemone` is unchanged. Two things differ and are handled here:

1. Small option budget. All options of one question share `head_max_len` tokens and each option is cut at
   48 tokens. Our profile descriptions put the distinguishing part at the end ("... — in LAKHS per annum"),
   so options are rewritten with the value and qualifiers first, long ids get short aliases, and large
   option sets are shortlisted (protected choices such as ASK_USER are always kept). Dropped options are
   reported with probability 0, so the caller's validation and margins work unchanged.
2. Small state budget. The state is serialised in priority order (field and elements first, page text
   last) because Laya truncates the end.

Nothing leaves your machine: the weights are downloaded once from Hugging Face, then run in-process.
"""

import math
import os
import re
import threading

from .questions import SPECIAL

PROTECTED = {*SPECIAL, "REVIEW", "BLOCKED"}  # never shortlisted away: they are how the model says "not sure"
STATE_ORDER = ("field", "elements", "recent_actions", "candidate", "page")
FIELD_ORDER = (
    "element",
    "label",
    "context",
    "value",
    "current_value",
    "checked",
    "selected",
    "pressed",
    "expanded",
    "required",
    "input_type",
    "placeholder",
    "help",
    "multiple",
    "accept",
    "role",
)
OPTION_TOKENS = 48  # Laya's hard per-option cap
WORD = re.compile(r"[a-z0-9]+")

_lock = threading.Lock()
_agent = None
_embed = None
_embed_cache = {}


def settings():
    env = os.environ.get
    return {
        "repo": env("LAYA_MODEL", "convaiinnovations/laya"),
        "checkpoint": env("LAYA_CHECKPOINT", "").strip() or None,  # "", "multilingual" or "typed-decisions"
        "device": env("LAYA_DEVICE", "").strip() or None,  # cpu | cuda | mps; empty = auto
        "max_len": int(env("LAYA_MAX_LEN", "2048")),
        "head_max_len": int(env("LAYA_HEAD_MAX_LEN", "1024")),
        "max_options": int(env("LAYA_MAX_OPTIONS", "20")),
    }


def installed():
    try:
        import laya  # noqa: F401
    except ImportError:
        return False
    return True


def agent():
    """Load the checkpoint once per process (first run downloads it from Hugging Face)."""
    global _agent, _embed
    with _lock:
        if _agent is None:
            try:
                import laya
            except ImportError:
                raise RuntimeError("Laya isn't installed: run `uv sync --extra laya`.") from None
            s = settings()
            loaded = laya.load(s["repo"], device=s["device"], subfolder=s["checkpoint"])
            # Raise the token budgets above the shipped defaults (512/192 or 1024/256): a form step has
            # many elements and a profile has many facts. The encoders accept long inputs, but Laya was
            # trained on short ones, so measure with scripts/probe_fields.py before trusting it.
            loaded.cfg["max_len"] = s["max_len"]
            loaded.cfg["head_max_len"] = s["head_max_len"]
            try:
                _embed = laya.embed_fn_from_agent(loaded)
            except Exception:
                _embed = None  # shortlisting falls back to word overlap only
            _agent = loaded
        return _agent


def describe_source(fact):
    """Option text for one profile fact: value and qualifiers first, so the 48-token cut keeps what differs."""
    base, *qualifiers = fact.about.split(" — ")
    return " · ".join([f"value: {fact.value[:60]}", *qualifiers, base])


def flat(value):
    """Render a structured criterion (an observed element) as short text instead of JSON."""
    if not isinstance(value, dict):
        return value if isinstance(value, str) else str(value)
    keys = [k for k in FIELD_ORDER if k in value] + [k for k in value if k not in FIELD_ORDER]
    parts = []
    for key in keys:
        item = value[key]
        if item in (None, "", False) or (key == "label" and "element" in value):
            continue
        parts.append(str(item) if key == "element" else key if item is True else f"{key}: {item}")
    return "; ".join(parts)


def order_state(state):
    """Most decision-relevant parts first: Laya cuts the serialised state from the end."""
    if not isinstance(state, dict):
        return state
    ordered = {k: state[k] for k in STATE_ORDER if k in state}
    ordered.update({k: v for k, v in state.items() if k not in ordered})
    return ordered


def words(text):
    return set(WORD.findall(str(text).lower()))


def query_text(state):
    field = state["field"]
    return " ".join(str(field.get(k, "")) for k in ("label", "context", "placeholder", "help", "input_type"))


def shortlist(query, options, limit, embed=None):
    """Keep every protected option plus the `limit` best others (word overlap weighted by rarity, fused
    with embedding similarity when available). Returns kept keys in their original order."""
    free = [k for k in options if k not in PROTECTED]
    if len(free) <= limit:
        return list(options)
    q = words(query)
    bags = {k: words(options[k]) for k in free}
    df = {}
    for bag in bags.values():
        for w in bag:
            df[w] = df.get(w, 0) + 1
    lexical = {k: sum(math.log(1 + len(free) / df[w]) for w in bags[k] & q) for k in free}
    ranks = [sorted(free, key=lambda k: -lexical[k])]  # sorted() is stable: ties keep profile order
    if embed is not None:
        try:
            similarity = embedding_similarity(query, {k: options[k] for k in free}, embed)
            ranks.append(sorted(free, key=lambda k: -similarity[k]))
        except Exception:
            pass
    fused = {k: 0.0 for k in free}
    for ranking in ranks:
        for position, key in enumerate(ranking):
            fused[key] += 1 / (60 + position)
    kept = set(sorted(free, key=lambda k: -fused[k])[:limit])
    return [k for k in options if k in PROTECTED or k in kept]


def embedding_similarity(query, options, embed):
    missing = [text for text in options.values() if text not in _embed_cache]
    vectors = embed([query] + missing)
    for text, vector in zip(missing, vectors[1:]):
        _embed_cache[text] = vector
    q = vectors[0]
    qn = math.sqrt(float((q * q).sum())) or 1.0
    result = {}
    for key, text in options.items():
        v = _embed_cache[text]
        vn = math.sqrt(float((v * v).sum())) or 1.0
        result[key] = float((q * v).sum()) / (qn * vn)
    return result


def fits(options, tok, head_max_len):
    if tok is None:
        return len(options) <= 12
    used = sum(1 + min(OPTION_TOKENS, len(tok(" " + text, add_special_tokens=False)["input_ids"])) for text in options)
    return used <= head_max_len - 48  # leave room for the question type and the start of the instructions


def prepare(qid, question, state, tok, embed, s):
    """Laya-sized version of one choice question. Returns (question, alias -> original key, dropped keys)."""
    criteria = {k: flat(v) for k, v in question["criteria"].items()}
    keep = list(criteria)
    rendered = [f"{k}: {v}" for k, v in criteria.items()]
    # Only a question about one field has a query to shortlist against (which fact answers it?). Target
    # questions keep every element: Laya then gives each fewer tokens, and index + label come first.
    if not fits(rendered, tok, s["head_max_len"]) and isinstance(state.get("field"), dict):
        keep = shortlist(query_text(state), criteria, s["max_options"], embed)
    aliases, sized = {}, {}
    for n, key in enumerate(keep, 1):
        alias = key if (key in PROTECTED or len(key) <= 10) else f"#{n}"
        aliases[alias] = key
        sized[alias] = criteria[key]
    dropped = [k for k in criteria if k not in keep]
    return {**question, "criteria": sized}, aliases, dropped


def system_one(body, runner=None, tok=None, embed=None):
    """Answer TypeSafe-shaped `choice` questions with a local Laya checkpoint.

    `runner`, `tok` and `embed` are for tests; by default they come from the loaded checkpoint.
    """
    s = settings()
    if runner is None:
        loaded = agent()
        runner, tok, embed = loaded.predict, loaded.tok, _embed
    state = order_state(body["state"])
    questions, maps = {}, {}
    for qid, question in body["questions"].items():
        if question.get("type") != "choice":
            raise ValueError(f"Laya backend only asks choice questions, not {question.get('type')!r}.")
        questions[qid], aliases, dropped = prepare(qid, question, state, tok, embed, s)
        maps[qid] = (aliases, dropped)
    raw = runner(state, questions)
    answers = {}
    for qid, answer in raw.get("answers", {}).items():
        aliases, dropped = maps[qid]
        probabilities = {aliases[k]: v for k, v in answer["probabilities"].items()}
        probabilities.update({k: 0.0 for k in dropped})
        answers[qid] = {
            "choice": aliases[answer["choice"]],
            "probabilities": probabilities,
            "confidence": answer.get("confidence", 0.0),
            "shortlisted": bool(dropped),
        }
    return {"answers": answers, "usage": raw.get("usage", {}), "model": f"laya:{s['checkpoint'] or 'english'}"}
