"""Отправка ответа на задание.

Единственная часть сервера, которая пишет в СДО, поэтому она разделена надвое.

``stage_files`` кладёт файлы в черновичную область Moodle. Это обратимо и
никому не видно: пока не выполнен финальный POST, ответ не сдан.

``submit`` делает тот самый финальный POST. В здешней настройке Moodle стадии
«черновик ответа» нет — сохранение сразу отправляет работу на проверку, — так
что инструмент требует явного подтверждения.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..client import SdoClient, SdoError
from . import courses, html
from .index import index

#: Поле формы, в котором лежит идентификатор черновичной области.
DRAFT_FIELD = "files_filemanager"

#: Имя файлового поля у репозитория «Загрузить файл».
UPLOAD_FIELD = "repo_upload_file"


class SubmissionClosed(SdoError):
    """Задание не принимает ответы."""


async def get_submission_form(client: SdoClient, cmid: int) -> dict[str, Any]:
    """Открывает форму ответа и собирает всё, что нужно для загрузки и отправки.

    Каждое открытие формы создаёт новую черновичную область, поэтому
    возвращённый ``draft_itemid`` нужно использовать и при загрузке, и при
    отправке — иначе файлы не доедут.
    """
    cmid = int(cmid)
    info = await index.get(client, cmid)
    if info["type"] != "assign":
        raise SdoError(f"Элемент {cmid} — это «{info['type']}», а не задание.")

    resp = await client.request("GET", f"/mod/assign/view.php?id={cmid}&action=editsubmission")
    page = resp.text
    soup = html.BeautifulSoup(page, "lxml")

    form = soup.select_one('form[action*="view.php"][method="post"]')
    if form is None or not form.select_one(f'input[name="{DRAFT_FIELD}"]'):
        raise SubmissionClosed(
            f"У задания «{info['name']}» нет формы отправки файлов. "
            "Возможно, срок прошёл, ответ уже сдан или задание не принимает файлы."
        )

    fields = {
        i["name"]: i.get("value", "")
        for i in form.select("input[name]")
        if i.get("type") == "hidden"
    }
    draft = fields.get(DRAFT_FIELD)
    if not draft:
        raise SubmissionClosed(f"У задания «{info['name']}» не нашлась черновичная область.")

    form_max_bytes = _int_option(page, "maxbytes") or 0
    effective, limit_source = await _resolve_max_bytes(
        client, info["course_id"], form_max_bytes
    )

    return {
        "cmid": cmid,
        "name": info["name"],
        "course_id": info["course_id"],
        "draft_itemid": int(draft),
        "sesskey": fields.get("sesskey") or await client.get_sesskey(),
        "fields": fields,
        "upload_repo_id": _upload_repo_id(page),
        "max_bytes": form_max_bytes,
        "effective_max_bytes": effective,
        "max_bytes_source": limit_source,
        "max_files": _int_option(page, "maxfiles"),
        "already_attached": _int_option(page, "filecount") or 0,
    }


async def _resolve_max_bytes(
    client: SdoClient, course_id: int, form_max_bytes: int
) -> tuple[int, str]:
    """Определяет реально действующий предел размера файла.

    Moodle пишет в форму 0, когда у задания своего предела нет — тогда
    действует лимит курса. Спрашиваем его у СДО, а не подставляем догадку.
    """
    if form_max_bytes > 0:
        return form_max_bytes, "задание"
    try:
        state = await courses.get_course_state(client, course_id)
    except SdoError:
        return 0, "неизвестно"
    course_max = int(state.get("course", {}).get("maxbytes") or 0)
    if course_max > 0:
        return course_max, "курс"
    return 0, "неизвестно"


def _upload_repo_id(page: str) -> int:
    match = re.search(r'"id":"(\d+)","name":"[^"]*","type":"upload"', page)
    if not match:
        raise SdoError("На странице задания не нашёлся репозиторий «Загрузить файл».")
    return int(match.group(1))


def _int_option(page: str, key: str) -> int | None:
    match = re.search(r'"%s"\s*:\s*"?(-?\d+)"?' % key, page)
    return int(match.group(1)) if match else None


async def stage_files(
    client: SdoClient, cmid: int, paths: list[str]
) -> dict[str, Any]:
    """Загружает файлы в черновичную область задания. Ответ при этом НЕ сдаётся."""
    form = await get_submission_form(client, cmid)
    limit_bytes = form["effective_max_bytes"] or 0
    limit_files = form["max_files"] or 0

    prepared: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise SdoError(f"Файл не найден: {path}")
        if limit_bytes and path.stat().st_size > limit_bytes:
            raise SdoError(
                f"«{path.name}» весит {path.stat().st_size / 1048576:.1f} МБ, "
                f"а предел ({form['max_bytes_source']}) — "
                f"{limit_bytes / 1048576:.1f} МБ."
            )
        prepared.append(path)

    if limit_files and len(prepared) > limit_files:
        raise SdoError(
            f"Задание принимает не больше {limit_files} файлов, передано {len(prepared)}."
        )

    uploaded: list[dict[str, Any]] = []
    for path in prepared:
        with path.open("rb") as fh:
            resp = await client.request(
                "POST",
                "/repository/repository_ajax.php",
                params={"action": "upload"},
                data={
                    "sesskey": form["sesskey"],
                    "repo_id": str(form["upload_repo_id"]),
                    "itemid": str(form["draft_itemid"]),
                    "savepath": "/",
                    "title": path.name,
                    "author": "",
                    "license": "unknown",
                    "overwrite": "1",
                },
                files={UPLOAD_FIELD: (path.name, fh)},
            )
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise SdoError(f"Загрузка «{path.name}»: СДО вернула не JSON") from exc
        if payload.get("error") or payload.get("errorcode"):
            raise SdoError(f"Загрузка «{path.name}»: {payload.get('error')}")
        uploaded.append(
            {
                "name": payload.get("file") or path.name,
                "url": payload.get("url"),
                "local_path": str(path),
                "size_bytes": path.stat().st_size,
            }
        )

    return {
        "cmid": form["cmid"],
        "assignment": form["name"],
        "draft_itemid": form["draft_itemid"],
        "staged": uploaded,
        "staged_count": len(uploaded),
        "max_files": limit_files,
        "max_bytes": limit_bytes,
        "max_bytes_source": form["max_bytes_source"],
        "submitted": False,
        "next_step": (
            "Файлы лежат в черновичной области и НЕ сданы. Чтобы сдать, вызови "
            f"submit_assignment(cmid={form['cmid']}, draft_itemid={form['draft_itemid']}, "
            "confirm=true) — это необратимо, сначала спроси пользователя."
        ),
    }


async def list_staged(client: SdoClient, draft_itemid: int) -> list[dict[str, Any]]:
    """Показывает, что сейчас лежит в черновичной области."""
    sesskey = await client.get_sesskey()
    resp = await client.request(
        "POST",
        "/repository/draftfiles_ajax.php",
        params={"action": "list"},
        data={
            "sesskey": sesskey,
            "itemid": str(int(draft_itemid)),
            "filepath": "/",
"clientid": "mcp",
        },
    )
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return []
    return [
        {"name": f.get("filename"), "size_bytes": f.get("size"), "url": f.get("url")}
        for f in payload.get("list", [])
        if f.get("filename") and f.get("filename") != "."
    ]


async def submit(
    client: SdoClient,
    cmid: int,
    draft_itemid: int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Финальный POST: отправляет ответ на проверку. Необратимо."""
    if not confirm:
        return {
            "submitted": False,
            "error": "confirmation_required",
            "message": (
                "Отправка ответа необратима: в этой настройке Moodle стадии черновика "
                "нет, работа сразу уходит на проверку. Спроси пользователя и повтори "
                "вызов с confirm=true."
            ),
        }

    form = await get_submission_form(client, cmid)
    itemid = int(draft_itemid) if draft_itemid else form["draft_itemid"]

    staged = await list_staged(client, itemid)
    if not staged:
        raise SdoError(
            f"В черновичной области {itemid} нет файлов. "
            "Сначала вызови stage_assignment_files — и используй тот же draft_itemid."
        )

    payload = dict(form["fields"])
    payload[DRAFT_FIELD] = str(itemid)
    payload["submitbutton"] = "Сохранить"

    resp = await client.request(
        "POST", "/mod/assign/view.php", data=payload, follow_redirects=True
    )
    after = html.BeautifulSoup(resp.text, "lxml")
    table = after.select_one(".submissionstatustable table") or after.select_one(
        ".submissionstatustable"
    )
    status = html.key_value_table(table) if table is not None else {}

    return {
        "submitted": True,
        "cmid": int(cmid),
        "assignment": form["name"],
        "draft_itemid": itemid,
        "files": staged,
        "status": status,
        "message": "Ответ отправлен. Проверь состояние в status.",
    }
