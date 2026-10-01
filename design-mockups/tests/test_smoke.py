"""Smoke tests: every variant renders, every design opens in every state, and
picking in the gallery feeds `design mix`."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from design_mockups import picks as picks_mod
from design_mockups.app import STATES, GalleryScreen, MockupsApp
from design_mockups.cli import main
from design_mockups.designs import DESIGNS, find
from design_mockups.elements import ELEMENTS
from design_mockups.kit import PALETTES, Ctx, palette
from design_mockups.picks import Picks


@pytest.fixture(autouse=True)
def tmp_picks(tmp_path, monkeypatch):
    monkeypatch.setattr(picks_mod, "PATH", tmp_path / "picks.json")


@pytest.mark.parametrize("name", list(PALETTES))
@pytest.mark.parametrize("dark", [True, False])
def test_every_variant_renders(name, dark):
    c = Ctx(palette(name, dark))
    for el in ELEMENTS.values():
        assert el.variants, el.slug
        for v in el.variants:
            for _caption, item, phase in el.samples(c):
                con = Console(file=io.StringIO(), width=100)
                con.print(v.render(c.with_(item=item, phase=phase)))
                assert con.file.getvalue().strip(), (el.slug, v.key)


def test_design_picks_name_real_variants():
    for d in DESIGNS:
        for slug, key in d.picks.items():
            assert slug in ELEMENTS, (d.slug, slug)
            assert key in {v.key for v in ELEMENTS[slug].variants}, (d.slug, slug, key)


def test_find_accepts_words_digits_slugs():
    assert find("one") == 0 and find("2") == 1 and find("ledger") == 3 and find("nope") is None


@pytest.mark.parametrize("index", range(len(DESIGNS)))
@pytest.mark.parametrize("size", [(140, 42), (80, 24)])
async def test_design_opens_in_every_state(index, size):
    app = MockupsApp(index=index, picks=Picks())
    async with app.run_test(size=size) as pilot:
        for key, name, _ in STATES:
            await pilot.press(key)
            await pilot.pause()
            assert app.state == name
        for key in ("t", "c", "left_square_bracket", "right_square_bracket", "l", "question_mark", "escape"):
            await pilot.press(key)
            await pilot.pause()


async def test_gallery_pick_feeds_mix():
    app = MockupsApp(gallery="recording", picks=Picks())
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, GalleryScreen)
        await pilot.press("c")
        await pilot.pause()
        assert app.picks.elements["recording"] == "c"
        assert Picks.load().elements["recording"] == "c"
        await pilot.press("m")
        await pilot.pause()
        assert app.mode == "mix" and app.design().picks["recording"] == "c"
        await pilot.press("5")
        await pilot.pause()
        await pilot.press("L")
        await pilot.pause()
        assert Picks.load().layout != "classic"


def test_cli_list_and_picks(capsys):
    main(["list"])
    assert "baseline" in capsys.readouterr().out
    main(["picks"])
    assert "context-header" in capsys.readouterr().out
