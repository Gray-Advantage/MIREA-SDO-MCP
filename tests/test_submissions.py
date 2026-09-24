"""Разбор формы отправки задания и ограничений, которые она накладывает."""

import pytest

from mirea_sdo_mcp.client import SdoError
from mirea_sdo_mcp.moodle import submissions


@pytest.fixture
def page(soup):
    return (soup("assign_editsubmission"), )


def raw(name: str) -> str:
    from tests.conftest import FIXTURES

    return (FIXTURES / f"{name}.html").read_text("utf-8")


class TestFormParsing:
    def test_черновичная_область_найдена(self, soup):
        form = soup("assign_editsubmission").select_one("form")
        draft = form.select_one(f'input[name="{submissions.DRAFT_FIELD}"]')
        assert draft is not None
        assert draft["value"].isdigit()

    def test_скрытые_поля_формы_на_месте(self, soup):
        form = soup("assign_editsubmission").select_one("form")
        names = {i["name"] for i in form.select('input[type="hidden"][name]')}
        assert {"action", "sesskey", "id", "lastmodified"} <= names
        assert form.select_one('input[name="action"]')["value"] == "savesubmission"

    def test_репозиторий_загрузки_определяется(self):
        assert submissions._upload_repo_id(raw("assign_editsubmission")) == 4

    def test_отсутствие_репозитория_даёт_понятную_ошибку(self):
        with pytest.raises(SdoError, match="Загрузить файл"):
            submissions._upload_repo_id("<html></html>")

    def test_ограничения_читаются(self):
        page = raw("assign_editsubmission")
        assert submissions._int_option(page, "maxbytes") == 20971520
        assert submissions._int_option(page, "maxfiles") == 2

    def test_неизвестная_настройка_даёт_none(self):
        assert submissions._int_option(raw("assign_editsubmission"), "нетакого") is None


class TestSafety:
    @pytest.mark.asyncio
    async def test_без_подтверждения_ничего_не_отправляется(self):
        result = await submissions.submit(client=None, cmid=1, confirm=False)
        assert result["submitted"] is False
        assert result["error"] == "confirmation_required"
        assert "необратим" in result["message"]
