"""Настройки сервера: адреса СДО и пути к локальному состоянию."""

from __future__ import annotations

import os
from pathlib import Path

BASE_URL = os.environ.get("SDO_BASE_URL", "https://online-edu.mirea.ru").rstrip("/")

#: Точка входа в SSO РТУ МИРЭА (Keycloak). Локальной формы логина у СДО нет.
LOGIN_URL = f"{BASE_URL}/auth/oidc/?source=loginpage"

#: Страница, по которой определяем, что вход состоялся.
DASHBOARD_URL = f"{BASE_URL}/my/"

SESSION_COOKIE = "MoodleSession"

STATE_DIR = Path(os.environ.get("SDO_STATE_DIR", Path.home() / ".mirea-sdo-mcp"))
SESSION_FILE = STATE_DIR / "session.json"
BROWSER_PROFILE_DIR = STATE_DIR / "browser-profile"

#: Куда складывать скачанное, если вызывающий не указал путь явно.
DOWNLOAD_DIR = Path(os.environ.get("SDO_DOWNLOAD_DIR", STATE_DIR / "downloads"))

#: Аварийный фолбэк: кука из окружения перебивает сохранённую сессию.
ENV_SESSION = os.environ.get("MOODLE_SESSION") or None

HTTP_TIMEOUT = float(os.environ.get("SDO_HTTP_TIMEOUT", "30"))
DOWNLOAD_TIMEOUT = float(os.environ.get("SDO_DOWNLOAD_TIMEOUT", "300"))

#: Пауза между запросами, чтобы не долбить СДО (секунды).
REQUEST_DELAY = float(os.environ.get("SDO_REQUEST_DELAY", "0.25"))

USER_AGENT = os.environ.get(
    "SDO_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

#: Кэш для предпросмотра файлов: сюда качаем то, что читаем, но не отдаём пользователю.
CACHE_DIR = STATE_DIR / "cache"

#: Откуда сервер берёт обновления. Формат «владелец/репозиторий».
UPDATE_REPO = os.environ.get("SDO_UPDATE_REPO", "Gray-Advantage/MIREA-SDO-MCP")
UPDATE_CHECK_TTL = float(os.environ.get("SDO_UPDATE_CHECK_TTL", "900"))
UPDATE_CACHE_FILE = STATE_DIR / "update-check.json"

#: Проверку версии при старте можно выключить: SDO_UPDATE_CHECK=0
UPDATE_CHECK_ENABLED = os.environ.get("SDO_UPDATE_CHECK", "1") not in {"0", "false", "no"}

#: Системные браузеры, которые пробуем до скачивания собственного Chromium.
#: Пустое значение SDO_BROWSER_CHANNELS отключает этот путь.
BROWSER_CHANNELS = tuple(
    c.strip()
    for c in os.environ.get("SDO_BROWSER_CHANNELS", "chrome,msedge").split(",")
    if c.strip()
)
