"""Per-layer overview PNGs of the overworld plane.

layer_<n>.png       full plane extent, 1 px per tile
layer_<n>_zoom.png  crop around placed groups, 3 px per tile (layers with placements)

Grey = overworld maps on that layer; blue = proposed placement; orange =
tentative placement (uncertain/unreachable group). Groups are identified by
the "#n" labels used in REPORT.md (colour encodes status only, never identity).
"+" marks the source tile of an exit that leads into a placed group.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SURFACE = (252, 252, 251)
OW_FILL = (201, 200, 195)
OW_EDGE = (176, 175, 170)
PROPOSED = (42, 120, 214)      # categorical slot 1 (blue)
TENTATIVE = (235, 104, 52)     # categorical slot 2 (orange)
INK = (11, 11, 11)
INK2 = (82, 81, 78)


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
    ow_rects = {}
    for m in G[ow_id]["maps"]:
        X, Y, Z = maps[m].coords
        ow_rects.setdefault(Z, []).append((24 * X, 24 * Y, 24 * X + 24, 24 * Y + 24))
    placed = {}
    entrances = {}
    for g in plan["groups"]:
        p = g.get("placement")
        if not p or p.get("status") not in ("proposed", "tentative"):
            continue
        for m, (x, y, z) in p["member_origins"].items():
            placed.setdefault(z, []).append((x, y, x + maps[m].width, y + maps[m].height, g["id"], p["status"]))
        for key in ("entrances_from_overworld", "entrances_from_other_groups"):
            for e in g.get(key, []):
                if "plane_source" in e:
                    sx, sy, sz = e["plane_source"]
                    entrances.setdefault(sz, set()).add((sx, sy))
    xs = [r[0] for v in list(ow_rects.values()) + list(placed.values()) for r in v]
    xe = [r[2] for v in list(ow_rects.values()) + list(placed.values()) for r in v]
    ys = [r[1] for v in list(ow_rects.values()) + list(placed.values()) for r in v]
    ye = [r[3] for v in list(ow_rects.values()) + list(placed.values()) for r in v]
    pad = 8
    x0, y0, x1, y1 = min(xs) - pad, min(ys) - pad, max(xe) + pad, max(ye) + pad
    if placed:
        zx0 = min(r[0] for v in placed.values() for r in v) - 30
        zy0 = min(r[1] for v in placed.values() for r in v) - 30
        zx1 = max(r[2] for v in placed.values() for r in v) + 30
        zy1 = max(r[3] for v in placed.values() for r in v) + 30
    layers = sorted(set(ow_rects) | set(placed))
    written = []
    for z in layers:
        for kind in ("full", "zoom"):
            if kind == "zoom" and z not in placed:
                continue
            if kind == "full":
                bx0, by0, bx1, by1, s = x0, y0, x1, y1, 1
            else:
                bx0, by0, bx1, by1, s = zx0, zy0, zx1, zy1, 3
            img = Image.new("RGB", ((bx1 - bx0) * s, (by1 - by0) * s + 80), SURFACE)
            d = ImageDraw.Draw(img)
            oy = 80  # header band

            def R(r, fill, outline=None):
                d.rectangle([(r[0] - bx0) * s, (r[1] - by0) * s + oy, (r[2] - bx0) * s - 1,
                             (r[3] - by0) * s - 1 + oy], fill=fill, outline=outline)

            for r in ow_rects.get(z, ()):
                R(r, OW_FILL, OW_EDGE if s > 1 else None)
            groups_here = {}
            for r in placed.get(z, ()):
                col = PROPOSED if r[5] == "proposed" else TENTATIVE
                R(r, col, INK if s > 1 else None)
                g = groups_here.setdefault(r[4], [r[0], r[1], r[2], r[3]])
                g[0], g[1] = min(g[0], r[0]), min(g[1], r[1])
                g[2], g[3] = max(g[2], r[2]), max(g[3], r[3])
            for (ex, ey) in sorted(entrances.get(z, ())):
                cx, cy = (ex - bx0) * s + s // 2, (ey - by0) * s + s // 2 + oy
                arm = 2 if s == 1 else 4
                d.line([cx - arm, cy, cx + arm, cy], fill=INK)
                d.line([cx, cy - arm, cx, cy + arm], fill=INK)
            f = _font(11 if s == 1 else 14)
            for gid, (gx0, gy0, gx1, gy1) in sorted(groups_here.items()):
                tx, ty = (gx1 - bx0) * s + 2, (gy0 - by0) * s + oy - 2
                d.text((tx, ty), "#{}".format(lab[gid]), fill=INK, font=f, stroke_width=2, stroke_fill=SURFACE)
            ft = _font(16)
            fs = _font(11)
            d.text((6, 4), "Overworld plane - layer {}{}".format(
                z, "" if kind == "full" else " (zoom, 3 px per tile)"), fill=INK, font=ft)
            d.text((6, 24), "x {}..{}  y {}..{}  ({} px per tile)".format(bx0, bx1 - 1, by0, by1 - 1, s),
                   fill=INK2, font=fs)
            lx = 6
            for col, text in ((OW_FILL, "overworld map on this layer"), (PROPOSED, "proposed placement"),
                              (TENTATIVE, "tentative (owner decision)")):
                d.rectangle([lx, 44, lx + 10, 54], fill=col, outline=INK2)
                d.text((lx + 14, 42), text, fill=INK2, font=fs)
                lx += 14 + int(d.textlength(text, font=fs)) + 14
            d.text((6, 60), "+ source tile of an exit into a placed group    #n = group label in REPORT.md",
                   fill=INK2, font=fs)
            name = "layer_{}.png".format(z) if kind == "full" else "layer_{}_zoom.png".format(z)
            img.save(out / name, optimize=True)
            written.append(name)
    return written
