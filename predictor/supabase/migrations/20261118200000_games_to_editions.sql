-- Rename predictor.games to predictor.editions (user, 2026-11-18): "game" reads as a hockey
-- match. An edition is one run of the FHA Predictor, e.g. prem-2026-27 (launching March 2027);
-- 2027/28 will be the next edition. Rounds belong to an edition; the season leaderboard is per
-- edition (its view keeps the name leaderboard_season, as the site's tab is "Season").
-- Pure rename: no data changes.

alter table predictor.games rename to editions;
alter sequence predictor.games_id_seq rename to editions_id_seq;
alter table predictor.editions rename constraint games_pkey to editions_pkey;
alter table predictor.editions rename constraint games_slug_key to editions_slug_key;

alter table predictor.rounds rename column game_id to edition_id;
alter table predictor.rounds rename constraint rounds_game_id_fkey to rounds_edition_id_fkey;
alter table predictor.rounds rename constraint rounds_game_id_number_key to rounds_edition_id_number_key;

-- Views keep their old output column name after a rename, so rebuild them with edition_id.
drop view predictor.leaderboard_alltime;
drop view predictor.leaderboard_season;
drop view predictor.leaderboard_round;
drop view predictor.pick_points;

create view predictor.pick_points with (security_invoker = true) as
select p.player_id,
       p.match_id,
       p.round_id,
       r.edition_id,
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
select pp.edition_id,
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
group by pp.edition_id, pp.round_id, pp.player_id, pr.display_name, pr.handle, pl.kind;

-- Season (one edition): ties stay joint.
create view predictor.leaderboard_season with (security_invoker = true) as
select pp.edition_id,
       pp.player_id,
       coalesce(pr.display_name, pr.handle, 'Player') as name,
       pl.kind,
       coalesce(sum(pp.points), 0)::integer            as points,
       count(*) filter (where pp.is_exact)::integer    as exact_scores,
       count(distinct pp.round_id)::integer            as rounds_played,
       rank() over (partition by pp.edition_id order by coalesce(sum(pp.points), 0) desc) as position
from predictor.pick_points pp
join predictor.rounds r   on r.id = pp.round_id and r.deadline <= now()
join predictor.players pl on pl.id = pp.player_id
join public.profiles pr   on pr.id = pp.player_id
where pp.on_leaderboard
group by pp.edition_id, pp.player_id, pr.display_name, pr.handle, pl.kind;

-- All-time, every edition: ties stay joint.
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

grant select on predictor.pick_points, predictor.leaderboard_round, predictor.leaderboard_season,
                predictor.leaderboard_alltime
  to anon, authenticated;
grant all on predictor.pick_points, predictor.leaderboard_round, predictor.leaderboard_season,
             predictor.leaderboard_alltime
  to service_role;
