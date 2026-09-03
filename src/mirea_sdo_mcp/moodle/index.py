"""Индекс элементов курсов: по cmid узнаём курс, тип и название.

Нужен потому, что URL элемента в Moodle содержит его тип (``/mod/folder/view.php``),
а инструментам удобнее принимать один только cmid.
"""

from __future__ import annotations

import time
from typing import Any

from ..client import SdoClient, SdoError
from . import courses

_TTL_SECONDS = 600.0


class ActivityIndex:
    def __init__(self) -> None:
        self._by_cmid: dict[int, dict[str, Any]] = {}
        self._built_at = 0.0

    def _fresh(self) -> bool:
        return bool(self._by_cmid) and (time.monotonic() - self._built_at) < _TTL_SECONDS

    async def build(self, client: SdoClient, force: bool = False) -> None:
        if self._fresh() and not force:
            return
        table: dict[int, dict[str, Any]] = {}
        for course in await courses.list_courses(client):
            cid = course["course_id"]
            try:
                tree = await courses.get_course_tree(client, cid)
            except SdoError:
                continue
            for section in tree["sections"]:
                for module in section["modules"]:
                    table[module["cmid"]] = {
                        **module,
                        "course_id": cid,
                        "course_name": course["name"],
                        "section": section["title"],
                    }
        self._by_cmid = table
        self._built_at = time.monotonic()

    async def get(self, client: SdoClient, cmid: int) -> dict[str, Any]:
        cmid = int(cmid)
        await self.build(client)
        if cmid not in self._by_cmid:
            await self.build(client, force=True)
        if cmid not in self._by_cmid:
            raise SdoError(
                f"Элемент {cmid} не найден среди доступных курсов. "
                "Проверь cmid через get_course или search_activities."
            )
        return self._by_cmid[cmid]

    async def all(self, client: SdoClient) -> list[dict[str, Any]]:
        await self.build(client)
        return list(self._by_cmid.values())


index = ActivityIndex()
