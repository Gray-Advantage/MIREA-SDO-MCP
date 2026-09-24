"""HTTP-слой: одна кука на все запросы, добыча sesskey, вежливый темп."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import config
from .auth import Session, SessionExpired, load_session

_SESSKEY_RE = re.compile(r'"sesskey"\s*:\s*"([^"]+)"')

#: Сеть до СДО иногда моргает, поэтому короткие повторы вместо падения.
_RETRIES = 3
_BACKOFF = 0.8
_RETRY_STATUSES = {429, 500, 502, 503, 504}

_RELOGIN_HINT = (
    "Сессия СДО недействительна или истекла. Запусти инструмент sdo_login — "
    "откроется окно браузера для входа через SSO РТУ МИРЭА."
)


class SdoError(RuntimeError):
    """Ошибка обращения к СДО."""


class SdoClient:
    """Асинхронный клиент СДО. Живёт всё время работы сервера."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._session: Session | None = None
        self._sesskey: str | None = None
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    # --- жизненный цикл -------------------------------------------------

    async def _ensure(self) -> httpx.AsyncClient:
        async with self._lock:
            stored = load_session()
            if stored is None:
                raise SessionExpired(
                    "Сессия СДО не сохранена. Запусти инструмент sdo_login — "
                    "откроется окно браузера для входа через SSO РТУ МИРЭА."
                )
            if self._http is None or (self._session and self._session.cookie != stored.cookie):
                if self._http is not None:
                    await self._http.aclose()
                self._session = stored
                self._sesskey = None
                self._http = httpx.AsyncClient(
                    base_url=config.BASE_URL,
                    timeout=config.HTTP_TIMEOUT,
                    follow_redirects=True,
                    headers={"User-Agent": config.USER_AGENT},
                    cookies={config.SESSION_COOKIE: stored.cookie},
                )
            return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def invalidate(self) -> None:
        """Сбрасывает кэш sesskey — например, после смены сессии."""
        self._sesskey = None

    @property
    def session(self) -> Session | None:
        return self._session

    # --- низкий уровень -------------------------------------------------

    async def _throttle(self) -> None:
        delay = config.REQUEST_DELAY
        if delay <= 0:
            return
        gap = time.monotonic() - self._last_request
        if gap < delay:
            await asyncio.sleep(delay - gap)
        self._last_request = time.monotonic()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Запрос к СДО с повтором при разрыве связи или таймауте."""
        http = await self._ensure()
        last: Exception | None = None

        for attempt in range(_RETRIES):
            await self._throttle()
            try:
                resp = await http.request(method, url, **kwargs)
            except httpx.TooManyRedirects as exc:
                # Протухшая кука заставляет /login/index.php редиректить сам на себя.
                raise SessionExpired(_RELOGIN_HINT) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt + 1 < _RETRIES:
                    await asyncio.sleep(_BACKOFF * (attempt + 1))
                    continue
            except httpx.HTTPError as exc:
                raise SdoError(f"Сетевая ошибка при обращении к {url}: {exc}") from exc
            else:
                if resp.status_code in _RETRY_STATUSES and attempt + 1 < _RETRIES:
                    await asyncio.sleep(_BACKOFF * (attempt + 1))
                    continue
                _assert_logged_in(resp)
                return resp

        detail = f"{type(last).__name__}: {last}" if last else "неизвестная причина"
        raise SdoError(
            f"СДО не отвечает: {url} ({_RETRIES} попытки). {detail}"
        ) from last

    async def get_html(self, url: str, **kwargs: Any) -> BeautifulSoup:
        resp = await self.request("GET", url, **kwargs)
        if resp.status_code == 404:
            raise SdoError(f"Страница не найдена: {url}")
        if resp.status_code >= 400:
            raise SdoError(f"СДО ответила {resp.status_code} на {url}")
        return BeautifulSoup(resp.text, "lxml")

    async def get_sesskey(self, *, refresh: bool = False) -> str:
        """sesskey — CSRF-токен Moodle; вытаскиваем его из HTML любой страницы."""
        if self._sesskey and not refresh:
            return self._sesskey
        resp = await self.request("GET", "/my/")
        match = _SESSKEY_RE.search(resp.text)
        if not match:
            raise SessionExpired(_RELOGIN_HINT)
        self._sesskey = match.group(1)
        return self._sesskey

    # --- Moodle AJAX RPC ------------------------------------------------

    async def rpc(self, methodname: str, args: dict[str, Any] | None = None) -> Any:
        """Вызов ``/lib/ajax/service.php``.

        Работает только для функций, помеченных в Moodle как доступные из AJAX.
        Остальное берётся парсингом HTML.
        """
        payload = [{"index": 0, "methodname": methodname, "args": args or {}}]
        for attempt in (0, 1):
            sesskey = await self.get_sesskey(refresh=attempt > 0)
            resp = await self.request(
                "POST",
                "/lib/ajax/service.php",
                params={"sesskey": sesskey, "info": methodname},
                json=payload,
            )
            try:
                body = resp.json()
            except json.JSONDecodeError as exc:
                raise SdoError(f"{methodname}: СДО вернула не JSON") from exc

            if isinstance(body, dict) and body.get("error"):
                if "sesskey" in str(body.get("error", "")).lower() and attempt == 0:
                    continue
                raise SdoError(f"{methodname}: {body.get('error')}")

            if not isinstance(body, list) or not body:
                raise SdoError(f"{methodname}: неожиданный ответ СДО")

            entry = body[0]
            if entry.get("error"):
                exc_info = entry.get("exception") or {}
                code = exc_info.get("errorcode", "")
                if code == "invalidsesskey" and attempt == 0:
                    continue
                message = exc_info.get("message") or entry.get("error")
                raise SdoError(f"{methodname}: {message} ({code})")
            return entry.get("data")
        raise SdoError(f"{methodname}: не удалось выполнить запрос")

    # --- скачивание -----------------------------------------------------

    async def download(
        self, url: str, dest_dir: Path, filename: str | None = None
    ) -> dict[str, Any]:
        """Качает файл потоком в ``dest_dir``.

        Имя берётся из ``filename``, иначе из Content-Disposition, иначе из URL.
        """
        http = await self._ensure()
        await self._throttle()
        dest_dir.mkdir(parents=True, exist_ok=True)

        async with http.stream("GET", url, timeout=config.DOWNLOAD_TIMEOUT) as resp:
            _assert_logged_in(resp)
            resp.raise_for_status()
            name = (
                sanitize_filename(filename)
                if filename
                else _filename_from_headers(resp.headers) or _filename_from_url(url)
            )
            dest = dest_dir / name
            tmp = dest.with_name(dest.name + ".part")
            size = 0
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    fh.write(chunk)
                    size += len(chunk)
            tmp.replace(dest)
            content_type = resp.headers.get("content-type", "")

        return {
            "path": str(dest),
            "name": name,
            "size_bytes": size,
            "content_type": content_type,
            "url": url,
        }


def _assert_logged_in(resp: httpx.Response) -> None:
    final = str(resp.url)
    if "/login/index.php" in final or "sso.mirea.ru" in final:
        raise SessionExpired(_RELOGIN_HINT)


_DISPOSITION_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


def _filename_from_headers(headers: httpx.Headers) -> str | None:
    disposition = headers.get("content-disposition", "")
    match = _DISPOSITION_RE.search(disposition)
    if not match:
        return None
    from urllib.parse import unquote

    return sanitize_filename(unquote(match.group(1)))


def _filename_from_url(url: str) -> str:
    from urllib.parse import unquote

    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "download"
    return sanitize_filename(name)


_BAD_CHARS = re.compile(r'[/\\\0:*?"<>|]')


def sanitize_filename(name: str) -> str:
    """Делает имя безопасным для файловой системы, не теряя кириллицу."""
    cleaned = _BAD_CHARS.sub("_", name).strip().strip(".")
    return cleaned[:200] or "download"


def absolute(url: str) -> str:
    return urljoin(config.BASE_URL + "/", url)
