"""Проверка и установка обновлений из релизов GitHub.

Сервер умеет ставиться двумя способами, и обновляется он по-разному:

* как инструмент uv (``uv tool install``) — переустановкой из git;
* из локальной копии репозитория (``uv run --directory``) — через ``git pull``.

Способ определяется автоматически по тому, откуда запущен пакет.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from . import config

GITHUB_API = "https://api.github.com"

#: Последний результат проверки — его подмешивает sdo_auth_status.
_last_check: dict[str, Any] | None = None


def current_version() -> str:
    try:
        from importlib.metadata import version

        return version("mirea-sdo-mcp")
    except Exception:
        from . import __version__

        return __version__


def _as_tuple(value: str) -> tuple[int, ...]:
    """``v1.2.3`` → ``(1, 2, 3)``. Нечисловые хвосты отбрасываются."""
    numbers = re.findall(r"\d+", value or "")
    return tuple(int(n) for n in numbers[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return _as_tuple(latest) > _as_tuple(current)


# --- проверка ----------------------------------------------------------


def _read_cache() -> dict[str, Any] | None:
    try:
        raw = json.loads(config.UPDATE_CACHE_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - raw.get("checked_at", 0) > config.UPDATE_CHECK_TTL:
        return None
    return raw


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        config.ensure_state_dir()
        config.UPDATE_CACHE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False), "utf-8"
        )
    except OSError:
        pass


async def check_for_update(force: bool = False) -> dict[str, Any]:
    """Спрашивает у GitHub последний релиз. Результат кэшируется на 6 часов."""
    global _last_check

    current = current_version()
    if not force:
        cached = _read_cache()
        if cached:
            cached["current_version"] = current
            cached["update_available"] = is_newer(cached.get("latest_version", ""), current)
            cached["from_cache"] = True
            _last_check = cached
            return cached

    url = f"{GITHUB_API}/repos/{config.UPDATE_REPO}/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
            resp = await http.get(url, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        return {
            "current_version": current,
            "update_available": False,
            "checked": False,
            "message": f"Не удалось связаться с GitHub: {exc}",
        }

    if resp.status_code == 404:
        result = {
            "current_version": current,
            "latest_version": None,
            "update_available": False,
            "checked": True,
            "message": (
                f"У репозитория {config.UPDATE_REPO} ещё нет опубликованных релизов."
            ),
        }
        _last_check = result
        return result

    if resp.status_code != 200:
        return {
            "current_version": current,
            "update_available": False,
            "checked": False,
            "message": f"GitHub ответил {resp.status_code}.",
        }

    release = resp.json()
    latest = str(release.get("tag_name") or "").lstrip("vV")
    result = {
        "current_version": current,
        "latest_version": latest,
        "update_available": is_newer(latest, current),
        "checked": True,
        "checked_at": time.time(),
        "published_at": release.get("published_at"),
        "release_url": release.get("html_url"),
        "release_notes": (release.get("body") or "")[:1000],
        "repo": config.UPDATE_REPO,
        "from_cache": False,
    }
    _write_cache(result)
    _last_check = result
    return result


def last_check() -> dict[str, Any] | None:
    return _last_check


async def check_in_background() -> None:
    """Тихая проверка при старте: молчит при любой ошибке."""
    if not config.UPDATE_CHECK_ENABLED:
        return
    try:
        await check_for_update()
    except Exception:
        pass


# --- установка ---------------------------------------------------------


def install_kind() -> tuple[str, Path | None]:
    """Определяет, как сервер установлен: ``uv-tool``, ``git`` или ``unknown``."""
    package_dir = Path(__file__).resolve().parent

    if "uv/tools" in sys.prefix.replace("\\", "/"):
        return "uv-tool", None

    root = package_dir.parent.parent  # src/mirea_sdo_mcp -> src -> корень
    if (root / ".git").exists():
        return "git", root
    return "unknown", root


def _run(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=600
        )
    except FileNotFoundError:
        return 127, f"Команда не найдена: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, "Команда не уложилась в 10 минут."
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()[-3000:]


async def apply_update(ref: str | None = None) -> dict[str, Any]:
    """Ставит свежую версию тем же способом, каким сервер установлен."""
    before = current_version()
    kind, root = install_kind()
    source = f"git+https://github.com/{config.UPDATE_REPO}"
    if ref:
        source += f"@{ref}"

    if kind == "uv-tool":
        steps = [["uv", "tool", "install", "--force", "--from", source, "mirea-sdo-mcp"]]
        cwd = None
    elif kind == "git":
        steps = [["git", "pull", "--ff-only"], ["uv", "sync"]]
        cwd = root
    else:
        return {
            "ok": False,
            "install_kind": kind,
            "message": (
                "Не удалось понять, как сервер установлен. Обнови вручную: "
                f"uv tool install --force --from {source} mirea-sdo-mcp"
            ),
        }

    log: list[dict[str, Any]] = []
    for step in steps:
        code, output = await asyncio.to_thread(_run, step, cwd)
        log.append({"command": " ".join(step), "exit_code": code, "output": output[-800:]})
        if code != 0:
            return {
                "ok": False,
                "install_kind": kind,
                "version_before": before,
                "steps": log,
                "message": f"Шаг «{' '.join(step)}» завершился с кодом {code}.",
            }

    config.UPDATE_CACHE_FILE.unlink(missing_ok=True)
    return {
        "ok": True,
        "install_kind": kind,
        "version_before": before,
        "steps": log,
        "message": (
            "Обновление установлено. Чтобы новая версия заработала, "
            "перезапусти MCP-сервер (в Claude Code — /mcp restart или перезапуск клиента)."
        ),
    }
