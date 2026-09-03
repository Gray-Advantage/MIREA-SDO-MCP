"""Форумы и объявления.

Разметку списка тем не удалось сверить с живыми данными: на момент разработки
во всех доступных курсах форумы пусты. Парсер написан по стандартной разметке
Moodle и покрывает несколько её вариантов; пустой форум обрабатывается честно.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import SdoClient, absolute
from . import html
from .files import _pluginfile_links
from .index import index

_DISCUSSION_ID_RE = re.compile(r"[?&]d=(\d+)")


async def list_discussions(client: SdoClient, cmid: int) -> dict[str, Any]:
    info = await index.get(client, cmid)
    soup = await client.get_html(f"/mod/forum/view.php?id={int(cmid)}")

    discussions: list[dict[str, Any]] = []
    seen: set[int] = set()
    for a in soup.select('a[href*="discuss.php"]'):
        match = _DISCUSSION_ID_RE.search(a["href"])
        if not match:
            continue
        did = int(match.group(1))
        if did in seen:
            continue
        seen.add(did)
        row = a.find_parent("tr")
        discussions.append(
            {
                "discussion_id": did,
                "title": a.get_text(" ", strip=True),
                "url": absolute(a["href"]),
                "row": html.table_rows(row)[0] if row is not None else None,
            }
        )

    main = html.main_region(soup)
    text = html.text_of(html.strip_noise(main)) if main is not None else ""
    return {
        "cmid": int(cmid),
        "name": info["name"],
        "type": "forum",
        "course_id": info["course_id"],
        "url": f"/mod/forum/view.php?id={int(cmid)}",
        "discussion_count": len(discussions),
        "discussions": discussions,
        "empty": not discussions,
        "note": "В форуме нет тем для обсуждения." if not discussions else None,
        "intro": text[:500],
    }


async def get_discussion(client: SdoClient, discussion_id: int) -> dict[str, Any]:
    """Сообщения одной темы форума."""
    soup = await client.get_html(f"/mod/forum/discuss.php?d={int(discussion_id)}")

    posts: list[dict[str, Any]] = []
    for article in soup.select('[data-region="post"], .forumpost, article.forum-post-container'):
        header = article.select_one("h3, .subject, [data-region-content='forum-post-core-subject']")
        author = article.select_one(".author, [data-region-content='forum-post-core-author']")
        body = article.select_one(
            "[data-region-content='forum-post-core-message'], .posting, .post-content-container"
        )
        posts.append(
            {
                "subject": header.get_text(" ", strip=True) if header else "",
                "author": author.get_text(" ", strip=True) if author else "",
                "text": html.text_of(html.strip_noise(body)) if body else "",
                "files": _pluginfile_links(article),
            }
        )

    main = html.main_region(soup)
    return {
        "discussion_id": int(discussion_id),
        "title": html.page_title(soup),
        "url": f"/mod/forum/discuss.php?d={int(discussion_id)}",
        "post_count": len(posts),
        "posts": posts,
        "text": html.text_of(html.strip_noise(main)) if main is not None and not posts else "",
    }
