"""Open an FHA Predictor round: hockey_stats -> Supabase, plus the round's static JSON.

One run, one set of match ids, so the database and the pick screen can never disagree:
  1. ref.clubs / ref.teams: upsert the round's clubs and teams (hockey_stats ids, display names,
     badge keys, colours from analysis/club_colours.csv).
  2. predictor.editions: this run of the FHA Predictor ("prem-2026-27"), created on first use.
  3. predictor.rounds: the round, deadline = first kick-off (or --deadline for a practice replay).
  4. ref.matches: the round's matches with UTC kick-offs (scores and status are never sent here:
     publish_results.py owns them, so a re-run can't wipe a result); predictor.round_matches
     links them to the round.
  5. web/public/data/predictor/round-N.json via round_json.py (form, last meeting).

Not yet: the FHA Model's picks (needs its auth user, and the model can't see Phase 2 fixtures).

Needs SUPABASE_SERVICE_ROLE_KEY in .env: the project's secret key (sb_secret_...) or the legacy
service_role key. It bypasses row-level security, so it never leaves .env.

  python3 predictor/publish_round.py --round 10 --from 2026-11-21 --to 2026-11-21
  python3 predictor/publish_round.py --round 10 --matches 7131,7132,6529 --label "Round 10: Top 6"
  python3 predictor/publish_round.py ... --deadline "2027-01-10 18:00"   # practice replay (UK time)
  python3 predictor/publish_round.py ... --dry-run                       # show, change nothing
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import round_json as rj  # noqa: E402

ROOT = rj.ROOT
COLOURS = os.path.join(ROOT, "analysis", "club_colours.csv")


def env(name, default=None):
    if os.environ.get(name):
        return os.environ[name]
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k == name and v:
                return v.strip().strip('"').strip("'")
    return default


class Supabase:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        ref = env("SUPABASE_PROJECT_REF", "<supabase-project-ref>")
        self.url = f"https://{ref}.supabase.co/rest/v1"
        self.key = env("SUPABASE_SERVICE_ROLE_KEY")
        if not self.key or self.key.startswith("your-"):
            sys.exit("SUPABASE_SERVICE_ROLE_KEY missing from .env (dashboard: Project Settings > API Keys > secret)")

    def call(self, method, schema, path, body=None, prefer=None):
        headers = {"apikey": self.key, "Accept-Profile": schema, "Content-Profile": schema,
                   "Content-Type": "application/json"}
        if self.key.startswith("eyJ"):  # legacy JWT keys also go in Authorization; sb_secret_ keys must not
            headers["Authorization"] = f"Bearer {self.key}"
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}/{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode()
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {schema}.{path} failed: {e.code} {e.read().decode()[:400]}")
        return json.loads(text) if text else None

    def get(self, schema, path):
        return self.call("GET", schema, path)

    def upsert(self, schema, table, rows, on_conflict):
        if self.dry_run:
            print(f"  [dry run] upsert {schema}.{table}: {len(rows)} row(s)")
            return rows
        return self.call("POST", schema, f"{table}?on_conflict={on_conflict}", rows,
                         "resolution=merge-duplicates,return=representation")

    def delete(self, schema, path):
        if self.dry_run:
            print(f"  [dry run] delete {schema}.{path}")
            return
        self.call("DELETE", schema, path)


def colours():
    if not os.path.exists(COLOURS):
        return {}
    with open(COLOURS, encoding="utf-8") as f:
        return {r["club"]: (r["primary"] or None, r["secondary"] or None) for r in csv.DictReader(f)}


def uk_to_utc(text):
    return rj.kickoff_utc(dt.datetime.fromisoformat(text).isoformat(sep=" "))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--matches", help="comma-separated hockey_stats match ids")
    p.add_argument("--from", dest="date_from", help="first date (UK), with --to: every Prem match in the window")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--label", help='default "Round N"')
    p.add_argument("--deadline", help="override, UK local time (practice replays); default first kick-off")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="re-publish a round that is no longer open")
    args = p.parse_args()
    if not args.matches and not (args.date_from and args.date_to):
        p.error("give --matches, or --from and --to")

    rows = rj.round_matches(args)
    if not rows:
        sys.exit("no matches found for that round")
    seasons = {r["season"] for r in rows}
    if len(seasons) != 1:
        sys.exit(f"matches span seasons {sorted(seasons)}")
    season = seasons.pop()
    deadline = uk_to_utc(args.deadline) if args.deadline else min(rj.kickoff_utc(r["starts_at"]) for r in rows)
    now = dt.datetime.now(dt.timezone.utc)
    if dt.datetime.fromisoformat(deadline.replace("Z", "+00:00")) <= now:
        sys.exit(f"deadline {deadline} has passed: nobody could play it (use --deadline for a practice replay)")

    sb = Supabase(args.dry_run)
    print(f"Round {args.round}, {season}: {len(rows)} matches, deadline {deadline}")

    # 1. ref: clubs then teams (teams reference clubs)
    col = colours()
    clubs, teams = {}, {}
    for r in rows:
        gender = "W" if "women" in r["competition"].lower() else "M"
        for side in ("home", "away"):
            club = r[f"{side}_club"]
            prim, sec = col.get(club, (None, None))
            clubs[int(r[f"{side}_club_id"])] = {
                "id": int(r[f"{side}_club_id"]), "name": club, "short_name": rj.short(club),
                "badge_key": rj.slug(club), "primary_colour": prim, "secondary_colour": sec}
            teams[int(r[f"{side}_team_id"])] = {
                "id": int(r[f"{side}_team_id"]), "club_id": int(r[f"{side}_club_id"]),
                "gender": gender, "display_name": rj.short(club)}
    sb.upsert("ref", "clubs", list(clubs.values()), "id")
    sb.upsert("ref", "teams", list(teams.values()), "id")
    print(f"  ref: {len(clubs)} clubs, {len(teams)} teams")

    # 2. the edition (one run of the FHA Predictor, per hockey season)
    slug = "prem-" + season.replace("/", "-")
    edition = sb.upsert("predictor", "editions", [{"slug": slug, "name": f"Premier Division {season}",
                                             "season": season}], "slug")
    edition_id = edition[0].get("id") if not args.dry_run else "?"
    print(f"  edition: {slug} (id {edition_id})")

    # 3. the round; never quietly reopen one that has locked or been scored
    label = args.label or f"Round {args.round}"
    if not args.dry_run:
        existing = sb.get("predictor", f"rounds?edition_id=eq.{edition_id}&number=eq.{args.round}&select=id,status")
        if existing and existing[0]["status"] != "open" and not args.force:
            sys.exit(f"round {args.round} is {existing[0]['status']}; re-run with --force if you mean it")
    rnd = sb.upsert("predictor", "rounds", [{"edition_id": edition_id, "number": args.round, "label": label,
                                             "deadline": deadline}], "edition_id,number")
    round_id = rnd[0].get("id") if not args.dry_run else "?"
    print(f"  round: {label} (id {round_id})")

    # 4. ref.matches (the facts: schedule fields only, scores/status belong to publish_results.py),
    #    then predictor.round_matches (which of them this round uses)
    ids = [int(r["id"]) for r in rows]
    sb.upsert("ref", "matches", [{
        "id": int(r["id"]), "home_team_id": int(r["home_team_id"]),
        "away_team_id": int(r["away_team_id"]), "kickoff": rj.kickoff_utc(r["starts_at"])} for r in rows], "id")
    sb.upsert("predictor", "round_matches", [{"round_id": round_id, "match_id": i} for i in ids],
              "round_id,match_id")
    if not args.dry_run:  # unlink matches left from an earlier run of this round (fails if picked)
        stale = sb.get("predictor", f"round_matches?round_id=eq.{round_id}"
                                    f"&match_id=not.in.({','.join(map(str, ids))})&select=match_id")
        if stale:
            sb.delete("predictor", f"round_matches?round_id=eq.{round_id}"
                                   f"&match_id=in.({','.join(str(s['match_id']) for s in stale)})")
            print(f"  unlinked {len(stale)} match(es) no longer in this round (they stay in ref.matches)")
    print(f"  matches: {len(ids)} in ref.matches, linked to the round")

    # 5. the pick screen's static JSON, from the same rows
    data = rj.build(args, rows)
    if args.deadline:
        data["deadline"] = deadline
    if args.dry_run:
        print(f"  [dry run] would write web/public/data/predictor/round-{args.round}.json")
    else:
        rj.write(data)


if __name__ == "__main__":
    main()
