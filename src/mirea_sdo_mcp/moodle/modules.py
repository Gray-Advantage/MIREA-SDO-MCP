"""Универсальный доступ к содержимому элемента курса по одному cmid."""

from __future__ import annotations

from typing import Any

from ..client import SdoClient
from . import assignments, files, forums, html, quizzes
from .index import index

#: Типы, для которых есть отдельный разбор. Остальное отдаётся текстом страницы.
SPECIALISED = {"folder", "resource", "assign", "quiz", "forum", "page", "url", "label"}


async def get_activity(client: SdoClient, cmid: int) -> dict[str, Any]:
    """Содержимое элемента курса. Тип определяется автоматически."""
    info = await index.get(client, cmid)
    kind = info["type"]

    if kind == "folder":
        return await files.list_folder(client, cmid)
    if kind == "resource":
        detail = await files.resource_info(client, cmid)
        return {
            "cmid": int(cmid),
            "name": info["name"],
            "type": "resource",
            "course_id": info["course_id"],
            "section": info.get("section"),
            "file": detail,
        }
    if kind == "assign":
        return await assignments.get_assignment(client, cmid)
    if kind == "quiz":
        return await quizzes.get_quiz(client, cmid)
    if kind == "forum":
        return await forums.list_discussions(client, cmid)
    if kind in {"page", "label"}:
        return await get_page(client, cmid)
    if kind == "url":
        return await get_url(client, cmid)
    return await get_generic(client, cmid)


async def get_page(client: SdoClient, cmid: int) -> dict[str, Any]:
    """Текст элементов «Страница» и «Пояснение»."""
    info = await index.get(client, cmid)
    kind = info["type"]
    soup = await client.get_html(f"/mod/{kind}/view.php?id={int(cmid)}")
    main = html.main_region(soup)
    if main is None:
        return {"cmid": int(cmid), "name": info["name"], "type": kind, "text": ""}

    clean = html.strip_noise(main)
    return {
        "cmid": int(cmid),
        "name": info["name"],
        "type": kind,
        "course_id": info["course_id"],
        "section": info.get("section"),
        "url": f"/mod/{kind}/view.php?id={int(cmid)}",
        "text": html.text_of(clean),
        "links": html.links(clean),
        "files": files._pluginfile_links(soup),
    }


async def get_url(client: SdoClient, cmid: int) -> dict[str, Any]:
    """Элемент «Гиперссылка»: внешний адрес, на который он ведёт."""
    info = await index.get(client, cmid)
    soup = await client.get_html(f"/mod/url/view.php?id={int(cmid)}")
    main = html.main_region(soup)
    external = html.links(main, external_only=True) if main is not None else []
    workaround = soup.select_one(".urlworkaround a[href]")
    target = workaround["href"] if workaround else (external[0]["url"] if external else None)
    return {
        "cmid": int(cmid),
        "name": info["name"],
        "type": "url",
        "course_id": info["course_id"],
        "section": info.get("section"),
        "target_url": target,
        "links": external,
        "text": html.text_of(html.strip_noise(main)) if main is not None else "",
    }


async def get_generic(client: SdoClient, cmid: int) -> dict[str, Any]:
    """Запасной разбор для типов без отдельной поддержки (lanebs, webinars и пр.)."""
    info = await index.get(client, cmid)
    kind = info["type"]
    soup = await client.get_html(f"/mod/{kind}/view.php?id={int(cmid)}")
    main = html.main_region(soup)
    clean = html.strip_noise(main) if main is not None else None
    return {
        "cmid": int(cmid),
        "name": info["name"],
        "type": kind,
        "course_id": info["course_id"],
        "section": info.get("section"),
        "url": f"/mod/{kind}/view.php?id={int(cmid)}",
        "text": html.text_of(clean) if clean is not None else "",
        "links": html.links(clean, external_only=True) if clean is not None else [],
        "files": files._pluginfile_links(soup),
        "note": f"Тип «{kind}» разбирается универсально: текст страницы и ссылки.",
    }
