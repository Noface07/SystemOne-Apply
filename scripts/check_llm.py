"""Check the decision model's API key and model with ONE tiny request, and show the provider's exact answer.

uv run python scripts/check_llm.py
"""

import json

from jev_apply import llm_backend
from jev_apply.cli import load_environment


def main():
    load_environment()
    s = llm_backend.settings()
    print(f"model: {s['model'] or '(not set)'}")
    print(f"base URL: {s['base']}")
    print(f"key: {'set, ends in ...' + s['key'][-4:] if s['key'] else 'NOT SET'}")
    if not s["key"] or not s["model"]:
        return
    if "openrouter.ai" in s["base"]:
        response = llm_backend.CLIENT.get(f"{s['base']}/key", headers={"Authorization": f"Bearer {s['key']}"})
        if response.is_error:
            print(f"\nkey check: HTTP {response.status_code}: {llm_backend.provider_message(response)}")
        else:
            info = response.json().get("data", {})
            print("\nkey check (from OpenRouter):")
            for name in ("label", "is_free_tier", "usage", "usage_daily", "limit", "limit_remaining", "rate_limit"):
                if name in info:
                    print(f"  {name}: {json.dumps(info[name])}")
            if info.get("is_free_tier"):
                print("  -> no credits bought yet: free models are limited to 50 requests a day on this account.")
    body = {"model": s["model"], "max_tokens": 20, "messages": [{"role": "user", "content": "Reply with the word OK."}]}
    if s["reasoning"] == "none":
        body["reasoning"] = {"enabled": False}
    response = llm_backend.CLIENT.post(
        f"{s['base']}/chat/completions", json=body, headers={"Authorization": f"Bearer {s['key']}"}
    )
    print(f"\ntest request: HTTP {response.status_code}")
    if response.is_error:
        print(f"  provider says: {llm_backend.provider_message(response)}")
        text = llm_backend.provider_message(response).lower()
        if "upstream" in text or "temporarily" in text:
            print("  -> the free model's provider is busy right now (not your daily limit). Try again later, or set")
            print("     DECISION_FALLBACK_MODELS in .env to other free models to fall back on.")
        elif "per-day" in text or "per day" in text or "daily" in text:
            print("  -> daily free limit used up. It resets daily; $10 of credits raises it to 1,000 a day.")
        elif "data policy" in text or "privacy" in text:
            print("  -> change your OpenRouter privacy settings to allow this free model's providers.")
    else:
        print("  model replied:", response.json()["choices"][0]["message"]["content"].strip()[:60])
        print("  -> the decision model works.")


if __name__ == "__main__":
    main()
