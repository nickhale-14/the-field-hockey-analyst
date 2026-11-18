"""How non-Prem champions got there: every Division 1 and Conference title, week by week.

Built 2026-10-28 for the Hot Starts follow-up (Hub p27, winter break title race); findings in
docs/findings.md ("How non-Prem champions got there"). Completed seasons only (90 matches).
Tables: status='R' league games, 3/1/0, ordered by points, goal difference, goals scored; weekly
snapshots are the table after each week ending Sunday. "After N" uses each side's first N games,
so postponements don't distort it.

  python3 analysis/title_race.py              # checkpoint after 5 games
  python3 analysis/title_race.py --games 10   # the winter break (round 10)
"""
import collections
import datetime as dt
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("ft", os.path.join(ROOT, "content/series/build-full-time.py"))
ft = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ft)

N = int(ft.arg("--games", 5))
SHORT = {"d1n": "D1N", "d1s": "D1S", "cn": "CN", "cm": "CM", "cw": "CW", "ce": "CE"}


def load():
    rows = ft.q("""
      SELECT m.id, date(m.starts_at) d, s.name season, c.name comp, m.home_team_id hid, m.away_team_id aid,
             m.home_score hs, m.away_score aws, ht.name hn, at.name an
      FROM matches m JOIN competitions c ON c.id=m.competition_id JOIN seasons s ON s.id=m.season_id
      JOIN teams ht ON ht.id=m.home_team_id JOIN teams at ON at.id=m.away_team_id
      WHERE m.status='R' ORDER BY m.starts_at, m.id""")
    out = collections.defaultdict(list)
    for m in rows:
        lg = ft.league_of(m["comp"])
        if lg and lg[1] != "prem":
            out[(m["season"], lg[0], lg[1])].append(m)
    return {k: v for k, v in out.items() if len(v) == 90}


def add(t, m):
    """Add one result to a table: team id -> [P, W, D, L, GF, GA, Pts]."""
    hs, as_ = int(m["hs"]), int(m["aws"])
    for tid, f, a in ((m["hid"], hs, as_), (m["aid"], as_, hs)):
        r = t[tid]
        r[0] += 1; r[4] += f; r[5] += a
        r[1 if f > a else 2 if f == a else 3] += 1
        r[6] += 3 if f > a else 1 if f == a else 0


def order(t):
    return sorted(t, key=lambda k: (-t[k][6], -(t[k][4] - t[k][5]), -t[k][4]))


def snapshots(ms):
    t, snaps, by_week = collections.defaultdict(lambda: [0] * 7), [], collections.OrderedDict()
    for m in ms:
        d = dt.date.fromisoformat(m["d"])
        by_week.setdefault(d + dt.timedelta(days=6 - d.weekday()), []).append(m)
    for wk, wms in by_week.items():
        for m in wms:
            add(t, m)
        snaps.append((wk, order(t), {k: list(v) for k, v in t.items()}))
    return snaps


def after(ms, n):
    """Table from each side's first n games."""
    t, seen = collections.defaultdict(lambda: [0] * 7), collections.Counter()
    for m in ms:
        hs, as_ = int(m["hs"]), int(m["aws"])
        for tid, f, a in ((m["hid"], hs, as_), (m["aid"], as_, hs)):
            if seen[tid] < n:
                seen[tid] += 1
                one = collections.defaultdict(lambda: [0] * 7)
                add(one, {"hid": tid, "aid": "_", "hs": f, "aws": a})
                t[tid] = [x + y for x, y in zip(t[tid], one[tid])]
    return order(t), t


def main():
    names, prof, perfect = {}, [], []
    for (season, g, lg), ms in sorted(load().items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2])):
        for m in ms:
            names[m["hid"]], names[m["aid"]] = m["hn"], m["an"]
        snaps = snapshots(ms)
        fin_order, fin = snaps[-1][1], snaps[-1][2]
        champ, ru = fin_order[0], fin_order[1]
        top = [o[0] == champ for _, o, _ in snaps]
        last_off = max((i for i, x in enumerate(top) if not x), default=-1)
        good = snaps[last_off + 1][2][champ][0]
        deficits = [(t[o[0]][6] - t.get(champ, [0] * 7)[6], t.get(champ, [0] * 7)[0], o.index(champ) + 1 if champ in o else None,
                     names[o[0]]) for _, o, t in snaps]
        late = max(((d, gp, p, l) for d, gp, p, l in deficits if gp >= 9), default=(0, 0, 0, ""))
        n_order, n_t = after(ms, N)
        for k in n_order:
            if n_t[k][1] == N:
                perfect.append((g, season, SHORT[lg], names[k], fin_order.index(k) + 1))
        prof.append(dict(g=g, season=season, lg=SHORT[lg], champ=names[champ], rec="{1}-{2}-{3}".format(*fin[champ]),
                         pts=fin[champ][6], margin=fin[champ][6] - fin[ru][6], top=sum(top), weeks=len(snaps),
                         wire=all(top[1:]), good=good, pos_n=n_order.index(champ) + 1, leader_n=names[n_order[0]],
                         leader_n_fin=fin_order.index(n_order[0]) + 1, worst=max(deficits), late=late))
    for g in "MW":
        ps = [p for p in prof if p["g"] == g]
        print(f"\n===== {'Men' + chr(39) + 's/Open' if g == 'M' else 'Women' + chr(39) + 's'} =====")
        print(f"{'season':8}{'lg':4}{'champion':30}{'rec':8}{'pts':>4}{'mgn':>4}{'top':>7} {'wire':5}{'good':>5}"
              f"{'@' + str(N):>4}  leader after {N} (finish)   late deficit (game 9+)")
        for p in ps:
            ld = "" if p["leader_n"] == p["champ"] else f"{p['leader_n'][:22]} ({p['leader_n_fin']})"
            lt = f"{p['late'][0]} behind {p['late'][3][:20]} after {p['late'][1]} ({p['late'][2]})" if p["late"][0] > 0 else ""
            print(f"{p['season']:8}{p['lg']:4}{p['champ'][:29]:30}{p['rec']:8}{p['pts']:>4}{p['margin']:>4}"
                  f"{p['top']:>3}/{p['weeks']:<3} {'yes' if p['wire'] else '':5}{p['good']:>5}{p['pos_n']:>4}  {ld:28}{lt}")
        print(f"\nleader after {N} won: {sum(p['pos_n'] == 1 for p in ps)} of {len(ps)}; "
              f"top for good by game 9: {sum(p['good'] <= 9 for p in ps)}; not until 13+: {sum(p['good'] >= 13 for p in ps)}; "
              f"wire-to-wire: {sum(p['wire'] for p in ps)}; won on GD: {sum(p['margin'] == 0 for p in ps)}; "
              f"share of weeks top: {sum(p['top'] / p['weeks'] for p in ps) / len(ps):.2f}")
        pf = [x for x in perfect if x[0] == g]
        print(f"perfect after {N}: {len(pf)}, won {sum(x[4] == 1 for x in pf)}: "
              + "; ".join(f"{x[3]} {x[1]} {x[2]} -> {x[4]}" for x in pf))


if __name__ == "__main__":
    main()
