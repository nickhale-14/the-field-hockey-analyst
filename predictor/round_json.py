"""Static JSON for one FHA Predictor round: the pick screen's read-only extras.

Writes web/public/data/predictor/round-<N>.json with, per match: both teams (display name,
badge key, hockey_stats ids), kick-off in UTC with the UK offset applied, each team's last three
results before that match, and how their last meeting finished. No Supabase involved: this is
public, read-only data (anyone can open it), so nothing private ever goes in it.

Reads the local MySQL (docker compose `db`), standard library only, like content/series/*.py.

  python3 predictor/round_json.py --round 10 --from 2026-11-21 --to 2026-11-22   # every Prem match in the window
  python3 predictor/round_json.py --round 10 --matches 7131,7132,6529             # exact match ids
  python3 predictor/round_json.py ... --stdout                                    # print, write nothing
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT_DIR = os.path.join(ROOT, "web", "public", "data", "predictor")
UK = ZoneInfo("Europe/London")  # EH gives naive UK local times
FORM_N = 3

# Display names follow the content rules: "X Uni", never "University of X" (memory: uni naming).
SHORT = {
    "Hampstead & Westminster": "H&W",
    "University of Birmingham": "Birmingham Uni",
    "University of Nottingham": "Nottingham Uni",
    "University of Exeter": "Exeter Uni",
    "Durham University": "Durham Uni",
    "Loughborough Students": "Loughborough",
}


def q(sql):
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "mysql", "-uhockey_stats", "-phockey_stats", "hockey_stats",
         "-B", "--default-character-set=utf8mb4", "-e", sql],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout.splitlines()
    if not out:
        return []
    head = out[0].split("\t")
    return [dict(zip(head, (None if v == "NULL" else v for v in r.split("\t")))) for r in out[1:]]


def short(club):
    s = re.sub(r"\s+HC$", "", club)
    return SHORT.get(s, s)


def slug(club):
    return re.sub(r"[^a-z0-9]+", "-", club.lower()).strip("-")


def kickoff_utc(naive):
    local = dt.datetime.fromisoformat(naive).replace(tzinfo=UK)
    return local.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


MATCH_COLS = """m.id, m.starts_at, m.status, m.home_score, m.away_score, c.name AS competition, s.name AS season,
       m.home_team_id, m.away_team_id, hc.id AS home_club_id, hc.name AS home_club,
       ac.id AS away_club_id, ac.name AS away_club
FROM matches m
JOIN competitions c ON c.id = m.competition_id
JOIN seasons s ON s.id = m.season_id
JOIN teams ht ON ht.id = m.home_team_id JOIN clubs hc ON hc.id = ht.club_id
JOIN teams at ON at.id = m.away_team_id JOIN clubs ac ON ac.id = at.club_id"""


def round_matches(args):
    if args.matches:
        ids = ",".join(str(int(x)) for x in args.matches.split(","))
        where = f"m.id IN ({ids})"
    else:
        d_from = dt.date.fromisoformat(args.date_from)
        d_to = dt.date.fromisoformat(args.date_to) + dt.timedelta(days=1)
        where = (f"m.starts_at >= '{d_from}' AND m.starts_at < '{d_to}' "
                 "AND c.name LIKE '%Premier Division%'")
    rows = q(f"SELECT {MATCH_COLS} WHERE {where} ORDER BY m.starts_at, m.id")
    if args.matches and len(rows) != len(args.matches.split(",")):
        sys.exit(f"asked for {len(args.matches.split(','))} matches, found {len(rows)}")
    return rows


def history(team_ids):
    ids = ",".join(sorted(team_ids))
    # status='R' only: the other codes are not results (hard rule)
    return q(f"SELECT {MATCH_COLS} WHERE m.status = 'R' "
             f"AND (m.home_team_id IN ({ids}) OR m.away_team_id IN ({ids})) ORDER BY m.starts_at DESC, m.id DESC")


def team(row, side):
    club = row[f"{side}_club"]
    return {"team_id": int(row[f"{side}_team_id"]), "club_id": int(row[f"{side}_club_id"]),
            "name": short(club), "badge_key": slug(club)}


def form(team_id, before, past):
    out = []
    for r in past:
        if r["starts_at"] >= before or team_id not in (r["home_team_id"], r["away_team_id"]):
            continue
        home = r["home_team_id"] == team_id
        gf, ga = (int(r["home_score"]), int(r["away_score"]))[:: 1 if home else -1]
        opp = "away" if home else "home"
        out.append({"date": r["starts_at"][:10], "venue": "H" if home else "A",
                    "opponent": short(r[f"{opp}_club"]), "opponent_team_id": int(r[f"{opp}_team_id"]),
                    "for": gf, "against": ga, "result": "W" if gf > ga else "L" if gf < ga else "D"})
        if len(out) == FORM_N:
            break
    return out


def last_meeting(m, past):
    pair = {m["home_team_id"], m["away_team_id"]}
    for r in past:
        if r["starts_at"] < m["starts_at"] and {r["home_team_id"], r["away_team_id"]} == pair:
            return {"date": r["starts_at"][:10], "season": r["season"],
                    "home_team_id": int(r["home_team_id"]), "away_team_id": int(r["away_team_id"]),
                    "home_score": int(r["home_score"]), "away_score": int(r["away_score"])}
    return None


def build(args, rows=None):
    rows = rows if rows is not None else round_matches(args)
    if not rows:
        sys.exit("no matches found for that round")
    past = history({r["home_team_id"] for r in rows} | {r["away_team_id"] for r in rows})
    matches = []
    for m in rows:
        matches.append({
            "match_id": int(m["id"]),
            "gender": "W" if "women" in m["competition"].lower() else "M",
            "kickoff": kickoff_utc(m["starts_at"]),
            "home": {**team(m, "home"), "form": form(m["home_team_id"], m["starts_at"], past)},
            "away": {**team(m, "away"), "form": form(m["away_team_id"], m["starts_at"], past)},
            "last_meeting": last_meeting(m, past),
        })
    matches.sort(key=lambda x: (x["gender"] != "M", x["kickoff"], x["match_id"]))
    return {
        "round": args.round,
        "deadline": min(x["kickoff"] for x in matches),
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "matches": matches,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--matches", help="comma-separated hockey_stats match ids")
    p.add_argument("--from", dest="date_from", help="first date (UK), with --to: every Prem match in the window")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    args = p.parse_args()
    if not args.matches and not (args.date_from and args.date_to):
        p.error("give --matches, or --from and --to")
    data = build(args)
    if args.stdout:
        sys.stdout.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        return
    write(data)


def write(data):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"round-{data['round']}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {os.path.relpath(path, ROOT)}: {len(data['matches'])} matches, deadline {data['deadline']}")


if __name__ == "__main__":
    main()
