-- Matches are hockey facts, not predictor data (user, 2026-11-18).
--
-- Before: predictor.matches held fixtures + results and belonged to one round.
-- After:  ref.matches holds every fixture and result (ids ARE hockey_stats matches.id);
--         predictor.round_matches says which matches a round uses ("round 10 = these six");
--         predictor.picks point at a (round, match) pair, so one match can serve two games.
-- Feature schemas reference ref, never the other way round.
--
-- The 7-day result freeze is dropped, not moved: the user will handle freezing later with a
-- scheduled job driven by the fixtures. Until then only the service role (publish_results.py)
-- can write scores; the public never could.
--
-- Safe only while there are no picks: it refuses to run otherwise.

do $$
begin
  if exists (select 1 from predictor.picks) then
    raise exception 'predictor.picks has rows: this migration assumes an empty game';
  end if;
end $$;

------------------------------------------------------------------------------------------
-- ref.matches: the facts
------------------------------------------------------------------------------------------
create table ref.matches (
  id           integer primary key,              -- hockey_stats matches.id
  home_team_id integer not null references ref.teams (id),
  away_team_id integer not null references ref.teams (id),
  kickoff      timestamptz not null,             -- UTC; the feed converts from Europe/London
  home_score   smallint,                         -- null until played
  away_score   smallint,
  status       text not null default 'S' check (status in ('S', 'R', 'P', 'A')),
  check (status <> 'R' or (home_score is not null and away_score is not null))
);
create index on ref.matches (kickoff);

insert into ref.matches (id, home_team_id, away_team_id, kickoff, home_score, away_score, status)
select id, home_team_id, away_team_id, kickoff, home_score, away_score, status
from predictor.matches;

------------------------------------------------------------------------------------------
-- predictor.round_matches: which matches a round uses
------------------------------------------------------------------------------------------
create table predictor.round_matches (
  round_id integer not null references predictor.rounds (id),
  match_id integer not null references ref.matches (id),
  primary key (round_id, match_id)
);
create index on predictor.round_matches (match_id);

insert into predictor.round_matches (round_id, match_id)
select round_id, id from predictor.matches;

------------------------------------------------------------------------------------------
-- Out with the old: views, picks (empty), the freeze, predictor.matches
------------------------------------------------------------------------------------------
drop view predictor.leaderboard_alltime;
drop view predictor.leaderboard_season;
drop view predictor.leaderboard_round;
drop view predictor.pick_points;
drop table predictor.picks;                      -- its policies and trigger go with it
drop trigger freeze_scored_results on predictor.matches;
drop function predictor.freeze_scored_results();
drop table predictor.matches;

------------------------------------------------------------------------------------------
-- predictor.picks: a player's score for one match of one round
------------------------------------------------------------------------------------------
create table predictor.picks (
  player_id    uuid not null references predictor.players (id) on delete cascade,
  round_id     integer not null,
  match_id     integer not null,
  home         smallint not null check (home between 0 and 15),
  away         smallint not null check (away between 0 and 15),
  submitted_at timestamptz not null default now(),
  primary key (player_id, round_id, match_id),
  -- No cascade: a match can't leave a round once someone has picked it.
  foreign key (round_id, match_id) references predictor.round_matches (round_id, match_id)
);
create index on predictor.picks (round_id, match_id);

create trigger stamp_pick before insert or update on predictor.picks
  for each row execute function predictor.stamp_pick();

------------------------------------------------------------------------------------------
-- Views: as before, reading scores from ref.matches
------------------------------------------------------------------------------------------
create view predictor.pick_points with (security_invoker = true) as
select p.player_id,
       p.match_id,
       p.round_id,
       r.game_id,
       case when m.status in ('P', 'A') then 0
            when m.status = 'R' then predictor.pick_score(p.home, p.away, m.home_score, m.away_score)
       end                                                          as points,
       (m.status = 'R' and p.home = m.home_score and p.away = m.away_score) as is_exact,
       (m.status = 'R' and predictor.pick_score(p.home, p.away, m.home_score, m.away_score) = 3)
                                                                    as is_one_off,
       (pl.kind in ('admin', 'model') or pr.confirmed_at <= r.deadline) as on_leaderboard
from predictor.picks p
join ref.matches m        on m.id = p.match_id
join predictor.rounds r   on r.id = p.round_id
join predictor.players pl on pl.id = p.player_id
join public.profiles pr   on pr.id = p.player_id;

-- Round: ties split by more exact scores, then more one-goal-offs, else joint.
create view predictor.leaderboard_round with (security_invoker = true) as
select pp.game_id,
       pp.round_id,
       pp.player_id,
       coalesce(pr.display_name, pr.handle, 'Player') as name,
       pl.kind,
       coalesce(sum(pp.points), 0)::integer            as points,
       count(*) filter (where pp.is_exact)::integer    as exact_scores,
       count(*) filter (where pp.is_one_off)::integer  as one_goal_offs,
       rank() over (partition by pp.round_id
                    order by coalesce(sum(pp.points), 0) desc,
                             count(*) filter (where pp.is_exact) desc,
                             count(*) filter (where pp.is_one_off) desc) as position
from predictor.pick_points pp
join predictor.rounds r   on r.id = pp.round_id and r.deadline <= now()
join predictor.players pl on pl.id = pp.player_id
join public.profiles pr   on pr.id = pp.player_id
where pp.on_leaderboard
group by pp.game_id, pp.round_id, pp.player_id, pr.display_name, pr.handle, pl.kind;

-- Season (one game): ties stay joint.
create view predictor.leaderboard_season with (security_invoker = true) as
select pp.game_id,
       pp.player_id,
       coalesce(pr.display_name, pr.handle, 'Player') as name,
       pl.kind,
       coalesce(sum(pp.points), 0)::integer            as points,
       count(*) filter (where pp.is_exact)::integer    as exact_scores,
       count(distinct pp.round_id)::integer            as rounds_played,
       rank() over (partition by pp.game_id order by coalesce(sum(pp.points), 0) desc) as position
from predictor.pick_points pp
join predictor.rounds r   on r.id = pp.round_id and r.deadline <= now()
join predictor.players pl on pl.id = pp.player_id
join public.profiles pr   on pr.id = pp.player_id
where pp.on_leaderboard
group by pp.game_id, pp.player_id, pr.display_name, pr.handle, pl.kind;

-- All-time, every game: ties stay joint.
create view predictor.leaderboard_alltime with (security_invoker = true) as
select pp.player_id,
       coalesce(pr.display_name, pr.handle, 'Player') as name,
       pl.kind,
       coalesce(sum(pp.points), 0)::integer            as points,
       count(*) filter (where pp.is_exact)::integer    as exact_scores,
       count(distinct pp.round_id)::integer            as rounds_played,
       rank() over (order by coalesce(sum(pp.points), 0) desc) as position
from predictor.pick_points pp
join predictor.rounds r   on r.id = pp.round_id and r.deadline <= now()
join predictor.players pl on pl.id = pp.player_id
join public.profiles pr   on pr.id = pp.player_id
where pp.on_leaderboard
group by pp.player_id, pr.display_name, pr.handle, pl.kind;

------------------------------------------------------------------------------------------
-- Row-level security
------------------------------------------------------------------------------------------
alter table ref.matches             enable row level security;
alter table predictor.round_matches enable row level security;
alter table predictor.picks         enable row level security;

create policy "public read" on ref.matches             for select to anon, authenticated using (true);
create policy "public read" on predictor.round_matches for select to anon, authenticated using (true);

-- Picks: your own whenever; everyone's once the round's deadline has passed.
create policy "read own or locked" on predictor.picks for select to anon, authenticated
  using (
    player_id = (select auth.uid())
    or exists (select 1 from predictor.rounds r where r.id = picks.round_id and r.deadline <= now())
  );

-- Write your own, only before the deadline (server clock).
create policy "save own before deadline" on predictor.picks for insert to authenticated
  with check (
    player_id = (select auth.uid())
    and exists (select 1 from predictor.rounds r where r.id = picks.round_id and r.deadline > now())
  );
create policy "change own before deadline" on predictor.picks for update to authenticated
  using (
    player_id = (select auth.uid())
    and exists (select 1 from predictor.rounds r where r.id = picks.round_id and r.deadline > now())
  )
  with check (
    player_id = (select auth.uid())
    and exists (select 1 from predictor.rounds r where r.id = picks.round_id and r.deadline > now())
  );

------------------------------------------------------------------------------------------
-- Grants
------------------------------------------------------------------------------------------
grant select on ref.matches to anon, authenticated;
grant select on predictor.round_matches, predictor.picks, predictor.pick_points,
               predictor.leaderboard_round, predictor.leaderboard_season,
               predictor.leaderboard_alltime
  to anon, authenticated;
grant insert (player_id, round_id, match_id, home, away), update (home, away) on predictor.picks
  to authenticated;

grant all on ref.matches, predictor.round_matches, predictor.picks, predictor.pick_points,
              predictor.leaderboard_round, predictor.leaderboard_season, predictor.leaderboard_alltime
  to service_role;
