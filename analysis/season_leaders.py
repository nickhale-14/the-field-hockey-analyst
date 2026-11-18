"""Who leads what, across every national league at once: the lookup behind "the most in any league" card facts.

Built 2026-11-02 for Hot Starts (Women's), meant for any card or caption. One division at a time, Prem included,
league games only (phase splits and play-offs left out). For each team: goals for and against, per game, and
cards; for each player: league goals. Each list is shown across all leagues and then league by league, ties kept.
Event gaps are listed first: a game whose score has more goals than logged events can't back a scorer or cards
claim, so check any leader whose league appears there.

  python3 analysis/season_leaders.py --div W
  python3 analysis/season_leaders.py --div M --season 2026/27 --top 5
"""
import collections
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("ft", os.path.join(ROOT, "content/series/build-full-time.py"))
ft = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ft)

DIV, SEASON, TOP = ft.arg("--div", "W"), ft.arg("--season", "2026/27"), int(ft.arg("--top", 5))
NAMES = dict(ft.LEAGUES)


def load():
    rows = ft.q(f"""
      SELECT m.id, c.name comp, m.home_team_id hid, m.away_team_id aid, m.home_score hs, m.away_score aws,
             ht.name hn, at.name an,
             (SELECT count(*) FROM match_events e WHERE e.match_id=m.id AND e.event_type IN ('FG','PC','PS')) ev
      FROM matches m JOIN competitions c ON c.id=m.competition_id JOIN seasons s ON s.id=m.season_id
      JOIN teams ht ON ht.id=m.home_team_id JOIN teams at ON at.id=m.away_team_id
      WHERE m.status='R' AND s.name='{SEASON}'""")
    games = [m for m in rows if (lg := ft.league_of(m["comp"])) and lg[0] == DIV and not m.update(lg=lg[1])]
    teams = {}
    for m in games:
        for tid, name, gf, ga in ((m["hid"], m["hn"], m["hs"], m["aws"]), (m["aid"], m["an"], m["aws"], m["hs"])):
            t = teams.setdefault(tid, dict(name=name, lg=m["lg"], p=0, gf=0, ga=0, G=0, Y=0, R=0))
            t["p"] += 1
            t["gf"] += int(gf)
            t["ga"] += int(ga)
    ids = ",".join(m["id"] for m in games)
    for r in ft.q(f"""SELECT team_id, event_type, count(*) n FROM match_events
                      WHERE match_id IN ({ids}) AND event_type IN ('GC','YC','RC') GROUP BY team_id, event_type"""):
        if r["team_id"] in teams:
            teams[r["team_id"]][r["event_type"][0]] += int(r["n"])
    scorers = ft.q(f"""
      SELECT e.team_id, p.first_name, p.last_name, count(*) n FROM match_events e JOIN players p ON p.id=e.player_id
      WHERE e.match_id IN ({ids}) AND e.event_type IN ('FG','PC','PS') GROUP BY e.team_id, e.player_id""")
    gaps = [m for m in games if int(m["hs"]) + int(m["aws"]) > int(m["ev"])]
    return teams, scorers, gaps


def ranked(items, key, label, best=max):
    """The top TOP by key, extended to keep anyone tied with the last one."""
    items = sorted(items, key=key, reverse=best is max)
    cut = key(items[min(TOP, len(items)) - 1]) if items else None
    return [(label(x), key(x)) for x in items if (key(x) >= cut if best is max else key(x) <= cut)]


def show(title, rows):
    print(f"  {title}: " + "; ".join(f"{a} {b:g}" if isinstance(b, float) else f"{a} {b}" for a, b in rows))


if __name__ == "__main__":
    teams, scorers, gaps = load()
    print(f"{'Women' if DIV == 'W' else 'Men'}'s {SEASON}, league games only, {len(teams)} teams")
    print("Event gaps (score has more goals than logged events):",
          "; ".join(f'{NAMES[m["lg"]]}: {m["hn"]} {m["hs"]}-{m["aws"]} {m["an"]} ({m["ev"]} logged)'
                    for m in gaps) or "none")
    for t in teams.values():
        t["cards"] = t["G"] + t["Y"] + t["R"]
        t["gfpg"], t["gapg"], t["cpg"] = (round(t[k] / t["p"], 2) for k in ("gf", "ga", "cards"))
    for p in scorers:
        p["n"], p["lg"], p["team"] = int(p["n"]), teams[p["team_id"]]["lg"], teams[p["team_id"]]["name"]
    tag = lambda t: f'{t["name"]} ({t["lg"]}, {t["p"]}g)'
    for scope, keys in [("ALL NATIONAL LEAGUES", [k for k, _ in ft.LEAGUES])] + [(n, [k]) for k, n in ft.LEAGUES]:
        ts = [t for t in teams.values() if t["lg"] in keys]
        ps = [p for p in scorers if p["lg"] in keys]
        if not ts:
            continue
        print(f"\n{scope}")
        show("Top scorers", ranked(ps, lambda p: p["n"], lambda p: f'{p["first_name"]} {p["last_name"]} ({p["team"]})'))
        show("Most goals", ranked(ts, lambda t: t["gf"], tag))
        show("Most goals per game", ranked(ts, lambda t: t["gfpg"], tag))
        show("Fewest conceded", ranked(ts, lambda t: t["ga"], tag, min))
        show("Fewest conceded per game", ranked(ts, lambda t: t["gapg"], tag, min))
        show("Fewest cards", ranked(ts, lambda t: t["cards"], lambda t: f'{tag(t)} {t["G"]}G/{t["Y"]}Y/{t["R"]}R', min))
        show("Most cards", ranked(ts, lambda t: t["cards"], lambda t: f'{tag(t)} {t["G"]}G/{t["Y"]}Y/{t["R"]}R'))
