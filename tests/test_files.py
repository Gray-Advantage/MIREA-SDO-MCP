"""Маски поиска, имена файлов и разбор диапазонов страниц."""

import pytest

from mirea_sdo_mcp.client import sanitize_filename
from mirea_sdo_mcp.moodle.files import filter_by_pattern
from mirea_sdo_mcp.reader import _parse_pages
from mirea_sdo_mcp.client import SdoError


def entry(name: str, path: str | None = None) -> dict:
    return {"name": name, "path": path or name}


class TestPattern:
    def test_glob_по_расширению(self):
        items = [entry("Лекция №1.pdf"), entry("Задание.docx"), entry("Схема.PDF")]
        assert len(filter_by_pattern(items, "*.pdf")) == 2

    def test_glob_по_началу_имени(self):
        items = [entry("Лекция №1.pdf"), entry("Практика 1.pdf")]
        found = filter_by_pattern(items, "лекция*")
        assert [f["name"] for f in found] == ["Лекция №1.pdf"]

    def test_маска_совпадает_и_по_вложенному_пути(self):
        items = [entry("ПР1.pdf", "2026_2027/ПР1.pdf")]
        assert filter_by_pattern(items, "*2026_2027*")

    def test_регулярное_выражение_с_префиксом(self):
        items = [entry("Практическое занятие №3.docx"), entry("Лекция.docx")]
        found = filter_by_pattern(items, r"re:практич.*\.docx")
        assert len(found) == 1

    def test_пустая_маска_ничего_не_отсекает(self):
        items = [entry("a.pdf"), entry("b.docx")]
        assert filter_by_pattern(items, None) == items


class TestFilename:
    def test_кириллица_сохраняется(self):
        assert sanitize_filename("Лекция №1.pdf") == "Лекция №1.pdf"

    def test_разделители_пути_заменяются(self):
        assert sanitize_filename("а/б\\в.pdf") == "а_б_в.pdf"

    def test_пустое_имя_получает_запасное(self):
        assert sanitize_filename("...") == "download"

    def test_длина_ограничена(self):
        assert len(sanitize_filename("я" * 500)) == 200


class TestPageRanges:
    def test_одна_страница(self):
        assert _parse_pages("3", 10) == [3]

    def test_диапазон(self):
        assert _parse_pages("2-4", 10) == [2, 3, 4]

    def test_смешанный_список_без_повторов(self):
        assert _parse_pages("1,4,3-5", 10) == [1, 4, 3, 5]

    def test_выход_за_границы_отсекается(self):
        assert _parse_pages("8-12", 10) == [8, 9, 10]

    def test_по_умолчанию_первые_пять(self):
        assert _parse_pages(None, 40) == [1, 2, 3, 4, 5]
        assert _parse_pages(None, 3) == [1, 2, 3]

    def test_пустой_документ(self):
        assert _parse_pages("1-5", 0) == []

    def test_мусор_даёт_понятную_ошибку(self):
        with pytest.raises(SdoError, match="диапазон страниц"):
            _parse_pages("abc", 10)
