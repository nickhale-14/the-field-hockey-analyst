"""
Historic Premier Division results transcribed from flashscore.com pages.

Why: the league API starts at 2022/23 and Altius only has the finals. For the
seasons before that, Flashscore has match-level results back to 2009/10
(both genders). Its pages are JavaScript-rendered and its terms forbid
automated scraping, so this module does NOT fetch anything: a person opens the
season's results page in a browser, selects all, and saves the page text to
`data/flashscore/raw/<yyyy>-<yy>_<men|women>.txt`. This parses that text.

The page text is one token per line. A match is:

    <dd.mm.yyyy>
    [Awrd]              <- optional flag line (awarded walkover)
    <home team>
    <away team>
    <home score>
    <away score>
    [<h1 home> <h1 away> <h2 home> <h2 away>]   <- optional half scores, one per line

Section headers ("Premier Division - Winners stage", "ROUND 7") set the stage
and round for the matches that follow. "ENGLAND:" / "Standings" / "Draw" /
adverts are noise.

Scores only - no scorers, no cards - so nothing here touches player identity.
Everything is emitted under source="england_hockey" with "flashscore:"-prefixed
source ids, the same pattern as the Altius importer, so teams join up with the
league data and the (source, source_id) upsert stays idempotent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ingest.models import Club, Competition, Match, Season, Team
from ingest.schemas import ClubIn, CompetitionIn, DatasetIn, MatchIn, SeasonIn, TeamIn
from ingest.sources.eh_altius import norm_team_name

SOURCE = "england_hockey"

# Normalised Flashscore label (trailing " W" removed, lower-case, punctuation
# stripped) -> the club name as our DB spells it. Only needed where
# normalisation alone can't bridge it.
FLASHSCORE_TEAM_OVERRIDES: dict[str, str] = {
    "exeter univ": "university of exeter",
    "durham": "durham university",
    "brooklands": "brooklands manchester university",
    "loughborough": "loughborough students",
    "birmingham": "university of birmingham",
    "nottingham": "university of nottingham",
    "cardiff": "cardiff & met",
}

# Flashscore section suffix -> our stage key. Regular-season sections have no
# suffix. Anything unmapped is reported, not guessed.
STAGES: dict[str, str] = {
    "": "regular",
    "winners stage": "phase2_top",
    "losers stage": "phase2_lower",
    "play offs": "finals",
    "relegation": "relegation",
    "final group": "league_finals_pool",
}

STAGE_COMPETITION_NAMES: dict[str, str] = {
    "regular": "{g} Premier Division",
    "phase2_top": "{g} Premier Division Phase 2 Top 6",
    "phase2_lower": "{g} Premier Division Phase 2 Lower 6",
    "finals": "{g} Premier Division Finals Weekend",
    "relegation": "{g} Premier Division Relegation Play Offs",
    "league_finals_pool": "{g} Premier Division League Finals Pool",
}

_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_INT = re.compile(r"^\d+$")
_SECTION = re.compile(r"^Premier Division( Women)?(?: - (.+))?$")
_ROUND = re.compile(r"^ROUND (\d+)$", re.I)
_NOISE = {"ENGLAND:", "Standings", "Draw", "Advertisement", "AD", "FINAL", "SEMI-FINALS", "3RD PLACE"}
_FLAGS = {"Awrd": "W", "Pen.": "R", "AET": "R", "Abn.": "A", "Postp.": "P", "Canc.": "A"}


@dataclass
class FlashscoreMatch:
    date: datetime
    home: str
    away: str
    home_score: int
    away_score: int
    stage: str
    round: int | None = None
    status: str = "R"  # R result, W awarded/walkover, A abandoned, P postponed
    halves: tuple[int, int, int, int] | None = None  # h1 home, h1 away, h2 home, h2 away


@dataclass
class ParseResult:
    matches: list[FlashscoreMatch] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def parse_flashscore_text(text: str) -> ParseResult:
    lines = [ln.strip() for ln in text.splitlines()]
    # Our capture files carry a small header ending in '---'; skip it if present.
    if "---" in lines:
        lines = lines[lines.index("---") + 1:]
    out = ParseResult()
    stage: str | None = None
    round_no: int | None = None
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if not ln or ln in _NOISE:
            i += 1
            continue
        m = _SECTION.match(ln)
        if m:
            key = (m.group(2) or "").strip().lower()
            if key not in STAGES:
                out.problems.append(f"line {i + 1}: unknown section '{ln}'")
                stage = None
            else:
                stage = STAGES[key]
            round_no = None
            i += 1
            continue
        m = _ROUND.match(ln)
        if m:
            round_no = int(m.group(1))
            i += 1
            continue
        if _DATE.match(ln):
            try:
                date = datetime.strptime(ln, "%d.%m.%Y")
            except ValueError:
                out.problems.append(f"line {i + 1}: bad date '{ln}'")
                i += 1
                continue
            j = i + 1
            status = "R"
            while j < n and lines[j] in _FLAGS:
                status = _FLAGS[lines[j]]
                j += 1
            names: list[str] = []
            while j < n and len(names) < 2 and lines[j] and not _INT.match(lines[j]) and not _DATE.match(lines[j]):
                names.append(lines[j])
                j += 1
            nums: list[int] = []
            while j < n and _INT.match(lines[j]):
                nums.append(int(lines[j]))
                j += 1
            if len(names) != 2 or len(nums) < 2:
                out.problems.append(f"line {i + 1}: could not read match starting '{ln}' (names={names}, nums={nums})")
                i = max(j, i + 1)
                continue
            if stage is None:
                out.problems.append(f"line {i + 1}: match '{names[0]} v {names[1]}' has no recognised section")
                i = j
                continue
            halves = tuple(nums[2:6]) if len(nums) >= 6 else None
            out.matches.append(
                FlashscoreMatch(
                    date=date, home=names[0], away=names[1], home_score=nums[0], away_score=nums[1],
                    stage=stage, round=round_no, status=status, halves=halves,  # type: ignore[arg-type]
                )
            )
            i = j
            continue
        # Anything else (tab labels, page chrome) is ignored.
        i += 1
    return out


def season_and_gender_from_filename(path: Path) -> tuple[str, str]:
    """'2019-20_women.txt' -> ('2019/20', 'W')."""
    m = re.match(r"^(\d{4})-(\d{2})_(men|women)\.txt$", path.name)
    if not m:
        raise ValueError(f"{path.name}: expected '<yyyy>-<yy>_<men|women>.txt'")
    return f"{m.group(1)}/{m.group(2)}", "W" if m.group(3) == "women" else "M"


# --------------------------------------------------------------------------
# Resolving Flashscore team labels onto our teams
# --------------------------------------------------------------------------

@dataclass
class TeamRef:
    source_id: str
    name: str
    club_source_id: str
    club_name: str
    gender: str
    played_prem: bool


@dataclass
class FlashscoreResolver:
    seasons: dict[str, str]
    teams: list[TeamRef]
    existing_competitions: set[str] = field(default_factory=set)

    def find_team(self, label: str, gender: str) -> TeamRef | None:
        wanted = _flash_key(label)
        cands = [t for t in self.teams if t.gender == gender and (norm_team_name(t.name) == wanted or norm_team_name(t.club_name) == wanted)]
        if not cands:
            return None
        prem = [t for t in cands if t.played_prem]
        if len(prem) == 1:
            return prem[0]
        firsts = [t for t in (prem or cands) if re.search(r"\b(1|M1|W1)$", t.name)]
        if len(firsts) == 1:
            return firsts[0]
        return cands[0] if len(cands) == 1 else None


def _flash_key(label: str) -> str:
    s = re.sub(r"\s+W$", "", label.strip())
    key = norm_team_name(s)
    return norm_team_name(FLASHSCORE_TEAM_OVERRIDES.get(key, key))


def build_flashscore_resolver(session: Session) -> FlashscoreResolver:
    seasons = {s.name: s.source_id for s in session.scalars(select(Season).where(Season.source == SOURCE))}
    gender_of: dict[str, set[str]] = {}
    prem: set[str] = set()
    for side in (Match.home_team_id, Match.away_team_id):
        for tsid, cname in session.execute(
            select(Team.source_id, Competition.name).select_from(Match)
            .join(Team, Team.id == side).join(Competition, Competition.id == Match.competition_id)
            .where(Match.source == SOURCE).distinct()
        ):
            gender_of.setdefault(tsid, set()).add("W" if "women" in cname.lower() else "M")
            if "premier division" in cname.lower():
                prem.add(tsid)
    teams: list[TeamRef] = []
    for tsid, tname, csid, cname in session.execute(
        select(Team.source_id, Team.name, Club.source_id, Club.name).join(Club, Club.id == Team.club_id).where(Team.source == SOURCE)
    ):
        genders = gender_of.get(tsid, set())
        if len(genders) != 1:
            continue  # never played, or (impossibly) both - can't place it
        teams.append(TeamRef(tsid, tname, csid, cname, genders.pop(), tsid in prem))
    comps = {c.source_id for c in session.scalars(select(Competition).where(Competition.source == SOURCE))}
    return FlashscoreResolver(seasons=seasons, teams=teams, existing_competitions=comps)


# --------------------------------------------------------------------------
# Building datasets
# --------------------------------------------------------------------------

@dataclass
class ImportReport:
    files: list[str] = field(default_factory=list)
    matches: int = 0
    team_map: dict[str, str] = field(default_factory=dict)  # "M: Exeter Univ." -> "University of Exeter M1"
    teams_created: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build_datasets(files: Iterable[Path], resolver: FlashscoreResolver) -> tuple[list[DatasetIn], ImportReport]:
    report = ImportReport()
    by_season: dict[str, DatasetIn] = {}
    created: dict[tuple[str, str], TeamIn] = {}

    for path in sorted(files):
        season_name, gender = season_and_gender_from_filename(path)
        parsed = parse_flashscore_text(path.read_text(encoding="utf-8"))
        report.files.append(f"{path.name}: {len(parsed.matches)} matches" + (f", {len(parsed.problems)} problems" if parsed.problems else ""))
        report.problems.extend(f"{path.name}: {p}" for p in parsed.problems)
        if not parsed.matches:
            continue

        if season_name not in by_season:
            sid = resolver.seasons.get(season_name) or f"flashscore:season:{season_name.replace('/', '-')}"
            by_season[season_name] = DatasetIn(source=SOURCE, season=SeasonIn(source_id=sid, name=season_name))
        ds = by_season[season_name]
        g_word = "Women's" if gender == "W" else "Men's"

        for fm in parsed.matches:
            comp_sid = f"flashscore:competition:{season_name.replace('/', '-')}:{gender}:{fm.stage}"
            if all(c.source_id != comp_sid for c in ds.competitions):
                ds.competitions.append(CompetitionIn(source_id=comp_sid, name=STAGE_COMPETITION_NAMES[fm.stage].format(g=g_word)))

            sides: list[str] = []
            for label in (fm.home, fm.away):
                ref = resolver.find_team(label, gender)
                key = f"{gender}: {label}"
                if ref is not None:
                    sides.append(ref.source_id)
                    report.team_map.setdefault(key, ref.name)
                    continue
                # Unknown to the league data (e.g. relegated before 2022): create it.
                ck = (gender, _flash_key(label))
                if ck not in created:
                    club_sid = f"flashscore:club:{_slug(ck[1])}"
                    team_sid = f"flashscore:team:{gender}:{_slug(ck[1])}"
                    name = re.sub(r"\s+W$", "", label) + (" W1" if gender == "W" else " M1")
                    if all(c.source_id != club_sid for c in ds.clubs):
                        ds.clubs.append(ClubIn(source_id=club_sid, name=re.sub(r"\s+W$", "", label) + " HC"))
                    created[ck] = TeamIn(source_id=team_sid, club_source_id=club_sid, name=name)
                    report.teams_created.append(f"{key} -> NEW {name} ({team_sid})")
                team = created[ck]
                if all(t.source_id != team.source_id for t in ds.teams):
                    ds.teams.append(team)
                    if all(c.source_id != team.club_source_id for c in ds.clubs):
                        ds.clubs.append(ClubIn(source_id=team.club_source_id, name=re.sub(r"\s+W$", "", label) + " HC"))
                sides.append(team.source_id)
                report.team_map.setdefault(key, f"NEW {team.name}")

            ds.matches.append(
                MatchIn(
                    source_id=f"flashscore:match:{season_name.replace('/', '-')}:{gender}:{fm.date:%Y%m%d}:{_slug(fm.home)}:{_slug(fm.away)}",
                    season_source_id=ds.season.source_id,
                    competition_source_id=comp_sid,
                    home_team_source_id=sides[0],
                    away_team_source_id=sides[1],
                    starts_at=fm.date,
                    status=fm.status,
                    home_score=fm.home_score,
                    away_score=fm.away_score,
                )
            )
            report.matches += 1

    return list(by_season.values()), report
