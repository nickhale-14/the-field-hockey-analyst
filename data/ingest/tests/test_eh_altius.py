from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ingest.sources import eh_altius as alt

FIXTURES = Path(__file__).parent / "fixtures" / "altius"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_competition_matches() -> None:
    rows = alt.parse_competition_matches(_read("competition_558_matches.html"))
    assert [r.match_id for r in rows] == [8203, 8204, 8205, 8206]
    final = rows[-1]
    assert (final.home_abbr, final.away_abbr, final.stage) == ("WIM", "OLG", "Open-Mens Finals")
    assert final.scoreline == "2 - 3" and final.status == "Official"
    assert rows[2].scoreline == "2 - 2 (3 - 1 SO)"


def test_parse_competition_teams() -> None:
    abbrs = alt.parse_competition_teams(_read("competition_558_teams.html"))
    assert abbrs == {
        "OLG": "Old Georgians",
        "WIM": "Wimbledon",
        "SUR": "Surbiton",
        "H&W": "Hampstead & Westminster",
    }


def test_parse_match_grand_final_2026() -> None:
    m = alt.parse_match(_read("match_8206.html"), 8206)
    assert (m.home, m.away) == ("Wimbledon", "Old Georgians")
    assert (m.home_score, m.away_score) == (2, 3)
    assert (m.home_shootout, m.away_shootout) == (None, None)
    assert m.status == "Official"
    assert m.starts_at == datetime(2026, 5, 3, 18, 0)
    assert m.venue == "Surbiton Hockey Club"
    assert m.stage == "Open-Mens Finals"
    assert m.competition_title == "EHL Open- Mens Premier Division Finals"

    assert [(g.team, g.minute, g.raw_name, g.action, g.score) for g in m.goals] == [
        ("Wimbledon", 3, "KAEPPELER Kei", "Field Goal", "1 - 0"),
        ("Wimbledon", 25, "TIPPER Louis", "Field Goal", "2 - 0"),
        ("Old Georgians", 25, "WEIR Henry", "Penalty Corner", "2 - 1"),
        ("Old Georgians", 36, "AMES David", "Penalty Corner", "2 - 2"),
        ("Old Georgians", 40, "CROFT HENRY", "Penalty Corner", "2 - 3"),
    ]
    assert m.cards == []
    assert ("Umpire", "BARSTOW Dan (ENG)") in m.officials
    assert ("Tournament Director", "PENNYCOOK Jo (ENG)") in m.officials

    wim = m.lineups["Wimbledon"]
    assert len(wim) == 16
    gk = wim[0]
    assert (gk.shirt, gk.raw_name, gk.altius_player_id, gk.goalkeeper) == ("1", "REYNOLDS-COTTERILL Toby (GK)", 60065, True)
    assert any(p.captain and p.raw_name.startswith("HOOPER Sam") for p in wim)
    assert all(p.raw_name not in ("COACH", "MANAGER") for p in wim)
    assert len(m.lineups["Old Georgians"]) == 16


def test_parse_scoreline() -> None:
    assert alt.parse_scoreline("2 - 3") == (2, 3, None, None)
    assert alt.parse_scoreline("2 - 2 (3 - 1 SO)") == (2, 2, 3, 1)
    assert alt.parse_scoreline("0 - 0 (4 - 3 SO)") == (0, 0, 4, 3)
    assert alt.parse_scoreline("-") == (None, None, None, None)


def test_split_altius_name() -> None:
    assert alt.split_altius_name("REYNOLDS-COTTERILL Toby (GK)") == ("Toby", "Reynolds-Cotterill")
    assert alt.split_altius_name("HOOPER Sam (C)") == ("Sam", "Hooper")
    assert alt.split_altius_name("DE VISSER Aline (C, GK)") == ("Aline", "De Visser")
    assert alt.split_altius_name("CROFT HENRY") == ("Henry", "Croft")  # all-caps slip on the site
    assert alt.split_altius_name("VAN DER MERWE Jan") == ("Jan", "Van Der Merwe")
    assert alt.split_altius_name("Le HURAY Martha") == ("Martha", "Le Huray")
    assert alt.split_altius_name("O'DEA Kevin") == ("Kevin", "O'Dea")
    assert alt.split_altius_name("BANDURAK Nicholas") == ("Nicholas", "Bandurak")


def test_norm_team_name() -> None:
    assert alt.norm_team_name("Old Georgians M1") == "old georgians"
    assert alt.norm_team_name("O Georgians") == "old georgians"
    assert alt.norm_team_name("H&W") == "hampstead & westminster"
    assert alt.norm_team_name("Hampstead & Westminster W1") == "hampstead & westminster"
    assert alt.norm_team_name("Bowdon 1") == "bowdon"
    assert alt.norm_team_name("Cardiff & Met") == "cardiff & met"
    assert alt.norm_team_name("Loughborough Students W1") == "loughborough students"


def test_season_name_for() -> None:
    assert alt.season_name_for(datetime(2026, 5, 3)) == "2025/26"
    assert alt.season_name_for(datetime(2025, 10, 4)) == "2025/26"
    assert alt.season_name_for(datetime(2023, 4, 2)) == "2022/23"


WIM = alt.TeamRef("t-wim", "Wimbledon M1", "c-wim", "Wimbledon HC")
OG = alt.TeamRef("t-og", "Old Georgians M1", "c-og", "Old Georgians HC")
SUR = alt.TeamRef("t-sur", "Surbiton M1", "c-sur", "Surbiton HC")
HW = alt.TeamRef("t-hw", "Hampstead & Westminster M1", "c-hw", "Hampstead & Westminster HC")


def _resolver() -> alt.Resolver:
    return alt.Resolver(
        seasons={"2025/26": "season-guid"},
        teams={("2025/26", "M"): [OG, WIM, HW, SUR]},
        players=[
            alt.PlayerRef("p-hooper", "Sam", "Hooper", "c-wim", {"t-wim"}),
            # Registered with Holcombe, but all his events are for Surbiton M1.
            alt.PlayerRef("p-nick", "Nick", "Bandurak", "c-hol", {"t-hol", "t-sur"}),
            alt.PlayerRef("p-tom-c", "Tom", "Carson", "c-og", {"t-og"}),
            alt.PlayerRef("p-james-c", "James", "Carson", "c-og", {"t-og"}),
            alt.PlayerRef("p-trc", "Toby", "Reynolds Cotterill", "c-hw", set()),
            # Two different Emma Smiths at two clubs, no team evidence for either.
            alt.PlayerRef("p-smith-1", "Emma", "Smith", "c-a", set()),
            alt.PlayerRef("p-smith-2", "Emma", "Smith", "c-b", set()),
            # The league API's odd splits of compound surnames and spellings.
            alt.PlayerRef("p-huray", "Martha le", "Huray", "c-sur", set()),
            alt.PlayerRef("p-lacort", "Guadalupe Fernandez", "Lacort", "c-eg", set()),
            alt.PlayerRef("p-lewis", "Philippa", "Lewis", "c-hw", set()),
            alt.PlayerRef("p-gabb", "Anna-Ross", "Gabbitass", "c-rea", set()),
        ],
    )


def test_find_team_by_altius_label() -> None:
    r = _resolver()
    assert r.find_team("2025/26", "M", "Old Georgians").source_id == "t-og"
    assert r.find_team("2025/26", "M", "O Georgians").source_id == "t-og"
    assert r.find_team("2025/26", "M", "H&W").source_id == "t-hw"
    assert r.find_team("2025/26", "M", "Reading") is None  # not in this fake season
    assert r.find_team("2025/26", "W", "Wimbledon") is None  # wrong gender


def test_find_player_matching_rules() -> None:
    r = _resolver()
    assert r.find_player(WIM, "Sam", "Hooper").source_id == "p-hooper"
    assert r.find_player(WIM, "Samuel", "Hooper").source_id == "p-hooper"  # nickname
    # Transferred player: found via his Surbiton events despite Holcombe club_id.
    assert r.find_player(SUR, "Nicholas", "Bandurak").source_id == "p-nick"
    assert r.find_player(OG, "James", "Carson").source_id == "p-james-c"  # two Carsons: exact first name
    assert r.find_player(OG, "Jimmy", "Carson") is None  # ambiguous -> don't guess
    # No team evidence, other club, but the only Reynolds-Cotterill in the DB.
    assert r.find_player(WIM, "Toby", "Reynolds-Cotterill").source_id == "p-trc"
    assert r.find_player(WIM, "Kei", "Kaeppeler") is None  # unknown -> will be created
    assert r.find_player(WIM, "Emma", "Smith") is None  # two candidates, nothing to pick between them
    # Compound surnames split differently by the league API -> whole-name match.
    assert r.find_player(SUR, "Martha", "Le Huray").source_id == "p-huray"
    assert r.find_player(SUR, "Guadalupe", "Fernandez Lacort").source_id == "p-lacort"
    # One-letter spelling variants in longer first names.
    assert r.find_player(HW, "Phillipa", "Lewis").source_id == "p-lewis"
    assert r.find_player(HW, "Anna-Rose", "Gabbitass").source_id == "p-gabb"
    assert r.find_player(HW, "Tom", "Lewis") is None  # short names get no fuzziness


def test_explain_player_reasons() -> None:
    r = _resolver()
    assert r.explain_player(SUR, "Nicholas", "Bandurak")[1] == "team+fuzzy-first"
    assert r.explain_player(WIM, "Sam", "Hooper")[1] == "team"
    assert r.explain_player(WIM, "Toby", "Reynolds-Cotterill")[1] == "unique-name"
    assert r.explain_player(SUR, "Martha", "Le Huray")[1] == "club+fullname"
    assert r.explain_player(WIM, "Emma", "Smith") == (None, "")
    assert r.by_source_id("p-hooper").last_name == "Hooper"


def test_no_link_overrides_are_for_real_lineup_names() -> None:
    # Every override must be (our team name, 'SURNAME Given') with no role markers.
    for team, name in alt.NO_LINK:
        assert "(" not in name and name.split()[0].isupper(), (team, name)
