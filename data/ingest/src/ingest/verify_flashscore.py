"""
Independently verify the hand-transcribed Flashscore seasons against Altius.

2019/20 and 2020/21 came into the database by hand: pages read in a browser
and pasted, then parsed (`sources/flashscore_text.py`). Nothing has ever
checked them, and they feed the predictor, so a typo would be silent.

England Hockey's own Altius site carries both of those Prem seasons. Note what
that is and isn't: Altius (englandhockey.altiusrt.com) is England Hockey's OWN
results platform - a different SYSTEM, not a different organisation - so it is
the authoritative record, while Flashscore is an unaffiliated commercial
aggregator. Where they disagree, England Hockey wins. The two EH systems (this
and the league API) cover different periods and barely overlap, so "do EH's own
two systems agree" is not a question this can answer.

    206 = 2019/20 women   207 = 2019/20 men
    246 = 2020/21 men     247 = 2020/21 women

(2021/22 is NOT on Altius - it stays unverified.)

Altius abbreviates team names ("Hamp&West", "Loughboro Stu"), so both sides
are normalised to a canonical key before matching on season + gender + home
+ away, which is unique in a round robin.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from ingest.sources.eh_altius import AltiusClient, parse_competition_matches

# competition id -> (season, gender). Confirmed by comparing team sets and
# match counts against the database before trusting any of it.
COMPETITIONS = {
    206: ("2019/20", "W"),
    207: ("2019/20", "M"),
    246: ("2020/21", "M"),
    247: ("2020/21", "W"),
}

# Altius's abbreviation -> the canonical club key we also reduce our own names to.
ALTIUS_NAMES = {
    "beeston": "beeston",
    "birmingham univ": "birmingham", "birmingham": "birmingham",
    "bowdon hightown": "bowdon",
    "buckingham": "buckingham",
    "clifton robs": "clifton robinsons",
    "e grinstead": "east grinstead", "egrinstead": "east grinstead",
    "hamp&west": "hampstead westminster",
    "holcombe": "holcombe",
    "loughboro stu": "loughborough students", "loughboro": "loughborough students",
    "surbiton": "surbiton",
    "brooklands mu": "brooklands", "brooklands": "brooklands",
    "exeter univ": "exeter", "exeter": "exeter",
    "old georgians": "old georgians", "georgians": "old georgians",
    "reading": "reading",
    "wimbledon": "wimbledon",
    "durham": "durham",
    "oxted": "oxted",
    "swansea": "swansea",
}


def canonical(name: str) -> str:
    """Reduce one of OUR team names to the same key space as ALTIUS_NAMES."""
    n = name.lower().strip()
    n = re.sub(r"\s*\b([mw]\d|\d)\b\s*$", "", n)          # drop M1 / W1 / 1 suffix
    n = n.replace("&", " ").replace("'", "")
    n = re.sub(r"\buniversity of\b|\buniversity\b|\bunivers?i?t?y?\b", "", n)
    n = re.sub(r"\bmanchester\b", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    aliases = {
        "hampstead westminster": "hampstead westminster",
        "clifton robinsons": "clifton robinsons",
        "loughborough students": "loughborough students",
        "brooklands manchester": "brooklands",
        "brooklands": "brooklands",
    }
    return aliases.get(n, n)


@dataclass
class Row:
    season: str
    gender: str
    home: str
    away: str
    home_score: int
    away_score: int
    when: str = ""


def _parse_when(s: str) -> datetime | None:
    for fmt in ("%d %b %Y %H:%M", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def fetch_altius(client: AltiusClient) -> list[Row]:
    out: list[Row] = []
    for cid, (season, gender) in COMPETITIONS.items():
        for m in parse_competition_matches(client.get_html(f"/competitions/{cid}/matches")):
            score = re.match(r"\s*(\d+)\s*-\s*(\d+)", m.scoreline or "")
            if not score:
                continue  # unplayed / cancelled
            out.append(Row(
                season, gender,
                ALTIUS_NAMES.get(m.home_abbr.lower().strip(), m.home_abbr.lower().strip()),
                ALTIUS_NAMES.get(m.away_abbr.lower().strip(), m.away_abbr.lower().strip()),
                int(score.group(1)), int(score.group(2)), m.when,
            ))
    return out


def fetch_ours(session: Session) -> list[Row]:
    rows = session.execute(
        text(
            """
            SELECT s.name AS season, c.name AS competition,
                   th.name AS home, ta.name AS away,
                   m.home_score, m.away_score, m.starts_at
            FROM matches m
            JOIN seasons s ON s.id = m.season_id
            JOIN competitions c ON c.id = m.competition_id
            JOIN teams th ON th.id = m.home_team_id
            JOIN teams ta ON ta.id = m.away_team_id
            WHERE s.name IN ('2019/20', '2020/21') AND m.status = 'R'
            """
        )
    ).mappings().all()
    return [
        Row(r["season"], "W" if "women" in r["competition"].lower() else "M",
            canonical(r["home"]), canonical(r["away"]),
            r["home_score"], r["away_score"], str(r["starts_at"]))
        for r in rows
    ]


def verify(session: Session) -> str:
    client = AltiusClient()
    try:
        theirs = fetch_altius(client)
    finally:
        client.close()
    ours = fetch_ours(session)

    def key(r: Row) -> tuple:
        return (r.season, r.gender, r.home, r.away)

    theirs_by, ours_by = {key(r): r for r in theirs}, {key(r): r for r in ours}
    theirs_names = {r.home for r in theirs} | {r.away for r in theirs}
    ours_names = {r.home for r in ours} | {r.away for r in ours}
    unmapped = sorted(theirs_names - ours_names)

    out: list[str] = []
    out.append("=" * 90)
    out.append("FLASHSCORE (third-party, hand-transcribed) vs ENGLAND HOCKEY'S OWN RECORD (Altius)")
    out.append("=" * 90)
    out.append(f"Altius matches with a score: {len(theirs)}   ours: {len(ours)}")
    if unmapped:
        out.append(f"!! team names that didn't map: {unmapped}")

    only_theirs = [k for k in theirs_by if k not in ours_by]
    only_ours = [k for k in ours_by if k not in theirs_by]
    diffs = [
        (k, ours_by[k], theirs_by[k])
        for k in ours_by
        if k in theirs_by
        and (ours_by[k].home_score, ours_by[k].away_score)
        != (theirs_by[k].home_score, theirs_by[k].away_score)
    ]

    out.append(f"\non Altius but not in our DB: {len(only_theirs)}")
    for k in sorted(only_theirs)[:15]:
        t = theirs_by[k]
        out.append(f"    {k[0]} {k[1]}  {k[2]} {t.home_score}-{t.away_score} {k[3]}   ({t.when})")
    out.append(f"\nin our DB but not on Altius: {len(only_ours)}")
    for k in sorted(only_ours)[:15]:
        o = ours_by[k]
        out.append(f"    {k[0]} {k[1]}  {k[2]} {o.home_score}-{o.away_score} {k[3]}")
    out.append(f"\nSCORE DIFFERENCES: {len(diffs)}")
    for k, o, t in sorted(diffs):
        out.append(f"    {k[0]} {k[1]}  {k[2]} v {k[3]}:  ours {o.home_score}-{o.away_score}"
                   f"   Altius {t.home_score}-{t.away_score}")

    matched = len(ours_by) - len(only_ours)
    if not diffs and not only_ours and not only_theirs:
        out.append(f"\nCLEAN - all {matched} matches agree with England Hockey's own record.")
    else:
        out.append(f"\n{matched - len(diffs)} of {matched} matched fixtures agree.")
    out.append("\nNote: 2021/22 is not on Altius and stays unverified.")
    return "\n".join(out)
