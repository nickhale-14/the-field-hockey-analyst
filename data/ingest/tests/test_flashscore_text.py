from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ingest.sources import flashscore_text as fs

SAMPLE = """Title: Premier Division 2021/2022 Results - Field hockey/England
Captured: test
---
Advertisement
FIELD HOCKEY
Premier Division - Losers stage
ENGLAND:
Standings
23.04.2022
Brooklands
Exeter Univ.
2
6
2
2
0
4
Premier Division - Winners stage
ENGLAND:
Standings
21.02.2022
Awrd
Hampstead & Westminster
Holcombe
5
0
Premier Division
ENGLAND:
Standings
ROUND 11
06.12.2021
Brooklands
Holcombe
2
8
ROUND 1
19.09.2021
Surbiton
Holcombe
6
4
Premier Division - Play Offs
ENGLAND:
Draw
FINAL
14.04.2019
Surbiton
Hampstead & Westminster
1
3
0
1
1
2
"""


def test_parse_sections_rounds_flags_and_halves() -> None:
    res = fs.parse_flashscore_text(SAMPLE)
    assert res.problems == []
    m = res.matches
    assert len(m) == 5

    assert (m[0].stage, m[0].home, m[0].away, m[0].home_score, m[0].away_score) == ("phase2_lower", "Brooklands", "Exeter Univ.", 2, 6)
    assert m[0].halves == (2, 2, 0, 4)
    assert m[0].date == datetime(2022, 4, 23)

    assert (m[1].stage, m[1].status, m[1].home_score, m[1].away_score) == ("phase2_top", "W", 5, 0)  # awarded walkover
    assert m[1].halves is None

    assert (m[2].stage, m[2].round) == ("regular", 11)
    assert (m[3].stage, m[3].round, m[3].home) == ("regular", 1, "Surbiton")
    assert (m[4].stage, m[4].halves) == ("finals", (0, 1, 1, 2))


def test_unknown_section_is_reported_not_guessed() -> None:
    res = fs.parse_flashscore_text("---\nPremier Division - Mystery stage\n01.01.2020\nA\nB\n1\n0\n")
    assert res.matches == []
    assert any("unknown section" in p for p in res.problems)
    assert any("no recognised section" in p for p in res.problems)


def test_filename_convention() -> None:
    assert fs.season_and_gender_from_filename(Path("2019-20_women.txt")) == ("2019/20", "W")
    assert fs.season_and_gender_from_filename(Path("2021-22_men.txt")) == ("2021/22", "M")


def _resolver() -> fs.FlashscoreResolver:
    T = fs.TeamRef
    return fs.FlashscoreResolver(
        seasons={},
        teams=[
            T("t-sur-m1", "Surbiton M1", "c-sur", "Surbiton HC", "M", True),
            T("t-sur-m2", "Surbiton M2", "c-sur", "Surbiton HC", "M", False),
            T("t-sur-w1", "Surbiton W1", "c-sur", "Surbiton HC", "W", True),
            T("t-exe", "University of Exeter M1", "c-exe", "University of Exeter HC", "M", True),
            T("t-bow-w", "Bowdon 1", "c-bow", "Bowdon HC", "W", True),
            T("t-bow-m", "Bowdon", "c-bow", "Bowdon HC", "M", True),
            T("t-bee-m1", "Beeston M1", "c-bee", "Beeston HC", "M", True),
            T("t-bee-m2", "Beeston 2", "c-bee", "Beeston HC", "M", False),
            T("t-lou-w", "Loughborough Students W1", "c-lou", "Loughborough Students HC", "W", True),
        ],
    )


def test_team_resolution() -> None:
    r = _resolver()
    assert r.find_team("Surbiton", "M").source_id == "t-sur-m1"  # not the M2
    assert r.find_team("Surbiton W", "W").source_id == "t-sur-w1"
    assert r.find_team("Exeter Univ.", "M").source_id == "t-exe"  # override
    assert r.find_team("Bowdon W", "W").source_id == "t-bow-w"
    assert r.find_team("Bowdon", "M").source_id == "t-bow-m"
    assert r.find_team("Beeston", "M").source_id == "t-bee-m1"
    assert r.find_team("Loughborough W", "W").source_id == "t-lou-w"
    assert r.find_team("Havant", "M") is None  # unknown -> importer creates it


def test_build_datasets_creates_unknown_teams_and_names_competitions(tmp_path: Path) -> None:
    (tmp_path / "2021-22_men.txt").write_text(SAMPLE, encoding="utf-8")
    datasets, report = fs.build_datasets([tmp_path / "2021-22_men.txt"], _resolver())
    assert report.problems == []
    assert report.matches == 5
    ds = datasets[0]
    assert ds.season.name == "2021/22" and ds.season.source_id == "flashscore:season:2021-22"
    names = {c.name for c in ds.competitions}
    assert names == {
        "Men's Premier Division",
        "Men's Premier Division Phase 2 Top 6",
        "Men's Premier Division Phase 2 Lower 6",
        "Men's Premier Division Finals Weekend",
    }
    # Unknown clubs got created once each, with gendered team names.
    created = {t.name for t in ds.teams}
    assert created == {"Brooklands M1", "Holcombe M1", "Hampstead & Westminster M1"}
    assert any("Holcombe" in line for line in report.teams_created)
    walkover = next(m for m in ds.matches if m.status == "W")
    assert (walkover.home_score, walkover.away_score) == (5, 0)
    assert all(m.source_id.startswith("flashscore:match:2021-22:M:") for m in ds.matches)
