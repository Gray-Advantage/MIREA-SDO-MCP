"""Оценки: сводка по всем курсам и отчёт по одному курсу."""

from __future__ import annotations

import re
from typing import Any

from ..client import SdoClient
from . import html

_COURSE_ID_RE = re.compile(r"[?&]id=(\d+)")


async def get_grades_overview(client: SdoClient) -> list[dict[str, Any]]:
    """Итоговая оценка по каждому курсу (отчёт «Обзор оценок»)."""
    soup = await client.get_html("/grade/report/overview/index.php")
    return parse_overview(soup)


def parse_overview(soup: Any) -> list[dict[str, Any]]:
    """Разбирает отчёт «Обзор оценок». Без сети — только разметка."""
    table = soup.select_one('div[role="main"] table') or soup.select_one("table")
    if table is None:
        return []

    result: list[dict[str, Any]] = []
    for tr in table.select("tr")[1:]:
        cells = tr.select("th, td")
        if not cells:
            continue
        values = [html.text_of(html.strip_noise(c)).replace("\n", " ").strip() for c in cells]
        if not values or not values[0]:
            continue
        link = tr.select_one("a[href]")
        match = _COURSE_ID_RE.search(link["href"]) if link else None
        result.append(
            {
                "course": values[0],
                "course_id": int(match.group(1)) if match else None,
                "grade": _blank(values[1] if len(values) > 1 else ""),
            }
        )
    return result


async def get_course_grades(client: SdoClient, course_id: int) -> dict[str, Any]:
    """Постатейный отчёт по оценкам одного курса."""
    soup = await client.get_html(f"/grade/report/user/index.php?id={int(course_id)}")
    return parse_course_report(soup, int(course_id))


def parse_course_report(soup: Any, course_id: int) -> dict[str, Any]:
    """Разбирает отчёт по оценкам курса. Без сети — только разметка."""
    table = soup.select_one('div[role="main"] table') or soup.select_one("table")
    if table is None:
        return {"course_id": course_id, "items": [], "headers": []}

    headers = [
        html.text_of(html.strip_noise(th)).replace("\n", " ").strip()
        for th in table.select("thead th")
    ]
    items: list[dict[str, Any]] = []
    for tr in table.select("tbody tr"):
        cells = tr.select("th, td")
        if not cells:
            continue
        header = cells[0].select_one(".gradeitemheader")
        kind_el = cells[0].select_one("span.text-uppercase")
        kind = kind_el.get_text(" ", strip=True) if kind_el else None
        values = [html.text_of(html.strip_noise(c)).replace("\n", " ").strip() for c in cells]
        name = header.get_text(" ", strip=True) if header else values[0]
        if not name or len(values) < 2:
            continue
        item: dict[str, Any] = {"item": name, "kind": kind}
        for i, header in enumerate(headers[1:], start=1):
            if i < len(values):
                item[header or f"col{i}"] = _blank(values[i])
        lowered = name.lower()
        item["is_total"] = "итог" in lowered or "сумма" in lowered
        items.append(item)

    return {
        "course_id": course_id,
        "title": html.page_title(soup),
        "headers": headers,
        "item_count": len(items),
        "items": items,
    }


def _blank(value: str) -> str | None:
    return None if value in {"", "-", "—"} else value
