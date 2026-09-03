"""Задания: список по курсу, карточка задания, дедлайны."""

from __future__ import annotations

import re
import time
from typing import Any

from ..client import SdoClient, absolute
from . import courses, html
from .files import _pluginfile_links
from .index import index


async def list_assignments(client: SdoClient, course_id: int) -> list[dict[str, Any]]:
    """Сводка заданий курса со сроками и состоянием ответа."""
    soup = await client.get_html(f"/mod/assign/index.php?id={int(course_id)}")
    return parse_index(soup, int(course_id))


def parse_index(soup: Any, course_id: int) -> list[dict[str, Any]]:
    """Разбирает таблицу /mod/assign/index.php. Без сети — только разметка."""
    table = soup.select_one('div[role="main"] table') or soup.select_one("table")
    if table is None:
        return []

    result: list[dict[str, Any]] = []
    section = ""
    for tr in table.select("tr")[1:]:
        cells = tr.select("th, td")
        if not cells:
            continue
        values = [html.text_of(html.strip_noise(c)).replace("\n", " ").strip() for c in cells]
        if values[0]:
            section = values[0]
        link = tr.select_one('a[href*="view.php"]')
        result.append(
            {
                "cmid": _cmid_from(link["href"]) if link else None,
                "name": values[1] if len(values) > 1 else "",
                "section": section,
                "due": _blank(values[2] if len(values) > 2 else ""),
                "submission": _blank(values[3] if len(values) > 3 else ""),
                "grade": _blank(values[4] if len(values) > 4 else ""),
                "url": absolute(link["href"]) if link else None,
                "course_id": course_id,
            }
        )
    return [r for r in result if r["name"]]


def _cmid_from(href: str) -> int | None:
    match = re.search(r"[?&]id=(\d+)", href)
    return int(match.group(1)) if match else None


def _blank(value: str) -> str | None:
    return None if value in {"", "-", "—"} else value


async def list_all_assignments(client: SdoClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for course in await courses.list_courses(client):
        try:
            out.extend(await list_assignments(client, course["course_id"]))
        except Exception:
            continue
    return out


async def get_assignment(client: SdoClient, cmid: int) -> dict[str, Any]:
    """Карточка задания: условие, состояние ответа, приложенные файлы."""
    info = await index.get(client, cmid)
    soup = await client.get_html(f"/mod/assign/view.php?id={int(cmid)}")
    main = html.main_region(soup)

    status: dict[str, Any] = {}
    table = soup.select_one(".submissionstatustable table") or soup.select_one(
        ".submissionstatustable"
    )
    if table is not None:
        status = html.key_value_table(table)

    description = ""
    for selector in (".activity-description", "#intro", ".activity-header .description"):
        el = soup.select_one(selector)
        if el is not None:
            description = html.text_of(html.strip_noise(el))
            if description:
                break

    return {
        "cmid": int(cmid),
        "name": info["name"],
        "type": "assign",
        "course_id": info["course_id"],
        "course": info.get("course_name"),
        "section": info.get("section"),
        "url": f"/mod/assign/view.php?id={int(cmid)}",
        "description": description,
        "status": status,
        "files": _pluginfile_links(soup),
        "text": html.text_of(html.strip_noise(main)) if main is not None else "",
    }


async def get_deadlines(client: SdoClient, days: int = 30, limit: int = 50) -> list[dict[str, Any]]:
    """Ближайшие дедлайны по всем курсам (лента событий Moodle)."""
    now = int(time.time())
    data = await client.rpc(
        "core_calendar_get_action_events_by_timesort",
        {
            "limitnum": int(limit),
            "timesortfrom": now,
            "timesortto": now + int(days) * 86400,
            "limittononsuspendedevents": True,
        },
    )
    events = data.get("events", []) if isinstance(data, dict) else []
    return [
        {
            "name": e.get("name"),
            "activity": e.get("activityname"),
            "type": e.get("modulename"),
            "course_id": (e.get("course") or {}).get("id"),
            "course": (e.get("course") or {}).get("fullname"),
            "timestamp": e.get("timesort"),
            "when": html.strip_tags(e.get("formattedtime", "")),
            "overdue": e.get("overdue"),
            "action": (e.get("action") or {}).get("name"),
            "url": e.get("url") or e.get("viewurl"),
        }
        for e in events
    ]
