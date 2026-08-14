"""Windows process control used by the small graphical bot launcher."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path


WEB_PORT = 8080
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class ControlError(RuntimeError):
    pass


def _run_hidden(args, *, timeout=15):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def listener_pid(port=WEB_PORT) -> int | None:
    command = (
        f"Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
        "-ErrorAction SilentlyContinue | Select-Object -First 1 "
        "-ExpandProperty OwningProcess"
    )
    result = _run_hidden(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return int(result.stdout.strip().splitlines()[-1])
    except ValueError:
        return None


def process_info(pid: int) -> dict:
    command = (
        f"Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    result = _run_hidden(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def is_expected_bot_process(info: dict) -> bool:
    name = str(info.get("Name") or "").lower()
    command_line = str(info.get("CommandLine") or "").lower()
    return name in {"python.exe", "pythonw.exe"} and "main.py" in command_line


def dashboard_is_ready(port=WEB_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.25):
            return True
    except OSError:
        return False


def wait_for_port(*, running: bool, timeout=30, port=WEB_PORT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dashboard_is_ready(port) is running:
            return True
        time.sleep(0.25)
    return dashboard_is_ready(port) is running


def stop_bot(*, port=WEB_PORT) -> str:
    pid = listener_pid(port)
    if pid is None:
        return "Бот уже остановлен"
    info = process_info(pid)
    if not is_expected_bot_process(info):
        raise ControlError(
            f"Порт {port} занят посторонним процессом PID {pid}. Он не был остановлен."
        )
    result = _run_hidden(["taskkill.exe", "/PID", str(pid), "/T", "/F"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ControlError(f"Не удалось остановить бота PID {pid}: {detail}")
    if not wait_for_port(running=False, timeout=15, port=port):
        raise ControlError(f"Процесс завершён, но порт {port} не освободился")
    return f"Бот остановлен (PID {pid})"


def start_bot(project_dir: str | os.PathLike, *, port=WEB_PORT) -> str:
    project = Path(project_dir).resolve()
    existing = listener_pid(port)
    if existing is not None:
        info = process_info(existing)
        if is_expected_bot_process(info):
            return f"Бот уже запущен (PID {existing})"
        raise ControlError(
            f"Порт {port} занят посторонним процессом PID {existing}. Запуск отменён."
        )

    pythonw = project / ".venv" / "Scripts" / "pythonw.exe"
    main = project / "main.py"
    if not pythonw.is_file():
        raise ControlError(f"Не найден Python: {pythonw}")
    if not main.is_file():
        raise ControlError(f"Не найден файл запуска: {main}")

    creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(pythonw), "-u", "main.py"],
        cwd=str(project),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    if not wait_for_port(running=True, timeout=35, port=port):
        raise ControlError("Бот запущен, но сайт не открыл порт 8080 за 35 секунд")
    pid = listener_pid(port)
    return f"Бот запущен{f' (PID {pid})' if pid else ''}"


def restart_bot(project_dir: str | os.PathLike, *, port=WEB_PORT) -> str:
    stop_bot(port=port)
    return start_bot(project_dir, port=port)
