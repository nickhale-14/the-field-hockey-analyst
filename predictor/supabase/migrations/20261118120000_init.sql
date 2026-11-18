-- FHA Supabase project: first migration (2026-11-18).
--
-- Three layers (docs/predictor-game.md):
--   public    : one account for everything FHA (profiles, site-wide roles)
--   ref       : clubs and teams, copied in by the weekly feed; ids ARE the hockey_stats ids
--   predictor : the FHA Weekly Predictor (games, rounds, matches, players, picks)
-- Points and leaderboards are views, never stored, so a corrected result rescores everything.
-- The Golden Goal is parked (2026-11-18); round ties split on exact scores, then one-goal-offs.
--
-- Roles: anon (not signed in), authenticated (signed in, anonymous sign-ins included),
-- service_role (the weekly feed scripts; bypasses RLS).

------------------------------------------------------------------------------------------
-- ref: reference data from hockey_stats, read-only to the public
------------------------------------------------------------------------------------------
create schema ref;

create table ref.clubs (
  id               integer primary key,          -- hockey_stats clubs.id
  name             text not null,
  short_name       text,
  badge_key        text,                         -- file name under the site's badge folder
  primary_colour   text,
  secondary_colour text
);

create table ref.teams (
  id           integer primary key,              -- hockey_stats teams.id
  club_id      integer not null references ref.clubs (id),
  gender       text not null check (gender in ('M', 'W')),   -- M = Men's/Open
  display_name text not null
);
create index on ref.teams (club_id);

------------------------------------------------------------------------------------------
-- public: accounts shared by every FHA feature
------------------------------------------------------------------------------------------
create table public.profiles (
  id                uuid primary key references auth.users (id) on delete cascade,
  handle            text check (handle ~ '^[a-z0-9_]{3,20}$'),
  display_name      text check (char_length(display_name) between 1 and 30),
  favourite_team_id integer references ref.teams (id) on delete set null,
  -- When the email was confirmed (null while anonymous). Kept in step with auth.users by
  -- trigger, so views can use it without reading the auth schema.
  confirmed_at      timestamptz,
  created_at        timestamptz not null default now()
);
create unique index profiles_handle_key on public.profiles (handle);

create table public.user_roles (
  user_id uuid not null references public.profiles (id) on delete cascade,
  role    text not null check (role in ('site_admin')),
  primary key (user_id, role)
);

-- A profile row for every sign-in, anonymous included; confirmed_at follows auth.users.
create function public.sync_profile_from_auth() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (id, confirmed_at)
  values (new.id, case when not coalesce(new.is_anonymous, false) then new.email_confirmed_at end)
  on conflict (id) do update set confirmed_at = excluded.confirmed_at;
  return new;
end $$;
revoke execute on function public.sync_profile_from_auth() from public, anon, authenticated;

create trigger on_auth_user_saved
  after insert or update of email_confirmed_at, is_anonymous on auth.users
  for each row execute function public.sync_profile_from_auth();

------------------------------------------------------------------------------------------
-- predictor: the game
------------------------------------------------------------------------------------------
create schema predictor;

create table predictor.games (
  id     integer generated always as identity primary key,
  slug   text not null unique,                   -- e.g. 'prem-2026-27'
  name   text not null,                          -- e.g. 'Premier Division 2026/27'
  season text not null                           -- '2026/27', as in hockey_stats
);

create table predictor.rounds (
  id        integer generated always as identity primary key,
  game_id   integer not null references predictor.games (id),
  number    integer not null,
  label     text not null,                       -- 'Round 12: Top 6'
  deadline  timestamptz not null,                -- first kick-off of the six; THE lock
  status    text not null default 'open' check (status in ('open', 'locked', 'scored')),
  scored_at timestamptz,                         -- results re-score for 7 days, then frozen
  unique (game_id, number)
);

create table predictor.matches (
  id           integer primary key,              -- hockey_stats matches.id
  round_id     integer not null references predictor.rounds (id),
  home_team_id integer not null references ref.teams (id),
  away_team_id integer not null references ref.teams (id),
  kickoff      timestamptz not null,             -- UTC; publish_round.py converts from Europe/London
  home_score   smallint,                         -- null until played
  away_score   smallint,
  status       text not null default 'S' check (status in ('S', 'R', 'P', 'A')),
  check (status <> 'R' or (home_score is not null and away_score is not null))
);
create index on predictor.matches (round_id);

create table predictor.players (
  id        uuid primary key references public.profiles (id) on delete cascade,
  kind      text not null default 'public' check (kind in ('public', 'admin', 'model')),
  joined_at timestamptz not null default now()
);

create table predictor.picks (
  player_id    uuid not null references predictor.players (id) on delete cascade,
  match_id     integer not null references predictor.matches (id),
  home         smallint not null check (home between 0 and 15),
  away         smallint not null check (away between 0 and 15),
  submitted_at timestamptz not null default now(),
  primary key (player_id, match_id)
);
create index on predictor.picks (match_id);

-- A saved pick's time is the server's, whatever the client sends.
create function predictor.stamp_pick() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.submitted_at := now();
  return new;
end $$;

create trigger stamp_pick before insert or update on predictor.picks
  for each row execute function predictor.stamp_pick();

-- A round scored more than 7 days ago is frozen: its results can no longer change.
create function predictor.freeze_scored_results() returns trigger
language plpgsql set search_path = '' as $$
begin
  if (new.home_score, new.away_score, new.status) is distinct from
     (old.home_score, old.away_score, old.status)
     and exists (select 1 from predictor.rounds r
                 where r.id = old.round_id and r.scored_at < now() - interval '7 days') then
    raise exception 'round % was scored more than 7 days ago and is frozen', old.round_id;
  end if;
  return new;
end $$;

create trigger freeze_scored_results before update on predictor.matches
  for each row execute function predictor.freeze_scored_results();

------------------------------------------------------------------------------------------
-- Scoring: Exact 5 / one goal off 3 / right result 2 (LOCKED 2026-11-16).
-- Must agree with analysis/game_scoring.py rule_points("Exact 5 / one goal off 3 / result 2").
------------------------------------------------------------------------------------------
create function predictor.pick_score(ph integer, pa integer, h integer, a integer)
returns integer language sql immutable set search_path = '' as $$
  select case
    when ph = h and pa = a                                         then 5
    when sign(ph - pa) = sign(h - a) and abs(ph - h) + abs(pa - a) = 1 then 3
    when sign(ph - pa) = sign(h - a)                               then 2
    else 0
  end
$$;

-- Points per pick. Null until the match is played; postponed or abandoned scores 0.
-- on_leaderboard: the admin and the FHA Model always; anyone else only if their email
-- was confirmed before that round's deadline.
create view predictor.pick_points with (security_invoker = true) as
select p.player_id,
       p.match_id,
       m.round_id,
       r.game_id,
       case when m.status in ('P', 'A') then 0
            when m.status = 'R' then predictor.pick_score(p.home, p.away, m.home_score, m.away_score)
       end                                                          as points,
       (m.status = 'R' and p.home = m.home_score and p.away = m.away_score) as is_exact,
       (m.status = 'R' and predictor.pick_score(p.home, p.away, m.home_score, m.away_score) = 3)
                                                                    as is_one_off,
       (pl.kind in ('admin', 'model') or pr.confirmed_at <= r.deadline) as on_leaderboard
from predictor.picks p
join predictor.matches m  on m.id = p.match_id
join predictor.rounds r   on r.id = m.round_id
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
alter table ref.clubs            enable row level security;
alter table ref.teams            enable row level security;
alter table public.profiles      enable row level security;
alter table public.user_roles    enable row level security;
alter table predictor.games      enable row level security;
alter table predictor.rounds     enable row level security;
alter table predictor.matches    enable row level security;
alter table predictor.players    enable row level security;
alter table predictor.picks      enable row level security;

create policy "public read" on ref.clubs         for select to anon, authenticated using (true);
create policy "public read" on ref.teams         for select to anon, authenticated using (true);
create policy "public read" on predictor.games   for select to anon, authenticated using (true);
create policy "public read" on predictor.rounds  for select to anon, authenticated using (true);
create policy "public read" on predictor.matches for select to anon, authenticated using (true);
create policy "public read" on predictor.players for select to anon, authenticated using (true);

-- Profiles hold no email, so names and handles are public (leaderboards need them).
create policy "public read" on public.profiles for select to anon, authenticated using (true);
create policy "edit own"    on public.profiles for update to authenticated
  using (id = (select auth.uid())) with check (id = (select auth.uid()));

create policy "read own" on public.user_roles for select to authenticated
  using (user_id = (select auth.uid()));

-- Joining the game: your own row only, always as 'public' (column grant below).
create policy "join self" on predictor.players for insert to authenticated
  with check (id = (select auth.uid()) and kind = 'public');

-- Picks: your own whenever; everyone's once the round's deadline has passed.
create policy "read own or locked" on predictor.picks for select to anon, authenticated
  using (
    player_id = (select auth.uid())
    or exists (select 1 from predictor.matches m join predictor.rounds r on r.id = m.round_id
               where m.id = picks.match_id and r.deadline <= now())
  );

-- Write your own, only before the deadline (server clock).
create policy "save own before deadline" on predictor.picks for insert to authenticated
  with check (
    player_id = (select auth.uid())
    and exists (select 1 from predictor.matches m join predictor.rounds r on r.id = m.round_id
                where m.id = picks.match_id and r.deadline > now())
  );
create policy "change own before deadline" on predictor.picks for update to authenticated
  using (
    player_id = (select auth.uid())
    and exists (select 1 from predictor.matches m join predictor.rounds r on r.id = m.round_id
                where m.id = picks.match_id and r.deadline > now())
  )
  with check (
    player_id = (select auth.uid())
    and exists (select 1 from predictor.matches m join predictor.rounds r on r.id = m.round_id
                where m.id = picks.match_id and r.deadline > now())
  );

------------------------------------------------------------------------------------------
-- Grants (new schemas get nothing by default; public tables get too much, so reset them)
------------------------------------------------------------------------------------------
revoke all on public.profiles, public.user_roles from anon, authenticated;
grant select on public.profiles to anon, authenticated;
grant update (handle, display_name, favourite_team_id) on public.profiles to authenticated;
grant select on public.user_roles to authenticated;

grant usage on schema ref, predictor to anon, authenticated, service_role;

grant select on all tables in schema ref to anon, authenticated;
grant select on predictor.games, predictor.rounds, predictor.matches, predictor.players,
               predictor.picks, predictor.pick_points, predictor.leaderboard_round,
               predictor.leaderboard_season, predictor.leaderboard_alltime
  to anon, authenticated;
grant insert (id) on predictor.players to authenticated;
grant insert (player_id, match_id, home, away), update (home, away) on predictor.picks
  to authenticated;
grant execute on function predictor.pick_score(integer, integer, integer, integer)
  to anon, authenticated;

grant all on all tables in schema ref, predictor to service_role;
grant all on public.profiles, public.user_roles to service_role;
grant usage on all sequences in schema predictor to service_role;
