"""Общие приёмы разбора страниц Moodle."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..client import absolute

#: Служебная разметка, которая только мешает при извлечении текста.
_NOISE = (
    ".accesshide",
    ".visually-hidden",
    ".sr-only",
    "script",
    "style",
    ".activity-navigation",
    ".modified",
    "#page-footer",
)


def main_region(soup: BeautifulSoup) -> Tag | None:
    return soup.select_one('div[role="main"]') or soup.select_one("#region-main")


def strip_noise(node: Tag) -> Tag:
    for selector in _NOISE:
        for el in node.select(selector):
            el.decompose()
    return node


def text_of(node: Tag | None) -> str:
    if node is None:
        return ""
    return re.sub(r"[ \t]*\n[ \t]*", "\n", node.get_text("\n", strip=True)).strip()


def main_text(soup: BeautifulSoup) -> str:
    node = main_region(soup)
    return text_of(strip_noise(node)) if node is not None else ""


def page_title(soup: BeautifulSoup) -> str:
    for selector in ("h1.h2", '[role="main"] h2', "h1", "title"):
        el = soup.select_one(selector)
        if el is not None:
            title = el.get_text(strip=True)
            if title:
                return title.split("|")[0].strip()
    return ""


def table_rows(table: Tag) -> list[list[str]]:
    """Таблица в виде списка строк; скрытые подписи Moodle убраны."""
    rows: list[list[str]] = []
    for tr in table.select("tr"):
        cells = tr.select("th, td")
        if not cells:
            continue
        rows.append([text_of(strip_noise(c)).replace("\n", " ").strip() for c in cells])
    return rows


def links(node: Tag, *, external_only: bool = False) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for a in node.select("a[href]"):
        href = a["href"]
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = absolute(href)
        if external_only and "online-edu.mirea.ru" in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"text": a.get_text(" ", strip=True)[:200], "url": url})
    return out


def strip_tags(value: str) -> str:
    """Снимает разметку со строк, которые Moodle отдаёт в JSON уже как HTML."""
    if not value:
        return ""
    text = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def key_value_table(table: Tag) -> dict[str, Any]:
    """Двухколоночная таблица «параметр — значение» в словарь."""
    result: dict[str, Any] = {}
    for row in table_rows(table):
        if len(row) >= 2 and row[0]:
            result[row[0]] = row[1]
    return result
