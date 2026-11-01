"""HUD-сервер Джарвіса.

- http://127.0.0.1:8765        — сторінка HUD (hud/index.html)
- ws://127.0.0.1:8766          — живі події: стан, текст, системні дані, звук

Використання з wake.py:
    hud = Hud(); hud.start()
    hud.emit("state", state="listening")
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import subprocess
import threading
import time
from pathlib import Path

import psutil

HTTP_PORT = 8765
WS_PORT = 8766
HUD_DIR = Path(__file__).parent / "hud"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # не засмічуємо термінал
        pass


class Hud:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set = set()
        self._ready = threading.Event()
        self.session = {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "latency": 0.0}
        self.model = ""
        self.mood: dict = {}

    # ── Запуск ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        handler = functools.partial(_QuietHandler, directory=str(HUD_DIR))
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._run_ws, daemon=True).start()
        self._ready.wait(5)

    def _run_ws(self) -> None:
        import websockets

        async def handler(ws):
            self._clients.add(ws)
            try:
                await ws.send(json.dumps({"type": "hello", "model": self.model, "session": self.session,
                                          "mood": {"jarvis": self.mood.get("jarvis", ""), "user": self.mood.get("user", "")}}, ensure_ascii=False))
                async for _ in ws:
                    pass
            finally:
                self._clients.discard(ws)

        async def main():
            async with websockets.serve(handler, "127.0.0.1", WS_PORT):
                self._ready.set()
                await self._stats_loop()

        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(main())

    # ── Події ───────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return bool(self._clients)

    def emit(self, type_: str, **data) -> None:
        """Потокобезпечно надсилає подію всім відкритим HUD."""
        if self._loop is None or not self._clients:
            return
        msg = json.dumps({"type": type_, **data}, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)

    async def _broadcast(self, msg: str) -> None:
        for ws in list(self._clients):
            try:
                await ws.send(msg)
            except Exception:
                self._clients.discard(ws)

    def add_usage(self, result: dict, latency: float) -> None:
        usage = result.get("usage") or {}
        t_in = int(usage.get("prompt_tokens", 0))
        t_out = int(usage.get("completion_tokens", 0))
        cost = float(result.get("cost_usd") or 0) or (t_in * 3 + t_out * 15) / 1_000_000  # ≈ тариф Sonnet
        s = self.session
        s["requests"] += 1
        s["tokens_in"] += t_in
        s["tokens_out"] += t_out
        s["cost"] += cost
        s["latency"] = round(latency, 2)
        self.emit("session", session=s)

    # ── Системні дані раз на секунду ────────────────────────────────────────
    async def _stats_loop(self) -> None:
        net_prev = psutil.net_io_counters()
        psutil.cpu_percent(percpu=True)
        boot = psutil.boot_time()
        while True:
            await asyncio.sleep(1)
            if not self._clients:
                net_prev = psutil.net_io_counters()
                continue
            net = psutil.net_io_counters()
            battery = psutil.sensors_battery()
            vm = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            stats = {
                "cpu": psutil.cpu_percent(),
                "cores": psutil.cpu_percent(percpu=True),
                "ram": vm.percent,
                "ram_used": round(vm.used / 2**30, 1),
                "ram_total": round(vm.total / 2**30, 1),
                "disk": disk.percent,
                "net_down": (net.bytes_recv - net_prev.bytes_recv) / 1024,
                "net_up": (net.bytes_sent - net_prev.bytes_sent) / 1024,
                "battery": battery.percent if battery else None,
                "charging": bool(battery and battery.power_plugged),
                "uptime": int(time.time() - boot),
                "procs": len(psutil.pids()),
            }
            net_prev = net
            await self._broadcast(json.dumps({"type": "stats", **stats}))


def open_window(fullscreen: bool = False) -> None:
    """Відкриває HUD окремим вікном Chrome без вкладок; якщо Chrome немає — у браузері за замовчуванням."""
    url = f"http://127.0.0.1:{HTTP_PORT}"
    args = ["open", "-na", "Google Chrome", "--args", f"--app={url}", "--window-size=1440,900"]
    if fullscreen:
        args.append("--start-fullscreen")
    if subprocess.run(args, capture_output=True).returncode != 0:
        subprocess.run(["open", url])
