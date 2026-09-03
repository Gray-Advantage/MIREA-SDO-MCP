from pathlib import Path

import pytest
from bs4 import BeautifulSoup

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def soup():
    """Загружает обезличенный образец страницы СДО."""

    def load(name: str) -> BeautifulSoup:
        return BeautifulSoup((FIXTURES / f"{name}.html").read_text("utf-8"), "lxml")

    return load
