"""
German 1. Bundesliga (field, men + women) importer - scrapes hockeybundesliga.de.

The Hockeyliga's site has no API, but it is server-rendered and keyless, has
no terms of use (only an Impressum), and its robots.txt disallows nothing but
/admin/. This reads three kinds of page, politely (one request at a time, a
short sleep between):

  season page    /match-center/... (current) or /die-hockeyliga/service/archiv/...
                 -> the league id (data-league) and every match date (data-matchdate)
  match day      /matchcenter/matchdaytab/<league>/<YYYY-MM-DD>
                 -> fixtures: clubs (stable slugs), score, round, venue, match path
  match report   /leaguematch/<comp>/<pairing>/report
                 -> kickoff, umpires, HT/FT/shootout, goals + cards per side, squads
  match page     /leaguematch/<comp>/<pairing>  (only for matches with goals)
                 -> the live ticker, which is the only place goal TYPE is given

Coverage: 2022/23 has scores and scorer names only (every minute is 0', no
squads, no cards). 2023/24 onward has minutes, cards, squads and typed goals.

Identity: this goes into its OWN database (hockey_stats_de), never the England
one - the English analysis code reads every played match with no source filter.
Everything is source="hockeybundesliga". Clubs are keyed on the site's club
slug, which is stable across seasons (the per-season team ids in the URLs are
not). Teams are "<club>:M" / "<club>:W" and named "<club> M1" / "<club> W1",
like England's. The site gives players no ids, so a player is club + name.

Scores: draws have been settled by a shootout ("n.P.") in recent seasons. The
match score stored is the drawn regulation score (so goal stats are right) and
the shootout goes in home_shootout/away_shootout, exactly as for England's
finals. Goals whose type the ticker doesn't give (2022/23, or a match without a
ticker) are stored as event_type "G" - a goal of unknown type - rather than
guessed as field goals.

Times are German local time (naive), as the site shows them.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import httpx

from ingest.schemas import (
    ClubIn,
    CompetitionIn,
    DatasetIn,
    MatchEventIn,
    MatchIn,
    MatchOfficialIn,
    MatchPlayerIn,
    PlayerIn,
    SeasonIn,
    TeamIn,
    VenueIn,
)

BASE_URL = "https://www.hockeybundesliga.de"
SOURCE = "hockeybundesliga"

# Season -> gender -> the page that lists that season's match dates. The
# current season lives in the match centre; finished ones move to the archive
# (/die-hockeyliga/service/archiv), so when 2026/27 ends, repoint it there.
SEASONS: dict[str, dict[str, str]] = {
    "2022/23": {
        "M": "/die-hockeyliga/service/archiv/feldhockey-2022-2023-1-bundesliga-herren",
        "W": "/die-hockeyliga/service/archiv/feldhockey-2022-2023-1-bundesliga-damen",
    },
    "2023/24": {
        "M": "/die-hockeyliga/service/archiv/feldhockey-2023-2024-1-bundesliga-herren",
        "W": "/die-hockeyliga/service/archiv/feldhockey-2023-2024-1-bundesliga-damen",
    },
    "2024/25": {
        "M": "/die-hockeyliga/service/archiv/feldhockey-2024-2025-1-bundesliga-herren",
        "W": "/die-hockeyliga/service/archiv/feldhockey-2024-2025-1-bundesliga-damen",
    },
    "2025/26": {
        "M": "/die-hockeyliga/service/archiv/feldhockey-2025-2026-1-bundesliga-herren",
        "W": "/die-hockeyliga/service/archiv/feldhockey-2025-2026-1-bundesliga-damen",
    },
    "2026/27": {
        "M": "/match-center/1-bundesliga-1/herren",
        "W": "/match-center/1-bundesliga-1/damen",
    },
}

GENDER_LABEL = {"M": "Men's", "W": "Women's"}

# Round labels on the match-day fragment. Anything not listed stops the import
# so a new kind of round gets looked at rather than silently filed as league.
# 2022/23 and 2023/24 split the league into two pools of six (2022/23 "Staffel
# A/B", 2023/24 "Pool A/B"), each a double round-robin, plus games against the
# other pool (2022/23 "Hinrunde", 2023/24 "Staffel"). All league; the pools
# weren't seeded by strength. H/D suffixes are Herren/Damen.
REGULAR_ROUNDS = {"Hauptrunde", "Hinrunde", "Rückrunde", "Staffel", "Pool A", "Pool B", "Staffel A", "Staffel B"}
PLAYOFF_ROUNDS = {
    "Playoffs", "Viertelfinale", "Halbfinale", "Finale", "Final4", "Final 4", "Spiel um Platz 3",
    "K.O. Phase H", "K.O. Phase D",
}
PLAYDOWN_ROUNDS = {"Playdowns", "Playdown H", "Playdown D"}  # the bottom sides' relegation round

# Card icon fill colour on the report page -> (event_type, card).
CARD_FILLS = {
    "#007836": ("GC", "green"),    # triangle
    "#f8d200": ("YC", "yellow"),   # rounded square
    "#ff0014": ("RC", "red"),      # circle
}

# Results the site never published on the match itself (match day shows only
# the kickoff, the report is empty) but that are confirmed elsewhere. Same bar
# as England's corrections.SCORE_CORRECTIONS: human-confirmed only, evidence
# recorded. Applied on import, so a re-import keeps them. No scorers - the
# site has none to give.
# match path -> (home_score, away_score, why)
MISSING_RESULTS: dict[str, tuple[int, int, str]] = {
    "/leaguematch/1-feldhockey-bundesliga-damen-22-23-bndbl1d/hthc--msc": (
        4, 2,
        "HTHC v Münchner SC W, 3 Sep 2022: the site's own 2022/23 table counts it as a "
        "4-2 HTHC win (HTHC +1 W, 4:2; MSC +1 L, 2:4); Flashscore shows it finished 4-2. "
        "Confirmed by the user 2026-10-20.",
    ),
}

GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}


class HblClient:
    """Polite: one request at a time, a 2s pause after each. With a cache_dir,
    pages fetched with cache=True (reports and tickers of FINISHED matches,
    which don't change) are kept on disk, so re-running after a parser fix
    costs no requests. Delete the folder to force a refetch."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        delay_seconds: float = 2.0,
        timeout: float = 30.0,
        cache_dir: Path | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": "field-hockey-analyst (polite scraper; contact via GitHub nickhale-14)"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._delay = delay_seconds
        self._cache = cache_dir
        self.requests = 0
        self.cache_hits = 0

    def get_html(self, path: str, params: dict | None = None, cache: bool = False) -> str:
        cached = self._cache / (path.strip("/").replace("/", "__") + ".html") if (cache and self._cache) else None
        if cached is not None and cached.exists():
            self.cache_hits += 1
            return cached.read_text(encoding="utf-8")
        html = self._fetch(path, params)
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(html, encoding="utf-8")
        return html

    def _fetch(self, path: str, params: dict | None, attempts: int = 4) -> str:
        # A season is ~600 requests; one dropped connection shouldn't lose the
        # lot, so retry network errors and 5xx with a growing wait.
        for attempt in range(1, attempts + 1):
            try:
                resp = self._client.get(path, params=params)
                self.requests += 1
                if resp.status_code < 500:
                    resp.raise_for_status()
                    time.sleep(self._delay)
                    return resp.text
                if attempt == attempts:
                    resp.raise_for_status()
            except httpx.TransportError:
                if attempt == attempts:
                    raise
            time.sleep(10 * attempt)
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------
# A small DOM. The pages are deeply nested divs told apart by BEM class names,
# so this builds a tree with the stdlib parser and finds nodes by class.
# --------------------------------------------------------------------------

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list["Node | str"] = field(default_factory=list)

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def find_all(self, cls: str | None = None, tag: str | None = None) -> list["Node"]:
        out: list[Node] = []
        stack = list(reversed(self.children))
        while stack:
            n = stack.pop()
            if isinstance(n, str):
                continue
            if (cls is None or n.has_class(cls)) and (tag is None or n.tag == tag):
                out.append(n)
            stack.extend(reversed(n.children))
        return out

    def find(self, cls: str | None = None, tag: str | None = None) -> "Node | None":
        found = self.find_all(cls, tag)
        return found[0] if found else None

    def text(self) -> str:
        parts: list[str] = []
        stack: list[Node | str] = [self]
        while stack:
            n = stack.pop()
            if isinstance(n, str):
                parts.append(n)
            elif n.tag not in ("script", "style", "svg"):
                stack.extend(reversed(n.children))
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def svg_fills(self) -> list[str]:
        """Fill colours drawn by the icons under this node, in order. Skips
        <defs> (the yellow card's clip-path mask is filled white)."""
        out: list[str] = []
        stack: list[Node | str] = list(reversed(self.children))
        while stack:
            n = stack.pop()
            if isinstance(n, str) or n.tag in ("defs", "clippath"):
                continue
            fill = (n.attrs.get("fill") or "").lower()
            if fill and fill != "none":
                out.append(fill)
            stack.extend(reversed(n.children))
        return out


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        # Close back to the matching open tag; tolerate stray end tags.
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    b = _TreeBuilder()
    b.feed(html)
    b.close()
    return b.root


# --------------------------------------------------------------------------
# Page parsers
# --------------------------------------------------------------------------

def parse_season_page(html: str) -> tuple[int, list[str]]:
    """The league id and every match date listed on a season page."""
    leagues = set(re.findall(r'data-league="(\d+)"', html))
    if len(leagues) != 1:
        raise ValueError(f"expected one data-league on the season page, found {sorted(leagues)}")
    dates = sorted(set(re.findall(r'data-matchdate="(\d{4}-\d{2}-\d{2})"', html)))
    return int(leagues.pop()), dates


@dataclass
class FixtureRow:
    match_path: str          # /leaguematch/<comp>/<pairing>, the match's stable key
    date: str                # YYYY-MM-DD
    home_club: str           # club slug
    home_name: str
    home_logo: str | None
    away_club: str
    away_name: str
    away_logo: str | None
    played: bool
    kickoff: str | None      # "20:30" for a match not yet played
    home_score: int | None   # as the match day shows it (includes shootout goals after n.P.)
    away_score: int | None
    shootout: bool
    round: str
    venue: str | None


def _club_slug(href: str) -> str:
    m = re.match(r"^/club/([^/]+)/", href)
    if not m:
        raise ValueError(f"unexpected team link {href!r}")
    return m.group(1)


def parse_matchday(html: str, date: str) -> list[FixtureRow]:
    root = parse_html(html)
    rows: list[FixtureRow] = []
    for row in root.find_all("table-schedule-condensed__table-row"):
        home_a = row.find("table-schedule-condensed__link--home")
        away_a = row.find("table-schedule-condensed__link--away")
        if home_a is None or away_a is None:
            # An unfilled knockout slot ("TEAM 1 v TEAM 2, K.O. Phase D") - no teams, nothing to load.
            continue

        def _team(a: Node) -> tuple[str, str, str | None]:
            name = a.find("mc-lite__team--desktop")
            img = a.find(tag="img")
            return _club_slug(a.attrs["href"]), name.text() if name else a.text(), img.attrs.get("src") if img else None

        score_cell = next(
            (c for c in row.find_all("table-schedule-condensed__table-cell") if "leaguematch/" in c.attrs.get("onclick", "")),
            None,
        )
        if score_cell is None:
            raise ValueError(f"{date}: fixture row without a match link: {row.text()[:120]!r}")
        path = re.search(r"(/leaguematch/[^'#?]+)", score_cell.attrs["onclick"]).group(1)
        wrapper = score_cell.find("table-schedule-condensed__time-wrapper")
        played = wrapper is not None and wrapper.has_class("table-schedule-condensed__time-wrapper--final")
        txt = score_cell.text()
        home_score = away_score = None
        kickoff = None
        if played:
            m = re.match(r"(\d+)\s*:\s*(\d+)", txt)
            if not m:
                raise ValueError(f"{date} {path}: played but no score in {txt!r}")
            home_score, away_score = int(m.group(1)), int(m.group(2))
        else:
            k = score_cell.find("table-schedule-condensed__time")
            kickoff = k.text() if k and re.fullmatch(r"\d{1,2}:\d{2}", k.text()) else None  # "--:--" = not set yet

        group = row.find("table-schedule-condensed__table-cell--group")
        venue_cell = row.find("table-schedule-condensed__table-cell--venue")
        hc, hn, hl = _team(home_a)
        ac, an, al = _team(away_a)
        rows.append(
            FixtureRow(
                match_path=path, date=date,
                home_club=hc, home_name=hn, home_logo=hl,
                away_club=ac, away_name=an, away_logo=al,
                played=played, kickoff=kickoff,
                home_score=home_score, away_score=away_score,
                shootout="n.P." in txt,
                round=group.text() if group else "",
                venue=(venue_cell.text() or None) if venue_cell else None,
            )
        )
    return rows


@dataclass
class ReportPlayer:
    first: str
    last: str
    number: int | None
    goalkeeper: bool
    captain: bool

    @property
    def full(self) -> str:
        return f"{self.first} {self.last}".strip()


@dataclass
class ReportEvent:
    side: str                # "home" / "away"
    kind: str                # "goal" / "card"
    name: str
    period: int | None
    minute: int | None
    score: str | None = None  # running score after a goal, e.g. "2:1"
    card: tuple[str, str] | None = None
    card_fill: str | None = None  # set when the icon colour isn't a known card


@dataclass
class MatchReport:
    starts_at: datetime | None
    home: str
    away: str
    home_score: int          # regulation score (shootout goals not included)
    away_score: int
    home_shootout: int | None
    away_shootout: int | None
    umpires: list[str]
    venue: str | None
    events: list[ReportEvent]
    squads: dict[str, list[ReportPlayer]]
    staff: dict[str, set[str]] = field(default_factory=dict)
    shootout_kicks: tuple[int, int] | None = None  # scored kicks as listed, to cross-check the score


def _parse_german_datetime(text: str) -> datetime | None:
    # "Sonntag, 18. Oktober 2026, 13:00 MEZ"
    m = re.search(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})(?:,\s*(\d{1,2}):(\d{2}))?", text)
    if not m:
        return None
    month = GERMAN_MONTHS.get(m.group(2).lower())
    if month is None:
        raise ValueError(f"unknown month in {text!r}")
    return datetime(int(m.group(3)), month, int(m.group(1)), int(m.group(4) or 0), int(m.group(5) or 0))


def _event_name_minute(text: str) -> tuple[str, int | None]:
    # "Paul Smith, 3’" - minute 0 means the site has no minute (2022/23).
    m = re.match(r"^(.*?),\s*(\d+)\s*[’'′]?\s*$", text)
    if not m:
        return text.strip(), None
    minute = int(m.group(2))
    return m.group(1).strip(), minute or None


def parse_report(html: str) -> MatchReport:
    root = parse_html(html)

    header = root.find("match-header__info-bar")
    date_node = header.find("match-header__info-bar-match-date") if header else None
    starts_at = _parse_german_datetime(date_node.text()) if date_node else None
    ump = header.find("match-header__info-bar-match-umpire") if header else None
    umpires = [s.text().rstrip(",").strip() for s in ump.find_all(tag="span")] if ump else []
    umpires = [u for u in umpires if u]
    loc = header.find("match-header__location-label") if header else None

    board = root.find("match-header__score-board")
    teams = [n.text() for n in board.find_all("match-header__country")]
    score = [int(x) for x in re.findall(r"\d+", board.find("match-header__score").text())]
    shootout = board.find("shootout-badge") is not None
    if shootout:
        # "( 2 : 1 , 3 : 3 )" -> HT, then the regulation score; the big score includes the shootout.
        inner = [int(x) for x in re.findall(r"\d+", board.find("match-header__halftime-score").text())]
        reg_h, reg_a = inner[-2], inner[-1]
        so_h, so_a = score[0] - reg_h, score[1] - reg_a
        if so_h == so_a == 0:
            # Badge but the regulation score equals the final (2022/23 UHC v DHC
            # W, "n.P. 4:3 (4:3, 4:3)", goal events 4-3): not a drawn match, so
            # no shootout. The listed kicks then come back as a warning.
            so_h = so_a = None
    else:
        reg_h, reg_a = score
        so_h = so_a = None

    events: list[ReportEvent] = []
    shootout_hits = {"home": 0, "away": 0}
    for legend in root.find_all("match-report__legend"):
        title = legend.find("match-report__legend-title")
        title_text = title.text() if title else ""
        if title_text not in ("Tore & Karten", "Shootout"):
            continue
        for row in legend.find_all("match-report__legend-table-row"):
            when = row.find("match-report__legend-table-row-time")
            period = None
            if when:
                pm = re.match(r"(\d)/4", when.text())
                period = int(pm.group(1)) if pm else None
            for col in row.find_all("match-report__legend-table-row-actions-col"):
                side = "away" if col.has_class("mod-col-away") else "home"
                for item in col.find_all("match-report__legend-table-row-actions-item"):
                    spans = [c for c in item.children if isinstance(c, Node) and c.tag == "span"]
                    if len(spans) < 2:
                        continue
                    marker, who = spans[0], spans[1].text()
                    if title_text == "Shootout":
                        if re.fullmatch(r"\d+:\d+", marker.text()):
                            shootout_hits[side] += 1
                        continue
                    name, minute = _event_name_minute(who)
                    if re.fullmatch(r"\d+:\d+", marker.text()):
                        events.append(ReportEvent(side, "goal", name, period, minute, score=marker.text()))
                    else:
                        fills = marker.svg_fills()
                        fill = fills[0] if fills else None
                        card = CARD_FILLS.get(fill) if fill else None
                        events.append(ReportEvent(side, "card", name, period, minute, card=card, card_fill=None if card else fill))

    squads: dict[str, list[ReportPlayer]] = {}
    staff: dict[str, set[str]] = {}
    for side, cls in (("home", "lineup__team-one"), ("away", "lineup__team-two")):
        block = root.find(cls)
        staff[side] = {
            n.text()
            for group in (block.find_all("player-group") if block else [])
            if (t := group.find("match-report__players-group-title")) and t.text() == "Staff"
            for n in group.find_all("player-group__player-name")
        }
        players: list[ReportPlayer] = []
        seen: set[tuple[str, int | None]] = set()
        containers = [
            pc
            for group in (block.find_all("player-group") if block else [])
            if not ((t := group.find("match-report__players-group-title")) and t.text() == "Staff")
            for pc in group.find_all("player-group__player-container")
        ]
        for pc in containers:
            name_node = pc.find("player-group__player-name")
            if name_node is None:
                continue
            last_node = name_node.find(tag="strong")
            last = last_node.text() if last_node else ""
            full = name_node.text()
            first = full[: len(full) - len(last)].strip() if last and full.endswith(last) else full
            pos = (pc.find("player-group__player-position").text() if pc.find("player-group__player-position") else "")
            num_node = pc.find("player-group__player-number")
            num = int(num_node.text()) if num_node and num_node.text().isdigit() else None
            key = (full, num)
            if key in seen:
                continue
            seen.add(key)
            players.append(ReportPlayer(first, last, num, goalkeeper=pos in ("TW", "ETW"), captain=pos == "K"))
        squads[side] = players

    return MatchReport(
        starts_at=starts_at,
        home=teams[0], away=teams[1],
        home_score=reg_h, away_score=reg_a,
        home_shootout=so_h, away_shootout=so_a,
        umpires=umpires,
        venue=loc.text() if loc else None,
        events=events,
        squads=squads,
        staff=staff,
        shootout_kicks=(shootout_hits["home"], shootout_hits["away"]) if shootout else None,
    )


def goal_type(label: str) -> str | None:
    """Ticker goal label ("FELDTOR", "KURZE ECKE - TOR", "7M", ...) -> FG/PC/PS.
    7M is the 7-metre penalty, i.e. a penalty stroke."""
    up = label.upper()
    if "ECKE" in up:
        return "PC"
    if up in ("7M", "7 M") or "SIEBENMETER" in up or "7-METER" in up or "STROKE" in up or "PENALTY" in up:
        return "PS"
    if "FELDTOR" in up:
        return "FG"
    return None


def parse_ticker_goals(html: str) -> dict[str, str]:
    """Running score -> goal label, from the match page's live ticker
    ("TOR 3:3, FELDTOR" -> {"3:3": "FELDTOR"}). Empty when there's no ticker."""
    root = parse_html(html)
    out: dict[str, str] = {}
    for block in root.find_all("liveticker--goal"):
        heading = block.find("liveticker__heading")
        if heading is None:
            continue
        m = re.match(r"TOR\s+(\d+:\d+)\s*,\s*(.+)$", heading.text())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _split_name(full: str) -> tuple[str, str]:
    parts = full.split(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", full)


@dataclass
class ImportReport:
    seasons: list[str] = field(default_factory=list)
    matches: int = 0
    played: int = 0
    goals_untyped: int = 0
    skipped: int = 0
    # problems stop the import (something new the parser doesn't understand);
    # warnings are the source disagreeing with itself, reported and loaded anyway.
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class HockeyBundesligaSource:
    def __init__(
        self,
        client: HblClient,
        seasons: list[str],
        genders: list[str],
        log=print,
        skip_played: set[str] | None = None,
    ) -> None:
        self.client = client
        self.seasons = seasons
        self.genders = genders
        # Match paths already loaded as played: left alone, so a weekly run only
        # fetches what's newly finished.
        self.skip_played = skip_played or set()
        self.report = ImportReport()
        self._log = log

    def load_all(self) -> list[DatasetIn]:
        return [self.load_season(s) for s in self.seasons]

    def load_season(self, season: str) -> DatasetIn:
        ds = DatasetIn(source=SOURCE, generated_at=datetime.now(), season=SeasonIn(source_id=season, name=season))
        clubs: dict[str, ClubIn] = {}
        teams: dict[str, TeamIn] = {}
        venues: dict[str, VenueIn] = {}
        players: dict[str, PlayerIn] = {}
        comps: dict[str, CompetitionIn] = {}

        for gender in self.genders:
            page = SEASONS[season][gender]
            league_id, dates = parse_season_page(self.client.get_html(page))
            label = f"{season} {GENDER_LABEL[gender]} (league {league_id}, {len(dates)} match days)"
            self._log(f"  {label}")
            seen_paths: set[str] = set()
            for date in dates:
                html = self.client.get_html(f"/matchcenter/matchdaytab/{league_id}/{date}", {"isHomePage": 0})
                for fx in parse_matchday(html, date):
                    if fx.match_path in seen_paths:
                        continue  # a match listed on two days (rescheduled) - keep the first
                    seen_paths.add(fx.match_path)
                    match = self._build_match(season, gender, league_id, fx, clubs, teams, venues, players, comps)
                    if match is not None:
                        ds.matches.append(match)
            self.report.seasons.append(label)

        ds.competitions = list(comps.values())
        ds.clubs = list(clubs.values())
        ds.teams = list(teams.values())
        ds.venues = list(venues.values())
        ds.players = list(players.values())
        return ds

    def _build_match(self, season, gender, league_id, fx: FixtureRow, clubs, teams, venues, players, comps) -> MatchIn | None:
        if fx.round in REGULAR_ROUNDS:
            comp_key, comp_name = f"{league_id}:league", f"{GENDER_LABEL[gender]} 1. Bundesliga"
        elif fx.round in PLAYOFF_ROUNDS:
            comp_key, comp_name = f"{league_id}:playoffs", f"{GENDER_LABEL[gender]} 1. Bundesliga Play-offs"
        elif fx.round in PLAYDOWN_ROUNDS:
            comp_key, comp_name = f"{league_id}:playdowns", f"{GENDER_LABEL[gender]} 1. Bundesliga Play-downs"
        else:
            self.report.problems.append(f"{season} {fx.match_path}: unknown round {fx.round!r}")
            return None
        comps.setdefault(comp_key, CompetitionIn(source_id=comp_key, name=comp_name))

        team_ids = {}
        for side, slug, name, logo in (("home", fx.home_club, fx.home_name, fx.home_logo), ("away", fx.away_club, fx.away_name, fx.away_logo)):
            clubs[slug] = ClubIn(source_id=slug, name=name, logo_url=f"{BASE_URL}{logo.split('?')[0]}" if logo else None)
            tid = f"{slug}:{gender}"
            teams[tid] = TeamIn(source_id=tid, club_source_id=slug, name=f"{name} {gender}1")
            team_ids[side] = tid
        if fx.venue:
            venues.setdefault(fx.venue, VenueIn(source_id=fx.venue, name=fx.venue))

        self.report.matches += 1
        match = MatchIn(
            source_id=fx.match_path,
            season_source_id=season,
            competition_source_id=comp_key,
            home_team_source_id=team_ids["home"],
            away_team_source_id=team_ids["away"],
            venue_source_id=fx.venue,
            starts_at=datetime.fromisoformat(f"{fx.date}T{fx.kickoff or '00:00'}"),
            status="S",
        )
        if not fx.played and fx.match_path in MISSING_RESULTS:
            match.status = "R"
            match.home_score, match.away_score, _ = MISSING_RESULTS[fx.match_path]
            self.report.played += 1
            return match
        if not fx.played:
            return match
        if fx.match_path in self.skip_played:
            self.report.skipped += 1
            return None

        report = parse_report(self.client.get_html(f"{fx.match_path}/report", cache=True))
        self.report.played += 1
        match.status = "R"
        match.starts_at = report.starts_at or match.starts_at
        match.home_score, match.away_score = report.home_score, report.away_score
        match.home_shootout, match.away_shootout = report.home_shootout, report.away_shootout
        match.officials = [MatchOfficialIn(name=u, role="Umpire") for u in report.umpires]
        if report.shootout_kicks is not None and report.shootout_kicks != (report.home_shootout, report.away_shootout):
            self.report.warnings.append(
                f"{fx.match_path}: shootout {report.home_shootout}-{report.away_shootout} from the score, "
                f"{report.shootout_kicks[0]}-{report.shootout_kicks[1]} from the listed kicks"
            )

        shown = (report.home_score + (report.home_shootout or 0), report.away_score + (report.away_shootout or 0))
        if shown != (fx.home_score, fx.away_score):
            self.report.warnings.append(f"{fx.match_path}: match day says {fx.home_score}-{fx.away_score}, report says {shown}")

        # Squads, and a name -> player id map per side for the events.
        by_name: dict[str, dict[str, str]] = {"home": {}, "away": {}}
        for side in ("home", "away"):
            club = fx.home_club if side == "home" else fx.away_club
            for i, p in enumerate(report.squads.get(side, [])):
                pid = f"{club}:{_slug(p.full)}"
                players.setdefault(pid, PlayerIn(source_id=pid, club_source_id=club, first_name=p.first, last_name=p.last))
                by_name[side][p.full] = pid
                match.squad.append(
                    MatchPlayerIn(
                        source_id=f"{fx.match_path}:{side}:{i}",
                        team_source_id=team_ids[side],
                        player_source_id=pid,
                        shirt_number=p.number,
                        is_goalkeeper=p.goalkeeper,
                        is_captain=p.captain,
                    )
                )

        goals = [e for e in report.events if e.kind == "goal"]
        ticker = self._ticker(fx.match_path) if goals else {}
        for n, e in enumerate(report.events):
            if e.name in report.staff.get(e.side, set()):
                # A card shown to a coach, not a player (cf. England's EVENT_REMOVALS) - not a player event.
                self.report.warnings.append(f"{fx.match_path}: {e.kind} for staff member {e.name} skipped")
                continue
            club = fx.home_club if e.side == "home" else fx.away_club
            pid = by_name[e.side].get(e.name)
            if pid is None and e.name:
                pid = f"{club}:{_slug(e.name)}"
                first, last = _split_name(e.name)
                players.setdefault(pid, PlayerIn(source_id=pid, club_source_id=club, first_name=first, last_name=last))
            if e.kind == "goal":
                label = ticker.get(e.score or "")
                etype = goal_type(label) if label else None
                if label and etype is None:
                    self.report.problems.append(f"{fx.match_path}: unknown goal label {label!r}")
                if etype is None:
                    etype = "G"
                    self.report.goals_untyped += 1
                card = None
            else:
                if e.card is None:
                    self.report.problems.append(f"{fx.match_path}: card with unknown icon colour {e.card_fill!r} ({e.name})")
                    continue
                etype, card = e.card
            match.events.append(
                MatchEventIn(
                    source_id=f"{fx.match_path}:event:{n}",
                    type=etype,
                    team_source_id=team_ids[e.side],
                    player_source_id=pid,
                    period=e.period,
                    minute=e.minute,
                    card=card,
                )
            )

        n_goals = (sum(1 for e in goals if e.side == "home"), sum(1 for e in goals if e.side == "away"))
        if goals and n_goals != (report.home_score, report.away_score):
            self.report.warnings.append(
                f"{fx.match_path}: goal events {n_goals[0]}-{n_goals[1]} vs score {report.home_score}-{report.away_score}"
            )
        return match

    def _ticker(self, path: str) -> dict[str, str]:
        try:
            return parse_ticker_goals(self.client.get_html(path, cache=True))
        except httpx.HTTPStatusError:
            return {}
