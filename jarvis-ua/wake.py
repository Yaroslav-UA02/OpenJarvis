"""Джарвіс — живий голосовий асистент: wake word, потокова відповідь, перебивання, HUD, настрій.

Цикл:
  1. Локально слухає мікрофон і чекає на «Hey Jarvis» / «Джарвіс» (openWakeWord, без інтернету).
  2. «Дзинь» → записує фразу (кінець — 0.7 с тиші) → ElevenLabs Scribe (хмара, не гріє ноут)
     + аналіз інтонації. Без інтернету — локальний Whisper.
  3. Claude відповідає ПОТОКОМ: перше речення вже звучить, поки пишеться друге.
  4. Поки Джарвіс говорить — можна перебити голосом або словом «Джарвіс», він замовкає і слухає.
  5. Настрій Джарвіса і твій зберігаються між сесіями (~/.openjarvis/jarvis_state.json).

Запуск:  jw   (або як служба: service.sh install)
"""

from __future__ import annotations

import collections
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from hud_server import Hud, open_window  # noqa: E402

# ── Налаштування ────────────────────────────────────────────────────────────
WAKE_MODEL = "hey_jarvis"
WAKE_THRESHOLD = 0.5        # хибні спрацювання → 0.6–0.7, погано чує → 0.35–0.4
FOLLOW_UP_SECONDS = 6.0     # скільки чекати продовження розмови без wake word
END_SILENCE = 0.7           # пауза, після якої фраза вважається завершеною (с)
HISTORY_LIMIT = 20
HISTORY_RESET_SECONDS = 600
SPEECH_RMS = 500            # стартове значення; далі калібрується під шум кімнати
SPEECH_MIN, SPEECH_MAX = 150, 700

STT_MODE = "cloud"          # "cloud" — ElevenLabs Scribe (швидко, не гріє); "local" — Whisper на Mac
CLAUDE_EFFORT = "low"       # low — найшвидше; medium — розумніше, але повільніше
MAX_TOKENS = 1024

BARGE_IN = True             # перебивання голосом
HEADPHONES = False          # True, якщо в навушниках — тоді перебивання чутливіше
BARGE_FRAMES = 6            # скільки кадрів (~32 мс) поспіль має звучати твій голос, щоб перебити

HUD_ENABLED = True
HUD_FULLSCREEN = False

STOP_WORDS = ("дякую", "відбій", "все, дякую", "на сьогодні все", "можеш іти")
STT_JUNK = ("дякую за перегляд", "субтитри", "продовження далі", "редактор субтитрів")

SR_IN = 16000               # мікрофон
SR_OUT = 24000              # голос ElevenLabs (pcm_24000)
CHUNK = 512                 # 32 мс
WAKE_FRAME = 1280           # 80 мс — розмір кадру для openWakeWord
SOUND_WAKE = "/System/Library/Sounds/Tink.aiff"
SOUND_SLEEP = "/System/Library/Sounds/Pop.aiff"

DAYS = ("понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя")
STATE_FILE = Path.home() / ".openjarvis" / "jarvis_state.json"
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
AUDIO_TAG = re.compile(r"\s*\[[a-z ,\-]+\]\s*")
MOOD_TAG = re.compile(r"<mood\b([^>]*)/?>", re.I)
SENTENCE_END = re.compile(r"[.!?…]+[\"»)]*\s+|\n+")

MOOD_INSTRUCTIONS = """
## Твій настрій і реакції
У тебе є власний настрій, і він змінюється від того, як з тобою говорять, від часу доби і від того, що відбувається.
Він впливає на тон: бадьорий — жартуєш і підколюєш; втомлений чи роздратований — коротше і сухіше; задоволений — теплішаєш.
Настрій не скидається на кожній репліці, він плавно тягнеться далі.

Перед кожною реплікою користувача є рядок [голос: ...] — це аналіз його інтонації (гучність, темп, тон) порівняно з його звичайною манерою.
Враховуй його разом зі словами, але не коментуй прямо — просто реагуй, як жива людина.

Якщо питання очевидне або тупе — реагуй по-людськи (підколи, [sighs]), але відповідь все одно дай.

Якщо репліка починається з [перебив] — він перебив тебе на півслові. Не ображайся і не повторюй сказане, просто реагуй на нове.

Відповідай ЖИВОЮ розмовою: перше речення коротке, щоб звук пішов одразу. Без списків і markdown.

В САМОМУ КІНЦІ кожної відповіді, окремим рядком, додавай службовий тег (він не озвучується і не показується):
<mood jarvis="твій настрій 1–3 словами" user="його настрій 1–3 словами"/>
""".strip()


# ── Утиліти ─────────────────────────────────────────────────────────────────
def beep(path: str) -> None:
    subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt((x.astype(np.float32) ** 2).mean())) if x.size else 0.0


def pcm_to_wav(pcm: np.ndarray, sr: int = SR_IN) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


def voice_features(pcm: np.ndarray, text: str) -> dict:
    """Гучність, темп і висота тону. Наближено, але досить, щоб відчути настрій."""
    pcm = pcm.astype(np.float32)
    win = 640
    frames = pcm[: pcm.size // win * win].reshape(-1, win)
    if frames.size == 0:
        return {"loud": 0.0, "rate": 0.0, "pitch": 0.0, "pitch_var": 0.0}
    level = np.sqrt((frames ** 2).mean(axis=1))
    voiced = frames[level > SPEECH_RMS]
    pitches = []
    for f in voiced[::2]:
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[win - 1:]
        lo, hi = SR_IN // 400, SR_IN // 70
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] > 0.3 * ac[0]:
            pitches.append(SR_IN / lag)
    speech_sec = max(0.5, len(voiced) * win / SR_IN)
    return {
        "loud": float(level[level > SPEECH_RMS].mean()) if voiced.size else 0.0,
        "rate": len(text.split()) / speech_sec,
        "pitch": float(np.median(pitches)) if pitches else 0.0,
        "pitch_var": float(np.std(pitches)) if len(pitches) > 3 else 0.0,
    }


# ── Настрій і «звичайний голос» Яро — зберігаються між сесіями ─────────────
class State:
    def __init__(self) -> None:
        self.data = {"jarvis": "спокійний", "user": "невідомо", "updated": 0, "baseline": {}}
        try:
            self.data.update(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass

    def save(self) -> None:
        STATE_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))

    def describe_voice(self, f: dict) -> str:
        base = self.data.setdefault("baseline", {})
        n = base.get("n", 0)
        words = []
        if n >= 3:
            def cmp(key, more, less, k=0.18):
                b = base.get(key) or 0
                if not b or not f[key]:
                    return
                d = (f[key] - b) / b
                if d > k:
                    words.append(more)
                elif d < -k:
                    words.append(less)
            cmp("loud", "голосніше звичайного", "тихіше звичайного", 0.3)
            cmp("rate", "швидше звичайного", "повільніше звичайного")
            cmp("pitch", "вищий тон", "нижчий тон", 0.1)
            cmp("pitch_var", "емоційна, жива інтонація", "монотонно")
        a = 1 / min(n + 1, 20)
        for k, v in f.items():
            if v:
                base[k] = base.get(k, v) * (1 - a) + v * a
        base["n"] = n + 1
        if n < 3:
            return "ще вивчаю його звичайну манеру"
        return ", ".join(words) or "звичайна манера"

    def context(self) -> str:
        now = datetime.now()
        ago = time.time() - (self.data.get("updated") or 0)
        if ago > 86400 * 2:
            last = "давно не говорили"
        elif ago > 3600 * 3:
            last = f"востаннє говорили ~{int(ago // 3600)} год тому"
        else:
            last = f"востаннє говорили ~{max(1, int(ago // 60))} хв тому"
        return (
            f"## Зараз\n{DAYS[now.weekday()]}, {now.strftime('%d.%m.%Y, %H:%M')}. {last}.\n"
            f"Твій настрій зараз: {self.data['jarvis']}. Яро востаннє був: {self.data['user']}."
        )

    def update(self, reply: str) -> str:
        m = MOOD_TAG.search(reply)
        if m:
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            self.data["jarvis"] = attrs.get("jarvis", self.data["jarvis"])[:40]
            self.data["user"] = attrs.get("user", self.data["user"])[:40]
        self.data["updated"] = time.time()
        self.save()
        return MOOD_TAG.sub("", reply).strip()


# ── Джарвіс ─────────────────────────────────────────────────────────────────
class Jarvis:
    def __init__(self) -> None:
        import anthropic
        from rich.console import Console

        from openjarvis.core.config import load_config
        from openjarvis.prompt.builder import SystemPromptBuilder

        self.console = Console()
        self.config = load_config()
        self.model = self.config.intelligence.default_model or "claude-sonnet-5-5"
        self.claude = anthropic.Anthropic()
        self.claude_extra = self._claude_options()

        self.base_prompt = SystemPromptBuilder(
            agent_template=self.config.agent.default_system_prompt or "",
            memory_files_config=self.config.memory_files,
            system_prompt_config=self.config.system_prompt,
        ).build() + "\n\n" + MOOD_INSTRUCTIONS

        self.eleven_key = os.environ.get("ELEVENLABS_API_KEY", "")
        self.voice_id = getattr(self.config.speech, "voice_id", "") or "JBFqnCBsd6RMkjVDRZzb"
        if re.match(r"^[a-z][fm]_[a-z]+$", self.voice_id):  # kokoro-ID не підходить ElevenLabs
            self.voice_id = "JBFqnCBsd6RMkjVDRZzb"
        self.tts_model = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
        self.stt_models = ["scribe_v2", "scribe_v1"]
        self.whisper = None

        self.state = State()
        self.history: list[dict] = []
        self.last_turn = 0.0
        self.interrupt = threading.Event()
        self.playing = threading.Event()
        self.detector = None

        self.hud = Hud()
        self.hud.model = self.model

    def log(self, text: str, style: str = "") -> None:
        self.console.print(text, style=style or None, markup=False)

    def emit_mood(self) -> None:
        self.hud.emit("mood", jarvis=self.state.data["jarvis"], user=self.state.data["user"])

    def _claude_options(self) -> dict:
        """Мінімум «обдумування» для швидкої відповіді. Для Sonnet 5.5 це between_tools + low effort."""
        if re.search(r"claude-(sonnet|opus|haiku)-5|claude-fable-5", self.model):
            return {"thinking": {"type": "between_tools"},
                    "extra_body": {"output_config": {"effort": CLAUDE_EFFORT}}}
        return {}

    # ── Запис голосу ────────────────────────────────────────────────────────
    def record(self, startup_silence: float, prebuffer: list | None = None) -> np.ndarray | None:
        """Пише фразу до паузи END_SILENCE. prebuffer — те, що ти вже почав казати, перебиваючи."""
        import sounddevice as sd

        self.hud.emit("state", state="listening")
        q: queue.Queue = queue.Queue()
        frames = list(prebuffer or [])
        voiced = len(frames)
        silent, started = 0, time.time()
        end_chunks = int(END_SILENCE * SR_IN / CHUNK)
        with sd.InputStream(samplerate=SR_IN, channels=1, dtype="int16", blocksize=CHUNK,
                            callback=lambda d, *_: q.put(d[:, 0].copy())):
            while True:
                f = q.get()
                frames.append(f)
                level = rms(f)
                if len(frames) % 4 == 0:
                    self.hud.emit("mic", level=min(1.0, level / 4000))
                if level > SPEECH_RMS:
                    voiced += 1
                    silent = 0
                elif voiced:
                    silent += 1
                    if silent >= end_chunks:
                        break
                elif time.time() - started > startup_silence:
                    break
                if len(frames) * CHUNK > SR_IN * 30:
                    break
        if voiced < 5:  # менше ~160 мс голосу — це не фраза
            return None
        return np.concatenate(frames)

    # ── Розпізнавання ───────────────────────────────────────────────────────
    def transcribe(self, pcm: np.ndarray) -> str:
        self.hud.emit("state", state="transcribing")
        wav = pcm_to_wav(pcm)
        if STT_MODE == "cloud" and self.eleven_key:
            import httpx

            for model in list(self.stt_models):
                try:
                    r = httpx.post(
                        "https://api.elevenlabs.io/v1/speech-to-text",
                        headers={"xi-api-key": self.eleven_key},
                        data={"model_id": model, "language_code": "ukr", "tag_audio_events": "false"},
                        files={"file": ("speech.wav", wav, "audio/wav")},
                        timeout=20,
                    )
                    if r.status_code == 200:
                        return (r.json().get("text") or "").strip()
                    if r.status_code in (400, 404, 422) and "model" in r.text.lower():
                        self.stt_models.remove(model)  # такої моделі немає — пробуємо наступну
                        continue
                    self.log(f"   ↳ Scribe {r.status_code}: {r.text[:160]} → локальний Whisper", "yellow")
                    break
                except Exception as exc:
                    self.log(f"   ↳ Scribe недоступний ({exc}) → локальний Whisper", "yellow")
                    break
        return self._whisper(wav)

    def _whisper(self, wav: bytes) -> str:
        if self.whisper is None:
            from openjarvis.speech._discovery import get_speech_backend

            self.log("   ↳ завантажую локальний Whisper…", "dim")
            self.whisper = get_speech_backend(self.config)
        if self.whisper is None:
            return ""
        lang = (getattr(self.config.speech, "language", "") or "").strip() or None
        return self.whisper.transcribe(wav, format="wav", language=lang).text.strip()

    # ── Голос: потокове озвучення речень ────────────────────────────────────
    def speaker(self, sentences: queue.Queue) -> None:
        """Бере речення з черги і стрімить їх з ElevenLabs прямо в динаміки."""
        import httpx
        import sounddevice as sd

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream"
        with sd.OutputStream(samplerate=SR_OUT, channels=1, dtype="int16") as out:
            while True:
                text = sentences.get()
                if text is None or self.interrupt.is_set():
                    break
                text = EMOJI.sub("", re.sub(r"[*_#`>]+", "", text)).strip()
                if not text:
                    continue
                try:
                    with httpx.stream(
                        "POST", url, params={"output_format": f"pcm_{SR_OUT}"},
                        headers={"xi-api-key": self.eleven_key},
                        json={"text": text, "model_id": self.tts_model},
                        timeout=30,
                    ) as r:
                        if r.status_code != 200:
                            self.log(f"   ↳ ElevenLabs {r.status_code}: {r.read()[:160]!r}", "red")
                            continue
                        rest = b""
                        for chunk in r.iter_bytes(4800):
                            if self.interrupt.is_set():
                                break
                            data = rest + chunk
                            cut = len(data) - len(data) % 2
                            rest = data[cut:]
                            pcm = np.frombuffer(data[:cut], dtype=np.int16)
                            if pcm.size:
                                if not self.playing.is_set():
                                    self.playing.set()
                                    self.hud.emit("state", state="speaking")
                                self.hud.emit("voice", level=min(1.0, rms(pcm) / 6000))
                                out.write(pcm.reshape(-1, 1))
                except Exception as exc:
                    self.log(f"   ↳ помилка озвучення: {exc}", "red")
            if not self.interrupt.is_set():
                time.sleep(0.15)  # дати динаміку доіграти хвіст
        self.playing.clear()

    # ── Перебивання ─────────────────────────────────────────────────────────
    def barge_monitor(self, stop: threading.Event, prebuffer: collections.deque) -> None:
        """Поки Джарвіс говорить: твій голос (гучніший за його «луну») або слово «Джарвіс» → стоп."""
        import sounddevice as sd

        q: queue.Queue = queue.Queue()
        echo = 0.0
        loud_run = 0
        wake_buf = np.zeros(0, dtype=np.int16)
        with sd.InputStream(samplerate=SR_IN, channels=1, dtype="int16", blocksize=CHUNK,
                            callback=lambda d, *_: q.put(d[:, 0].copy())):
            while not stop.is_set():
                try:
                    f = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                prebuffer.append(f)
                if not self.playing.is_set():
                    continue
                level = rms(f)
                threshold = SPEECH_RMS * 1.5 if HEADPHONES else max(SPEECH_RMS * 2.5, echo * 2.4)
                if level > threshold:
                    loud_run += 1
                else:
                    loud_run = 0
                    echo = level if echo == 0 else echo * 0.95 + level * 0.05  # вчимо рівень «луни»
                hit = BARGE_IN and loud_run >= BARGE_FRAMES

                # слово «Джарвіс» працює як перебивання навіть через динаміки
                wake_buf = np.concatenate([wake_buf, f])
                while wake_buf.size >= WAKE_FRAME and not hit:
                    score = max(self.detector.predict(wake_buf[:WAKE_FRAME]).values())
                    wake_buf = wake_buf[WAKE_FRAME:]
                    hit = score >= WAKE_THRESHOLD
                if hit:
                    self.interrupt.set()
                    self.log("   ↳ перебив — слухаю", "yellow")
                    break
        self.detector.reset()

    # ── Відповідь Claude потоком ────────────────────────────────────────────
    def stream_claude(self, system: str):
        """Генерує шматки тексту. Якщо модель не приймає швидкі налаштування — відкочується до стандартних."""
        options = [self.claude_extra, {k: v for k, v in self.claude_extra.items() if k != "extra_body"}, {}]
        tried = set()
        for opt in options:
            key = json.dumps(opt, sort_keys=True)
            if key in tried:
                continue
            tried.add(key)
            yielded = False
            try:
                with self.claude.messages.stream(
                    model=self.model, max_tokens=MAX_TOKENS, system=system,
                    messages=self.history, **opt,
                ) as stream:
                    for text in stream.text_stream:
                        yielded = True
                        yield text
                        if self.interrupt.is_set():
                            break
                    try:
                        final = stream.get_final_message()
                        self.hud.add_usage({"usage": {"prompt_tokens": final.usage.input_tokens,
                                                      "completion_tokens": final.usage.output_tokens}},
                                           time.time() - self._asked_at)
                    except Exception:
                        pass
                if opt != self.claude_extra:
                    self.claude_extra = opt  # запам'ятати робочий варіант
                return
            except Exception as exc:
                if not yielded and ("400" in str(exc) or "invalid_request" in str(exc)):
                    self.log(f"   ↳ Claude не прийняв налаштування {opt or 'стандартні'} — пробую інші", "dim")
                    continue
                raise

    def respond(self, text: str, tone: str, interrupted: bool) -> tuple[str, list | None]:
        """Claude пише → речення одразу йдуть в озвучку. Повертає (відповідь, prebuffer якщо перебили)."""
        if time.time() - self.last_turn > HISTORY_RESET_SECONDS:
            self.history = []
        prefix = "[перебив] " if interrupted else ""
        self.history.append({"role": "user", "content": f"[голос: {tone}]\n{prefix}{text}"})
        self.history = self.history[-HISTORY_LIMIT:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)

        self.interrupt.clear()
        self.playing.clear()
        self.hud.emit("state", state="thinking")
        sentences: queue.Queue = queue.Queue()
        stop_barge = threading.Event()
        prebuffer: collections.deque = collections.deque(maxlen=int(1.2 * SR_IN / CHUNK))
        spk = threading.Thread(target=self.speaker, args=(sentences,), daemon=True)
        barge = threading.Thread(target=self.barge_monitor, args=(stop_barge, prebuffer), daemon=True)
        spk.start()
        barge.start()

        system = self.base_prompt + "\n\n" + self.state.context()
        raw, buf = "", ""
        self._asked_at = time.time()
        try:
            for piece in self.stream_claude(system):
                raw += piece
                buf += piece
                cut = buf.find("<")  # службовий <mood/> не озвучуємо
                speakable, held = (buf, "") if cut < 0 else (buf[:cut], buf[cut:])
                last = None
                for m in SENTENCE_END.finditer(speakable):
                    last = m
                if last and last.end() >= 12:  # готове речення — одразу в озвучку
                    sentences.put(speakable[: last.end()])
                    speakable = speakable[last.end():]
                buf = speakable + held
                if self.interrupt.is_set():
                    break
            tail = buf.split("<", 1)[0].strip()
            if tail and not self.interrupt.is_set():
                sentences.put(tail)
        except Exception as exc:
            self.log(f"Помилка Claude: {exc}", "red")
            self.hud.emit("state", state="error")
            sentences.put("Блін, Ярік, сервер не відповідає. Спробуй ще раз.")
        sentences.put(None)
        spk.join()
        stop_barge.set()
        barge.join(timeout=1)

        reply = self.state.update(raw) if raw else ""
        if self.interrupt.is_set():
            reply += " …"
        self.history.append({"role": "assistant", "content": reply or "…"})
        self.last_turn = time.time()
        self.emit_mood()
        return reply, (list(prebuffer) if self.interrupt.is_set() else None)

    def say(self, text: str) -> None:
        q: queue.Queue = queue.Queue()
        q.put(text)
        q.put(None)
        self.interrupt.clear()
        self.speaker(q)

    # ── Один діалог після wake word ─────────────────────────────────────────
    def conversation(self) -> None:
        first, prebuffer, interrupted = True, None, False
        while True:
            self.log("🎙  Слухаю…", "cyan")
            pcm = self.record(startup_silence=5.0 if first else FOLLOW_UP_SECONDS, prebuffer=prebuffer)
            if pcm is None:
                break
            text = self.transcribe(pcm)
            if not text or any(j in text.lower() for j in STT_JUNK):
                self.log(f"   ↳ почув «{text}» — схоже на шум", "dim")
                if first:
                    first = False
                    continue
                break
            first = False
            tone = self.state.describe_voice(voice_features(pcm, text))
            self.log(f"Ти: {text}   ({tone})")
            self.hud.emit("user", text=text)

            if any(w in text.lower() for w in STOP_WORDS):
                bye = "Давай, Ярік. На зв'язку."
                self.hud.emit("jarvis", text=bye)
                self.say(bye)
                break

            reply, prebuffer = self.respond(text, tone, interrupted)
            interrupted = prebuffer is not None
            shown = AUDIO_TAG.sub(" ", reply).strip()
            self.log(f"Джарвіс: {shown}   [{self.state.data['jarvis']}]\n", "green")
            self.hud.emit("jarvis", text=shown)
        self.hud.emit("state", state="idle")
        beep(SOUND_SLEEP)

    def calibrate(self, levels: list[float]) -> None:
        global SPEECH_RMS
        noise = float(np.percentile(levels, 30))
        new = float(np.clip(noise * 3, SPEECH_MIN, SPEECH_MAX))
        if abs(new - SPEECH_RMS) > 25:
            SPEECH_RMS = new
            self.log(f"   ↳ фон кімнати {int(noise)}, поріг мови {int(new)}", "dim")

    # ── Головний цикл ───────────────────────────────────────────────────────
    def run(self) -> None:
        import openwakeword
        import sounddevice as sd
        from openwakeword.model import Model

        if HUD_ENABLED:
            self.hud.start()
            self.hud.mood = self.state.data

        openwakeword.utils.download_models(model_names=[WAKE_MODEL])
        self.detector = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")

        if STT_MODE != "cloud" or not self.eleven_key:
            self.log("Прогріваю локальний Whisper…", "dim")
            self._whisper(pcm_to_wav(np.zeros(SR_IN // 2, dtype=np.int16)))

        self.log(
            f"Джарвіс слухає. Скажи «Hey Jarvis» або «Джарвіс». Настрій: {self.state.data['jarvis']}. "
            f"STT: {STT_MODE}, голос: {self.tts_model}. HUD: http://127.0.0.1:8765  Вихід: Ctrl+C\n",
            "bold green",
        )
        while True:
            frames: queue.Queue = queue.Queue()
            n = 0
            levels: list[float] = []
            with sd.InputStream(samplerate=SR_IN, channels=1, dtype="int16", blocksize=WAKE_FRAME,
                                callback=lambda d, *_: frames.put(d[:, 0].copy())):
                while True:
                    frame = frames.get()
                    score = max(self.detector.predict(frame).values())
                    n += 1
                    level = rms(frame)
                    levels.append(level)
                    if n % 3 == 0:
                        self.hud.emit("mic", level=min(1.0, level / 4000))
                    if n % 50 == 0:
                        self.calibrate(levels[-250:])
                        levels = levels[-250:]
                    if score >= WAKE_THRESHOLD:
                        break

            self.log(f"wake: {score:.2f}", "dim")
            self.detector.reset()
            if HUD_ENABLED and not self.hud.connected:
                open_window(HUD_FULLSCREEN)
            beep(SOUND_WAKE)
            self.emit_mood()
            time.sleep(0.2)
            self.conversation()
            self.detector.reset()
            self.log("Чекаю на «Джарвіс»…", "dim")


def single_instance() -> object:
    """Не даємо запуститись другій копії (служба + ручний запуск)."""
    import fcntl

    lock = open(Path.home() / ".openjarvis" / "jarvis.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Джарвіс уже працює у фоні. Зупинити: jarvis-off")
        sys.exit(0)
    return lock


if __name__ == "__main__":
    _lock = single_instance()
    try:
        Jarvis().run()
    except KeyboardInterrupt:
        print("\nДжарвіс вимкнений.")
