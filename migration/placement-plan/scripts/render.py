"""Per-layer overview PNGs of the overworld plane.

layer_<n>.png       full plane extent, 1 px per tile
layer_<n>_zoom.png  crop around placed groups, 3 px per tile (layers with placements or stairwells)

Only occupied tiles are drawn (the footprint rule: empty legacy tiles are
void). Grey = overworld maps on that layer; blue = proposed placement;
orange = tentative placement; pale blue band = the view gap (view_radius
tiles) every placed group keeps from unrelated areas; black diamond = a
stairwell tile on that layer (labelled SWn in the zoom). Groups are
identified by the "#n" labels used in REPORT.md (colour encodes status only,
never identity). "+" marks the source tile of an exit into a placed group.
"""

from __future__ import annotations

import collections
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import footprint as fp

SURFACE = (252, 252, 251)
OW_FILL = (201, 200, 195)
PROPOSED = (42, 120, 214)      # categorical slot 1 (blue)
TENTATIVE = (235, 104, 52)     # categorical slot 2 (orange)
GAP = (205, 226, 251)          # blue step 100: the view-gap band (a zone, not a series)
INK = (11, 11, 11)
INK2 = (82, 81, 78)
HEADER = 96


def labels(plan) -> dict:
    """Stable '#n' label per placed group, ordered by group id."""
    ids = sorted(g["id"] for g in plan["groups"] if g.get("placement", {}).get("status") in ("proposed", "tentative"))
    return {gid: i + 1 for i, gid in enumerate(ids)}


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - old Pillow
        return ImageFont.load_default()


def render_layers(plan, maps, G, ow_id, out: Path):
    lab = labels(plan)
    radius = plan["settings"]["view_radius_tiles"]
    ow_tiles = collections.defaultdict(list)
    for m in G[ow_id]["maps"]:
        X, Y, Z = maps[m].coords
        ow_tiles[Z].extend((24 * X + x, 24 * Y + y) for (x, y) in maps[m].occupied)
    placed = collections.defaultdict(lambda: collections.defaultdict(set))  # layer -> gid -> tiles
    status = {}
    entrances = collections.defaultdict(set)
    for g in plan["groups"]:
        p = g.get("placement")
        if not p or p.get("status") not in ("proposed", "tentative"):
            continue
        status[g["id"]] = p["status"]
        for m, (x, y, z) in p["member_origins"].items():
            placed[z][g["id"]].update((x + tx, y + ty) for (tx, ty) in maps[m].occupied)
        for key in ("entrances_from_overworld", "entrances_from_other_groups"):
            for e in g.get(key, []):
                if "plane_source" in e:
                    sx, sy, sz = e["plane_source"]
                    entrances[sz].add((sx, sy))
    stair = collections.defaultdict(list)  # layer -> (x, y, id)
    chains = collections.defaultdict(lambda: collections.defaultdict(set))  # layer -> gid -> stairwell tiles
    for sw in plan["stairwells"]:
        for t in sw["tiles"][1:-1]:
            chains[t["layer"]][sw["group"]].add((t["x"], t["y"]))
            stair[t["layer"]].append((t["x"], t["y"], sw["id"]))
    for z in list(placed):
        for gid in [k for k, v in placed[z].items() if not v]:
            del placed[z][gid]
        if not placed[z]:
            del placed[z]
    ext = [b for g in plan["groups"] if g["classification"] == "overworld" for b in g["bounding_boxes"].values()]
    for z in placed:
        for ts in placed[z].values():
            bx = fp.bbox_of(ts)
            ext.append({"x_min": bx[0], "y_min": bx[1], "x_max": bx[2], "y_max": bx[3]})
    all_placed = [t for z in placed for ts in placed[z].values() for t in ts]
    zb = fp.bbox_of(all_placed)
    zx0, zy0, zx1, zy1 = zb[0] - 30, zb[1] - 30, zb[2] + 31, zb[3] + 31
    pad = 8
    x0 = min([b["x_min"] for b in ext] + [zx0 + pad]) - pad
    y0 = min([b["y_min"] for b in ext] + [zy0 + pad]) - pad
    x1 = max([b["x_max"] + 1 for b in ext] + [zx1 - pad]) + pad
    y1 = max([b["y_max"] + 1 for b in ext] + [zy1 - pad]) + pad
    layers = sorted(set(ow_tiles) | set(placed) | set(chains))
    written = []
    for z in layers:
        w, h = x1 - x0, y1 - y0
        buf = bytearray(bytes(SURFACE) * (w * h))

        def put(t, col):
            x, y = t[0] - x0, t[1] - y0
            if 0 <= x < w and 0 <= y < h:
                i = 3 * (y * w + x)
                buf[i:i + 3] = bytes(col)

        groups_here = placed.get(z, {})
        chains_here = chains.get(z, {})
        band = set()
        for gid in sorted(set(groups_here) | set(chains_here)):
            band |= fp.dilate(groups_here.get(gid, set()) | chains_here.get(gid, set()), radius)
        for t in band:
            put(t, GAP)
        for t in ow_tiles.get(z, ()):
            put(t, OW_FILL)
        for gid, ts in sorted(groups_here.items()):
            col = PROPOSED if status.get(gid) == "proposed" else TENTATIVE
            for t in ts:
                put(t, col)
        base = Image.frombytes("RGB", (w, h), bytes(buf))
        for kind in ("full", "zoom"):
            if kind == "zoom" and not groups_here and not chains_here:
                continue
            if kind == "full":
                s, bx0, by0, bx1, by1 = 1, x0, y0, x1, y1
                body = base
            else:
                s, bx0, by0, bx1, by1 = 3, zx0, zy0, zx1, zy1
                body = base.crop((zx0 - x0, zy0 - y0, zx1 - x0, zy1 - y0)).resize(
                    ((zx1 - zx0) * 3, (zy1 - zy0) * 3), Image.NEAREST)
            img = Image.new("RGB", (body.width, body.height + HEADER), SURFACE)
            img.paste(body, (0, HEADER))
            d = ImageDraw.Draw(img)

            def P(x, y):
                return (x - bx0) * s + s // 2, (y - by0) * s + s // 2 + HEADER

            for (ex, ey) in sorted(entrances.get(z, ())):
                cx, cy = P(ex, ey)
                arm = 2 if s == 1 else 4
                d.line([cx - arm, cy, cx + arm, cy], fill=INK)
                d.line([cx, cy - arm, cx, cy + arm], fill=INK)
            fs = _font(11)
            for (sx, sy, _sid) in sorted(stair.get(z, ())):
                cx, cy = P(sx, sy)
                r = 3 if s == 1 else 5
                d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=INK, outline=SURFACE)
            if s > 1:
                seen = []
                for (sx, sy, _sid) in sorted(stair.get(z, ()), key=lambda t: (t[1], t[0])):
                    if any(abs(sx - a) <= 3 and abs(sy - b) <= 3 for (a, b) in seen):
                        continue
                    seen.append((sx, sy))
                    nums = sorted({int(i[2:]) for (a, b, i) in stair[z] if abs(a - sx) <= 3 and abs(b - sy) <= 3})
                    cx, cy = P(sx, sy)
                    dy = 4 if len(seen) % 2 else -18
                    d.text((cx + 7, cy + dy), "SW" + ",".join(str(n) for n in nums), fill=INK, font=fs,
                           stroke_width=2, stroke_fill=SURFACE)
            f = _font(11 if s == 1 else 14)
            for gid, ts in sorted(groups_here.items()):
                _x0, gy0, gx1, _y1 = fp.bbox_of(ts)
                tx, ty = (gx1 + 1 - bx0) * s + 2, (gy0 - by0) * s + HEADER - 2
                d.text((tx, ty), "#{}".format(lab[gid]), fill=INK, font=f, stroke_width=2, stroke_fill=SURFACE)
            ft = _font(16)
            d.text((6, 4), "Overworld plane - layer {}{}".format(
                z, "" if kind == "full" else " (zoom, 3 px per tile)"), fill=INK, font=ft)
            d.text((6, 24), "x {}..{}  y {}..{}  ({} px per tile; occupied tiles only, empty legacy tiles are void)".format(
                bx0, bx1 - 1, by0, by1 - 1, s), fill=INK2, font=fs)
            lx = 6
            for col, text in ((OW_FILL, "overworld map"), (PROPOSED, "proposed placement"),
                              (TENTATIVE, "tentative"), (GAP, "{}-tile view gap".format(radius))):
                d.rectangle([lx, 44, lx + 10, 54], fill=col, outline=INK2)
                d.text((lx + 14, 42), text, fill=INK2, font=fs)
                lx += 14 + int(d.textlength(text, font=fs)) + 14
            d.text((6, 60), "+ source tile of an exit into a placed group    #n = group label in REPORT.md",
                   fill=INK2, font=fs)
            d.polygon([(10, 72), (15, 77), (10, 82), (5, 77)], fill=INK)
            d.text((20, 71), "stairwell tile on this layer (SWn = stairwell id in REPORT.md)", fill=INK2, font=fs)
            name = "layer_{}.png".format(z) if kind == "full" else "layer_{}_zoom.png".format(z)
            img.save(out / name, optimize=True)
            written.append(name)
    return written
