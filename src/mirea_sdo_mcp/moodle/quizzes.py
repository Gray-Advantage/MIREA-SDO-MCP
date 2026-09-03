"""Тесты: список по курсу и сведения о тесте (только просмотр)."""

from __future__ import annotations

import re
from typing import Any

from ..client import SdoClient, absolute
from . import html
from .index import index

_ID_RE = re.compile(r"[?&]id=(\d+)")


async def list_quizzes(client: SdoClient, course_id: int) -> list[dict[str, Any]]:
    soup = await client.get_html(f"/mod/quiz/index.php?id={int(course_id)}")
    return parse_index(soup, int(course_id))


def parse_index(soup: Any, course_id: int) -> list[dict[str, Any]]:
    """Разбирает таблицу /mod/quiz/index.php. Без сети — только разметка."""
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
        match = _ID_RE.search(link["href"]) if link else None
        result.append(
            {
                "cmid": int(match.group(1)) if match else None,
                "name": values[1] if len(values) > 1 else "",
                "section": section,
                "closes": _blank(values[2] if len(values) > 2 else ""),
                "grade": _blank(values[3] if len(values) > 3 else ""),
                "url": absolute(link["href"]) if link else None,
                "course_id": course_id,
            }
        )
    return [r for r in result if r["name"]]


async def get_quiz(client: SdoClient, cmid: int) -> dict[str, Any]:
    """Условия теста и попытки. Попытку не начинает — только читает страницу."""
    info = await index.get(client, cmid)
    soup = await client.get_html(f"/mod/quiz/view.php?id={int(cmid)}")
    main = html.main_region(soup)

    attempts: list[list[str]] = []
    for table in soup.select('div[role="main"] table'):
        rows = html.table_rows(table)
        if rows and any("опыт" in " ".join(rows[0]).lower() or "Попыт" in c for c in rows[0]):
            attempts = rows
            break

    return {
        "cmid": int(cmid),
        "name": info["name"],
        "type": "quiz",
        "course_id": info["course_id"],
        "section": info.get("section"),
        "url": f"/mod/quiz/view.php?id={int(cmid)}",
        "attempts_table": attempts,
        "text": html.text_of(html.strip_noise(main)) if main is not None else "",
    }


def _blank(value: str) -> str | None:
    return None if value in {"", "-", "—"} else value
