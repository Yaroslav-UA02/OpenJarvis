"""Джарвіс дзвонить тобі на мобільний.

  python call.py "Агент закінчив верстку банера для Avtostar. Продовжувати з каталогом?"
  python call.py --check        — перевірити, що агент і номер знайдені

Що відбувається:
  1. Знаходить в ElevenLabs агента «Jarvis Call» і номер «Jarvis US» (ID кешуються).
  2. Дзвонить на JARVIS_PHONE через Twilio, передає звіт як {{report}}.
  3. Чекає кінця розмови, забирає твоє рішення (decision / instructions).
  4. Пише підсумок у Telegram. Не взяв трубку — надсилає звіт у Telegram замість дзвінка.

Можна викликати з коду:  from call import call_owner;  result = call_owner("звіт")
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

AGENT_NAME = "Jarvis Call"
PHONE_LABEL = "Jarvis US"
MAX_CALLS_PER_DAY = 5          # захист балансу Twilio (~$0.31–0.41/хв на український мобільний)
WAIT_FOR_RESULT = 6 * 60       # скільки чекати завершення розмови, с
DEFAULT_OPENING = "Ярік, привіт, це Джарвіс. Є хвилинка? Маю новини по задачі."

API = "https://api.elevenlabs.io/v1/convai"
CACHE = Path.home() / ".openjarvis" / "call_state.json"


def _headers() -> dict:
    return {"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}


def _state() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _save(state: dict) -> None:
    CACHE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _find_ids(state: dict) -> tuple[str, str]:
    """ID агента і номера — шукаємо за назвами, кешуємо."""
    if not state.get("agent_id"):
        r = httpx.get(f"{API}/agents", params={"search": AGENT_NAME, "page_size": 30}, headers=_headers(), timeout=20)
        r.raise_for_status()
        agents = [a for a in r.json().get("agents", []) if a.get("name", "").strip().lower() == AGENT_NAME.lower()]
        if not agents:
            raise RuntimeError(f"агента «{AGENT_NAME}» не знайдено в ElevenLabs")
        state["agent_id"] = agents[0]["agent_id"]
    if not state.get("phone_id"):
        r = httpx.get(f"{API}/phone-numbers", headers=_headers(), timeout=20)
        r.raise_for_status()
        data = r.json()
        numbers = data if isinstance(data, list) else data.get("phone_numbers", [])
        match = [n for n in numbers if n.get("label") == PHONE_LABEL] or numbers
        if not match:
            raise RuntimeError("у ElevenLabs немає підключеного номера")
        state["phone_id"] = match[0]["phone_number_id"]
        state["from_number"] = match[0].get("phone_number", "")
    _save(state)
    return state["agent_id"], state["phone_id"]


def _telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       data={"chat_id": chat, "text": text[:4000]}, timeout=20)
        except Exception:
            pass


def _value(item) -> str:
    if isinstance(item, dict):
        return str(item.get("value") or "").strip()
    return str(item or "").strip()


def call_owner(report: str, opening: str = DEFAULT_OPENING, wait: bool = True) -> dict:
    """Дзвонить Яро. Повертає {status, decision, instructions, summary, transcript, ...}."""
    state = _state()
    today = datetime.now().strftime("%Y-%m-%d")
    calls = state.setdefault("calls", {})
    if calls.get(today, 0) >= MAX_CALLS_PER_DAY:
        _telegram(f"📵 Ліміт дзвінків на сьогодні вичерпано, тому пишу сюди.\n\n{report}")
        return {"status": "limit", "decision": "unclear", "instructions": "", "summary": report}

    to_number = os.environ.get("JARVIS_PHONE", "").replace(" ", "")
    if not to_number.startswith("+"):
        raise RuntimeError("JARVIS_PHONE не налаштовано (формат +380…)")
    agent_id, phone_id = _find_ids(state)

    r = httpx.post(f"{API}/twilio/outbound-call", headers=_headers(), timeout=30, json={
        "agent_id": agent_id,
        "agent_phone_number_id": phone_id,
        "to_number": to_number,
        "conversation_initiation_client_data": {"dynamic_variables": {"report": report, "opening": opening}},
    })
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("success"):
        msg = data.get("message") or data.get("detail") or r.text[:300]
        _telegram(f"⚠️ Не вдалося подзвонити ({msg}). Звіт:\n\n{report}")
        return {"status": "error", "error": str(msg), "decision": "unclear", "instructions": "", "summary": report}

    calls[today] = calls.get(today, 0) + 1
    _save(state)
    conv_id = data.get("conversation_id")
    result = {"status": "calling", "conversation_id": conv_id, "decision": "unclear", "instructions": "", "summary": ""}
    if not (wait and conv_id):
        return result

    # чекаємо кінця розмови і аналізу
    conv = {}
    finished_polls = 0  # після кінця розмови чекаємо аналіз ще ~30 с, не більше
    for _ in range(WAIT_FOR_RESULT // 6):
        time.sleep(6)
        try:
            c = httpx.get(f"{API}/conversations/{conv_id}", headers=_headers(), timeout=20)
            if c.status_code == 200:
                conv = c.json()
        except Exception:
            continue
        if conv.get("status") in ("done", "failed"):
            dc = (conv.get("analysis") or {}).get("data_collection_results")
            finished_polls += 1
            if dc or conv.get("status") == "failed" or finished_polls >= 5:
                break

    transcript = [(t.get("role"), (t.get("message") or "").strip())
                  for t in conv.get("transcript", []) if (t.get("message") or "").strip()]
    analysis = conv.get("analysis") or {}
    dc = analysis.get("data_collection_results") or {}
    answered = any(role == "user" for role, _ in transcript)
    result.update({
        "status": conv.get("status", "unknown") if answered else "no_answer",
        "decision": _value(dc.get("decision")) or ("unclear" if answered else "no_answer"),
        "instructions": _value(dc.get("instructions")),
        "summary": analysis.get("transcript_summary", ""),
        "transcript": transcript,
        "duration": (conv.get("metadata") or {}).get("call_duration_secs"),
    })

    if answered:
        icon = {"continue": "▶️", "stop": "⏹", "change": "🔁"}.get(result["decision"].lower(), "❓")
        _telegram(f"📞 Дзвінок ({result['duration'] or '?'} с)\n{icon} Рішення: {result['decision']}\n"
                  f"📝 Вказівки: {result['instructions'] or '—'}\n\n{result['summary']}")
    else:
        _telegram(f"📵 Не додзвонився, тому пишу сюди:\n\n{report}\n\nВідповідай тут або скажи «Джарвіс» біля ноута.")
    return result


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args[0] == "--check":
        st = _state()
        st.pop("agent_id", None)
        st.pop("phone_id", None)
        agent_id, phone_id = _find_ids(st)
        print(f"✅ Агент: {AGENT_NAME} ({agent_id})\n✅ Номер: {st.get('from_number')} ({phone_id})")
        print(f"✅ Дзвонитиму на: {os.environ.get('JARVIS_PHONE', '❌ JARVIS_PHONE не задано')[:7]}…")
        return
    report = " ".join(args)
    print("📞 Дзвоню…")
    res = call_owner(report)
    res.pop("transcript", None) if res.get("status") == "no_answer" else None
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
