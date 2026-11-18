"""
Scores we know to be wrong at the source, with the evidence that settles them.

Same philosophy as aliases.py: human-confirmed only, applied by `ingest repair`
so a re-import can never quietly reintroduce the error, and every entry carries
the reason so a future reader can check the call rather than trust it.

Only add an entry when a BETTER source contradicts the first AND the evidence is
specific (goal-by-goal, not just a different number somewhere). "Better" means
closer to the governing body: England Hockey's own systems outrank a commercial
aggregator like Flashscore.
"""
from __future__ import annotations

# match source_id -> (home_score, away_score, why)
SCORE_CORRECTIONS: dict[str, tuple[int, int, str]] = {
    # Flashscore's page said 3-2; England Hockey's own Altius record
    # (englandhockey.altiusrt.com/matches/2529) says 2-2 and lists every goal
    # with scorer, minute and running score: 19' James PC 1-0, 24' James PC 2-0,
    # 32' Hughes FG 2-1, 62' Rainey PC 2-2. Found 2026-09-24 by
    # `ingest verify-flashscore`, the only disagreement in 233 matches.
    "flashscore:match:2019-20:W:20191207:buckingham-w:bowdon-w": (
        2, 2, "Altius match 2529 lists 4 goals summing to 2-2; Flashscore had 3-2",
    ),
}


# match_events source_id -> why. Events the source logged that are not what they
# look like. Removed by `ingest repair` so a re-import cannot quietly restore them.
#
# Same bar as SCORE_CORRECTIONS: human-confirmed only, evidence recorded, and
# specific enough that a future reader can re-check the call rather than trust it.
EVENT_REMOVALS: dict[str, str] = {
    # A red card logged against East Grinstead W1 at 70' on 19 Apr 2026
    # (Women's Premier Division League Finals Pool B, East Grinstead 2-3 Durham).
    # It was shown to a COACH, not a player: the API gives a person id in the
    # event key (3dbc1904-6ac7-48bd-9e9a-3cb5212a165a) that appears nowhere in
    # the players table, because the person is not in any squad list. Confirmed
    # by the user 2026-10-04. Counting it made "one red card each division"
    # wrong for the Women's Prem - there were none to a player all season.
    "6bfcf2b2-1a7e-433f-994c-2a566c3a36c2|f876ddee-5449-4f70-8688-de6aa2d98df4"
    "|3dbc1904-6ac7-48bd-9e9a-3cb5212a165a|RC|70|0":
        "red card shown to a coach, not a player - person id is in no squad list",
}


# Squad places the source leaves unnamed (a player who hasn't consented to be
# named: match_players row with player_id NULL), identified from the club's OWN
# published team sheets. Applied by `ingest repair`, which creates the player
# (source "manual") and points the matching NULL rows at them.
#
# Key: (team source_id, season name, shirt number). Only add an entry when the
# shirt number is the only unnamed place it could be AND the club itself has put
# the name out publicly. If the club's graphics leave the player off too, leave
# them unnamed (Clifton Robinsons W1 #7, 2025/26: absent from the club's own
# 11 and 18 Oct 2025 lineups, so treat as a choice and do not chase it).
# Value: (manual source_id, first name, last name, why).
SQUAD_IDENTITIES: dict[tuple[str, str, int], tuple[str, str, str, str]] = {
    # Holcombe M1 2025/26, unnamed #1 GK in 14 of 16 league games. The club's
    # sheets for 22 Mar (v H&W), 28 Mar (v Wimbledon) and 11 Apr (v Surbiton)
    # list the same 16 as our squads, with "1. Dom Dixon (GK)" the only name not
    # in ours. Latif Khan kept goal in the other two (15 and 21 Mar). Confirmed
    # by the user 2026-10-19.
    ("0546a44c-602a-45ce-b662-d55951661e3c", "2025/26", 1): (
        "manual:player:holcombe:dom-dixon", "Dom", "Dixon",
        "club team sheets 22/28 Mar, 11 Apr 2026 name #1 GK Dom Dixon; only unnamed place",
    ),
    # Barnes W1 2025/26, unnamed #6 in 6 Phase 1 games (27 Sep-22 Nov). The
    # club's start-of-season squad graphic has "6. Liv Buchanan", and its other
    # numbers match our autumn squads (2 Thacker, 3 da Rocha, 10 Levy, 14 McCann,
    # 17 Pendle). No named Barnes player wore #6 all season. Confirmed by the
    # user 2026-10-19. (An Olivia Buchanan scored for University of Exeter W1 in
    # Feb 2025; possibly the same person, NOT linked - nothing in the data ties them.)
    ("f9e1e343-96e2-4159-8901-3285c899de3f", "2025/26", 6): (
        "manual:player:barnes:liv-buchanan", "Liv", "Buchanan",
        "club squad graphic 2025/26 names #6 Liv Buchanan; no named #6 all season",
    ),
}
