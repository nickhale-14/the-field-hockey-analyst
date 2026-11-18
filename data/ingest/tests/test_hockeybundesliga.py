from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ingest.sources import hockeybundesliga as hbl

FIXTURES = Path(__file__).parent / "fixtures" / "hockeybundesliga"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_season_page() -> None:
    html = '<div data-league="387" data-matchdate="2024-09-08"></div><div data-league="387" data-matchdate="2024-09-07"></div>'
    assert hbl.parse_season_page(html) == (387, ["2024-09-07", "2024-09-08"])


def test_parse_matchday_played() -> None:
    rows = hbl.parse_matchday(_read("matchday_2206_2026-10-18.html"), "2026-10-18")
    assert len(rows) == 6
    first = rows[0]
    assert first.match_path == "/leaguematch/1-feldhockey-bundesliga-herren-26-27-bndbl1h/uhc--bhc-126785"
    assert (first.home_club, first.home_name) == ("uhlenhorster-hockeyclub-uhc", "UHC Hamburg")
    assert (first.away_name, first.home_score, first.away_score) == ("Berliner HC", 7, 1)
    assert first.played and not first.shootout
    assert (first.round, first.venue) == ("Hauptrunde", "UHC - Wesselblek")
    so = next(r for r in rows if r.home_name == "Crefelder HTC")
    assert so.shootout and (so.home_score, so.away_score) == (4, 5)  # includes the shootout


def test_parse_matchday_scheduled() -> None:
    (row,) = hbl.parse_matchday(_read("matchday_2206_2026-10-22.html"), "2026-10-22")
    assert not row.played and row.kickoff == "20:30"
    assert row.home_score is None
    assert row.away_club == "harvestehuder-tennis-und-hockeyclub-hthc"


def test_parse_report_shootout_match() -> None:
    r = hbl.parse_report(_read("report_126872.html"))
    assert (r.home, r.away) == ("Crefelder HTC", "RW Köln")
    assert r.starts_at == datetime(2026, 10, 18, 13, 0)
    assert (r.home_score, r.away_score) == (3, 3)          # regulation
    assert (r.home_shootout, r.away_shootout) == (1, 2)    # shown as 4 : 5 n.P.
    assert r.shootout_kicks == (1, 2)
    assert r.umpires == ["Thomas Hinsken", "Christian Blasch"]
    assert r.venue == "Gerd-Wellen-Hockeyanlage (Crefelder HTC)"

    goals = [(e.side, e.name, e.minute, e.score) for e in r.events if e.kind == "goal"]
    assert goals == [
        ("home", "Paul Smith", 3, "1:0"),
        ("away", "Jakob Brilla", 6, "1:1"),
        ("home", "Hans Neethling", 22, "2:1"),
        ("away", "Florian Adrians", 32, "2:2"),
        ("home", "Jack Welch", 55, "3:2"),
        ("away", "Henrik Siegburg", 60, "3:3"),
    ]
    cards = [(e.side, e.name, e.minute, e.card) for e in r.events if e.kind == "card"]
    assert cards == [
        ("home", "Linus Michler", 35, ("GC", "green")),
        ("away", "Hugo von Montgelas", 57, ("GC", "green")),
    ]
    assert len(r.squads["home"]) == 18 and len(r.squads["away"]) == 18
    stumpf = next(p for p in r.squads["home"] if p.last == "Stumpf")
    assert (stumpf.first, stumpf.number, stumpf.goalkeeper, stumpf.captain) == ("Lukas", 15, True, False)
    hayner = next(p for p in r.squads["home"] if p.last == "Hayner")
    assert hayner.captain and not hayner.goalkeeper
    montgelas = next(p for p in r.squads["away"] if p.number == 19)
    assert (montgelas.first, montgelas.last) == ("Hugo", "von Montgelas")


def test_parse_report_2022_23_has_no_minutes() -> None:
    r = hbl.parse_report(_read("report_2223_mhc_dhc.html"))
    assert (r.home, r.away, r.home_score, r.away_score) == ("Mannheimer HC", "Düsseldorfer HC", 4, 0)
    assert r.home_shootout is None
    goals = [e for e in r.events if e.kind == "goal"]
    assert len(goals) == 4 and all(e.side == "home" and e.minute is None for e in goals)
    assert r.squads == {"home": [], "away": []}


def test_parse_ticker_goals() -> None:
    goals = hbl.parse_ticker_goals(_read("match_126872.html"))
    assert goals["1:0"] == "FELDTOR"
    assert goals["1:1"] == "KURZE ECKE - TOR"
    assert {hbl.goal_type(v) for v in goals.values()} == {"FG", "PC"}


def test_goal_type() -> None:
    assert hbl.goal_type("FELDTOR") == "FG"
    assert hbl.goal_type("KURZE ECKE - TOR") == "PC"
    assert hbl.goal_type("7M") == "PS"
    assert hbl.goal_type("SOMETHING NEW") is None


def test_card_colours_ignore_clip_path_mask() -> None:
    yellow = hbl.parse_html(
        '<span><svg fill="none"><g><rect fill="#F8D200"/><path fill="#F8D200"/></g>'
        '<defs><clipPath><rect fill="white"/></clipPath></defs></svg></span>'
    )
    assert yellow.svg_fills() == ["#f8d200", "#f8d200"]
    assert hbl.CARD_FILLS[yellow.svg_fills()[0]] == ("YC", "yellow")


def test_report_lists_staff_separately() -> None:
    r = hbl.parse_report(_read("report_126872.html"))
    assert "Johannes Schmitz" in r.staff["home"]
    assert all(p.full != "Johannes Schmitz" for p in r.squads["home"])


def test_matchday_skips_unfilled_slots_and_unset_kickoffs() -> None:
    placeholder = (
        '<div class="table-schedule-condensed__table-row"><div class="table-schedule-condensed__table-cell">'
        "TEAM 1</div><div>--:--</div><div>TEAM 2</div></div>"
    )
    assert hbl.parse_matchday(placeholder, "2023-06-02") == []
    unset = _read("matchday_2206_2026-10-22.html").replace(">20:30<", ">--:--<")
    (row,) = hbl.parse_matchday(unset, "2026-10-22")
    assert row.kickoff is None
