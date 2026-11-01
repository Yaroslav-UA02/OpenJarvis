"""Ранковий бриф Джарвіса.

Збирає: задачі з Notion, погоду в Мукачеві, курси, непрочитану пошту, події календаря,
новини IT і дизайну → Claude пише бриф голосом Джарвіса → ElevenLabs озвучує →
звучить на Mac і приходить голосовим у Telegram.

  python brief.py            — повний бриф (озвучка + Telegram)
  python brief.py --check    — перевірити кожне джерело, нічого не озвучуючи
  python brief.py --text     — лише текст брифу в терміналі (без озвучки і Telegram)

Ключі (у ~/.zshrc):
  NOTION_TOKEN, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GCAL_ICS_URL,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY, ELEVENLABS_API_KEY
"""

from __future__ import annotations

import email
import email.header
import imaplib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# ── Налаштування ────────────────────────────────────────────────────────────
TZ = ZoneInfo("Europe/Kyiv")
CITY, LAT, LON = "Мукачево", 48.4393, 22.7178
NOTION_DATABASES = {
    "Avtostar M": "b3ac1b94c4054df0a94d0b46ddbd195d",
    # "Інший проєкт": "id_бази",  ← додавай свої бази задач сюди
}
NEWS_FEEDS = {
    "DOU": "https://dou.ua/feed/",
    "AIN.UA": "https://ain.ua/feed/",
    "Smashing Magazine": "https://www.smashingmagazine.com/feed/",
    "Codrops": "https://tympanus.net/codrops/feed/",
    "Hacker News": "https://hnrss.org/frontpage?points=250",
}
NEWS_PER_FEED = 5
MAIL_LIMIT = 10
BRIEF_MODEL = os.environ.get("BRIEF_MODEL", "claude-sonnet-5-5")
OUT_DIR = Path.home() / ".openjarvis" / "briefs"

WMO = {
    0: "ясно", 1: "переважно ясно", 2: "мінлива хмарність", 3: "хмарно", 45: "туман", 48: "туман з інеєм",
    51: "легка мряка", 53: "мряка", 55: "сильна мряка", 61: "невеликий дощ", 63: "дощ", 65: "сильний дощ",
    66: "крижаний дощ", 67: "сильний крижаний дощ", 71: "невеликий сніг", 73: "сніг", 75: "сильний сніг",
    77: "снігова крупа", 80: "короткочасний дощ", 81: "зливи", 82: "сильні зливи", 85: "снігопад",
    86: "сильний снігопад", 95: "гроза", 96: "гроза з градом", 99: "сильна гроза з градом",
}
HTTP = dict(timeout=15, follow_redirects=True, headers={"User-Agent": "JarvisBrief/1.0"})


# ── Джерела ─────────────────────────────────────────────────────────────────
def get_weather() -> dict:
    r = httpx.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": LAT, "longitude": LON, "timezone": "Europe/Kyiv", "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
    }, **HTTP)
    r.raise_for_status()
    d = r.json()
    cur, day = d["current"], d["daily"]
    return {
        "місто": CITY,
        "зараз": f"{round(cur['temperature_2m'])}°, відчувається як {round(cur['apparent_temperature'])}°, "
                 f"{WMO.get(cur['weather_code'], 'без опису')}, вітер {round(cur['wind_speed_10m'])} км/год",
        "день": f"від {round(day['temperature_2m_min'][0])}° до {round(day['temperature_2m_max'][0])}°, "
                f"{WMO.get(day['weather_code'][0], '')}, ймовірність опадів {day['precipitation_probability_max'][0]}%",
    }


def get_rates() -> dict:
    out = {}
    r = httpx.get("https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json", **HTTP)
    r.raise_for_status()
    nbu = {x["cc"]: x["rate"] for x in r.json()}
    out["НБУ"] = {c: round(nbu[c], 2) for c in ("USD", "EUR") if c in nbu}
    try:  # ринковий курс monobank — може не відповісти через ліміт запитів
        m = httpx.get("https://api.monobank.ua/bank/currency", **HTTP)
        if m.status_code == 200:
            codes = {840: "USD", 978: "EUR"}
            out["monobank (купівля/продаж)"] = {
                codes[x["currencyCodeA"]]: f"{x['rateBuy']:.2f} / {x['rateSell']:.2f}"
                for x in m.json() if x.get("currencyCodeA") in codes and x.get("currencyCodeB") == 980
                and "rateBuy" in x
            }
    except Exception:
        pass
    return out


def _notion_title(prop: dict) -> str:
    return "".join(t.get("plain_text", "") for t in prop.get("title", []))


def get_tasks() -> dict:
    token = os.environ["NOTION_TOKEN"]
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}
    today = datetime.now(TZ).date()
    result = {"прострочено": [], "сьогодні": [], "найближчі 3 дні": [], "в роботі без дедлайну": [],
              "чекаю від брата": [], "всього відкритих": 0}
    for project, db in NOTION_DATABASES.items():
        body = {"filter": {"property": "Статус", "status": {"does_not_equal": "Готово"}},
                "sorts": [{"property": "Дедлайн", "direction": "ascending"}], "page_size": 100}
        r = httpx.post(f"https://api.notion.com/v1/databases/{db}/query", headers=headers, json=body, **{
            k: v for k, v in HTTP.items() if k != "headers"})
        if r.status_code == 404:
            raise RuntimeError(f"база «{project}» не відкрита для інтеграції (Share → Connections)")
        r.raise_for_status()
        for page in r.json()["results"]:
            p = page["properties"]
            title = _notion_title(p.get("Задача", {}))
            status = (p.get("Статус", {}).get("status") or {}).get("name", "")
            who = (p.get("Хто", {}).get("select") or {}).get("name", "")
            stage = (p.get("Етап", {}).get("select") or {}).get("name", "")
            dl = (p.get("Дедлайн", {}).get("date") or {}).get("start")
            item = f"{title} [{project}{', ' + stage if stage else ''}; {status}{'; ' + who if who else ''}" \
                   f"{'; дедлайн ' + dl[:10] if dl else ''}]"
            result["всього відкритих"] += 1
            due = date.fromisoformat(dl[:10]) if dl else None
            if status == "Чекаю від брата":
                result["чекаю від брата"].append(item)
            elif due and due < today:
                result["прострочено"].append(item)
            elif due == today:
                result["сьогодні"].append(item)
            elif due and due <= today + timedelta(days=3):
                result["найближчі 3 дні"].append(item)
            elif status == "В роботі":
                result["в роботі без дедлайну"].append(item)
    return result


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    return "".join(p.decode(c or "utf-8", "replace") if isinstance(p, bytes) else p for p, c in parts).strip()


def get_mail() -> dict:
    addr, pwd = os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(addr, pwd)
        imap.select("INBOX", readonly=True)  # readonly — листи лишаються непрочитаними
        typ, data = imap.search(None, "X-GM-RAW", '"is:unread category:primary newer_than:1d"')
        ids = data[0].split() if typ == "OK" else []
        letters = []
        for i in ids[-MAIL_LIMIT:][::-1]:
            typ, msg = imap.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if typ != "OK" or not msg or not isinstance(msg[0], tuple):
                continue
            h = email.message_from_bytes(msg[0][1])
            name, mail_addr = parseaddr(_decode(h.get("From")))
            letters.append(f"{name or mail_addr}: {_decode(h.get('Subject')) or '(без теми)'}")
        return {"непрочитаних за добу (Основні)": len(ids), "останні": letters}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def get_calendar() -> dict:
    import icalendar
    import recurring_ical_events

    # GCAL_ICS_URL може містити кілька посилань через пробіл — основний, сім'я, пари тощо
    urls = os.environ["GCAL_ICS_URL"].split()
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    found = {"сьогодні": [], "завтра": []}
    errors = []
    for url in urls:
        try:
            r = httpx.get(url, **HTTP)
            r.raise_for_status()
            cal = icalendar.Calendar.from_ical(r.content)
        except Exception as exc:
            errors.append(f"{url[:45]}…: {type(exc).__name__}")
            continue
        name = str(cal.get("X-WR-CALNAME") or "")
        for key, day_start in (("сьогодні", start), ("завтра", start + timedelta(days=1))):
            for ev in recurring_ical_events.of(cal).between(day_start, day_start + timedelta(days=1)):
                dt = ev.get("DTSTART").dt
                timed = isinstance(dt, datetime)
                when = dt.astimezone(TZ).strftime("%H:%M") if timed else "весь день"
                where = str(ev.get("LOCATION") or "")
                label = f" [{name}]" if name and len(urls) > 1 else ""
                found[key].append((when if timed else "00:00",
                                   f"{when} — {ev.get('SUMMARY')}{' (' + where + ')' if where else ''}{label}"))
    events = {k: [t for _, t in sorted(v)] for k, v in found.items()}
    if errors:
        events["не вдалося прочитати"] = errors
    return events


def _rss_items(xml_bytes: bytes) -> list[tuple[str, datetime | None]]:
    root = ET.fromstring(xml_bytes)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = []
    for it in root.iter("item"):  # RSS
        title = (it.findtext("title") or "").strip()
        when = it.findtext("pubDate")
        try:
            when = parsedate_to_datetime(when) if when else None
        except Exception:
            when = None
        items.append((title, when))
    for it in root.findall("a:entry", ns):  # Atom
        title = (it.findtext("a:title", default="", namespaces=ns) or "").strip()
        when = it.findtext("a:updated", default="", namespaces=ns) or it.findtext("a:published", default="", namespaces=ns)
        try:
            when = datetime.fromisoformat(when.replace("Z", "+00:00")) if when else None
        except Exception:
            when = None
        items.append((title, when))
    return items


def get_news() -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=36)
    out = {}

    def one(name_url):
        name, url = name_url
        try:
            r = httpx.get(url, **HTTP)
            r.raise_for_status()
            fresh = [t for t, w in _rss_items(r.content) if t and (w is None or w.astimezone(timezone.utc) >= since)]
            return name, fresh[:NEWS_PER_FEED]
        except Exception as exc:
            return name, [f"(недоступно: {type(exc).__name__})"]

    with ThreadPoolExecutor(6) as pool:
        for name, titles in pool.map(one, NEWS_FEEDS.items()):
            if titles:
                out[name] = titles
    return out


SOURCES = {
    "задачі": (get_tasks, ("NOTION_TOKEN",)),
    "погода": (get_weather, ()),
    "курси": (get_rates, ()),
    "пошта": (get_mail, ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")),
    "календар": (get_calendar, ("GCAL_ICS_URL",)),
    "новини": (get_news, ()),
}


def collect(verbose: bool = False) -> dict:
    def run(item):
        name, (fn, keys) = item
        missing = [k for k in keys if not os.environ.get(k)]
        if missing:
            return name, {"недоступно": "не налаштовано: " + ", ".join(missing)}
        try:
            return name, fn()
        except Exception as exc:
            return name, {"недоступно": f"{type(exc).__name__}: {str(exc)[:160]}"}

    with ThreadPoolExecutor(len(SOURCES)) as pool:
        data = dict(pool.map(run, SOURCES.items()))
    if verbose:
        for name, value in data.items():
            ok = "❌" if isinstance(value, dict) and "недоступно" in value else "✅"
            print(f"\n{ok} {name.upper()}\n" + json.dumps(value, ensure_ascii=False, indent=2))
    return data


# ── Текст брифу ─────────────────────────────────────────────────────────────
BRIEF_TASK = """
Зараз ти робиш ранковий бриф для Яро. Це голосове повідомлення, яке він слухатиме, поки прокидається.

Структура (природно, як розповідь друга, а не зачитування списку):
1. Привітання з урахуванням дня тижня і погоди — одне живе речення, можна з підколом.
2. Погода: що вдягнути / чи брати парасолю.
3. Задачі: спершу прострочене і горяще, потім сьогоднішнє, потім що на підході. Не зачитуй усе — головне і скільки ще всього. Якщо щось чекає від брата — нагадай пнути його.
4. Календар: зустрічі сьогодні з часом. Якщо порожньо — скажи, що день вільний для роботи.
5. Пошта: скільки непрочитаних і від кого щось важливе (клієнти, банк, оплати). Розсилки ігноруй.
6. Курси: долар і євро коротко, одним реченням.
7. Новини: 2–3 найцікавіші для веб-розробника і дизайнера, кожна одним реченням, своїми словами.
8. Фінал: одна фраза-мотивація або порада, з чого почати день.

Вимоги:
- 1.5–2 хвилини мовлення (220–300 слів). Без списків, без markdown, без емодзі.
- Числа словами там, де так природніше вимовляти («двадцять сім гривень»), дати — «до четверга», а не «2026-11-05».
- Аудіотеги [laughs], [sighs] тощо — максимум два на весь бриф.
- Якщо джерело «недоступно» — одним реченням скажи, що не вдалося дістати, без технічних деталей.
- Не додавай тег <mood>.
""".strip()


def write_brief(data: dict) -> str:
    import anthropic

    sys.path.insert(0, str(Path(__file__).parent))
    soul = ""
    for f in ("SOUL.md", "USER.md"):
        p = Path.home() / ".openjarvis" / f
        if p.exists():
            soul += p.read_text() + "\n\n"
    now = datetime.now(TZ)
    days = ("понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя")
    user = (f"Сьогодні {days[now.weekday()]}, {now.strftime('%d.%m.%Y')}, зараз {now.strftime('%H:%M')}.\n\n"
            f"Дані для брифу:\n{json.dumps(data, ensure_ascii=False, indent=1)}")
    client = anthropic.Anthropic()
    kwargs = dict(model=BRIEF_MODEL, max_tokens=2000, system=soul + BRIEF_TASK,
                  messages=[{"role": "user", "content": user}])
    try:
        msg = client.messages.create(**kwargs, thinking={"type": "between_tools"},
                                     extra_body={"output_config": {"effort": "medium"}})
    except Exception:
        msg = client.messages.create(**kwargs)
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return re.sub(r"<mood[^>]*>", "", text).strip()


# ── Озвучка і доставка ──────────────────────────────────────────────────────
def tts(text: str, fmt: str) -> bytes:
    voice = os.environ.get("ELEVENLABS_VOICE_ID") or _voice_from_config()
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        params={"output_format": fmt},
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        json={"text": text, "model_id": os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")},
        timeout=120,
    )
    r.raise_for_status()
    return r.content


def _voice_from_config() -> str:
    cfg = (Path.home() / ".openjarvis" / "config.toml").read_text()
    m = re.search(r'^voice_id\s*=\s*"([^"]+)"', cfg, re.M)
    return m.group(1) if m else "JBFqnCBsd6RMkjVDRZzb"


def send_telegram(text: str, mp3: bytes) -> str:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return "Telegram не налаштовано"
    api = f"https://api.telegram.org/bot{token}"
    title = f"☀️ Бриф на {datetime.now(TZ).strftime('%d.%m')}"
    sent = False
    try:  # голосове «бульбашкою» — потрібен OGG/Opus
        opus = tts(text, "opus_48000_64")
        r = httpx.post(f"{api}/sendVoice", data={"chat_id": chat, "caption": title},
                       files={"voice": ("brief.ogg", opus, "audio/ogg")}, timeout=60)
        sent = r.status_code == 200 and r.json().get("ok")
    except Exception:
        pass
    if not sent:  # запасний варіант — mp3 аудіофайлом
        r = httpx.post(f"{api}/sendAudio", data={"chat_id": chat, "title": title, "performer": "Джарвіс"},
                       files={"audio": ("brief.mp3", mp3, "audio/mpeg")}, timeout=60)
        if not (r.status_code == 200 and r.json().get("ok")):
            return f"Telegram помилка: {r.text[:200]}"
    clean = re.sub(r"\s*\[[a-z ,\-]+\]\s*", " ", text).strip()
    httpx.post(f"{api}/sendMessage", data={"chat_id": chat, "text": clean[:4000]}, timeout=30)
    return "надіслано в Telegram"


def main() -> None:
    args = set(sys.argv[1:])
    if "--check" in args:
        collect(verbose=True)
        return

    data = collect()
    text = write_brief(data)
    print(text)
    if "--text" in args:
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mp3 = tts(text, "mp3_44100_128")
    path = OUT_DIR / f"brief-{datetime.now(TZ).strftime('%Y-%m-%d')}.mp3"
    path.write_bytes(mp3)
    (OUT_DIR / f"brief-{datetime.now(TZ).strftime('%Y-%m-%d')}.txt").write_text(text)

    print("\n→", send_telegram(text, mp3))
    if "--no-speak" not in args:
        subprocess.run(["afplay", str(path)])


if __name__ == "__main__":
    main()
