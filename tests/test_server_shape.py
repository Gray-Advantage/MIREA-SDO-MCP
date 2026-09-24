"""Форма ответов инструментов и врезка о новой версии."""

from mirea_sdo_mcp import updater
from mirea_sdo_mcp.server import _decorate_result


def with_update(available: bool) -> None:
    updater._last_check = {
        "update_available": available,
        "current_version": "0.1.0",
        "latest_version": "0.9.9",
        "release_url": "https://example.invalid/r",
        "checked_at": 0,
    }


class TestResultShape:
    def test_список_приводится_к_словарю(self):
        updater._last_check = None
        assert _decorate_result([1, 2, 3]) == {"count": 3, "items": [1, 2, 3]}

    def test_пустой_список_тоже(self):
        updater._last_check = None
        assert _decorate_result([]) == {"count": 0, "items": []}

    def test_словарь_не_трогается(self):
        updater._last_check = None
        assert _decorate_result({"a": 1}) == {"a": 1}


class TestUpdateNotice:
    def test_врезка_добавляется_в_словарь(self):
        with_update(True)
        result = _decorate_result({"a": 1})
        assert result["a"] == 1
        assert result["_update"]["latest_version"] == "0.9.9"
        assert result["_update"]["how_to_update"] == "sdo_update"

    def test_врезка_добавляется_и_к_списку(self):
        with_update(True)
        assert "_update" in _decorate_result([1])

    def test_при_свежей_версии_врезки_нет(self):
        with_update(False)
        assert "_update" not in _decorate_result({"a": 1})

    def test_без_проверки_врезки_нет(self):
        updater._last_check = None
        assert "_update" not in _decorate_result({"a": 1})


class TestVersionCompare:
    def test_старшая_версия_новее(self):
        assert updater.is_newer("0.2.0", "0.1.0")

    def test_числа_сравниваются_как_числа(self):
        assert updater.is_newer("0.1.10", "0.1.9")

    def test_префикс_v_не_мешает(self):
        assert not updater.is_newer("v0.1.0", "0.1.0")
        assert updater.is_newer("v0.2.0", "0.1.0")

    def test_одинаковые_версии_не_новее(self):
        assert not updater.is_newer("1.0.0", "1.0.0")
