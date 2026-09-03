"""Получение и хранение сессии СДО.

Вход в СДО возможен только через SSO РТУ МИРЭА (Keycloak, ``sso.mirea.ru``) —
локальной формы логина у Moodle тут нет. Поэтому сервер открывает настоящее
окно браузера, где пользователь сам вводит свои учётные данные и код из почты,
и забирает только готовую куку ``MoodleSession``. Логин и пароль через сервер
никогда не проходят.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

from . import config


class AuthError(RuntimeError):
    """Не удалось получить или использовать сессию."""


class SessionExpired(AuthError):
    """Сохранённая кука больше не действует — нужен повторный вход."""


@dataclass(slots=True)
class Session:
    cookie: str
    saved_at: str
    user_id: int | None = None
    user_name: str | None = None

    @property
    def age_seconds(self) -> float:
        try:
            saved = datetime.fromisoformat(self.saved_at)
        except ValueError:
            return float("inf")
        return (datetime.now(timezone.utc) - saved).total_seconds()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_session() -> Session | None:
    """Отдаёт сессию: кука из окружения имеет приоритет над сохранённой."""
    if config.ENV_SESSION:
        return Session(cookie=config.ENV_SESSION, saved_at=_now_iso())
    if not config.SESSION_FILE.exists():
        return None
    try:
        raw: dict[str, Any] = json.loads(config.SESSION_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cookie = raw.get("cookie")
    if not cookie:
        return None
    return Session(
        cookie=cookie,
        saved_at=raw.get("saved_at") or _now_iso(),
        user_id=raw.get("user_id"),
        user_name=raw.get("user_name"),
    )


def save_session(session: Session) -> None:
    config.ensure_state_dir()
    config.SESSION_FILE.write_text(
        json.dumps(asdict(session), ensure_ascii=False, indent=2), "utf-8"
    )
    config.SESSION_FILE.chmod(0o600)


def clear_session() -> None:
    config.SESSION_FILE.unlink(missing_ok=True)


_LOGGED_IN_PROBE = """() => {
  const b = document.body;
  if (!b) return null;
  if (b.classList.contains('notloggedin')) return null;
  if (!(window.M && M.cfg && M.cfg.sesskey)) return null;
  const name = document.querySelector('.usertext, .userbutton .usertext, [data-region="user-menu-toggle"] .usertext');
  return {userId: M.cfg.userId, sesskey: M.cfg.sesskey, name: name ? name.textContent.trim() : null};
}"""


async def login_interactive(timeout_sec: float = 300.0) -> Session:
    """Открывает окно браузера и ждёт, пока пользователь войдёт через SSO.

    Профиль браузера сохраняется между запусками, поэтому Keycloak обычно
    помнит пользователя и повторный вход проходит в один клик.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise AuthError(
            "Playwright не установлен. Выполни: uv add playwright && uv run playwright install chromium"
        ) from exc

    config.ensure_state_dir()
    config.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:

        async def launch() -> Any:
            return await pw.chromium.launch_persistent_context(
                user_data_dir=str(config.BROWSER_PROFILE_DIR),
                headless=False,
                viewport={"width": 1280, "height": 900},
                user_agent=config.USER_AGENT,
                args=["--no-first-run", "--no-default-browser-check"],
            )

        try:
            ctx = await launch()
        except Exception as exc:
            if not _looks_like_missing_browser(exc):
                raise AuthError(f"Не удалось запустить Chromium: {exc}") from exc
            # Первый запуск после установки: браузера ещё нет, качаем сами.
            await asyncio.to_thread(_install_chromium)
            try:
                ctx = await launch()
            except Exception as retry_exc:
                raise AuthError(
                    "Не удалось запустить Chromium даже после установки. "
                    "Попробуй вручную: uv tool run --from mirea-sdo-mcp playwright install chromium\n"
                    f"Ошибка: {retry_exc}"
                ) from retry_exc

        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")

            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                if not ctx.pages:
                    raise AuthError("Окно браузера закрыли до завершения входа.")

                info = await _probe(ctx)
                if info is not None:
                    cookie = _extract_cookie(await ctx.cookies())
                    if cookie:
                        session = Session(
                            cookie=cookie,
                            saved_at=_now_iso(),
                            user_id=info.get("userId"),
                            user_name=info.get("name"),
                        )
                        save_session(session)
                        return session

                await asyncio.sleep(1.5)

            raise AuthError(
                f"Вход не завершён за {timeout_sec:.0f} с. Запусти sdo_login ещё раз "
                "и при необходимости увеличь timeout_sec."
            )
        finally:
            try:
                await ctx.close()
            except Exception:  # pragma: no cover - окно могли уже закрыть
                pass


def _looks_like_missing_browser(exc: Exception) -> bool:
    text = str(exc).lower()
    return "executable doesn" in text or "playwright install" in text


def _install_chromium() -> None:
    """Скачивает Chromium в то же окружение, откуда запущен сервер."""
    proc = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-500:]
        raise AuthError(
            "Не удалось скачать Chromium для входа в СДО.\n"
            f"Команда: {sys.executable} -m playwright install chromium\n{tail}"
        )


async def _probe(ctx: Any) -> dict[str, Any] | None:
    """Ищет среди вкладок ту, где Moodle уже считает нас залогиненными."""
    for page in list(ctx.pages):
        try:
            if config.BASE_URL not in page.url:
                continue
            info = await page.evaluate(_LOGGED_IN_PROBE)
        except Exception:
            # Страница в этот момент переходит по редиректу — просто пробуем позже.
            continue
        if info:
            return info
    return None


def _extract_cookie(cookies: list[dict[str, Any]]) -> str | None:
    host = config.BASE_URL.split("://", 1)[-1]
    for c in cookies:
        if c.get("name") != config.SESSION_COOKIE:
            continue
        domain = str(c.get("domain", "")).lstrip(".")
        if domain and domain not in host and host not in domain:
            continue
        return str(c["value"])
    return None
