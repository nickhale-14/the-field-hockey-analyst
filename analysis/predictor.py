"""
Score predictor: Poisson attack/defence ratings (Dixon-Coles style), one model
per gender, all national-league tiers fitted together.

Why this shape
- Hockey scores are frequent enough (3.6-5.5 goals a match) that a Poisson
  model on goals alone carries real signal - the same idea football analytics
  started with, on a sport where it works better.
- One model per gender, never pooled: the two scoring environments differ
  (goals per match, draw rate, home edge).
- All tiers in one fit: cross-tier games (relegation play-offs, teams moving
  between divisions) are what put Division 1 and the Prem on one scale. Each
  team's rating is shrunk toward a *learned* mean for its tier, so a team with
  little data sits at "typical for its division" rather than at the overall
  average.
- Time decay: older results count less (half-life tuned on a validation
  season, never on the test season).
- Home advantage is a fitted parameter per gender, not an assumption - the
  paired home/away test on 2019-2026 data gave +0.38 goals (men) / +0.19
  (women), so the fitted value is a sanity check on the model.

v2 additions (each justified by the v1 backtest diagnostics):
- Goal correlation: the men's v1 model under-called draws (16% predicted vs
  20% actual; 2-2 at 5.5% vs 8.2%) because independent Poissons can't make
  home and away goals move together. v2 uses a bivariate Poisson: a shared
  component lambda3 = c * min(lambda_h, lambda_a) is added to both sides,
  expected goals are unchanged, probability mass shifts toward draws. c is
  fitted by maximum likelihood after the ratings, so it's ~0 where the data
  don't want it (the women's).
- Promotion/relegation offsets: teams in their first season after moving
  tier get fitted attack/defence offsets (four parameters, learned from the
  earlier seasons in the training window). v1 found relegated men's sides
  ~0.75 goals/game worse than their rating; this lets the data set that.
- Season-boundary discount: results from previous seasons are multiplied by
  a tuned factor on top of the time decay, because squads change between
  seasons more than within them (v1: refitting weekly beat fitting once by
  6-7 points of accuracy even with a long memory).

Still not modelled: squad availability (international windows), per-club
home advantage, anything needing shots.

Usage (inside the jupyter container, which has scipy):
    python predictor.py --gender M --test-season 2025/26 --tune
    python predictor.py --gender W --test-season 2025/26 --half-life 1.0 --shrink 1.0 --season-discount 0.5
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize, minimize_scalar
from scipy.special import gammaln
from scipy.stats import poisson
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv(
    "DATABASE_URL", "mysql+pymysql://hockey_stats:hockey_stats@db:3306/hockey_stats?charset=utf8mb4"
)
MAX_GOALS = 12
TIERS = ["Premier", "Division 1", "Conference"]


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def tier_of(competition: str) -> str:
    if "Premier" in competition:
        return "Premier"
    if "Division 1" in competition:
        return "Division 1"
    return "Conference"


def load_matches(gender: str) -> pd.DataFrame:
    """All played matches for one gender, every tier and season, oldest first."""
    sql = text(
        """
        SELECT m.id AS match_id, s.name AS season, c.name AS competition, m.starts_at,
               m.home_team_id, ht.name AS home, m.away_team_id, at.name AS away,
               m.home_score, m.away_score
        FROM matches m
        JOIN seasons s ON s.id = m.season_id
        JOIN competitions c ON c.id = m.competition_id
        JOIN teams ht ON ht.id = m.home_team_id
        JOIN teams at ON at.id = m.away_team_id
        WHERE m.status = 'R'
        """
    )
    with create_engine(DATABASE_URL).connect() as conn:
        df = pd.read_sql(sql, conn)
    is_w = df["competition"].str.lower().str.contains("women")
    df = df[is_w] if gender == "W" else df[~is_w]
    df = df.copy()
    df["date"] = pd.to_datetime(df["starts_at"])
    df["tier"] = df["competition"].map(tier_of)
    # Finals Weekend is played at a neutral venue: keep the games (they're real
    # results) but don't let them teach the model about home advantage.
    df["neutral"] = df["competition"].str.contains("Finals Weekend")
    # Season index from the date (seasons run Aug/Sep -> May), used for the
    # season-boundary discount.
    df["season_idx"] = np.where(df["date"].dt.month >= 8, df["date"].dt.year, df["date"].dt.year - 1)
    return df.sort_values("date").reset_index(drop=True)


def season_index_of(ts: pd.Timestamp) -> int:
    return ts.year if ts.month >= 8 else ts.year - 1


TIER_RANK = {"Premier": 0, "Division 1": 1, "Conference": 2}


def team_moves(df: pd.DataFrame) -> dict[tuple[int, str], str]:
    """(team_id, season) -> 'promoted' / 'relegated' for a team's first season
    after changing tier, relative to the previous season they appear in."""
    tiers = team_tiers_by_season(df)
    seasons = sorted(df["season"].unique())
    prev_of = {s: seasons[i - 1] for i, s in enumerate(seasons) if i > 0}
    out: dict[tuple[int, str], str] = {}
    for (t, s), tier in tiers.items():
        p = prev_of.get(s)
        if p is None or (t, p) not in tiers:
            continue
        before, now = TIER_RANK[tiers[(t, p)]], TIER_RANK[tier]
        if now < before:
            out[(t, s)] = "promoted"
        elif now > before:
            out[(t, s)] = "relegated"
    return out


def team_tiers_by_season(df: pd.DataFrame) -> dict[tuple[int, str], str]:
    """(team_id, season) -> tier of the league they played in that season.
    League games dominate a team's season, so the modal tier is the league."""
    rows = []
    for side in ("home", "away"):
        rows.append(df[[f"{side}_team_id", "season", "tier"]].rename(columns={f"{side}_team_id": "team_id"}))
    t = pd.concat(rows)
    modal = t.groupby(["team_id", "season"])["tier"].agg(lambda s: s.value_counts().index[0])
    return modal.to_dict()


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

MOVE_KINDS = ("promoted", "relegated")


@dataclass
class Fit:
    mu: float
    home: float
    attack: dict[int, float]
    defence: dict[int, float]
    tier_attack: dict[str, float]
    tier_defence: dict[str, float]
    team_tier: dict[int, str]
    move_attack: dict[str, float]   # 'promoted'/'relegated' -> attack offset in first season after the move
    move_defence: dict[str, float]
    coupling: float = 0.0           # bivariate Poisson: lambda3 = coupling * min(lambda_h, lambda_a)
    tier_home: dict[str, float] = field(default_factory=dict)  # home edge offset by the match's tier (2026-10-20)

    def home_edge(self, tier: str) -> float:
        return self.home + self.tier_home.get(tier, 0.0)

    def rates(self, home_id: int, away_id: int, home_tier: str, away_tier: str,
              neutral: bool = False, home_move: str | None = None, away_move: str | None = None,
              match_tier: str | None = None) -> tuple[float, float]:
        ah = self.attack.get(home_id, self.tier_attack[home_tier]) + self.move_attack.get(home_move or "", 0.0)
        dh = self.defence.get(home_id, self.tier_defence[home_tier]) + self.move_defence.get(home_move or "", 0.0)
        aa = self.attack.get(away_id, self.tier_attack[away_tier]) + self.move_attack.get(away_move or "", 0.0)
        da = self.defence.get(away_id, self.tier_defence[away_tier]) + self.move_defence.get(away_move or "", 0.0)
        h = 0.0 if neutral else self.home_edge(match_tier or home_tier)
        return math.exp(self.mu + h + ah - da), math.exp(self.mu + aa - dh)


def _bivariate_nll(c: float, lh: np.ndarray, la: np.ndarray, hg: np.ndarray, ag: np.ndarray, w: np.ndarray) -> float:
    """Weighted negative log-likelihood of the bivariate Poisson with
    lambda3 = c * min(lh, la), marginal means held at lh, la."""
    l3 = c * np.minimum(lh, la)
    l1, l2 = lh - l3, la - l3
    kmax = int(min(hg.max(), ag.max()))
    ks = np.arange(kmax + 1)[:, None]
    valid = (ks <= hg[None, :]) & (ks <= ag[None, :])
    with np.errstate(divide="ignore", invalid="ignore"):
        logt = (
            (hg[None, :] - ks) * np.log(l1[None, :]) - gammaln(hg[None, :] - ks + 1)
            + (ag[None, :] - ks) * np.log(l2[None, :]) - gammaln(ag[None, :] - ks + 1)
            + ks * np.log(np.maximum(l3[None, :], 1e-300)) - gammaln(ks + 1)
        )
    logt = np.where(valid, logt, -np.inf)
    m = logt.max(axis=0)
    logp = -(l1 + l2 + l3) + m + np.log(np.exp(logt - m[None, :]).sum(axis=0))
    return float(-np.sum(w * logp))


def fit_model(
    df: pd.DataFrame,
    cutoff: pd.Timestamp,
    half_life_years: float,
    shrink: float,
    team_tier: dict[int, str],
    moves: dict[tuple[int, str], str],
    season_discount: float = 1.0,
    warm: Fit | None = None,
    tier_home: bool = True,
) -> Fit:
    """Fit on every match before `cutoff`, weighting older ones down.

    tier_home: let each tier's home edge differ from the overall one (an offset per tier, lightly
    ridged). Added 2026-10-20: the Premier Division shows no measurable home advantage while the
    tiers below do (analysis/home_advantage.py), and one shared edge carried the lower tiers' onto it."""
    d = df[df["date"] < cutoff]
    teams = sorted(set(d["home_team_id"]) | set(d["away_team_id"]))
    idx = {t: i for i, t in enumerate(teams)}
    T, K, M = len(teams), len(TIERS), len(MOVE_KINDS)
    tier_idx = np.array([TIERS.index(team_tier[t]) for t in teams])

    hi = d["home_team_id"].map(idx).to_numpy()
    ai = d["away_team_id"].map(idx).to_numpy()
    hg = d["home_score"].to_numpy(dtype=float)
    ag = d["away_score"].to_numpy(dtype=float)
    neutral = d["neutral"].to_numpy(dtype=float)
    mt = d["tier"].map(TIERS.index).to_numpy()
    home_on = 1.0 - neutral
    age_years = ((cutoff - d["date"]).dt.days.to_numpy() / 365.25)
    seasons_back = season_index_of(cutoff) - d["season_idx"].to_numpy()
    w = (0.5 ** (age_years / half_life_years)) * (season_discount ** np.maximum(seasons_back, 0))
    # Move indicator matrices: [n_matches, M] for the home side and the away side.
    mv_h = np.zeros((len(d), M))
    mv_a = np.zeros((len(d), M))
    for j, kind in enumerate(MOVE_KINDS):
        mv_h[:, j] = [moves.get((t, s)) == kind for t, s in zip(d["home_team_id"], d["season"])]
        mv_a[:, j] = [moves.get((t, s)) == kind for t, s in zip(d["away_team_id"], d["season"])]
    ridge_tier = 0.01  # tiny, only to pin the overall level

    n_core = 2 + 2 * T + 2 * K

    def unpack(theta):
        mu, home = theta[0], theta[1]
        att = theta[2:2 + T]
        de = theta[2 + T:2 + 2 * T]
        ta = theta[2 + 2 * T:2 + 2 * T + K]
        td = theta[2 + 2 * T + K:n_core]
        ma = theta[n_core:n_core + M]
        md = theta[n_core + M:n_core + 2 * M]
        th = theta[n_core + 2 * M:]
        return mu, home, att, de, ta, td, ma, md, th

    def objective(theta):
        mu, home, att, de, ta, td, ma, md, th = unpack(theta)
        eta_h = mu + (home + th[mt]) * home_on + att[hi] + mv_h @ ma - de[ai] - mv_a @ md
        eta_a = mu + att[ai] + mv_a @ ma - de[hi] - mv_h @ md
        lh, la = np.exp(eta_h), np.exp(eta_a)
        nll = np.sum(w * (lh - hg * eta_h + la - ag * eta_a))
        pen = shrink * (np.sum((att - ta[tier_idx]) ** 2) + np.sum((de - td[tier_idx]) ** 2))
        pen += ridge_tier * (np.sum(ta ** 2) + np.sum(td ** 2)) + 0.1 * (np.sum(ma ** 2) + np.sum(md ** 2))
        pen += 0.1 * np.sum(th ** 2)

        rh, ra = w * (lh - hg), w * (la - ag)
        g = np.zeros_like(theta)
        g[0] = np.sum(rh + ra)
        g[1] = np.sum(rh * home_on)
        g_att = np.bincount(hi, rh, T) + np.bincount(ai, ra, T)
        g_def = -np.bincount(ai, rh, T) - np.bincount(hi, ra, T)
        g_att += 2 * shrink * (att - ta[tier_idx])
        g_def += 2 * shrink * (de - td[tier_idx])
        g_ta = -2 * shrink * np.bincount(tier_idx, att - ta[tier_idx], K) + 2 * ridge_tier * ta
        g_td = -2 * shrink * np.bincount(tier_idx, de - td[tier_idx], K) + 2 * ridge_tier * td
        g[2:2 + T] = g_att
        g[2 + T:2 + 2 * T] = g_def
        g[2 + 2 * T:2 + 2 * T + K] = g_ta
        g[2 + 2 * T + K:n_core] = g_td
        g[n_core:n_core + M] = mv_h.T @ rh + mv_a.T @ ra + 0.2 * ma
        g[n_core + M:n_core + 2 * M] = -(mv_a.T @ rh + mv_h.T @ ra) + 0.2 * md
        g[n_core + 2 * M:] = np.bincount(mt, rh * home_on, K) + 0.2 * th
        if not tier_home:
            g[n_core + 2 * M:] = 0.0
        return nll + pen, g

    theta0 = np.zeros(n_core + 2 * M + K)
    theta0[0] = math.log(max(0.1, float(np.average((hg + ag) / 2, weights=w))))
    if warm is not None:
        theta0[0], theta0[1] = warm.mu, warm.home
        for t, i in idx.items():
            k = team_tier[t]
            theta0[2 + i] = warm.attack.get(t, warm.tier_attack.get(k, 0.0))
            theta0[2 + T + i] = warm.defence.get(t, warm.tier_defence.get(k, 0.0))
        for k, tk in enumerate(TIERS):
            theta0[2 + 2 * T + k] = warm.tier_attack.get(tk, 0.0)
            theta0[2 + 2 * T + K + k] = warm.tier_defence.get(tk, 0.0)
        for j, kind in enumerate(MOVE_KINDS):
            theta0[n_core + j] = warm.move_attack.get(kind, 0.0)
            theta0[n_core + M + j] = warm.move_defence.get(kind, 0.0)
        if tier_home:
            for k, tk in enumerate(TIERS):
                theta0[n_core + 2 * M + k] = warm.tier_home.get(tk, 0.0)

    bounds = [(None, None)] * (n_core + 2 * M) + [(None, None) if tier_home else (0.0, 0.0)] * K
    res = minimize(objective, theta0, jac=True, method="L-BFGS-B", bounds=bounds, options={"maxiter": 500})
    mu, home, att, de, ta, td, ma, md, th = unpack(res.x)

    # Goal correlation, fitted after the ratings with expected goals held fixed.
    eta_h = mu + (home + th[mt]) * home_on + att[hi] + mv_h @ ma - de[ai] - mv_a @ md
    eta_a = mu + att[ai] + mv_a @ ma - de[hi] - mv_h @ md
    lh, la = np.exp(eta_h), np.exp(eta_a)
    cres = minimize_scalar(lambda c: _bivariate_nll(c, lh, la, hg, ag, w), bounds=(0.0, 0.8), method="bounded")
    coupling = float(cres.x) if cres.fun < _bivariate_nll(0.0, lh, la, hg, ag, w) - 1e-6 else 0.0

    return Fit(
        mu=float(mu), home=float(home),
        attack={t: float(att[i]) for t, i in idx.items()},
        defence={t: float(de[i]) for t, i in idx.items()},
        tier_attack={tk: float(ta[k]) for k, tk in enumerate(TIERS)},
        tier_defence={tk: float(td[k]) for k, tk in enumerate(TIERS)},
        team_tier=dict(team_tier),
        move_attack={kind: float(ma[j]) for j, kind in enumerate(MOVE_KINDS)},
        move_defence={kind: float(md[j]) for j, kind in enumerate(MOVE_KINDS)},
        coupling=coupling,
        tier_home={tk: float(th[k]) for k, tk in enumerate(TIERS)},
    )


def score_grid(lh: float, la: float, coupling: float = 0.0) -> np.ndarray:
    """Joint probability of every scoreline up to MAX_GOALS. With coupling > 0
    the goals share a common Poisson component (bivariate Poisson), which
    moves mass onto draws without changing either side's expected goals."""
    n = np.arange(MAX_GOALS + 1)
    if coupling <= 0:
        grid = np.outer(poisson.pmf(n, lh), poisson.pmf(n, la))
    else:
        l3 = coupling * min(lh, la)
        p1, p2, p3 = poisson.pmf(n, lh - l3), poisson.pmf(n, la - l3), poisson.pmf(n, l3)
        grid = np.zeros((MAX_GOALS + 1, MAX_GOALS + 1))
        for k in range(MAX_GOALS + 1):
            if p3[k] < 1e-12:
                break
            grid[k:, k:] += p3[k] * np.outer(p1[: MAX_GOALS + 1 - k], p2[: MAX_GOALS + 1 - k])
    return grid / grid.sum()


def outcome_probs(lh: float, la: float, coupling: float = 0.0) -> tuple[float, float, float, tuple[int, int]]:
    """P(home win), P(draw), P(away win) and the single most likely scoreline."""
    grid = score_grid(lh, la, coupling)
    p_home = float(np.tril(grid, -1).sum())
    p_draw = float(np.trace(grid))
    p_away = float(np.triu(grid, 1).sum())
    h, a = np.unravel_index(int(np.argmax(grid)), grid.shape)
    return p_home, p_draw, p_away, (int(h), int(a))


# --------------------------------------------------------------------------
# Prediction runs
# --------------------------------------------------------------------------

def current_tier_map(df: pd.DataFrame, tiers_by_season: dict, cutoff: pd.Timestamp, upcoming: pd.DataFrame | None = None) -> dict[int, str]:
    """Each team's tier as of `cutoff`: the tier they play in during the
    season containing the cutoff if known (so a promoted team is treated as a
    Prem team from day one), else their most recent season before it."""
    d = df[df["date"] < cutoff]
    both = pd.concat([
        d[["home_team_id", "date", "tier"]].rename(columns={"home_team_id": "team_id"}),
        d[["away_team_id", "date", "tier"]].rename(columns={"away_team_id": "team_id"}),
    ]).sort_values("date")
    out = both.groupby("team_id")["tier"].last().to_dict()
    if upcoming is not None:
        for side in ("home", "away"):
            for t, s, tier in upcoming[[f"{side}_team_id", "season", "tier"]].drop_duplicates().itertuples(index=False):
                out[t] = tiers_by_season.get((t, s), tier)
    return out


def predict_season(df: pd.DataFrame, season: str, half_life: float, shrink: float, rolling: bool,
                   season_discount: float = 1.0, tier_home: bool = True) -> tuple[pd.DataFrame, Fit]:
    """Predict every match of `season`. rolling=True refits at the start of each
    match week using everything played before it (how it would run live);
    rolling=False fits once at the season start and never updates."""
    tiers_by_season = team_tiers_by_season(df)
    moves = team_moves(df)
    test = df[df["season"] == season].copy()
    test["week"] = test["date"].dt.to_period("W-SUN").dt.start_time
    weeks = sorted(test["week"].unique())

    rows = []
    fit: Fit | None = None
    for wk in weeks:
        cutoff = pd.Timestamp(wk)
        this_week = test[test["week"] == wk]
        if fit is None or rolling:
            tt = current_tier_map(df, tiers_by_season, cutoff, upcoming=test)
            fit = fit_model(df, cutoff, half_life, shrink, tt, moves, season_discount=season_discount, warm=fit,
                            tier_home=tier_home)
        for _, r in this_week.iterrows():
            ht = tiers_by_season[(r["home_team_id"], season)]
            at = tiers_by_season[(r["away_team_id"], season)]
            lh, la = fit.rates(r["home_team_id"], r["away_team_id"], ht, at, neutral=bool(r["neutral"]),
                               home_move=moves.get((r["home_team_id"], season)), away_move=moves.get((r["away_team_id"], season)),
                               match_tier=r["tier"])
            p_h, p_d, p_a, (sh, sa) = outcome_probs(lh, la, fit.coupling)
            rows.append({
                "date": r["date"].date(), "competition": r["competition"], "tier": r["tier"],
                "home": r["home"], "away": r["away"], "home_score": r["home_score"], "away_score": r["away_score"],
                "xg_home": lh, "xg_away": la, "p_home": p_h, "p_draw": p_d, "p_away": p_a,
                "pred_score": f"{sh}-{sa}",
                "home_known": r["home_team_id"] in fit.attack, "away_known": r["away_team_id"] in fit.attack,
                "home_id": r["home_team_id"], "away_id": r["away_team_id"],
            })
    return pd.DataFrame(rows), fit


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def outcome(h: int, a: int) -> str:
    return "H" if h > a else ("A" if a > h else "D")


def score(pred: pd.DataFrame) -> dict:
    actual = np.array([outcome(h, a) for h, a in zip(pred["home_score"], pred["away_score"])])
    P = pred[["p_home", "p_draw", "p_away"]].to_numpy()
    labels = np.array(["H", "D", "A"])
    picked = labels[P.argmax(axis=1)]
    y = (actual[:, None] == labels[None, :]).astype(float)
    p_actual = (P * y).sum(axis=1)
    diff_pred = pred["xg_home"] - pred["xg_away"]
    diff_act = pred["home_score"] - pred["away_score"]
    return {
        "n": len(pred),
        "accuracy": float((picked == actual).mean()),
        "log_loss": float(-np.log(np.clip(p_actual, 1e-12, 1)).mean()),
        "brier": float(((P - y) ** 2).sum(axis=1).mean()),
        "exact_score": float((pred["pred_score"] == pred["home_score"].astype(str) + "-" + pred["away_score"].astype(str)).mean()),
        "goal_mae": float((np.abs(pred["xg_home"] - pred["home_score"]).mean() + np.abs(pred["xg_away"] - pred["away_score"]).mean()) / 2),
        "margin_bias": float((diff_act - diff_pred).mean()),
        "pred_goals_per_match": float((pred["xg_home"] + pred["xg_away"]).mean()),
        "actual_goals_per_match": float((pred["home_score"] + pred["away_score"]).mean()),
        "home_pick_rate": float((picked == "H").mean()),
        "draw_pick_rate": float((picked == "D").mean()),
        "pred_draw_rate": float(pred["p_draw"].mean()),
        "actual_draw_rate": float((actual == "D").mean()),
    }


def baselines(df: pd.DataFrame, season: str, pred: pd.DataFrame) -> dict:
    """Naive competitors the model must beat."""
    train = df[df["season"] < season]
    actual = np.array([outcome(h, a) for h, a in zip(pred["home_score"], pred["away_score"])])
    base = np.array([(train["home_score"] > train["away_score"]).mean(),
                     (train["home_score"] == train["away_score"]).mean(),
                     (train["home_score"] < train["away_score"]).mean()])
    y = (actual[:, None] == np.array(["H", "D", "A"])[None, :]).astype(float)
    out = {
        "always_home_accuracy": float((actual == "H").mean()),
        "base_rate_log_loss": float(-np.log((y * base[None, :]).sum(axis=1)).mean()),
        "base_rate_brier": float(((base[None, :] - y) ** 2).sum(axis=1).mean()),
    }
    # "Better goal difference last season" picks the winner; unknown -> home.
    prev = df[df["season"] == max(s for s in df["season"].unique() if s < season)]
    gd: dict[int, float] = {}
    for _, r in prev.iterrows():
        gd[r["home_team_id"]] = gd.get(r["home_team_id"], 0.0) + (r["home_score"] - r["away_score"])
        gd[r["away_team_id"]] = gd.get(r["away_team_id"], 0.0) + (r["away_score"] - r["home_score"])
    picks = []
    for _, r in pred.iterrows():
        gh, ga = gd.get(r["home_id"]), gd.get(r["away_id"])
        picks.append("H" if gh is None or ga is None or gh >= ga else "A")
    out["last_season_gd_accuracy"] = float((np.array(picks) == actual).mean())
    return out


def calibration(pred: pd.DataFrame) -> pd.DataFrame:
    P = pred[["p_home", "p_draw", "p_away"]].to_numpy()
    fav_p = P.max(axis=1)
    labels = np.array(["H", "D", "A"])
    fav = labels[P.argmax(axis=1)]
    actual = np.array([outcome(h, a) for h, a in zip(pred["home_score"], pred["away_score"])])
    bins = pd.cut(fav_p, [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    t = pd.DataFrame({"bin": bins, "p": fav_p, "hit": fav == actual})
    g = t.groupby("bin", observed=True).agg(n=("hit", "size"), predicted=("p", "mean"), actual=("hit", "mean"))
    return g


def promoted_relegated(df: pd.DataFrame, season: str, pred: pd.DataFrame) -> pd.DataFrame:
    """How the model did on teams that changed tier into the test season."""
    tiers = team_tiers_by_season(df)
    prev_season = max(s for s in df["season"].unique() if s < season)
    rank = {"Premier": 0, "Division 1": 1, "Conference": 2}
    moves: dict[int, str] = {}
    for (t, s), tier in tiers.items():
        if s == season and (t, prev_season) in tiers:
            before, now = rank[tiers[(t, prev_season)]], rank[tier]
            if now < before:
                moves[t] = "promoted"
            elif now > before:
                moves[t] = "relegated"
    rows = []
    for kind in ("promoted", "relegated"):
        ids = {t for t, k in moves.items() if k == kind}
        sub = pred[pred["home_id"].isin(ids) | pred["away_id"].isin(ids)]
        if sub.empty:
            continue
        # Bias from the moved team's point of view: actual minus predicted margin.
        sign = np.where(sub["home_id"].isin(ids), 1, -1)
        bias = ((sub["home_score"] - sub["away_score"]) - (sub["xg_home"] - sub["xg_away"])) * sign
        s = score(sub)
        rows.append({"group": kind, "teams": len(ids), "matches": len(sub), "accuracy": s["accuracy"],
                     "log_loss": s["log_loss"], "margin_bias_for_moved_team": float(bias.mean())})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Upcoming fixtures
# --------------------------------------------------------------------------

# Chosen by tuning on 2024/25 (see reports/). Re-tune when a season completes.
# tier_home (2026-10-20, reports/home_advantage_2026-10-20.txt + 2025/26 backtest): men's fits a Prem
# home edge of x1.02 v x1.20 in the Conferences and Prem log-loss improves 0.776 -> 0.760. Women's stays
# off: with a ~1-year memory the per-tier edge chases noise (Prem fitted x1.20, log-loss 0.823 -> 0.834).
TUNED = {
    "M": {"half_life": 50.0, "shrink": 0.3, "season_discount": 1.0, "tier_home": True},
    "W": {"half_life": 1.0, "shrink": 1.0, "season_discount": 0.25, "tier_home": False},
}


def load_fixtures(gender: str, until: pd.Timestamp) -> pd.DataFrame:
    sql = text(
        """
        SELECT m.id AS match_id, s.name AS season, c.name AS competition, m.starts_at,
               m.home_team_id, ht.name AS home, m.away_team_id, at.name AS away
        FROM matches m
        JOIN seasons s ON s.id = m.season_id
        JOIN competitions c ON c.id = m.competition_id
        JOIN teams ht ON ht.id = m.home_team_id
        JOIN teams at ON at.id = m.away_team_id
        WHERE m.status = 'S'
        """
    )
    with create_engine(DATABASE_URL).connect() as conn:
        df = pd.read_sql(sql, conn)
    is_w = df["competition"].str.lower().str.contains("women")
    df = (df[is_w] if gender == "W" else df[~is_w]).copy()
    df["date"] = pd.to_datetime(df["starts_at"])
    df = df[df["date"] <= until]
    df["tier"] = df["competition"].map(tier_of)
    df["neutral"] = df["competition"].str.contains("Finals Weekend")
    df["season_idx"] = np.where(df["date"].dt.month >= 8, df["date"].dt.year, df["date"].dt.year - 1)
    return df.sort_values("date").reset_index(drop=True)


def predict_upcoming(gender: str, until: pd.Timestamp, as_of: pd.Timestamp | None = None) -> tuple[pd.DataFrame, Fit]:
    """Fit on everything played before `as_of` (default: now) and predict
    every scheduled fixture up to `until`."""
    params = TUNED[gender]
    played = load_matches(gender)
    fixtures = load_fixtures(gender, until)
    as_of = as_of or pd.Timestamp.now().normalize()
    # Tiers/moves need the upcoming season's league placements, which only the
    # fixture list knows.
    both = pd.concat([played, fixtures.assign(home_score=np.nan, away_score=np.nan)], ignore_index=True)
    tiers_by_season = team_tiers_by_season(both)
    moves = team_moves(both)
    tt = current_tier_map(played, tiers_by_season, as_of, upcoming=fixtures)
    fit = fit_model(played, as_of, params["half_life"], params["shrink"], tt, moves,
                    season_discount=params["season_discount"], tier_home=params["tier_home"])
    rows = []
    for _, r in fixtures.iterrows():
        s = r["season"]
        ht, at = tiers_by_season[(r["home_team_id"], s)], tiers_by_season[(r["away_team_id"], s)]
        lh, la = fit.rates(r["home_team_id"], r["away_team_id"], ht, at, neutral=bool(r["neutral"]),
                           home_move=moves.get((r["home_team_id"], s)), away_move=moves.get((r["away_team_id"], s)))
        p_h, p_d, p_a, (sh, sa) = outcome_probs(lh, la, fit.coupling)
        rows.append({
            "date": r["date"].date(), "competition": r["competition"], "tier": r["tier"],
            "home": r["home"], "away": r["away"], "xg_home": round(lh, 2), "xg_away": round(la, 2),
            "p_home": round(p_h, 3), "p_draw": round(p_d, 3), "p_away": round(p_a, 3), "pred_score": f"{sh}-{sa}",
            "home_id": r["home_team_id"], "away_id": r["away_team_id"],
        })
    return pd.DataFrame(rows), fit


def projected_table(pred: pd.DataFrame, competition_substr: str = "Premier Division") -> pd.DataFrame:
    """Expected points/goals per team over the predicted fixtures."""
    p = pred[pred["competition"].str.contains(competition_substr) & ~pred["competition"].str.contains("Finals Weekend")]
    rows = []
    for _, r in p.iterrows():
        for side, opp in (("home", "away"), ("away", "home")):
            p_win = r["p_home"] if side == "home" else r["p_away"]
            rows.append({"team": r[side], "played": 1, "xpts": 3 * p_win + r["p_draw"],
                         "xgf": r[f"xg_{side}"], "xga": r[f"xg_{opp}"], "xwins": p_win})
    t = pd.DataFrame(rows).groupby("team").sum(numeric_only=True)
    t["xgd"] = t["xgf"] - t["xga"]
    return t.sort_values("xpts", ascending=False).round({"xpts": 1, "xgf": 1, "xga": 1, "xgd": 1, "xwins": 1})


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def tune(df: pd.DataFrame, val_season: str) -> tuple[float, float, float]:
    """Pick half-life, shrinkage and season discount by static-fit log-loss on
    the validation season. Static (one fit at season start) keeps this to a
    few dozen fits; the chosen values are then used for the rolling backtest."""
    best = None
    print(f"Tuning on {val_season} (static fit at season start):")
    for hl in (1.0, 4.0, 50.0):
        for sh in (0.3, 1.0, 3.0):
            for sd in (0.25, 0.5, 0.75, 1.0):
                pred, _ = predict_season(df, val_season, hl, sh, rolling=False, season_discount=sd)
                ll = score(pred)["log_loss"]
                print(f"  half-life={hl:<5} shrink={sh:<5} season-discount={sd:<5} log-loss={ll:.4f}")
                if best is None or ll < best[0]:
                    best = (ll, hl, sh, sd)
    print(f"  -> chosen half-life={best[1]} shrink={best[2]} season-discount={best[3]}\n")
    return best[1], best[2], best[3]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gender", choices=["M", "W"], required=True)
    ap.add_argument("--test-season", default="2025/26")
    ap.add_argument("--val-season", default="2024/25")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--half-life", type=float, default=1.0)
    ap.add_argument("--shrink", type=float, default=1.0)
    ap.add_argument("--season-discount", type=float, default=1.0)
    ap.add_argument("--out", default="output")
    ap.add_argument("--tier-home", choices=["on", "off"], default=None,
                    help="Per-tier home edge (default: TUNED for the gender; off = the pre-2026-10-20 model)")
    ap.add_argument("--upcoming", action="store_true", help="Predict scheduled fixtures instead of backtesting")
    ap.add_argument("--until", default="2026-12-31", help="With --upcoming: last fixture date to include")
    ap.add_argument("--competition", default="Premier Division", help="With --upcoming: which fixtures to print (all are written to CSV)")
    args = ap.parse_args()

    if args.upcoming:
        pred, fit = predict_upcoming(args.gender, pd.Timestamp(args.until))
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, f"upcoming_{args.gender}_to_{args.until}.csv")
        pred.drop(columns=["home_id", "away_id"]).to_csv(path, index=False)
        pd.set_option("display.width", 200)
        label = "Men" if args.gender == "M" else "Women"
        print(f"{label}: {len(pred)} scheduled fixtures to {args.until}; model fitted on everything played to date "
              f"(home advantage x{math.exp(fit.home):.3f}, coupling {fit.coupling:.3f})\n")
        show = pred[pred["competition"].str.contains(args.competition)]
        print(f"-- {args.competition} fixtures --")
        print(show.drop(columns=["home_id", "away_id", "tier", "competition"]).to_string(index=False))
        print(f"\n-- projected {args.competition} table over these fixtures (expected points) --")
        print(projected_table(pred, args.competition).to_string())
        print(f"\nAll fixtures written to {path}")
        return

    df = load_matches(args.gender)
    print(f"{'Men' if args.gender == 'M' else 'Women'}: {len(df)} played matches, seasons {sorted(df['season'].unique())}\n")

    hl, sh, sd = (tune(df, args.val_season) if args.tune else (args.half_life, args.shrink, args.season_discount))

    th = TUNED[args.gender]["tier_home"] if args.tier_home is None else args.tier_home == "on"
    static, fit_start = predict_season(df, args.test_season, hl, sh, rolling=False, season_discount=sd, tier_home=th)
    rolling, fit_end = predict_season(df, args.test_season, hl, sh, rolling=True, season_discount=sd, tier_home=th)

    pd.set_option("display.width", 160)
    print(f"=== {args.test_season} backtest ({'Men' if args.gender == 'M' else 'Women'}, half-life {hl}y, shrink {sh}, season discount {sd}) ===")
    print(f"Fitted home advantage at season start: x{math.exp(fit_start.home):.3f} on scoring rate "
          f"(~{math.exp(fit_start.mu + fit_start.home) - math.exp(fit_start.mu):+.2f} goals at a neutral-strength matchup)")
    print("Home edge by tier at season end: " + ", ".join(f"{k} x{math.exp(fit_end.home_edge(k)):.3f}" for k in TIERS))
    print(f"Tier means (attack / defence): " + ", ".join(f"{k}: {fit_end.tier_attack[k]:+.2f}/{fit_end.tier_defence[k]:+.2f}" for k in TIERS))
    print("First season after a move (attack / defence offsets, log scale): "
          + ", ".join(f"{k}: {fit_end.move_attack[k]:+.2f}/{fit_end.move_defence[k]:+.2f}" for k in MOVE_KINDS))
    print(f"Goal coupling (bivariate Poisson, share of min(xG) common to both sides): start {fit_start.coupling:.3f}, end {fit_end.coupling:.3f}")
    for name, pred in (("static (fit once, season start)", static), ("rolling (refit weekly)", rolling)):
        s = score(pred)
        print(f"\n-- {name} --")
        for k, v in s.items():
            print(f"  {k:<24} {v:.3f}" if isinstance(v, float) else f"  {k:<24} {v}")
    b = baselines(df, args.test_season, rolling)
    print("\n-- baselines --")
    for k, v in b.items():
        print(f"  {k:<26} {v:.3f}")

    print("\n-- rolling model by tier --")
    for tier in TIERS:
        sub = rolling[rolling["tier"] == tier]
        if len(sub):
            s = score(sub)
            print(f"  {tier:<12} n={s['n']:<5} acc={s['accuracy']:.3f} log-loss={s['log_loss']:.3f} goal-MAE={s['goal_mae']:.2f} exact={s['exact_score']:.3f}")

    print("\n-- calibration of the favourite (rolling) --")
    print(calibration(rolling).to_string())

    pr = promoted_relegated(df, args.test_season, rolling)
    if not pr.empty:
        print("\n-- teams that changed tier into the test season (rolling) --")
        print(pr.to_string(index=False))

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"predictions_{args.gender}_{args.test_season.replace('/', '-')}.csv")
    rolling.drop(columns=["home_id", "away_id"]).to_csv(path, index=False)
    print(f"\nPer-match predictions written to {path}")


if __name__ == "__main__":
    sys.exit(main())
