"""Run the local site, Flow worker, and public tunnel as one Mac application."""

from __future__ import annotations

import fcntl
import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE = Path(__file__).resolve().parent
LOGS = BASE / "studio_logs"
ADDRESS_FILE = BASE / "АДРЕС_СТУДИИ.txt"
LOCAL_URL = "http://127.0.0.1:8765"
DASHBOARD_URL = "http://127.0.0.1:8766/"
TUNNEL_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.lhr\.life")
TUNNEL_START_TIMEOUT = 30
STOP = threading.Event()
LINES: queue.Queue[tuple[int, str]] = queue.Queue()


def say(message: str) -> None:
    print(message, flush=True)


def fetch(url: str, timeout: float = 5, headers: dict | None = None) -> bytes | None:
    try:
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(4096)
    except (OSError, urllib.error.HTTPError):
        return None


def local_ready() -> bool:
    return fetch(LOCAL_URL + "/healthz") == b'{"ok":true}'


def remote_ready(url: str) -> bool:
    return fetch(url + "/healthz", timeout=4) == b'{"ok":true}'


def registered_tunnel_url(line: str) -> str:
    if not line.startswith("{"):
        return ""
    try:
        event = json.loads(line)
        if event["type"] == "run.localhost.tunnel.registered":
            host = event["data"]["address"]
            if TUNNEL_HOST.fullmatch(host):
                return "https://" + host
    except (TypeError, ValueError, KeyError):
        pass
    return ""


def dashboard_url(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return ""


def remote_worker_endpoint(endpoint: str) -> bool:
    parsed = urllib.parse.urlparse(endpoint)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.path.endswith("/backend.php")


def run_remote_worker(endpoint: str) -> int:
    origin = dashboard_url(endpoint)
    LOGS.mkdir(exist_ok=True)
    lock = (LOGS / "launcher.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("Исполнитель уже запущен. Остановите прежний запуск, если хотите сменить режим.")
        return 0
    ADDRESS_FILE.write_text(
        "Адрес студии на сервере:\n" + origin +
        "\n\nЛогин: studio\nПароль: пароль дашборда на сервере.\n"
        "Этот Mac только генерирует. Дашборд работает без туннеля.\n",
        encoding="utf-8")
    worker_log = (LOGS / "worker.log").open("a", encoding="utf-8")
    worker: subprocess.Popen | None = None

    def launch_worker() -> subprocess.Popen:
        say("Запускаю исполнитель Google Flow…")
        env = dict(os.environ)
        env.update(GFLOW_CLI_HOME=str(BASE / "gflow_home"), GFLOW_CLI_FLOW_HOST="flow.google.com",
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        return subprocess.Popen([sys.executable, "worker.py"], cwd=BASE, env=env,
                                stdin=subprocess.DEVNULL, stdout=worker_log,
                                stderr=subprocess.STDOUT, start_new_session=True)

    def on_signal(_signum, _frame):
        STOP.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        worker = launch_worker()
        say("Дашборд на сервере: " + origin)
        say("Этот Mac только исполняет очередь. Ctrl+C — остановить генератор.\n")
        subprocess.run(["open", origin], check=False)
        while not STOP.wait(1):
            if worker.poll() is not None:
                say("Исполнитель остановился; перезапускаю…")
                worker = launch_worker()
    finally:
        say("Останавливаю исполнитель…")
        stop_process(worker)
        worker_log.close()
    return 0


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=8)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stream_lines(process: subprocess.Popen) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        LINES.put((process.pid, line.rstrip()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="Run on this Mac only, without a tunnel")
    args = parser.parse_args()
    if sys.platform != "darwin":
        say("Этот запуск предназначен для macOS.")
        return 1
    if not (BASE / "worker_config.json").is_file():
        say("Сначала выполните ./install.sh и ./login.sh.")
        return 1
    config = json.loads((BASE / "worker_config.json").read_text(encoding="utf-8"))
    endpoint = str(config.get("endpoint") or "").strip()
    if not args.local and remote_worker_endpoint(endpoint):
        return run_remote_worker(endpoint)
    if not args.local and not (BASE / "site/local_data/access.json").is_file():
        say("Для запуска по внешней ссылке задайте пароль студии.")
        return 1
    if endpoint != LOCAL_URL + "/backend.php":
        say("Для локальной студии в worker_config.json нужен endpoint: " + LOCAL_URL + "/backend.php")
        say("Для дашборда на сервере укажите https://ВАШ-ДОМЕН/backend.php")
        return 1

    LOGS.mkdir(exist_ok=True)
    lock = (LOGS / "launcher.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("Студия уже запущена. Остановите прежний запуск, если хотите сменить режим.")
        return 0
    if local_ready():
        say("Порт 8765 уже занят другим сайтом. Остановите прежний запуск студии и повторите.")
        return 1

    server_log = (LOGS / "site.log").open("a", encoding="utf-8")
    worker_log = (LOGS / "worker.log").open("a", encoding="utf-8")
    server: subprocess.Popen | None = None
    worker: subprocess.Popen | None = None
    tunnel: subprocess.Popen | None = None
    tunnel_url = ""
    pending_url = ""
    tunnel_deadline = 0.0
    last_remote_check = 0.0
    remote_failures = 0

    def launch_server() -> subprocess.Popen:
        say("Запускаю сайт…")
        return subprocess.Popen(
            [sys.executable, "site/local_server.py"],
            cwd=BASE, stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True)

    def launch_worker() -> subprocess.Popen:
        say("Запускаю исполнитель Google Flow…")
        env = dict(os.environ)
        env.update(GFLOW_CLI_HOME=str(BASE / "gflow_home"), GFLOW_CLI_FLOW_HOST="flow.google.com",
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        return subprocess.Popen([sys.executable, "worker.py"], cwd=BASE, env=env,
                                stdin=subprocess.DEVNULL, stdout=worker_log,
                                stderr=subprocess.STDOUT, start_new_session=True)

    def launch_tunnel() -> subprocess.Popen:
        nonlocal tunnel_deadline, tunnel_url, pending_url, remote_failures
        tunnel_url = pending_url = ""
        remote_failures = 0
        tunnel_deadline = time.monotonic() + TUNNEL_START_TIMEOUT
        ADDRESS_FILE.write_text("Туннель подключается. Публичной ссылки пока нет.\n", encoding="utf-8")
        say("Создаю адрес для других компьютеров…")
        process = subprocess.Popen(
            ["/bin/bash", str(BASE / "share_site.sh"), "--output", "json"],
            cwd=BASE, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, start_new_session=True)
        threading.Thread(target=stream_lines, args=(process,), daemon=True).start()
        return process

    def on_signal(_signum, _frame):
        STOP.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        server = launch_server()
        for _ in range(20):
            if STOP.wait(0.5):
                return 0
            if local_ready():
                break
        if not local_ready():
            say("Сайт не запустился. Смотрите studio_logs/site.log")
            return 1
        worker = launch_worker()
        if args.local:
            say("Сайт доступен только на этом Mac: " + DASHBOARD_URL)
            say("Туннель не запущен. Ctrl+C — остановить сайт и генератор.\n")
        else:
            tunnel = launch_tunnel()
        subprocess.run(["open", DASHBOARD_URL], check=False)
        while not STOP.wait(1):
            if server.poll() is not None:
                say("Сайт остановился; перезапускаю…")
                server = launch_server()
                for _ in range(20):
                    if STOP.wait(0.5) or local_ready():
                        break
            if worker.poll() is not None:
                say("Исполнитель остановился; перезапускаю…")
                worker = launch_worker()
            while not LINES.empty():
                source_pid, line = LINES.get_nowait()
                if tunnel is None or source_pid != tunnel.pid:
                    continue
                address = registered_tunnel_url(line)
                if address:
                    pending_url = address
                elif any(word in line.lower() for word in ("error", "permission denied", "ssh:")):
                    say("Туннель: " + line)
            if pending_url and tunnel is not None and tunnel.poll() is None and remote_ready(pending_url):
                tunnel_url, pending_url = pending_url, ""
                last_remote_check = time.monotonic()
                ADDRESS_FILE.write_text(
                    "Адрес студии для других компьютеров:\n" + tunnel_url +
                    "\n\nЛогин: studio\nПароль: тот, который вы задали для студии.\n",
                    encoding="utf-8")
                say("\nАдрес студии: " + tunnel_url)
                say("Сохранил ссылку в АДРЕС_СТУДИИ.txt")
                say("Логин: studio. Пароль: пароль студии.")
                say("Mac и это окно Terminal должны оставаться включёнными. Ctrl+C — остановить.\n")
            if tunnel is not None and not tunnel_url and (
                tunnel.poll() is not None or time.monotonic() >= tunnel_deadline
            ):
                stop_process(tunnel)
                tunnel = None
                message = "Внешняя ссылка недоступна. Локальная студия работает: " + DASHBOARD_URL
                ADDRESS_FILE.write_text(message + "\n", encoding="utf-8")
                say(message)
                say("Чтобы повторить подключение, запустите студию по ссылке заново.")
            if tunnel is not None and tunnel_url and tunnel.poll() is not None:
                say("Туннель остановился; создаю новый адрес…")
                tunnel_url = ""
                tunnel = launch_tunnel()
            if not args.local and tunnel_url and time.monotonic() - last_remote_check >= 45:
                last_remote_check = time.monotonic()
                if remote_ready(tunnel_url):
                    remote_failures = 0
                else:
                    remote_failures += 1
                    if remote_failures >= 3:
                        say("Публичная ссылка недоступна; обновляю туннель…")
                        stop_process(tunnel)
                        tunnel = launch_tunnel()
                        tunnel_url = ""
                        remote_failures = 0
    finally:
        say("Останавливаю студию…")
        stop_process(tunnel)
        stop_process(worker)
        stop_process(server)
        server_log.close()
        worker_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
