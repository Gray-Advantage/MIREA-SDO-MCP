"""Файлы: перечисление, поиск по маске и скачивание."""

from __future__ import annotations

import fnmatch
import re
import time
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup, Tag

from .. import config
from ..client import SdoClient, SdoError, absolute, sanitize_filename
from . import courses
from .index import index


# --- перечисление ------------------------------------------------------


async def list_folder(client: SdoClient, cmid: int) -> dict[str, Any]:
    """Содержимое элемента «Папка», включая вложенные подпапки."""
    info = await index.get(client, cmid)
    soup = await client.get_html(f"/mod/folder/view.php?id={int(cmid)}")
    files = parse_folder(soup)

    return {
        "cmid": int(cmid),
        "name": info["name"],
        "type": "folder",
        "course_id": info["course_id"],
        "url": f"{config.BASE_URL}/mod/folder/view.php?id={int(cmid)}",
        "zip_url": f"{config.BASE_URL}/mod/folder/download_folder.php?id={int(cmid)}",
        "intro": _intro_text(soup),
        "file_count": len(files),
        "files": files,
    }


def parse_folder(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Разбирает дерево файлов со страницы папки. Без сети — только разметка."""
    root = soup.select_one(".foldertree .filemanager") or soup.select_one(".foldertree")
    found: list[dict[str, Any]] = []
    if root is not None:
        top = root.find("ul", recursive=False)
        if top is not None:
            _walk_folder(top, [], found)
    return found


def _walk_folder(ul: Tag, prefix: list[str], out: list[dict[str, Any]]) -> None:
    for li in ul.find_all("li", recursive=False):
        head = li.select_one(":scope > .fp-filename-icon")
        if head is None:
            continue
        link = head.select_one("a[href]")
        label = head.select_one(".fp-filename")
        name = label.get_text(strip=True) if label else ""

        if link is not None:
            filename = name or link.get_text(strip=True)
            out.append(
                {
                    "name": filename,
                    "path": "/".join([*prefix, filename]),
                    "url": absolute(link["href"]),
                    "extension": Path(filename).suffix.lower().lstrip("."),
                }
            )
        else:
            # Узел без ссылки — подпапка; её имя уходит в путь потомков.
            nested = li.find("ul", recursive=False)
            if nested is not None:
                _walk_folder(nested, [*prefix, name] if name else prefix, out)
            continue

        nested = li.find("ul", recursive=False)
        if nested is not None:
            _walk_folder(nested, [*prefix, name] if name else prefix, out)


def _intro_text(soup: BeautifulSoup) -> str:
    for selector in (".activity-description", "#intro", ".activity-header .description"):
        el = soup.select_one(selector)
        if el is not None:
            text = el.get_text(" ", strip=True)
            if text:
                return text
    return ""


async def resource_info(client: SdoClient, cmid: int) -> dict[str, Any]:
    """Сведения о файле-ресурсе. HEAD, поэтому сам файл не качается."""
    resp = await client.request("HEAD", f"/mod/resource/view.php?id={int(cmid)}")
    filename = _disposition_name(resp.headers.get("content-disposition", ""))
    final_url = str(resp.url)
    if not filename:
        from urllib.parse import unquote

        filename = sanitize_filename(unquote(final_url.rsplit("/", 1)[-1].split("?")[0]))
    size = resp.headers.get("content-length")
    return {
        "name": filename,
        "path": filename,
        "url": final_url,
        "content_type": resp.headers.get("content-type", ""),
        "size_bytes": int(size) if size and size.isdigit() else None,
        "extension": Path(filename).suffix.lower().lstrip("."),
    }


_DISPOSITION_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


def _disposition_name(value: str) -> str:
    match = _DISPOSITION_RE.search(value)
    if not match:
        return ""
    from urllib.parse import unquote

    return sanitize_filename(unquote(match.group(1)))


async def assignment_files(client: SdoClient, cmid: int) -> list[dict[str, Any]]:
    """Файлы, приложенные преподавателем к заданию."""
    soup = await client.get_html(f"/mod/assign/view.php?id={int(cmid)}")
    return _pluginfile_links(soup)


def _pluginfile_links(soup: BeautifulSoup) -> list[dict[str, Any]]:
    seen: set[str] = set()
    found: list[dict[str, Any]] = []
    for a in soup.select('a[href*="pluginfile.php"]'):
        url = absolute(a["href"])
        if url in seen:
            continue
        seen.add(url)
        name = a.get_text(strip=True) or Path(url.split("?")[0]).name
        found.append(
            {
                "name": name,
                "path": name,
                "url": url,
                "extension": Path(name).suffix.lower().lstrip("."),
            }
        )
    return found


#: Обход курса стоит десятков запросов, поэтому результат живёт немного в памяти.
_CACHE_TTL = 600.0
_file_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}


def clear_file_cache() -> None:
    _file_cache.clear()


async def list_course_files(
    client: SdoClient,
    course_id: int,
    pattern: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Все файлы курса: из папок, файлов-ресурсов и вложений к заданиям."""
    course_id = int(course_id)
    cached = _file_cache.get(course_id)
    if cached and not refresh and (time.monotonic() - cached[0]) < _CACHE_TTL:
        matched = filter_by_pattern(cached[1], pattern)
        return {
            "course_id": course_id,
            "pattern": pattern,
            "file_count": len(matched),
            "files": matched,
            "cached": True,
        }

    tree = await courses.get_course_tree(client, course_id)
    collected: list[dict[str, Any]] = []

    for section in tree["sections"]:
        for module in section["modules"]:
            if not module.get("available", True):
                continue
            entries = await _module_files(client, module)
            for entry in entries:
                collected.append(
                    {
                        **entry,
                        "cmid": module["cmid"],
                        "activity": module["name"],
                        "activity_type": module["type"],
                        "section": section["title"],
                        "course_id": course_id,
                    }
                )

    _file_cache[course_id] = (time.monotonic(), collected)
    matched = filter_by_pattern(collected, pattern)
    return {
        "course_id": course_id,
        "pattern": pattern,
        "file_count": len(matched),
        "files": matched,
        "cached": False,
    }


async def _module_files(client: SdoClient, module: dict[str, Any]) -> list[dict[str, Any]]:
    kind = module["type"]
    try:
        if kind == "folder":
            return (await list_folder(client, module["cmid"]))["files"]
        if kind == "resource":
            return [await resource_info(client, module["cmid"])]
        if kind == "assign":
            return await assignment_files(client, module["cmid"])
    except (SdoError, OSError):
        return []
    return []


# --- поиск -------------------------------------------------------------


def filter_by_pattern(
    entries: Iterable[dict[str, Any]], pattern: str | None
) -> list[dict[str, Any]]:
    """Фильтр по маске: glob (``*.pdf``) либо регулярка с префиксом ``re:``."""
    items = list(entries)
    if not pattern:
        return items
    if pattern.startswith("re:"):
        rx = re.compile(pattern[3:], re.IGNORECASE)
        return [e for e in items if rx.search(e["name"]) or rx.search(e.get("path", ""))]
    needle = pattern.casefold()
    return [
        e
        for e in items
        if fnmatch.fnmatch(e["name"].casefold(), needle)
        or fnmatch.fnmatch(e.get("path", "").casefold(), needle)
    ]


async def search_files(
    client: SdoClient,
    pattern: str,
    course_id: int | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    course_ids = [int(course_id)] if course_id else await courses.iter_course_ids(client)
    found: list[dict[str, Any]] = []
    for cid in course_ids:
        try:
            result = await list_course_files(client, cid, pattern, refresh)
        except SdoError:
            continue
        found.extend(result["files"])
    return found


# --- скачивание --------------------------------------------------------


def _target_dir(dest: str | None) -> Path:
    return Path(dest).expanduser() if dest else config.DOWNLOAD_DIR


async def download_file(
    client: SdoClient, url: str, dest: str | None = None, name: str | None = None
) -> dict[str, Any]:
    return await client.download(absolute(url), _target_dir(dest), name)


async def download_folder(
    client: SdoClient, cmid: int, dest: str | None = None, pattern: str | None = None
) -> dict[str, Any]:
    """Качает все файлы папки, сохраняя вложенную структуру."""
    listing = await list_folder(client, cmid)
    base = _target_dir(dest) / sanitize_filename(listing["name"])
    files = filter_by_pattern(listing["files"], pattern)

    saved, failed = [], []
    for entry in files:
        parts = [sanitize_filename(p) for p in entry["path"].split("/") if p] or [
            sanitize_filename(entry["name"])
        ]
        try:
            saved.append(await client.download(entry["url"], base.joinpath(*parts[:-1]), parts[-1]))
        except Exception as exc:
            failed.append({"name": entry["name"], "error": str(exc)})

    return {
        "cmid": int(cmid),
        "folder": listing["name"],
        "directory": str(base),
        "downloaded": len(saved),
        "failed": failed,
        "files": saved,
    }


async def download_course(
    client: SdoClient, course_id: int, dest: str | None = None, pattern: str | None = None
) -> dict[str, Any]:
    """Качает все материалы курса, раскладывая их по секциям и элементам."""
    listing = await list_course_files(client, course_id, pattern)
    course_list = await courses.list_courses(client)
    name = next(
        (c["name"] for c in course_list if c["course_id"] == int(course_id)),
        f"course-{course_id}",
    )
    base = _target_dir(dest) / sanitize_filename(name)

    saved, failed = [], []
    for entry in listing["files"]:
        parts = [
            sanitize_filename(entry["section"] or "Без секции"),
            sanitize_filename(entry["activity"]),
            *[sanitize_filename(p) for p in entry["path"].split("/") if p],
        ]
        try:
            saved.append(await client.download(entry["url"], base.joinpath(*parts[:-1]), parts[-1]))
        except Exception as exc:
            failed.append({"name": entry["name"], "error": str(exc)})

    return {
        "course_id": int(course_id),
        "course": name,
        "directory": str(base),
        "downloaded": len(saved),
        "failed": failed,
        "total_bytes": sum(f["size_bytes"] for f in saved),
    }
