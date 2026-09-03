"""Разбор реальной разметки СДО. Образцы сняты с живого сайта и обезличены."""

from mirea_sdo_mcp.moodle import assignments, files, grades, html, quizzes


class TestFolder:
    def test_плоская_папка_отдаёт_все_файлы(self, soup):
        found = files.parse_folder(soup("folder"))
        assert len(found) == 8
        assert found[0]["name"] == "Лекция №1.pdf"
        assert found[0]["path"] == "Лекция №1.pdf"
        assert found[0]["extension"] == "pdf"
        assert found[0]["url"].startswith("https://online-edu.mirea.ru/pluginfile.php/")

    def test_вложенные_подпапки_попадают_в_путь(self, soup):
        found = files.parse_folder(soup("folder_nested"))
        paths = [f["path"] for f in found]
        assert any(p.startswith("2025_2026/") for p in paths)
        assert any(p.startswith("2026_2027 (ЯщунТВ)/") for p in paths)
        assert "2026_2027 (ЯщунТВ)/ПР1_Base_Python.pdf" in paths

    def test_корневая_папка_не_добавляет_пустой_сегмент(self, soup):
        for entry in files.parse_folder(soup("folder_nested")):
            assert not entry["path"].startswith("/")
            assert "//" not in entry["path"]


class TestAssignments:
    def test_секция_протягивается_на_последующие_строки(self, soup):
        rows = assignments.parse_index(soup("assign_index"), 18555)
        assert rows
        assert all(r["section"] for r in rows)
        assert rows[0]["section"] == "Задания текущего контроля"
        assert rows[1]["section"] == "Задания текущего контроля"

    def test_cmid_и_состояние_ответа(self, soup):
        rows = assignments.parse_index(soup("assign_index"), 18555)
        first = rows[0]
        assert first["cmid"] == 930993
        assert first["name"] == "Контрольная работа №1"
        assert first["course_id"] == 18555
        assert "тве" in (first["submission"] or "").lower()

    def test_прочерк_превращается_в_none(self, soup):
        rows = assignments.parse_index(soup("assign_index"), 18555)
        assert rows[0]["due"] is None
        assert rows[0]["grade"] is None


class TestQuizzes:
    def test_список_тестов_со_сроками(self, soup):
        rows = quizzes.parse_index(soup("quiz_index"), 18555)
        assert len(rows) == 2
        assert rows[0]["cmid"] == 930995
        assert rows[0]["name"] == "Контрольное тестирование"
        assert "декабря 2026" in rows[0]["closes"]


class TestGrades:
    def test_обзор_отдаёт_id_курса_из_ссылки(self, soup):
        rows = grades.parse_overview(soup("grades_overview"))
        assert rows
        assert any(r["course_id"] == 18962 for r in rows)
        assert all(r["course"] for r in rows)

    def test_скрытая_подпись_типа_не_липнет_к_названию(self, soup):
        report = grades.parse_course_report(soup("grades_course"), 18555)
        names = [i["item"] for i in report["items"]]
        assert "Текущий контроль" in names
        assert not any(n.startswith("Вычисляемая оценка") for n in names)

    def test_тип_элемента_вынесен_в_отдельное_поле(self, soup):
        report = grades.parse_course_report(soup("grades_course"), 18555)
        item = next(i for i in report["items"] if i["item"] == "Текущий контроль")
        assert item["kind"] == "Вычисляемая оценка"
        assert item["Диапазон"] == "0–45"

    def test_итоговая_строка_помечена(self, soup):
        report = grades.parse_course_report(soup("grades_course"), 18555)
        assert any(i["is_total"] for i in report["items"])


class TestHtmlHelpers:
    def test_текст_страницы_без_служебной_разметки(self, soup):
        text = html.main_text(soup("page"))
        assert "Основная литература" in text

    def test_внешняя_ссылка_отделяется_от_внутренних(self, soup):
        node = html.main_region(soup("url"))
        external = html.links(node, external_only=True)
        assert len(external) == 1
        assert external[0]["url"].startswith("https://kinescope.io/")

    def test_снятие_тегов_со_строк_из_rpc(self):
        assert html.strip_tags('<a href="#">вторник</a><br>15 сентября') == "вторник 15 сентября"
        assert html.strip_tags("") == ""
