import re

import pytest

from infradash.btop import theme

HEX = re.compile(r"^#[0-9a-f]{6}$")


@pytest.fixture(autouse=True)
def _restore():
    yield
    theme.set_theme(theme.DEFAULT_THEME)


def test_default_is_marathon():
    assert theme.DEFAULT_THEME == "marathon"
    theme.set_theme("marathon")
    assert theme.c("main_bg") == "#0a0f0a" and theme.c("main_fg") == "#39ff5a"


@pytest.mark.parametrize("name", list(theme.THEMES))
def test_palettes_are_complete(name):
    theme.set_theme(name)
    for key in theme.BTOP:
        assert HEX.match(theme.c(key)), key
    for g in ("cpu", "free", "cached", "used", "download", "upload", "process", "temp"):
        assert len(theme.gradient(g)) == 101 and all(HEX.match(x) for x in theme.gradient(g))


def test_roles_match_across_themes():
    assert set(theme._ROLES_BTOP) == set(theme._ROLES_MARATHON)


def test_switching_clears_gradients():
    theme.set_theme("marathon")
    m = theme.gradient("cpu")
    theme.set_theme("btop")
    assert theme.gradient("cpu") != m


def test_unknown_theme():
    with pytest.raises(ValueError):
        theme.set_theme("solarized")
