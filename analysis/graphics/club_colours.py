"""
Club colours for graphics.

`analysis/club_colours.csv` (club, primary, secondary, source) is the source of
truth; graphics scripts call `colour_for(club)`. `--auto` seeds the file by
picking the dominant saturated colour from each club's badge (rough - meant to
be corrected by hand; hand edits are kept on re-runs because only rows with
source=auto are overwritten). `--sheet` renders a contact sheet of badge +
swatch + name so the wrong ones can be spotted at a glance.

Usage (inside the jupyter container):
    python graphics/club_colours.py --auto --sheet
"""
from __future__ import annotations

import argparse
import colorsys
import csv
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "mysql+pymysql://hockey_stats:hockey_stats@db:3306/hockey_stats?charset=utf8mb4")
HERE = os.path.dirname(os.path.abspath(__file__))
BADGES = os.path.join(HERE, "..", "badges")
CSV = os.path.join(HERE, "..", "club_colours.csv")  # analysis/club_colours.csv - the jupyter container mounts analysis/ only
NAVY, INK, LIME, MUTED = "#0B1220", "#F4F6F2", "#9EF01A", "#8B94A7"
FALLBACK = ["#9EF01A", "#F4F6F2", "#FF6B6B", "#4CC9F0", "#FFD166", "#C77DFF", "#06D6A0", "#FF9F1C",
            "#EF476F", "#118AB2", "#B5E48C", "#F72585", "#8ECAE6", "#FB8500"]


def slug(club: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", club.lower()).strip("-")


def badge_path(club: str) -> str | None:
    for ext in ("png", "jpg", "jpeg"):
        p = os.path.join(BADGES, f"{slug(club)}.{ext}")
        if os.path.exists(p):
            return p
    return None


def load_colours() -> dict[str, dict[str, str]]:
    if not os.path.exists(CSV):
        return {}
    with open(CSV, newline="", encoding="utf-8") as f:
        return {r["club"]: r for r in csv.DictReader(f)}


def save_colours(rows: dict[str, dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(CSV), exist_ok=True)
    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["club", "primary", "secondary", "source"])
        w.writeheader()
        for club in sorted(rows):
            w.writerow({k: rows[club].get(k, "") for k in w.fieldnames})


def colour_for(club: str, index: int = 0) -> tuple[str, str | None]:
    """(primary, secondary or None). Falls back to a palette entry by index."""
    r = load_colours().get(club)
    if r and r.get("primary"):
        return r["primary"], (r.get("secondary") or None)
    return FALLBACK[index % len(FALLBACK)], None


def dominant_colour(path: str) -> str:
    """Most common strongly-saturated, mid-brightness colour in the badge."""
    im = Image.open(path).convert("RGBA").resize((96, 96))
    px = np.array(im).reshape(-1, 4)
    px = px[px[:, 3] > 200][:, :3] / 255.0
    best, best_n = None, 0
    buckets: dict[tuple[int, int, int], int] = {}
    for r, g, b in px:
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        if s < 0.35 or v < 0.2 or v > 0.97:
            continue  # skip white/black/grey
        key = (int(h * 18), int(s * 3), int(v * 3))
        buckets[key] = buckets.get(key, 0) + 1
    if not buckets:
        # dark-only badge (black/navy crest): pick the darkest common tone
        dark = px[(px.max(axis=1) < 0.35)]
        return "#1F2937" if len(dark) else "#8B94A7"
    key = max(buckets, key=buckets.get)
    h = (key[0] + 0.5) / 18; s = min(1.0, (key[1] + 0.5) / 3 + 0.15); v = min(0.95, (key[2] + 0.5) / 3 + 0.15)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


def active_clubs() -> list[str]:
    with create_engine(DATABASE_URL).connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT c.name FROM clubs c JOIN teams t ON t.club_id=c.id JOIN matches m ON t.id IN (m.home_team_id, m.away_team_id)
            JOIN seasons s ON s.id=m.season_id WHERE s.name IN ('2025/26','2026/27') ORDER BY c.name
        """)).all()
    return [r[0] for r in rows]


def contact_sheet(rows: dict[str, dict[str, str]], out: str) -> None:
    clubs = sorted(rows)
    cols, size = 4, 1.05
    n_rows = (len(clubs) + cols - 1) // cols
    fig = plt.figure(figsize=(cols * 4.2, n_rows * size + 0.6), dpi=100); fig.patch.set_facecolor(NAVY)
    for i, club in enumerate(clubs):
        r, c = divmod(i, cols)
        x0 = c / cols; y0 = 1 - (r + 1) * size / (n_rows * size + 0.6)
        bp = badge_path(club)
        ax = fig.add_axes([x0 + 0.005, y0 + 0.002, 0.045, size / (n_rows * size + 0.6) * 0.9]); ax.axis("off")
        ax.add_patch(plt.Circle((0.5, 0.5), 0.5, color="#FFFFFF", transform=ax.transAxes))
        if bp:
            ib = fig.add_axes([x0 + 0.005 + 0.045 * 0.15, y0 + 0.002 + size / (n_rows * size + 0.6) * 0.9 * 0.15, 0.045 * 0.7, size / (n_rows * size + 0.6) * 0.9 * 0.7]); ib.axis("off")
            ib.imshow(Image.open(bp).convert("RGBA"))
        sw = fig.add_axes([x0 + 0.058, y0 + 0.008, 0.02, size / (n_rows * size + 0.6) * 0.7]); sw.axis("off")
        sw.add_patch(plt.Rectangle((0, 0), 1, 1, color=rows[club].get("primary") or "#444", transform=sw.transAxes))
        if rows[club].get("secondary"):
            sw.add_patch(plt.Rectangle((0, 0), 1, 0.4, color=rows[club]["secondary"], transform=sw.transAxes))
        fig.text(x0 + 0.083, y0 + size / (n_rows * size + 0.6) * 0.55, club.replace(" HC", ""), fontsize=9.5, color=INK, va="center", fontweight="bold")
        fig.text(x0 + 0.083, y0 + size / (n_rows * size + 0.6) * 0.25, f"{rows[club].get('primary','')}  {'(' + rows[club]['source'] + ')' if rows[club].get('source') else ''}", fontsize=8, color=MUTED, va="center")
    fig.text(0.01, 0.985, "CLUB COLOURS — tell me which are wrong", fontsize=13, fontweight="bold", color=LIME, va="center")
    fig.savefig(out); plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true", help="Seed/refresh auto colours from badges (hand-set rows kept)")
    ap.add_argument("--sheet", action="store_true", help="Render a contact sheet")
    ap.add_argument("--out", default="output/graphics/club_colours_sheet.png")
    args = ap.parse_args()
    rows = load_colours()
    if args.auto:
        for club in active_clubs():
            if club in rows and rows[club].get("source") == "hand":
                continue
            bp = badge_path(club)
            rows[club] = {"club": club, "primary": dominant_colour(bp) if bp else "", "secondary": "", "source": "auto" if bp else "none"}
        save_colours(rows)
        print(f"{len(rows)} clubs in {CSV}")
    if args.sheet:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        contact_sheet(rows, args.out)
        print(args.out)


if __name__ == "__main__":
    main()
