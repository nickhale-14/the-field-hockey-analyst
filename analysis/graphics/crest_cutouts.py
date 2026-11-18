"""Crest cutouts, two versions per club: <out>/navy/<club>.png and <out>/paper/<club>.png.

Background is read from the image border:
  white  -> flood-fill the white away from the border only (white inside a crest stays), soft edges.
  dark   -> flood-fill the dark away for navy; on paper keep the dark square as a rounded tile.
  colour -> the square IS the badge: rounded tile on both.
Navy fixes: dark mono crests (no colour) are reversed to paper; crests that are mostly dark get a
thin paper outline so they don't vanish into navy.
usage: cutout.py <src_dir> <out_dir>"""
import sys, os, glob, json
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

src, out = sys.argv[1], sys.argv[2]
for d in ("navy", "paper"):
    os.makedirs(f"{out}/{d}", exist_ok=True)
MAX = 512
PAPER = np.array([246, 245, 240], np.float32)
LUM = np.array([0.2126, 0.7152, 0.0722], np.float32)
report = {}


def flood_cut(a, light_bg, bgc=None):
    """Alpha from a border-connected region of near-bg pixels, ramped for soft edges."""
    minc, maxc = a.min(axis=2), a.max(axis=2)
    if light_bg:
        near, ramp = minc >= 170, np.clip((255 - minc) / 85, 0, 1)
    else:
        dist = np.abs(a - bgc).max(axis=2)
        near, ramp = dist <= 45, np.clip(dist / 45, 0, 1)
    lab, _ = ndimage.label(near)
    edge = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    bg = np.isin(lab, edge[edge > 0])
    alpha = np.ones(minc.shape, np.float32)
    alpha[bg] = ramp[bg]
    al = alpha[..., None]
    base = 255.0 if light_bg else bgc
    rgb = np.clip(np.where(al > 0.01, (a - base * (1 - al)) / np.maximum(al, 0.01), 0), 0, 255)
    return rgb, alpha


def to_img(rgb, alpha):
    img = Image.fromarray(np.dstack([rgb, alpha * 255]).astype(np.uint8), "RGBA")
    bbox = img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    return img.crop(bbox) if bbox else img


def tile(im):
    """Square logo kept whole, corners rounded."""
    im = im.convert("RGBA")
    m = Image.new("L", im.size, 0)
    r = int(min(im.size) * 0.14)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, im.width - 1, im.height - 1], r, fill=255)
    im.putalpha(m)
    return im


def outline(img, px=6):
    """Thin paper outline following the crest's silhouette."""
    a = img.getchannel("A")
    pad = px + 2
    big = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    am = Image.new("L", big.size, 0)
    am.paste(a, (pad, pad))
    grown = am.filter(ImageFilter.MaxFilter(2 * px + 1)).filter(ImageFilter.GaussianBlur(1))
    ring = Image.new("RGBA", big.size, (246, 245, 240, 0))
    ring.putalpha(grown)
    big.alpha_composite(ring)
    big.alpha_composite(img, (pad, pad))
    return big


for path in sorted(glob.glob(f"{src}/*.png")):
    name = os.path.basename(path)[:-4]
    im = Image.open(path).convert("RGB")
    src_size = im.size
    if max(im.size) < 400:          # small sources: upscale before cutting so the edges stay smooth
        k = 400 / max(im.size)
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    im.thumbnail((MAX, MAX), Image.LANCZOS)
    a = np.asarray(im).astype(np.float32)
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    med = np.median(border, axis=0)
    kind = "white" if med.min() >= 235 else ("dark" if med.max() <= 70 else "colour")

    if kind == "white":
        paper = navy = to_img(*flood_cut(a, True))
    elif kind == "dark":
        navy, paper = to_img(*flood_cut(a, False, med)), tile(im)
    else:
        paper = navy = tile(im)

    fix = ""
    if kind != "colour":
        px = np.asarray(navy).astype(np.float32)
        solid = px[..., 3] > 230
        rgb = px[..., :3][solid]
        lum, sat = rgb @ LUM, rgb.max(axis=1) - rgb.min(axis=1)
        dark = float((lum < 80).mean())
        colourful = float((sat > 60).mean())
        if kind == "white" and colourful <= 0.1 and dark >= 0.5:
            arr = px.copy()
            m = solid[..., None] & ((arr[..., :3] @ LUM) < 120)[..., None]
            arr[..., :3] = np.where(m, PAPER, arr[..., :3])
            navy, fix = Image.fromarray(arr.astype(np.uint8), "RGBA"), "reversed"
        elif dark >= 0.3:
            navy, fix = outline(navy), "outlined"
    navy.save(f"{out}/navy/{name}.png")
    paper.save(f"{out}/paper/{name}.png")
    report[name] = dict(kind=kind, fix=fix, src=src_size)
    print(f"{name:32s} {kind:7s} {fix:9s} src {src_size}")
json.dump(report, open(f"{out}/_report.json", "w"), indent=1)
