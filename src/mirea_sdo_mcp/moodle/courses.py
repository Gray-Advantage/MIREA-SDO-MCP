"""Дисциплины: список и структура курса."""

from __future__ import annotations

import json
import re
from typing import Any

from ..client import SdoClient, SdoError

CLASSIFICATIONS = ("all", "inprogress", "future", "past", "favourites", "hidden")

#: Типы элементов, внутри которых бывают файлы.
FILE_BEARING = {"folder", "resource", "assign"}


async def list_courses(
    client: SdoClient,
    classification: str = "all",
    search: str | None = None,
    include_hidden: bool = True,
) -> list[dict[str, Any]]:
    """Список дисциплин, на которые записан пользователь.

    Moodle считает «скрытые» (убранные пользователем с главной) отдельной
    категорией и не отдаёт их в ``all``, поэтому по умолчанию доливаем их
    и помечаем полем ``hidden``.
    """
    if classification not in CLASSIFICATIONS:
        raise SdoError(
            f"classification должен быть одним из {', '.join(CLASSIFICATIONS)}"
        )

    result = [
        {**c, "hidden": classification == "hidden"}
        for c in await _fetch(client, classification)
    ]

    if include_hidden and classification == "all":
        known = {c["course_id"] for c in result}
        for course in await _fetch(client, "hidden"):
            if course["course_id"] not in known:
                result.append({**course, "hidden": True})

    if search:
        needle = search.casefold()
        result = [c for c in result if needle in (c["name"] or "").casefold()]
    return result


async def _fetch(client: SdoClient, classification: str) -> list[dict[str, Any]]:
    courses: list[dict[str, Any]] = []
    offset = 0
    while True:
        data = await client.rpc(
            "core_course_get_enrolled_courses_by_timeline_classification",
            {
                "offset": offset,
                "limit": 50,
                "classification": classification,
                "sort": "fullname",
            },
        )
        chunk = data.get("courses", []) if isinstance(data, dict) else []
        courses.extend(chunk)
        nextoffset = data.get("nextoffset") if isinstance(data, dict) else None
        if not chunk or not nextoffset or nextoffset <= offset:
            break
        offset = nextoffset

    return [_course_summary(c) for c in courses]


def _course_summary(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "course_id": raw.get("id"),
        "name": raw.get("fullname"),
        "short_name": raw.get("shortname"),
        "url": raw.get("viewurl") or f"/course/view.php?id={raw.get('id')}",
        "start_date": raw.get("startdate"),
        "end_date": raw.get("enddate") or None,
        "visible": raw.get("visible"),
        "progress": raw.get("progress"),
        "category": raw.get("coursecategory"),
    }


async def get_course_state(client: SdoClient, course_id: int) -> dict[str, Any]:
    """Сырое состояние курса из ``core_courseformat_get_state``."""
    raw = await client.rpc("core_courseformat_get_state", {"courseid": int(course_id)})
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SdoError(f"курс {course_id}: не разобрать состояние") from exc
    if isinstance(raw, dict):
        return raw
    raise SdoError(f"курс {course_id}: неожиданный формат состояния")


async def get_course_tree(client: SdoClient, course_id: int) -> dict[str, Any]:
    """Дерево курса: секции по порядку, внутри — элементы."""
    state = await get_course_state(client, course_id)
    by_id = {str(cm["id"]): cm for cm in state.get("cm", [])}

    sections = []
    for section in state.get("section", []):
        modules = [
            _module_summary(by_id[cmid])
            for cmid in section.get("cmlist", [])
            if cmid in by_id
        ]
        sections.append(
            {
                "section_id": int(section["id"]),
                "number": section.get("number"),
                "title": _clean(section.get("title") or ""),
                "visible": section.get("visible"),
                "url": section.get("sectionurl"),
                "modules": modules,
            }
        )

    course = state.get("course", {})
    return {
        "course_id": int(course.get("id", course_id)),
        "url": course.get("baseurl"),
        "section_count": len(sections),
        "module_count": len(by_id),
        "sections": sections,
    }


def _module_summary(cm: dict[str, Any]) -> dict[str, Any]:
    modname = cm.get("module") or cm.get("modname")
    return {
        "cmid": int(cm["id"]),
        "name": _clean(cm.get("name") or ""),
        "type": modname,
        "url": cm.get("url"),
        "visible": cm.get("visible"),
        "available": cm.get("uservisible"),
        "has_files": modname in FILE_BEARING,
    }


_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: str) -> str:
    """Убирает разметку и лишние пробелы из названий, приходящих из Moodle."""
    text = _TAG_RE.sub("", value)
    return re.sub(r"\s+", " ", text).strip()


async def iter_course_ids(client: SdoClient) -> list[int]:
    return [c["course_id"] for c in await list_courses(client) if c["course_id"]]


async def search_activities(
    client: SdoClient,
    query: str,
    course_id: int | None = None,
    module_type: str | None = None,
) -> list[dict[str, Any]]:
    """Ищет элементы курсов по подстроке в названии."""
    needle = query.casefold()
    course_ids = [int(course_id)] if course_id else await iter_course_ids(client)

    found: list[dict[str, Any]] = []
    for cid in course_ids:
        try:
            tree = await get_course_tree(client, cid)
        except SdoError:
            continue
        for section in tree["sections"]:
            for module in section["modules"]:
                if module_type and module["type"] != module_type:
                    continue
                if needle and needle not in module["name"].casefold():
                    continue
                found.append(
                    {
                        **module,
                        "course_id": cid,
                        "section": section["title"],
                    }
                )
    return found
