"""Safe best-effort Git updater executed before the bot imports its modules."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


UPDATED = "updated"
UP_TO_DATE = "up_to_date"
SKIPPED = "skipped"
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
DEFAULT_TIMEOUT_SECONDS = 25
MAX_LOG_BYTES = 256_000


def _enabled() -> bool:
    return os.getenv("BOT_AUTO_UPDATE", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _sanitize(text: str) -> str:
    text = re.sub(r"https://[^/@\s]+@", "https://***@", text or "")
    return " ".join(text.strip().split())[:1_000]


def _write_log(project_dir: Path, message: str):
    try:
        log_dir = project_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "updater.log"
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            with path.open("rb") as file:
                file.seek(-MAX_LOG_BYTES // 2, os.SEEK_END)
                tail = file.read()
            with path.open("wb") as file:
                file.write(tail)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as file:
            file.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _git(project_dir: Path, *args: str, timeout: int = DEFAULT_TIMEOUT_SECONDS):
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    github_token = environment.get("GITHUB_TOKEN", "").strip()
    if github_token:
        basic = base64.b64encode(f"x-access-token:{github_token}".encode("utf-8")).decode("ascii")
        environment["GIT_CONFIG_COUNT"] = "1"
        environment["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        environment["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    command = [
        "git",
        "-c",
        f"safe.directory={project_dir}",
        "-C",
        str(project_dir),
        *args,
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=environment,
        check=False,
    )


def _failure(project_dir: Path, stage: str, result=None) -> str:
    detail = ""
    if result is not None:
        detail = _sanitize(result.stderr or result.stdout)
    suffix = f": {detail}" if detail else ""
    _write_log(project_dir, f"Обновление пропущено ({stage}){suffix}")
    return SKIPPED


def check_and_update(
    project_dir: str | os.PathLike | None = None,
    *,
    remote: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
) -> str:
    """Fetch and fast-forward a clean Git checkout.

    Runtime files are ignored by Git and are never touched.  Any tracked local
    edit, missing credentials, divergent history or network failure makes the
    updater keep the installed version instead of forcing a reset.
    """
    project = Path(project_dir or Path(__file__).resolve().parent).resolve()

    if not _enabled():
        _write_log(project, "Автообновление отключено через BOT_AUTO_UPDATE")
        return SKIPPED
    if shutil.which("git") is None:
        return _failure(project, "Git не установлен")
    if not (project / ".git").exists():
        return _failure(project, "папка не является Git-клоном")

    try:
        head = _git(project, "rev-parse", "HEAD")
        if head.returncode != 0:
            return _failure(project, "не удалось прочитать текущую версию", head)

        dirty = _git(project, "status", "--porcelain", "--untracked-files=no")
        if dirty.returncode != 0:
            return _failure(project, "не удалось проверить рабочую папку", dirty)
        if dirty.stdout.strip():
            return _failure(project, "есть ручные изменения в файлах кода")

        fetched = _git(project, "fetch", "--quiet", remote, branch)
        if fetched.returncode != 0:
            return _failure(project, "GitHub недоступен или нет авторизации", fetched)

        target_ref = f"refs/remotes/{remote}/{branch}"
        target = _git(project, "rev-parse", target_ref)
        if target.returncode != 0:
            return _failure(project, f"не найдена ветка {remote}/{branch}", target)
        if head.stdout.strip() == target.stdout.strip():
            _write_log(project, f"Версия актуальна: {head.stdout.strip()[:12]}")
            return UP_TO_DATE

        ancestor = _git(project, "merge-base", "--is-ancestor", "HEAD", target_ref)
        if ancestor.returncode != 0:
            return _failure(project, "ветки разошлись; автоматический reset запрещён")

        merged = _git(project, "merge", "--ff-only", "--quiet", target_ref)
        if merged.returncode != 0:
            return _failure(project, "не удалось применить fast-forward", merged)

        _write_log(
            project,
            f"Обновлено {head.stdout.strip()[:12]} -> {target.stdout.strip()[:12]}; перезапуск",
        )
        return UPDATED
    except subprocess.TimeoutExpired:
        return _failure(project, "истёк тайм-аут проверки GitHub")
    except OSError as error:
        return _failure(project, f"системная ошибка: {_sanitize(str(error))}")


def restart_current_process():
    """Replace the bootstrap process so all imported modules use the new files."""
    os.execv(sys.executable, [sys.executable, *sys.argv])
    raise RuntimeError("Не удалось перезапустить процесс после обновления")
