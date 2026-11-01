"""Джарвіс — режим постійного слухання (wake word).

Цикл:
  1. Локально слухає мікрофон і чекає на «Hey Jarvis» / «Джарвіс» (openWakeWord, без інтернету).
  2. Сигнал «дзинь» → записує фразу до паузи → Whisper розпізнає.
  3. Claude відповідає з урахуванням історії → ElevenLabs озвучує.
  4. Режим розмови: після відповіді ще 6 с слухає продовження без wake word.
     «дякую», «відбій», «все» — повертає в режим очікування.

Запуск:
  ~/.openjarvis/.venv/bin/python ~/.openjarvis/src/jarvis-ua/wake.py
"""

from __future__ import annotations

import io
import queue
import re
import subprocess
import sys
import time
import wave

import numpy as np

# ── Налаштування ────────────────────────────────────────────────────────────
WAKE_MODEL = "hey_jarvis"
WAKE_THRESHOLD = 0.5        # 0..1; хибні спрацювання → підняти до 0.6–0.7, не чує → 0.3–0.4
FOLLOW_UP_SECONDS = 6.0     # скільки чекати продовження розмови без wake word
HISTORY_LIMIT = 20          # скільки повідомлень пам'ятати в межах сесії
HISTORY_RESET_SECONDS = 600 # через 10 хв тиші розмова починається з чистого аркуша
SPEECH_RMS = 500            # поріг гучності, нижче якого вважаємо тишею

STOP_WORDS = ("дякую", "відбій", "все, дякую", "на сьогодні все", "можеш іти")
# Типові «галюцинації» Whisper на тиші — ігноруємо
WHISPER_JUNK = ("дякую за перегляд", "субтитри", "продовження далі", "редактор субтитрів")

SAMPLE_RATE = 16000
FRAME = 1280  # 80 мс — розмір кадру для openWakeWord
SOUND_WAKE = "/System/Library/Sounds/Tink.aiff"
SOUND_SLEEP = "/System/Library/Sounds/Pop.aiff"


def beep(path: str) -> None:
    subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wav_has_speech(wav_bytes: bytes) -> bool:
    """Чи є в записі хоч щось гучніше за тишу (захист від галюцинацій Whisper)."""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    if pcm.size == 0:
        return False
    chunks = pcm[: pcm.size // 1024 * 1024].reshape(-1, 1024).astype(np.float32)
    if chunks.size == 0:
        return False
    rms = np.sqrt((chunks ** 2).mean(axis=1))
    return bool((rms > SPEECH_RMS).sum() >= 3)  # мінімум ~0.2 с звуку


class Jarvis:
    def __init__(self) -> None:
        from rich.console import Console

        from openjarvis.cli._voice_chat import VoiceSession
        from openjarvis.core.config import load_config
        from openjarvis.engine import get_engine
        from openjarvis.intelligence import register_builtin_models
        from openjarvis.prompt.builder import SystemPromptBuilder

        self.console = Console()
        self.config = load_config()
        register_builtin_models()

        resolved = get_engine(self.config)
        if resolved is None:
            sys.exit("Немає доступного движка. Перевір `jarvis doctor`.")
        _, self.engine = resolved
        self.model = self.config.intelligence.default_model or "claude-sonnet-5-5"
        self.temperature = getattr(self.config.intelligence, "temperature", 0.7)
        self.max_tokens = getattr(self.config.intelligence, "max_tokens", 4096)

        self.system_prompt = SystemPromptBuilder(
            agent_template=self.config.agent.default_system_prompt or "",
            memory_files_config=self.config.memory_files,
            system_prompt_config=self.config.system_prompt,
        ).build()
        self.voice = VoiceSession(self.config)
        self.history: list = []
        self.last_turn = 0.0

    # ── Розпізнавання ───────────────────────────────────────────────────────
    def listen(self, startup_silence: float) -> str | None:
        from openjarvis.speech.voice_io import record_until_silence

        audio = record_until_silence(startup_silence_seconds=startup_silence)
        if not wav_has_speech(audio):
            return None
        stt = self.voice.get_stt_backend()
        if stt is None:
            sys.exit("STT не налаштовано. Перевір секцію [speech] у config.toml.")
        text = stt.transcribe(audio, format="wav", language=self.voice.get_stt_language()).text.strip()
        if not text or any(j in text.lower() for j in WHISPER_JUNK):
            return None
        return text

    # ── Відповідь ───────────────────────────────────────────────────────────
    def answer(self, text: str) -> str:
        from openjarvis.core.types import Message, Role

        if time.time() - self.last_turn > HISTORY_RESET_SECONDS:
            self.history = []
        self.history.append(Message(role=Role.USER, content=text))
        self.history = self.history[-HISTORY_LIMIT:]

        messages = [Message(role=Role.SYSTEM, content=self.system_prompt), *self.history]
        result = self.engine.generate(
            messages, model=self.model, temperature=self.temperature, max_tokens=self.max_tokens
        )
        reply = result.get("content", "") if isinstance(result, dict) else str(result)
        self.history.append(Message(role=Role.ASSISTANT, content=reply))
        self.last_turn = time.time()
        return reply

    def say(self, text: str) -> None:
        from openjarvis.cli._voice_chat import speak

        # Прибираємо markdown, щоб ElevenLabs не читав «зірочки»
        clean = re.sub(r"[*_#`>]+", "", text)
        speak(clean, self.console, self.voice)

    # ── Один діалог після wake word ─────────────────────────────────────────
    def conversation(self) -> None:
        first = True
        while True:
            self.console.print("[cyan]🎙  Слухаю…[/cyan]")
            text = self.listen(startup_silence=5.0 if first else FOLLOW_UP_SECONDS)
            if text is None:
                break
            first = False
            self.console.print(f"[bold]Ти:[/bold] {text}")

            if any(w in text.lower() for w in STOP_WORDS):
                self.say("До зв'язку, шефе.")
                break

            try:
                reply = self.answer(text)
            except Exception as exc:  # мережа, ліміти API тощо
                self.console.print(f"[red]Помилка Claude: {exc}[/red]")
                self.say("Шефе, не можу достукатися до сервера. Спробуйте ще раз.")
                break
            self.console.print(f"[green]Джарвіс:[/green] {reply}\n")
            self.say(reply)
        beep(SOUND_SLEEP)

    # ── Головний цикл ───────────────────────────────────────────────────────
    def run(self) -> None:
        import openwakeword
        import sounddevice as sd
        from openwakeword.model import Model

        openwakeword.utils.download_models(model_names=[WAKE_MODEL])
        detector = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")

        self.console.print("[dim]Прогріваю розпізнавання мови…[/dim]")
        self.voice.get_stt_backend()

        self.console.print(
            f"[bold green]Джарвіс слухає.[/bold green] Скажи «Hey Jarvis» або «Джарвіс». "
            f"Поріг: {WAKE_THRESHOLD}. Вихід: Ctrl+C\n"
        )
        while True:
            frames: queue.Queue = queue.Queue()
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME,
                callback=lambda data, *_: frames.put(data[:, 0].copy()),
            ):
                while True:
                    score = max(detector.predict(frames.get()).values())
                    if score >= WAKE_THRESHOLD:
                        break
            # Мікрофон закрито: далі пишемо фразу і говоримо, не чуючи самих себе
            self.console.print(f"[dim]wake: {score:.2f}[/dim]")
            detector.reset()
            beep(SOUND_WAKE)
            time.sleep(0.25)
            self.conversation()
            detector.reset()
            self.console.print("[dim]Чекаю на «Джарвіс»…[/dim]")


if __name__ == "__main__":
    try:
        Jarvis().run()
    except KeyboardInterrupt:
        print("\nДжарвіс вимкнений.")
