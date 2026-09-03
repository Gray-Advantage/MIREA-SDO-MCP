"""MCP-сервер СДО РТУ МИРЭА.

Все инструменты работают только на чтение: ничего не отправляют, не сдают
и не меняют на стороне СДО.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import auth, config, reader, updater
from .client import SdoClient, SdoError
from .moodle import assignments, courses, files, forums, grades, modules, quizzes
from .moodle.index import index

# По stdio идёт протокол MCP, поэтому болтливость HTTP-клиента здесь лишняя.
for _noisy in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

@contextlib.asynccontextmanager
async def lifespan(_server: "MCPServer") -> AsyncIterator[None]:
    """При старте тихо спрашиваем GitHub, нет ли новой версии."""
    task = asyncio.create_task(updater.check_in_background())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


mcp = MCPServer(
    "mirea-sdo",
    version=updater.current_version(),
    lifespan=lifespan,
    instructions=(
        "Доступ к СДО РТУ МИРЭА (Moodle) только на чтение: дисциплины, их "
        "структура, файлы, задания, оценки, тесты и форумы. Ничего не "
        "отправляет и не сдаёт. Если инструмент вернул error=session_expired, "
        "вызови sdo_login — откроется окно браузера для входа через SSO. "
        "Раз в сессию полезно вызвать sdo_check_updates; если он говорит "
        "update_available, предложи пользователю sdo_update."
    ),
)
client = SdoClient()


def tool(
    fn: Callable[..., Awaitable[Any]] | None = None, *, writes: bool = False
) -> Any:
    """Регистрирует инструмент и превращает наши исключения в понятный текст.

    ``writes=True`` — инструмент пишет на локальный диск (скачивание, вход).
    На стороне СДО не меняет ничего ни один из них.
    """

    def decorate(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except auth.SessionExpired as exc:
                return {"error": "session_expired", "message": str(exc)}
            except SdoError as exc:
                return {"error": "sdo_error", "message": str(exc)}
            except Exception as exc:  # pragma: no cover - страховка
                return {"error": type(exc).__name__, "message": str(exc)}

        annotations = ToolAnnotations(
            read_only_hint=not writes, destructive_hint=False, open_world_hint=True
        )
        return mcp.tool(annotations=annotations)(wrapper)

    return decorate if fn is None else decorate(fn)


# --- сессия ------------------------------------------------------------


@tool(writes=True)
async def sdo_login(timeout_sec: float = 300) -> dict[str, Any]:
    """Войти в СДО: откроется окно браузера для входа через SSO РТУ МИРЭА.

    Учётные данные вводятся прямо в браузере и через сервер не проходят —
    сохраняется только кука сессии. Профиль браузера сохраняется, поэтому
    повторный вход обычно проходит в один клик, без кода из почты.
    """
    session = await auth.login_interactive(timeout_sec)
    client.invalidate()
    return {
        "ok": True,
        "user_id": session.user_id,
        "user_name": session.user_name,
        "saved_to": str(config.SESSION_FILE),
        "message": "Вход выполнен, сессия сохранена.",
    }


@tool
async def sdo_auth_status() -> dict[str, Any]:
    """Проверить, жива ли сессия СДО, и под кем мы вошли."""
    session = auth.load_session()
    if session is None:
        return {
            "logged_in": False,
            "message": "Сессия не сохранена. Запусти sdo_login.",
        }
    sesskey = await client.get_sesskey(refresh=True)
    course_list = await courses.list_courses(client)
    status: dict[str, Any] = {
        "logged_in": True,
        "user_id": session.user_id,
        "user_name": session.user_name,
        "session_age_hours": round(session.age_seconds / 3600, 1),
        "sesskey_ok": bool(sesskey),
        "course_count": len(course_list),
        "base_url": config.BASE_URL,
        "version": updater.current_version(),
    }
    check = updater.last_check()
    if check and check.get("update_available"):
        status["update_available"] = True
        status["latest_version"] = check.get("latest_version")
        status["update_hint"] = "Доступна новая версия — вызови sdo_update."
    return status


@tool(writes=True)
async def sdo_logout() -> dict[str, Any]:
    """Забыть сохранённую сессию локально (в СДО ничего не делает)."""
    auth.clear_session()
    client.invalidate()
    return {"ok": True, "message": "Локальная сессия удалена."}


# --- обновления --------------------------------------------------------


@tool
async def sdo_check_updates(force: bool = False) -> dict[str, Any]:
    """Проверить, есть ли новая версия сервера в релизах GitHub.

    Результат кэшируется на 6 часов; force=True спрашивает GitHub заново.
    Полезно вызывать раз в сессию: если update_available, предложи sdo_update.
    """
    return await updater.check_for_update(force)


@tool(writes=True)
async def sdo_update(ref: str | None = None) -> dict[str, Any]:
    """Установить свежую версию сервера из GitHub.

    Способ определяется сам: инструмент uv переустанавливается из git,
    локальная копия репозитория обновляется через git pull. После установки
    сервер нужно перезапустить. ref — конкретный тег или ветка, если нужно.
    """
    return await updater.apply_update(ref)


# --- дисциплины --------------------------------------------------------


@tool
async def list_courses(
    classification: str = "all",
    search: str | None = None,
    include_hidden: bool = True,
) -> list[dict[str, Any]]:
    """Список дисциплин пользователя.

    classification: all, inprogress, future, past, favourites, hidden.
    Скрытые (убранные с главной страницы) курсы по умолчанию включены и
    помечены полем hidden.
    """
    return await courses.list_courses(client, classification, search, include_hidden)


@tool
async def get_course(course_id: int) -> dict[str, Any]:
    """Структура дисциплины: секции по порядку и элементы внутри них."""
    return await courses.get_course_tree(client, course_id)


@tool
async def search_activities(
    query: str, course_id: int | None = None, module_type: str | None = None
) -> list[dict[str, Any]]:
    """Найти элементы курсов по части названия.

    module_type ограничивает тип: folder, resource, assign, quiz, forum, page, url.
    """
    return await courses.search_activities(client, query, course_id, module_type)


@tool
async def refresh_index() -> dict[str, Any]:
    """Перечитать список курсов и их элементов, если в СДО что-то изменилось."""
    await index.build(client, force=True)
    files.clear_file_cache()
    entries = await index.all(client)
    return {"ok": True, "indexed_activities": len(entries)}


# --- содержимое --------------------------------------------------------


@tool
async def get_activity(cmid: int) -> dict[str, Any]:
    """Содержимое элемента курса по его cmid. Тип определяется автоматически."""
    return await modules.get_activity(client, cmid)


@tool
async def list_folder(cmid: int) -> dict[str, Any]:
    """Файлы внутри элемента «Папка», включая вложенные подпапки."""
    return await files.list_folder(client, cmid)


@tool
async def list_course_files(
    course_id: int, pattern: str | None = None, refresh: bool = False
) -> dict[str, Any]:
    """Все файлы дисциплины: из папок, файлов-ресурсов и вложений к заданиям.

    pattern — маска вида ``*.pdf`` либо регулярное выражение с префиксом ``re:``.
    """
    return await files.list_course_files(client, course_id, pattern, refresh)


@tool
async def search_files(pattern: str, course_id: int | None = None) -> dict[str, Any]:
    """Поиск файлов по маске во всех дисциплинах (или в одной).

    Примеры: ``*.pdf``, ``Лекция*``, ``re:практич.*\\.docx``.
    """
    found = await files.search_files(client, pattern, course_id)
    return {"pattern": pattern, "file_count": len(found), "files": found}


# --- файлы -------------------------------------------------------------


@tool
async def read_file(
    url: str | None = None,
    path: str | None = None,
    pages: str | None = None,
    max_chars: int = 20000,
) -> dict[str, Any]:
    """Прочитать файл постранично, не скачивая его в папку загрузок.

    Источник — ссылка в СДО (url) либо уже скачанный файл (path).
    pages: ``3``, ``1-5``, ``1,4,7-9``; без указания — первые пять страниц.
    Поддерживаются pdf, docx, pptx, xlsx и текстовые форматы. У pdf страницы
    настоящие, у презентации это слайды, у таблицы — листы, у остального —
    куски примерно по 3000 символов.
    """
    return await reader.read_file(client, url, path, pages, max_chars)


@tool(writes=True)
async def download_file(
    url: str, dest: str | None = None, name: str | None = None
) -> dict[str, Any]:
    """Скачать один файл по ссылке из СДО."""
    return await files.download_file(client, url, dest, name)


@tool(writes=True)
async def download_folder(
    cmid: int, dest: str | None = None, pattern: str | None = None
) -> dict[str, Any]:
    """Скачать всю папку целиком, сохранив вложенную структуру."""
    return await files.download_folder(client, cmid, dest, pattern)


@tool(writes=True)
async def download_course(
    course_id: int, dest: str | None = None, pattern: str | None = None
) -> dict[str, Any]:
    """Скачать все материалы дисциплины, разложив их по секциям и элементам."""
    return await files.download_course(client, course_id, dest, pattern)


# --- задания и сроки ---------------------------------------------------


@tool
async def list_assignments(course_id: int | None = None) -> list[dict[str, Any]]:
    """Задания со сроками и состоянием ответа. Без course_id — по всем курсам."""
    if course_id is None:
        return await assignments.list_all_assignments(client)
    return await assignments.list_assignments(client, course_id)


@tool
async def get_assignment(cmid: int) -> dict[str, Any]:
    """Карточка задания: условие, состояние ответа, приложенные файлы."""
    return await assignments.get_assignment(client, cmid)


@tool
async def get_deadlines(days: int = 30, limit: int = 50) -> list[dict[str, Any]]:
    """Ближайшие дедлайны по всем дисциплинам на указанное число дней вперёд."""
    return await assignments.get_deadlines(client, days, limit)


# --- оценки ------------------------------------------------------------


@tool
async def get_grades_overview() -> list[dict[str, Any]]:
    """Итоговая оценка по каждой дисциплине."""
    return await grades.get_grades_overview(client)


@tool
async def get_course_grades(course_id: int) -> dict[str, Any]:
    """Постатейный отчёт по оценкам одной дисциплины."""
    return await grades.get_course_grades(client, course_id)


# --- тесты и форумы ----------------------------------------------------


@tool
async def list_quizzes(course_id: int) -> list[dict[str, Any]]:
    """Тесты дисциплины со сроками закрытия и оценками."""
    return await quizzes.list_quizzes(client, course_id)


@tool
async def get_quiz(cmid: int) -> dict[str, Any]:
    """Условия теста и попытки. Попытку не начинает — только читает страницу."""
    return await quizzes.get_quiz(client, cmid)


@tool
async def list_forum_discussions(cmid: int) -> dict[str, Any]:
    """Темы форума или объявлений."""
    return await forums.list_discussions(client, cmid)


@tool
async def get_forum_discussion(discussion_id: int) -> dict[str, Any]:
    """Сообщения одной темы форума."""
    return await forums.get_discussion(client, discussion_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
