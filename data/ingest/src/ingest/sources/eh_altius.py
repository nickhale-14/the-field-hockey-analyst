"""
Premier Division Finals Weekend importer - scrapes englandhockey.altiusrt.com.

Why this exists: England Hockey runs the end-of-season Finals Weekend (semi
finals, 3rd/4th play-off, Grand Final) as a *tournament* on Altius RT, not as
league fixtures in the ehdwapi league API. The league API only ever carried
the 2023 and 2024 men's finals (and a ghost "TBC v TBC" row for 2025) - no
women's finals at all. Altius has all of them, with goal minutes/types, cards,
shootout detail, lineups, and officials.

Altius has no public API (the documented /api/v1 needs a key we don't have),
but its pages are server-rendered and stable, so this reads the HTML. It is a
polite scraper: one request per page, a short sleep between requests, and the
total is tiny (per finals weekend: 1 competition page + 1 teams page + 4 match
pages).

Identity: everything is emitted under source="england_hockey" so teams and
players join up with the league data. Altius ids live in a different id space
from the league API's GUIDs, so every entity created here gets a prefixed
source_id ("altius:match:8206", "altius:player:60065") - they can never
collide with a GUID, and the loader's (source, source_id) upsert stays
idempotent. Teams are NEVER created here: an Altius team name ("Old
Georgians", "O Georgians", "H&W") is resolved to the existing league team for
that season/gender, or the import stops and tells you. Players are resolved to
existing league players by club + name where possible; unmatched ones are
created with an altius: id and reported, so they can be aliased by hand later.

Times are stored as UK local time (naive), matching what the league API gives
us for every other match.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ingest.models import Club, Competition, Match, MatchEvent, Player, Season, Team
from ingest.schemas import (
    CompetitionIn,
    DatasetIn,
    MatchEventIn,
    MatchIn,
    MatchOfficialIn,
    PlayerIn,
    SeasonIn,
    VenueIn,
)

BASE_URL = "https://englandhockey.altiusrt.com"
SOURCE = "england_hockey"

# Altius competition id -> what it is. Found by walking the site's competition
# list (478 entries, 40 pages) on 2026-09-18; add new seasons here as they run.
KNOWN_FINALS_COMPETITIONS: dict[int, str] = {
    363: "2022/23 Men's",
    364: "2022/23 Women's",
    414: "2023/24 Men's",
    413: "2023/24 Women's",
    503: "2024/25 Men's",
    504: "2024/25 Women's",
    558: "2025/26 Men's",
    559: "2025/26 Women's",
}

# Altius "Action" text -> the league API's event codes, so downstream code
# (FG/PC/PS = goal, GC/YC/RC = card) needs no special-casing.
GOAL_TYPES = {"Field Goal": "FG", "Penalty Corner": "PC", "Penalty Stroke": "PS"}
CARD_TYPES = {"G": ("GC", "green"), "Y": ("YC", "yellow"), "R": ("RC", "red")}

# Lineup names that must NOT be linked to the league player the name-matcher
# would pick: common name, different club, no scoring evidence either way. A
# fresh player row is created instead (recoverable later with an alias); a
# goal credited to the wrong person is not. Keyed by (team label as our DB
# has it, Altius surname+given name); add the league player's source_id as
# the value if it's ever confirmed to be the same person.
NO_LINK: dict[tuple[str, str], str | None] = {
    ("Surbiton M1", "HUNT Daniel"): None,
    ("Old Georgians M1", "TINDALL James"): None,
    ("Wimbledon M1", "HOOPER Ben"): None,
    ("East Grinstead W1", "CARTER Amelie"): None,
    ("Oxted M1", "JAMIESON Jack"): None,
    ("Wimbledon W1", "BUCHANAN Jessica"): None,
}

# Altius team labels that don't reduce to the club name by normalisation alone.
TEAM_NAME_OVERRIDES = {
    "o georgians": "old georgians",
    "h&w": "hampstead & westminster",
    "h & w": "hampstead & westminster",
}

# First-name spellings that the two systems disagree on. Lower-case, both ways.
NICKNAMES = [
    {"nick", "nicholas"}, {"sam", "samuel", "samantha"}, {"dan", "daniel", "danny"},
    {"chris", "christopher"}, {"ben", "benjamin"}, {"ed", "eddie", "edward"},
    {"phil", "phillip", "philip"}, {"jonny", "jonathan", "jon", "johnny"},
    {"tom", "thomas"}, {"jack", "john"}, {"will", "william"}, {"matt", "matthew"},
    {"alex", "alexander", "alexandra"}, {"harry", "harold", "henry"}, {"jim", "james", "jamie"},
    {"liz", "elizabeth", "lizzie"}, {"kate", "katherine", "catherine", "katie"},
    {"tim", "timothy"}, {"rob", "robert", "robbie"}, {"mike", "michael"},
    {"josh", "joshua"}, {"steve", "stephen", "steven"}, {"charlie", "charles"},
    {"ollie", "oliver"}, {"joe", "joseph"}, {"toby", "tobias"}, {"max", "maximilian"},
    {"freddie", "fred", "frederick"}, {"ali", "alistair", "alastair"}, {"izzy", "isabel", "isabella", "isabelle"},
    {"ellie", "eleanor"}, {"beth", "bethany", "elizabeth"}, {"tess", "tessa"}, {"gabby", "gabriella", "gabrielle"},
    {"lottie", "charlotte"}, {"sophie", "sophia"}, {"em", "emily", "emma"},
    {"nicki", "nicky", "nikki", "nicola"}, {"clemmie", "clementine"}, {"mo", "mohamed", "mohammed", "muhammad"},
    {"jo", "joie", "joanna", "joanne"}, {"livvy", "liv", "olivia"}, {"pippa", "philippa", "phillipa"},
    {"maddie", "maddy", "madeleine", "madeline", "madison", "maddison"},
]


class AltiusClient:
    def __init__(self, base_url: str = BASE_URL, delay_seconds: float = 0.5, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": "field-hockey-analyst (polite scraper; contact via GitHub nickhale-14)"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._delay = delay_seconds

    def get_html(self, path: str) -> str:
        resp = self._client.get(path)
        resp.raise_for_status()
        time.sleep(self._delay)
        return resp.text

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------
# HTML parsing. Stdlib only: the pages are plain server-rendered tables.
# --------------------------------------------------------------------------

@dataclass
class Cell:
    text: str
    hrefs: list[str] = field(default_factory=list)
    is_header: bool = False


@dataclass
class Table:
    rows: list[list[Cell]]

    @property
    def headers(self) -> list[str]:
        for row in self.rows:
            if row and all(c.is_header for c in row):
                return [c.text for c in row]
        return []

    @property
    def body(self) -> list[list[Cell]]:
        return [r for r in self.rows if r and not all(c.is_header for c in r)]


class _PageParser(HTMLParser):
    """Collects every <table> (rows of cells, with link hrefs) and every
    portlet caption, in document order. Tag names arrive lower-cased, which is
    what makes the site's mixed <TR>/<tr> markup a non-issue."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self.captions: list[str] = []
        # Document-order stream of ("caption", str) / ("table", Table), so a
        # table can be tied to the caption that introduced it.
        self.items: list[tuple[str, Any]] = []
        self._table_stack: list[list[list[Cell]]] = []
        self._row: list[Cell] | None = None
        self._cell: Cell | None = None
        self._caption_depth: int | None = None
        self._caption_text: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        a = dict(attrs)
        if tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = Cell(text="", is_header=(tag == "th"))
        elif tag == "a" and self._cell is not None and a.get("href"):
            self._cell.hrefs.append(a["href"])
        elif tag == "div" and "caption" in (a.get("class") or "").split():
            self._caption_depth = self._depth
            self._caption_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell.text = " ".join(self._cell.text.split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table_stack:
            self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            table = Table(rows=self._table_stack.pop())
            self.tables.append(table)
            self.items.append(("table", table))
        elif tag == "div" and self._caption_depth == self._depth:
            caption = " ".join("".join(self._caption_text).split())
            self.captions.append(caption)
            self.items.append(("caption", caption))
            self._caption_depth = None
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.text += data
        if self._caption_depth is not None:
            self._caption_text.append(data)


def _parse(html: str) -> _PageParser:
    p = _PageParser()
    p.feed(html)
    p.close()
    return p


def _find_table(tables: Iterable[Table], *header_prefix: str) -> Table | None:
    for t in tables:
        h = t.headers
        if h[: len(header_prefix)] == list(header_prefix):
            return t
    return None


def _id_from_hrefs(hrefs: list[str], kind: str) -> int | None:
    for h in hrefs:
        m = re.search(rf"/{kind}/(\d+)", h)
        if m:
            return int(m.group(1))
    return None


@dataclass
class MatchListing:
    match_id: int
    number: str
    when: str
    home_abbr: str
    away_abbr: str
    stage: str
    scoreline: str
    status: str


def parse_competition_matches(html: str) -> list[MatchListing]:
    t = _find_table(_parse(html).tables, "Match #", "Date/Time", "Details", "Scoreline", "Status")
    if t is None:
        return []
    out: list[MatchListing] = []
    for row in t.body:
        mid = _id_from_hrefs([h for c in row for h in c.hrefs], "matches")
        if mid is None or len(row) < 5:
            continue
        m = re.match(r"(.+?)\s+v\s+(.+?)\s*(?:\((.*?)\))?\s*$", row[2].text)
        if not m:
            continue
        out.append(
            MatchListing(
                match_id=mid,
                number=row[0].text,
                when=row[1].text,
                home_abbr=m.group(1).strip(),
                away_abbr=m.group(2).strip(),
                stage=(m.group(3) or "").strip(),
                scoreline=row[3].text,
                status=row[4].text,
            )
        )
    return out


def parse_competition_teams(html: str) -> dict[str, str]:
    """abbreviation -> team name as Altius shows it (e.g. 'OLG' -> 'Old Georgians')."""
    out: dict[str, str] = {}
    for t in _parse(html).tables:
        for row in t.body:
            if len(row) >= 2 and re.fullmatch(r"[A-Z0-9&' .-]{2,20}", row[1].text) and row[0].text:
                out[row[1].text] = re.sub(r"\s*\([A-Z]{3}\)\s*$", "", row[0].text)
    return out


@dataclass
class LineupEntry:
    shirt: str
    raw_name: str
    altius_player_id: int | None
    goalkeeper: bool
    captain: bool


@dataclass
class GoalRow:
    team: str
    minute: int | None
    shirt: str
    raw_name: str
    action: str
    score: str


@dataclass
class CardRow:
    team: str
    minute: int | None
    shirt: str
    raw_name: str
    card: str  # G / Y / R
    umpire: str


@dataclass
class ParsedMatch:
    match_id: int
    competition_title: str
    stage: str
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    home_shootout: int | None
    away_shootout: int | None
    status: str
    starts_at: datetime | None
    venue: str | None
    lineups: dict[str, list[LineupEntry]]  # team name -> players
    goals: list[GoalRow]
    cards: list[CardRow]
    officials: list[tuple[str, str]]  # (role, name)


_SCORELINE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*\(\s*(\d+)\s*-\s*(\d+)\s*SO\s*\))?")


def parse_scoreline(text: str) -> tuple[int | None, int | None, int | None, int | None]:
    m = _SCORELINE.match(text or "")
    if not m:
        return None, None, None, None
    h, a, sh, sa = m.groups()
    return int(h), int(a), (int(sh) if sh else None), (int(sa) if sa else None)


def _int_or_none(text: str) -> int | None:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None


def parse_match(html: str, match_id: int) -> ParsedMatch:
    page = _parse(html)
    tables = page.tables

    # "Match Details" is a vertical key/value table (one <th> + one <td> per row).
    details: dict[str, str] = {}
    for t in tables:
        kv = {row[0].text: row[1].text for row in t.rows if len(row) == 2 and row[0].is_header and not row[1].is_header}
        if "Date/Time" in kv and "Match #" in kv:
            details = kv
            break

    h2h = _find_table(tables, "Competition", "Details", "Date/Time", "Teams")
    this_row = None
    if h2h:
        for row in h2h.body:
            if _id_from_hrefs([h for c in row for h in c.hrefs], "matches") == match_id:
                this_row = row
                break
    scoreline = this_row[6].text if this_row and len(this_row) > 6 else ""
    status_text = this_row[5].text if this_row and len(this_row) > 5 else ""
    home_score, away_score, home_so, away_so = parse_scoreline(scoreline)

    # The two lineup tables come home-first, each introduced by a portlet
    # caption holding the team's name as Altius spells it.
    lineup_tables: list[tuple[str, Table]] = []
    last_caption = ""
    for kind, item in page.items:
        if kind == "caption":
            last_caption = item
        elif item.headers[:3] == ["#", "Name", "Min"]:
            lineup_tables.append((last_caption, item))
    team_names = [name for name, _ in lineup_tables]
    lineups: dict[str, list[LineupEntry]] = {}
    for name, t in lineup_tables:
        entries: list[LineupEntry] = []
        for row in t.body:
            if len(row) < 2 or not row[1].text:
                continue
            raw = row[1].text
            if raw.upper() in ("COACH", "MANAGER") or row[0].text.upper() in ("COACH", "MANAGER"):
                continue
            entries.append(
                LineupEntry(
                    shirt=row[0].text,
                    raw_name=raw,
                    altius_player_id=_id_from_hrefs(row[1].hrefs, "players"),
                    goalkeeper="(GK)" in raw,
                    captain="(C)" in raw,
                )
            )
        lineups[name] = entries

    home = team_names[0] if len(team_names) > 0 else ""
    away = team_names[1] if len(team_names) > 1 else ""

    goals: list[GoalRow] = []
    gt = _find_table(tables, "Team", "Minute", "Shirt #", "Player", "Action")
    if gt:
        for row in gt.body:
            if len(row) < 6:
                continue
            goals.append(GoalRow(row[0].text, _int_or_none(row[1].text), row[2].text, row[3].text, row[4].text, row[5].text))

    cards: list[CardRow] = []
    ct = _find_table(tables, "Team", "Minute", "Shirt #", "Player", "Type")
    if ct:
        for row in ct.body:
            if len(row) < 6:
                continue
            card = (row[4].text or "").strip()[:1].upper()
            if card not in CARD_TYPES:
                continue
            cards.append(CardRow(row[0].text, _int_or_none(row[1].text), row[2].text, row[3].text, card, row[5].text))

    officials: list[tuple[str, str]] = []
    ot = _find_table(tables, "Role", "Official")
    if ot:
        for row in ot.body:
            if len(row) >= 2 and row[1].text:
                officials.append((row[0].text.strip(), row[1].text.strip()))

    starts_at = None
    if details.get("Date/Time"):
        try:
            starts_at = datetime.strptime(details["Date/Time"].strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            starts_at = None

    venue = details.get("Venue")
    if venue:
        venue = re.sub(r"\s*-\s*Pitch\s*\d+\s*$", "", venue).strip() or None

    return ParsedMatch(
        match_id=match_id,
        competition_title=re.sub(r"\s+Senior .*$", "", details.get("Competition", "")).strip(),
        stage=details.get("Title", ""),
        home=home,
        away=away,
        home_score=home_score,
        away_score=away_score,
        home_shootout=home_so,
        away_shootout=away_so,
        status=status_text,
        starts_at=starts_at,
        venue=venue,
        lineups=lineups,
        goals=goals,
        cards=cards,
        officials=officials,
    )


# --------------------------------------------------------------------------
# Resolving Altius names onto our league entities
# --------------------------------------------------------------------------

def _ascii_lower(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


_TEAM_SUFFIX = re.compile(r"\s+(m1|w1|1|men|women|ladies|hc|hockey club)$")


def norm_team_name(name: str) -> str:
    s = _ascii_lower(name).replace("&amp;", "&")
    s = re.sub(r"[^a-z0-9& ]+", " ", s)
    s = " ".join(s.split())
    s = TEAM_NAME_OVERRIDES.get(s, s)
    while True:
        s2 = _TEAM_SUFFIX.sub("", s)
        if s2 == s:
            return s
        s = s2


_NAME_PARTICLES = {"le", "la", "de", "da", "di", "du", "del", "van", "von", "der", "den", "mac", "mc", "o", "st"}


def split_altius_name(raw: str) -> tuple[str, str]:
    """'REYNOLDS-COTTERILL Toby (GK)' -> ('Toby', 'Reynolds-Cotterill').
    Altius writes the surname in capitals first, then given names. Role
    markers - '(GK)', '(C)', '(C, GK)' - are dropped."""
    s = re.sub(r"\s*\((?:\s*(?:GK|C)\s*,?\s*)+\)", "", raw, flags=re.I).strip()
    parts = s.split()
    if not parts:
        return "", ""
    # Surname = leading run of all-caps tokens, allowing a lower-case particle
    # before them ('Le HURAY Martha', 'de WIJN Sander').
    surname_tokens: list[str] = []
    for tok in parts:
        if tok.isupper() or tok in ("&", "-") or (tok.lower() in _NAME_PARTICLES and not surname_tokens):
            surname_tokens.append(tok)
        else:
            break
    if not surname_tokens or len(surname_tokens) == len(parts):
        surname_tokens, given = parts[:1], parts[1:]
    else:
        given = parts[len(surname_tokens):]

    def title_case(tok: str) -> str:
        return re.sub(r"(^|[-'’ ])([a-z])", lambda m: m.group(1) + m.group(2).upper(), tok.lower())

    last = " ".join(title_case(t) for t in surname_tokens)
    first = " ".join(title_case(t) if t.isupper() else t for t in given)
    return first, last


def _edit_distance_le_1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1:] == shorter:
            return True
    return False


def _first_names_match(a: str, b: str) -> bool:
    a, b = _ascii_lower(a).split()[:1], _ascii_lower(b).split()[:1]
    if not a or not b:
        return True
    a, b = a[0], b[0]
    if a == b:
        return True
    if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)):
        return True
    if any(a in group and b in group for group in NICKNAMES):
        return True
    # One typo/spelling variant in a longer name (Phillipa/Philippa, Anna-Rose/Anna-Ross).
    a2, b2 = re.sub(r"[^a-z]", "", a), re.sub(r"[^a-z]", "", b)
    return len(a2) >= 6 and len(b2) >= 6 and _edit_distance_le_1(a2, b2)


@dataclass
class TeamRef:
    source_id: str
    name: str
    club_source_id: str
    club_name: str


@dataclass
class PlayerRef:
    source_id: str
    first_name: str
    last_name: str
    club_source_id: str = ""
    teams: set[str] = field(default_factory=set)  # team source_ids they have events for
    club_name: str = ""


def _surname_key(last: str) -> str:
    return re.sub(r"[^a-z]", "", _ascii_lower(last))


def _fullname_key(first: str, last: str) -> str:
    return re.sub(r"[^a-z]", "", _ascii_lower(f"{first} {last}"))


@dataclass
class Resolver:
    seasons: dict[str, str]  # season name ('2024/25') -> season source_id
    teams: dict[tuple[str, str], list[TeamRef]]  # (season name, 'M'|'W') -> Prem teams
    players: list[PlayerRef]
    existing_competitions: dict[str, str] = field(default_factory=dict)  # source_id -> name

    def __post_init__(self) -> None:
        self._by_surname: dict[str, list[PlayerRef]] = {}
        self._by_fullname: dict[str, list[PlayerRef]] = {}
        self._by_sid: dict[str, PlayerRef] = {}
        for p in self.players:
            self._by_surname.setdefault(_surname_key(p.last_name), []).append(p)
            self._by_fullname.setdefault(_fullname_key(p.first_name, p.last_name), []).append(p)
            self._by_sid[p.source_id] = p

    def by_source_id(self, source_id: str) -> PlayerRef | None:
        return self._by_sid.get(source_id)

    def find_team(self, season: str, gender: str, altius_name: str) -> TeamRef | None:
        wanted = norm_team_name(altius_name)
        candidates = self.teams.get((season, gender), [])
        exact = [t for t in candidates if norm_team_name(t.name) == wanted or norm_team_name(t.club_name) == wanted]
        if len(exact) == 1:
            return exact[0]
        loose = [t for t in candidates if norm_team_name(t.name).startswith(wanted) or wanted.startswith(norm_team_name(t.name))]
        return loose[0] if len(loose) == 1 else None

    def find_player(self, team: TeamRef, first: str, last: str) -> PlayerRef | None:
        return self.explain_player(team, first, last)[0]

    def explain_player(self, team: TeamRef, first: str, last: str) -> tuple[PlayerRef | None, str]:
        """
        players.club_id is whichever club the league API first showed the
        person with - it does NOT follow transfers (Nick Bandurak's Surbiton
        goals sit under a Holcombe-club player row). So search every club and
        rank by evidence: has events for this exact team > registered at this
        club > unique name in the whole DB. Anything still ambiguous is left
        unresolved rather than guessed.

        Returns (player, reason). reason is "" for a miss, otherwise a short
        tag - "team" / "club" / "unique-name", suffixed with "+fuzzy-first"
        when the first names differ and "+fullname" when the compound-surname
        fallback was used - so an import can list its weakest decisions.
        """
        via_fullname = False
        cands = [p for p in self._by_surname.get(_surname_key(last), []) if _first_names_match(p.first_name, first)]
        if not cands:
            # The league API splits compound surnames differently ('Martha le'
            # + 'Huray', 'Guadalupe Fernandez' + 'Lacort'): compare whole names.
            cands = list(self._by_fullname.get(_fullname_key(first, last), []))
            via_fullname = bool(cands)
        if len(cands) > 1:
            exact = [p for p in cands if _ascii_lower(p.first_name) == _ascii_lower(first)]
            if len(exact) == 1 and not any(team.source_id in p.teams for p in cands if p is not exact[0]):
                cands = exact
        for tier, pick in (
            ("team", [p for p in cands if team.source_id in p.teams]),
            ("club", [p for p in cands if p.club_source_id == team.club_source_id]),
            ("unique-name", cands),
        ):
            if len(pick) == 1:
                ref = pick[0]
                reason = tier
                if not via_fullname and _ascii_lower(ref.first_name) != _ascii_lower(first):
                    reason += "+fuzzy-first"
                if via_fullname:
                    reason += "+fullname"
                return ref, reason
            if len(pick) > 1:
                return None, ""
        return None, ""


def build_resolver(session: Session) -> Resolver:
    """Snapshot of the league data the finals need to join onto: seasons, the
    Premier Division teams per season/gender, and every player per club."""
    seasons = {s.name: s.source_id for s in session.scalars(select(Season).where(Season.source == SOURCE))}

    # A team "is in the Prem" for a season if it played any match in a
    # competition named "...Premier Division..." that season (league, pools,
    # play-offs). Home and away sides are collected separately.
    teams: dict[tuple[str, str], dict[str, TeamRef]] = {}
    rows = []
    for side in (Match.home_team_id, Match.away_team_id):
        rows += session.execute(
            select(Season.name, Competition.name, Team.source_id, Team.name, Club.source_id, Club.name)
            .select_from(Match)
            .join(Season, Season.id == Match.season_id)
            .join(Competition, Competition.id == Match.competition_id)
            .join(Team, Team.id == side)
            .join(Club, Club.id == Team.club_id)
            .where(Competition.name.like("%Premier Division%"), Match.source == SOURCE)
        ).all()
    for season_name, comp_name, tsid, tname, csid, cname in rows:
        gender = "W" if "women" in comp_name.lower() else "M"
        teams.setdefault((season_name, gender), {})[tsid] = TeamRef(tsid, tname, csid, cname)

    players: dict[str, PlayerRef] = {}
    for csid, cname, psid, first, last in session.execute(
        select(Club.source_id, Club.name, Player.source_id, Player.first_name, Player.last_name)
        .join(Club, Club.id == Player.club_id)
        .where(Player.source == SOURCE)
    ):
        players[psid] = PlayerRef(psid, first, last, club_source_id=csid, club_name=cname)
    # Which teams each player has actually been credited with events for -
    # the strongest evidence of who they play for (see Resolver.find_player).
    for psid, tsid in session.execute(
        select(Player.source_id, Team.source_id)
        .select_from(MatchEvent)
        .join(Player, Player.id == MatchEvent.player_id)
        .join(Team, Team.id == MatchEvent.team_id)
        .distinct()
    ):
        if psid in players:
            players[psid].teams.add(tsid)

    comps = {c.source_id: c.name for c in session.scalars(select(Competition).where(Competition.source == SOURCE))}
    return Resolver(
        seasons=seasons,
        teams={k: list(v.values()) for k, v in teams.items()},
        players=list(players.values()),
        existing_competitions=comps,
    )


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------

def season_name_for(dt: datetime) -> str:
    y = dt.year if dt.month >= 8 else dt.year - 1
    return f"{y}/{(y + 1) % 100:02d}"


def competition_name_for(gender: str) -> str:
    return f"{'Women' if gender == 'W' else 'Men'}'s Premier Division Finals Weekend"


@dataclass
class ImportReport:
    competitions: list[str] = field(default_factory=list)
    matches: int = 0
    players_matched: int = 0
    players_created: list[str] = field(default_factory=list)
    # Matches made without team/club evidence, or with a fuzzy first name -
    # the ones a human should glance at. Keyed to dedupe repeats across matches.
    weak_matches: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


class EhAltiusFinalsSource:
    """Builds one DatasetIn per season for the given Altius competition ids."""

    def __init__(self, client: AltiusClient, resolver: Resolver, competition_ids: Iterable[int]) -> None:
        self._client = client
        self._resolver = resolver
        self._ids = list(competition_ids)
        self.report = ImportReport()
        self._weak_by_sid: dict[str, str] = {}

    def load_all(self) -> list[DatasetIn]:
        by_season: dict[str, DatasetIn] = {}
        for cid in self._ids:
            self._load_competition(cid, by_season)
        return list(by_season.values())

    def _dataset(self, by_season: dict[str, DatasetIn], season_name: str) -> DatasetIn:
        if season_name not in by_season:
            sid = self._resolver.seasons.get(season_name) or f"altius:season:{season_name.replace('/', '-')}"
            by_season[season_name] = DatasetIn(source=SOURCE, season=SeasonIn(source_id=sid, name=season_name))
        return by_season[season_name]

    def _load_competition(self, cid: int, by_season: dict[str, DatasetIn]) -> None:
        listing = parse_competition_matches(self._client.get_html(f"/competitions/{cid}/matches"))
        if not listing:
            self.report.problems.append(f"competition {cid}: no matches found")
            return
        abbrs = parse_competition_teams(self._client.get_html(f"/competitions/{cid}/teams"))

        parsed = [parse_match(self._client.get_html(f"/matches/{ml.match_id}"), ml.match_id) for ml in listing]
        dates = [p.starts_at for p in parsed if p.starts_at]
        if not dates:
            self.report.problems.append(f"competition {cid}: no match dates parsed")
            return
        season_name = season_name_for(max(dates))
        title = parsed[0].competition_title or f"Altius competition {cid}"
        gender = "W" if "women" in title.lower() else "M"
        comp_source_id = f"altius:competition:{cid}"
        comp_name = competition_name_for(gender)

        ds = self._dataset(by_season, season_name)
        if all(c.source_id != comp_source_id for c in ds.competitions):
            ds.competitions.append(CompetitionIn(source_id=comp_source_id, name=comp_name))
        self.report.competitions.append(f"{cid}: {title} -> {season_name} {comp_name}")

        known_players = {p.source_id for p in ds.players}
        known_venues = {v.source_id for v in ds.venues}

        for ml, pm in zip(listing, parsed):
            home_label = pm.home or abbrs.get(ml.home_abbr, ml.home_abbr)
            away_label = pm.away or abbrs.get(ml.away_abbr, ml.away_abbr)
            home = self._resolver.find_team(season_name, gender, home_label)
            away = self._resolver.find_team(season_name, gender, away_label)
            if home is None or away is None:
                self.report.problems.append(
                    f"match {pm.match_id} ({home_label} v {away_label}): could not resolve "
                    f"{'home' if home is None else ''}{' and ' if home is None and away is None else ''}"
                    f"{'away' if away is None else ''} team to a {season_name} Premier Division team"
                )
                continue

            team_by_label = {home_label: home, away_label: away}
            player_ids: dict[tuple[str, str], str] = {}  # (team label, raw name) -> player source_id
            for label, entries in pm.lineups.items():
                team = team_by_label.get(label)
                if team is None:
                    continue
                for e in entries:
                    first, last = split_altius_name(e.raw_name)
                    clean_name = re.sub(r"\s*\(.*?\)", "", e.raw_name).strip()
                    if (team.name, clean_name) in NO_LINK:
                        forced = NO_LINK[(team.name, clean_name)]
                        ref, reason = (self._resolver.by_source_id(forced), "override") if forced else (None, "")
                    else:
                        ref, reason = self._resolver.explain_player(team, first, last)
                    if ref is not None:
                        player_ids[(label, e.raw_name)] = ref.source_id
                        self.report.players_matched += 1
                        if reason != "team":
                            self.report.weak_matches.setdefault(
                                f"{season_name} {team.name}: '{e.raw_name}'",
                                f"-> {ref.first_name} {ref.last_name} ({ref.club_name or ref.club_source_id}) {reason}",
                            )
                            self._weak_by_sid.setdefault(ref.source_id, f"{season_name} {team.name}: '{e.raw_name}'")
                        continue
                    sid = f"altius:player:{e.altius_player_id}" if e.altius_player_id else f"altius:player:{team.club_source_id}:{_ascii_lower(last)}-{_ascii_lower(first)}"
                    player_ids[(label, e.raw_name)] = sid
                    if sid not in known_players:
                        ds.players.append(PlayerIn(source_id=sid, club_source_id=team.club_source_id, first_name=first, last_name=last))
                        known_players.add(sid)
                        self.report.players_created.append(f"{team.club_name}: {first} {last} ({sid})")
                    # Same person may already be resolvable next time via dedupe.

            venue_sid = None
            if pm.venue:
                venue_sid = f"altius:venue:{re.sub(r'[^a-z0-9]+', '-', _ascii_lower(pm.venue)).strip('-')}"
                if venue_sid not in known_venues:
                    ds.venues.append(VenueIn(source_id=venue_sid, name=pm.venue))
                    known_venues.add(venue_sid)

            events: list[MatchEventIn] = []
            for n, g in enumerate(pm.goals, start=1):
                team = team_by_label.get(g.team) or (home if norm_team_name(g.team) == norm_team_name(home_label) else away)
                code = GOAL_TYPES.get(g.action)
                if code is None:
                    self.report.problems.append(f"match {pm.match_id}: unknown goal action '{g.action}'")
                    continue
                events.append(
                    MatchEventIn(
                        source_id=f"altius:match:{pm.match_id}:goal:{n}",
                        type=code,
                        team_source_id=team.source_id,
                        player_source_id=player_ids.get((g.team, g.raw_name)) or self._player_by_name(player_ids, g.team, g.raw_name),
                        minute=g.minute,
                    )
                )
            for n, c in enumerate(pm.cards, start=1):
                team = team_by_label.get(c.team) or (home if norm_team_name(c.team) == norm_team_name(home_label) else away)
                code, colour = CARD_TYPES[c.card]
                events.append(
                    MatchEventIn(
                        source_id=f"altius:match:{pm.match_id}:card:{n}",
                        type=code,
                        team_source_id=team.source_id,
                        player_source_id=player_ids.get((c.team, c.raw_name)) or self._player_by_name(player_ids, c.team, c.raw_name),
                        minute=c.minute,
                        card=colour,
                    )
                )

            for ev in events:
                key = self._weak_by_sid.get(ev.player_source_id or "")
                if key and key in self.report.weak_matches and "** has " not in self.report.weak_matches[key]:
                    what = "goal" if ev.type in GOAL_TYPES.values() else "card"
                    self.report.weak_matches[key] += f"  ** has a {what} in the finals - check this one **"

            status = {"official": "R", "upcoming": "S", "abandoned": "A", "postponed": "P"}.get(pm.status.lower(), "R" if pm.home_score is not None else "S")
            starts_at = pm.starts_at or _parse_listing_datetime(ml.when)
            ds.matches.append(
                MatchIn(
                    source_id=f"altius:match:{pm.match_id}",
                    season_source_id=ds.season.source_id,
                    competition_source_id=comp_source_id,
                    home_team_source_id=home.source_id,
                    away_team_source_id=away.source_id,
                    venue_source_id=venue_sid,
                    starts_at=starts_at,
                    status=status,
                    home_score=pm.home_score,
                    away_score=pm.away_score,
                    home_shootout=pm.home_shootout,
                    away_shootout=pm.away_shootout,
                    events=events,
                    officials=[MatchOfficialIn(name=name, role=role) for role, name in pm.officials],
                )
            )
            self.report.matches += 1

    @staticmethod
    def _player_by_name(player_ids: dict[tuple[str, str], str], team: str, raw_name: str) -> str | None:
        # Goal/card rows and lineup rows can differ in trailing whitespace or
        # the (GK)/(C) markers; fall back to comparing the split name.
        want = split_altius_name(raw_name)
        for (label, name), sid in player_ids.items():
            if norm_team_name(label) == norm_team_name(team) and split_altius_name(name) == want:
                return sid
        return None


def _parse_listing_datetime(text: str) -> datetime:
    return datetime.strptime(" ".join(text.split()), "%d %b %Y %H:%M")
