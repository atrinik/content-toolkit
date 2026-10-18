"""Overview PNGs of the overworld plane (standard library only).

layer_<n>.png       full plane extent, 1 px per tile
layer_<n>_zoom.png  crop around placed groups, 3 px per tile (layers with placements or stairwells)
relayer_<n>.png     one relayered area: every affected layer before (legacy) and after (this plan)

Only occupied tiles are drawn (the footprint rule: empty legacy tiles are
void), every overworld tile on its final layer. Grey = overworld tiles that
stay on their layer; aqua = overworld tiles the relayering moved onto this
layer; blue = proposed placement; orange = tentative placement; pale blue band
= the view gap (view_radius tiles) every placed group keeps from unrelated
areas. Markers: black diamond = stairwell tile (labelled SWn / SLn / SRn),
hollow diamond = sloped stairwell tile, "+" = source tile of an exit into a
placed group, hollow square = wired exit (legacy dangling exit), "x" in the
relayer images = new stairs on a cut seam. Groups are identified by the "#n"
labels used in REPORT.md and relayered regions by "Rn" (colour encodes status
only, never identity).
"""

from __future__ import annotations

import collections
from pathlib import Path

import footprint as fp
from raster import Canvas

SURFACE = (252, 252, 251)
OW_FILL = (201, 200, 195)
PROPOSED = (42, 120, 214)      # categorical slot 1 (blue)
TENTATIVE = (235, 104, 52)     # categorical slot 2 (orange)
MOVED = (27, 175, 122)         # categorical slot 3 (aqua): overworld tiles moved onto this layer
GAP = (205, 226, 251)          # blue step 100: the view-gap band (a zone, not a series)
INK = (11, 11, 11)
INK2 = (82, 81, 78)
HEADER = 128
PREFIXES = ("SW", "SL", "SR")


def labels(plan) -> dict:
    """Stable '#n' label per placed group, ordered by group id."""
    ids = sorted(g["id"] for g in plan["groups"] if g.get("placement", {}).get("status") in ("proposed", "tentative"))
    return {gid: i + 1 for i, gid in enumerate(ids)}


def _legend(c: Canvas, x: int, y: int, items):
    for col, text in items:
        c.rect(x, y, x + 9, y + 9, col)
        c.outline(x, y, x + 9, y + 9, INK2)
        x += 14 + c.text(x + 14, y + 1, text, INK2, 1) + 12


def _header(c: Canvas, title: str, sub: str, radius: int):
    c.text(6, 6, title, INK, 2)
    c.text(6, 26, sub, INK2, 1)
    _legend(c, 6, 40, ((OW_FILL, "overworld"), (MOVED, "overworld moved here by the relayering"),
                       (PROPOSED, "proposed placement"), (TENTATIVE, "tentative"),
                       (GAP, "{}-tile view gap".format(radius))))
    c.cross(11, 63, 4, INK)
    c.text(20, 60, "source tile of an exit into a placed group    #N = group label in REPORT.MD", INK2, 1)
    c.diamond(11, 80, 4, INK)
    c.text(20, 77, "stairwell tile (SWN, SRN = ids in REPORT.MD)", INK2, 1)
    c.diamond(311, 80, 4, INK)
    c.diamond(311, 80, 2, SURFACE)
    c.text(320, 77, "sloped stairwell tile (SLN)", INK2, 1)
    c.outline(7, 92, 15, 100, INK)
    c.text(20, 93, "wired exit (led nowhere in legacy; W in the zoom)", INK2, 1)
    c.text(6, 110, "occupied tiles only, each on its final layer: empty legacy tiles are void", INK2, 1)


def _place_label(img, taken, cx, cy, text, ls):
    """Draw text near (cx, cy) at the first candidate spot that overlaps no earlier label."""
    w, h = Canvas.text_width(text, ls), 7 * ls
    for dx, dy in ((8, 7), (8, -21), (-8 - w, 7), (-8 - w, -21), (8, 21), (8, -35), (-8 - w, 21), (-8 - w, -35),
                   (8, 35), (8, -49)):
        r = (cx + dx - 1, cy + dy - 1, cx + dx + w + 1, cy + dy + h + 1)
        if not any(r[0] < t[2] and t[0] < r[2] and r[1] < t[3] and t[1] < r[3] for t in taken):
            break
    taken.append(r)
    img.text(r[0] + 1, r[1] + 1, text, INK, ls, halo=SURFACE)


def _stair_labels(img, P, stair, taken, ls, near=3):
    """Label clusters of stairwell tiles (within `near` tiles) with their ids (prefix kept, numbers joined)."""
    seen = []
    for (sx, sy, sid) in sorted(stair, key=lambda t: (t[1], t[0], PREFIXES.index(t[2][:2]))):
        if any(abs(sx - a) <= near and abs(sy - b) <= near and p_ == sid[:2] for (a, b, p_) in seen):
            continue
        seen.append((sx, sy, sid[:2]))
        nums = sorted({int(i[2:]) for (a, b, i) in stair
                       if abs(a - sx) <= near and abs(b - sy) <= near and i[:2] == sid[:2]})
        cx, cy = P(sx, sy)
        _place_label(img, taken, cx, cy, sid[:2] + ",".join(str(n) for n in nums), ls)


def overworld_tiles(relay):
    """{final layer: ([unmoved tiles], [tiles moved here])} for the overworld plane."""
    still = collections.defaultdict(list)
    moved = collections.defaultdict(list)
    for (L, x, y), ks in relay.final_positions().items():
        (moved if any(k[0] != L for k in ks) else still)[L].append((x, y))
    return still, moved


def render_layers(plan, maps, G, ow_id, out: Path, relay, ow_split=None):
    lab = labels(plan)
    radius = plan["settings"]["view_radius_tiles"]
    still, moved = ow_split if ow_split is not None else overworld_tiles(relay)
    placed = collections.defaultdict(lambda: collections.defaultdict(set))  # layer -> gid -> tiles
    status = {}
    entrances = collections.defaultdict(set)
    wired = collections.defaultdict(set)
    for g in plan["groups"]:
        for key in ("entrances_from_overworld", "exits_to_overworld", "entrances_from_other_groups",
                    "exits_to_other_groups"):
            for e in g.get(key, []):
                if e.get("form") == "wired" and "plane_source" in e:
                    sx, sy, sz = e["plane_source"]
                    wired[sz].add((sx, sy))
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
    sloped = collections.defaultdict(set)
    chains = collections.defaultdict(lambda: collections.defaultdict(set))  # layer -> owner -> tiles
    for sw in plan["stairwells"]:
        for t in sw["tiles"][1:-1]:
            if sw["kind"] != "relayering":
                chains[t["layer"]][sw["group"]].add((t["x"], t["y"]))
            stair[t["layer"]].append((t["x"], t["y"], sw["id"]))
            if sw["shape"] == "sloped":
                sloped[t["layer"]].add((t["x"], t["y"]))
    for z in list(placed):
        for gid in [k for k, v in placed[z].items() if not v]:
            del placed[z][gid]
        if not placed[z]:
            del placed[z]
    ext = [b for g in plan["groups"] if g["classification"] == "overworld" for b in g["bounding_boxes"].values()]
    all_placed = [t for z in placed for ts in placed[z].values() for t in ts]
    zb = fp.bbox_of(all_placed)
    zx0, zy0, zx1, zy1 = zb[0] - 30, zb[1] - 30, zb[2] + 31, zb[3] + 31
    pad = 8
    x0 = min([b["x_min"] for b in ext] + [zx0 + pad]) - pad
    y0 = min([b["y_min"] for b in ext] + [zy0 + pad]) - pad
    x1 = max([b["x_max"] + 1 for b in ext] + [zx1 - pad]) + pad
    y1 = max([b["y_max"] + 1 for b in ext] + [zy1 - pad]) + pad
    layers = sorted(set(still) | set(moved) | set(placed) | set(chains) | set(stair))
    written = []
    for z in layers:
        base = Canvas(x1 - x0, y1 - y0, SURFACE)
        groups_here = placed.get(z, {})
        chains_here = chains.get(z, {})
        band = set()
        for gid in sorted(set(groups_here) | set(chains_here)):
            band |= fp.dilate(groups_here.get(gid, set()) | chains_here.get(gid, set()), radius)
        for (x, y) in band:
            base.put(x - x0, y - y0, GAP)
        for (x, y) in still.get(z, ()):
            base.put(x - x0, y - y0, OW_FILL)
        for (x, y) in moved.get(z, ()):
            base.put(x - x0, y - y0, MOVED)
        for gid, ts in sorted(groups_here.items()):
            col = PROPOSED if status.get(gid) == "proposed" else TENTATIVE
            for (x, y) in ts:
                base.put(x - x0, y - y0, col)
        for kind in ("full", "zoom"):
            if kind == "zoom" and not groups_here and not chains_here:
                continue
            if kind == "full":
                s, bx0, by0, bx1, by1 = 1, x0, y0, x1, y1
                body = base
            else:
                s, bx0, by0, bx1, by1 = 3, zx0, zy0, zx1, zy1
                body = base.crop_scaled(zx0 - x0, zy0 - y0, zx1 - x0, zy1 - y0, 3, SURFACE)
            img = Canvas(max(body.w, 640), body.h + HEADER, SURFACE)
            img.paste(body, 0, HEADER)

            def P(x, y):
                return (x - bx0) * s + s // 2, (y - by0) * s + s // 2 + HEADER

            def inside(x, y):
                return bx0 <= x < bx1 and by0 <= y < by1

            for (ex, ey) in sorted(entrances.get(z, ())):
                cx, cy = P(ex, ey)
                img.cross(cx, cy, 2 if s == 1 else 4, INK)
            for (wx, wy) in sorted(wired.get(z, ())):
                cx, cy = P(wx, wy)
                r = 2 if s == 1 else 4
                img.outline(cx - r, cy - r, cx + r, cy + r, INK)
            for (sx, sy, _sid) in sorted(stair.get(z, ())):
                cx, cy = P(sx, sy)
                img.diamond(cx, cy, 2 if s == 1 else 4, INK, ring=SURFACE)
                if (sx, sy) in sloped.get(z, ()) and s > 1:
                    img.diamond(cx, cy, 2, SURFACE)
            ls = 1 if s == 1 else 2
            taken = []
            for gid, ts in sorted(groups_here.items()):
                _x0, gy0, gx1, _y1 = fp.bbox_of(ts)
                tx, ty = (gx1 + 1 - bx0) * s + 3, (gy0 - by0) * s + HEADER
                text = "#{}".format(lab[gid])
                img.text(tx, ty, text, INK, ls, halo=SURFACE)
                taken.append((tx - 1, ty - 1, tx + Canvas.text_width(text, ls) + 1, ty + 7 * ls + 1))
            if s > 1:
                _stair_labels(img, P, [t for t in stair.get(z, ()) if inside(t[0], t[1])], taken, ls)
                done = []
                for (wx, wy) in sorted(wired.get(z, ()), key=lambda t: (t[1], t[0])):
                    if not inside(wx, wy) or any(abs(wx - a) <= 4 and abs(wy - b) <= 4 for (a, b) in done):
                        continue
                    done.append((wx, wy))
                    cx, cy = P(wx, wy)
                    _place_label(img, taken, cx, cy, "W", ls)
            _header(img, "Overworld plane - layer {}{}".format(z, "" if kind == "full" else " (zoom)"),
                    "x {}..{}  y {}..{}  ({} px per tile)".format(bx0, bx1 - 1, by0, by1 - 1, s), radius)
            name = "layer_{}.png".format(z) if kind == "full" else "layer_{}_zoom.png".format(z)
            (out / name).write_bytes(img.png())
            written.append(name)
    written += render_relayer(plan, out, relay)
    return written


def relayer_areas(plan):
    """Relayered areas: regions whose boxes lie within 48 tiles merge, then every moved unit
    (storey or cave) joins the area of the regions that made it move and widens its box.
    Returns [x0, y0, x1, y1, [region and unit ids]] in region order."""
    rl = plan["relayering"]

    def near(a, b, m):
        return b[0] - m <= a[2] and a[0] - m <= b[2] and b[1] - m <= a[3] and a[1] - m <= b[3]

    areas = []
    for r in sorted(rl["regions"], key=lambda r: (r["bbox"][1], r["bbox"][0])):
        areas.append([r["bbox"][0], r["bbox"][1], r["bbox"][2], r["bbox"][3], [r["id"]]])
    merged = True
    while merged:
        merged = False
        for i in range(len(areas)):
            for j in range(i + 1, len(areas)):
                a, b = areas[i], areas[j]
                if near(a, b, 48):
                    a[0], a[1], a[2], a[3] = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])
                    a[4] += b[4]
                    del areas[j]
                    merged = True
                    break
            if merged:
                break
    for u in rl["units_moved"]:
        hit = [a for a in areas if set(a[4]) & set(u["linked_regions"])]
        if not hit:
            areas.append([u["bbox"][0], u["bbox"][1], u["bbox"][2], u["bbox"][3], [u["id"]]])
            continue
        a = hit[0]
        for b in hit[1:]:
            a[0], a[1], a[2], a[3] = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])
            a[4] += b[4]
            areas.remove(b)
        b = u["bbox"]
        a[0], a[1], a[2], a[3] = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])
        a[4].append(u["id"])
    order = {"R": 0, "U": 1}
    for a in areas:
        a[4] = sorted(a[4], key=lambda i: (order[i[0]], int(i[1:])))
    areas.sort(key=lambda a: (order[a[4][0][0]], int(a[4][0][1:])))
    return areas


def unit_span(ids):
    """U1 U2 U3 U7 -> U1-U3, U7."""
    nums = sorted(int(i[1:]) for i in ids)
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append("U{}".format(nums[i]) if i == j else "U{}-U{}".format(nums[i], nums[j]))
        i = j + 1
    return ", ".join(out) or "none"


def render_relayer(plan, out: Path, relay):
    """relayer_<n>.png: before/after panels of every layer an area's relayering touches."""
    rl = plan["relayering"]
    region_box = {r["id"]: r for r in rl["regions"]}
    tiles_before = collections.defaultdict(dict)   # legacy layer -> (x, y) -> moves?
    tiles_after = collections.defaultdict(dict)    # final layer -> (x, y) -> moved here?
    for (L, x, y), ks in relay.final_positions().items():
        tiles_after[L][(x, y)] = any(k[0] != L for k in ks)
    moved = set(relay.moved)
    for k in relay.tiles:
        tiles_before[k[0]][(k[1], k[2])] = k in moved
    # regions lowered in place keep their layer; their tiles are drawn with a texture
    rel_regions = [r for r in relay.regions if r["relayered"]]
    in_place = set()
    region_layers = {}
    for i, r in enumerate(rel_regions, 1):
        region_layers["R{}".format(i)] = {r["layer"]} | {relay.final_layer[k] for k in r["tiles"]}
        if relay.dr.get(r["index"], 0):
            in_place.update((k[1], k[2], k[0]) for k in r["tiles"])
    stairs = collections.defaultdict(list)
    for n in rl["new_stairs"]:
        stairs[n["low"][2]].append((n["low"][0], n["low"][1]))
        stairs[n["high"][2]].append((n["high"][0], n["high"][1]))
    sw_tiles = collections.defaultdict(list)
    for sw in plan["stairwells"]:
        for t in sw["tiles"]:
            sw_tiles[t["layer"]].append((t["x"], t["y"], sw["id"], t["role"]))
    written = []
    areas = relayer_areas(plan)
    outside = [k for k in relay.moved if not any(a[0] <= k[1] <= a[2] and a[1] <= k[2] <= a[3] for a in areas)]
    if outside:
        raise RuntimeError("{} moved tiles lie outside every relayer image, e.g. {}".format(len(outside), outside[0]))
    for n, (ax0, ay0, ax1, ay1, ids) in enumerate(areas, 1):
        ax0, ay0, ax1, ay1 = ax0 - 8, ay0 - 8, ax1 + 9, ay1 + 9
        layers = set()
        for k in relay.moved:
            if ax0 <= k[1] < ax1 and ay0 <= k[2] < ay1:
                layers.add(k[0])
                layers.add(relay.final_layer[k])
        for rid in ids:
            layers |= region_layers.get(rid, set())
        layers = sorted(layers, reverse=True)
        w, h = ax1 - ax0, ay1 - ay0
        s = max(1, min(4, 560 // max(w, 1), 420 // max(h, 1)))
        pw, ph = w * s, h * s
        gapx, top = 12, 106
        img = Canvas(max(len(layers) * (pw + gapx) + 70, 760), top + 2 * (ph + 24) + 8, SURFACE)
        rids = [i for i in ids if i.startswith("R")]
        units = [i for i in ids if i.startswith("U")]
        img.text(6, 6, "Relayering area {}: {}".format(n, " ".join(rids) or "-"), INK, 2)
        img.text(6, 26, "x {}..{}  y {}..{}  ({} px per tile); top row: legacy layers, bottom row: this plan".format(
            ax0, ax1 - 1, ay0, ay1 - 1, s), INK2, 1)
        _legend(img, 6, 40, ((OW_FILL, "stays on its layer"), (MOVED, "moves (top row) / moved here (bottom row)")))
        tx = 6 + 14 + Canvas.text_width("stays on its layer") + 12 + 14 + \
            Canvas.text_width("moves (top row) / moved here (bottom row)") + 12
        for yy in range(10):
            for xx in range(10):
                img.put(tx + xx, 40 + yy, MOVED if (xx + yy) % 2 else OW_FILL)
        img.outline(tx, 40, tx + 9, 49, INK2)
        img.text(tx + 14, 41, "lowered in place (keeps its layer, height minus 50)", INK2, 1)
        img.diamond(11, 63, 4, INK)
        img.text(20, 60, "stairwell tile (SRN, SWN, SLN; ends included; labelled in the bottom row)", INK2, 1)
        img.cross(11, 78, 4, INK)
        img.text(20, 75, "new stairs on a cut seam (NS in REPORT.MD)", INK2, 1)
        img.text(6, 90, "RN = relayered region, labelled where it ends up; moved units in this area: {}".format(
            unit_span(units)), INK2, 1)
        taken = []
        for row, data in enumerate((tiles_before, tiles_after)):
            oy = top + row * (ph + 24)
            img.text(6, oy + 14, "BEFORE" if row == 0 else "AFTER", INK, 1)
            for i, L in enumerate(layers):
                ox = 60 + i * (pw + gapx)
                img.text(ox, oy, "L{}".format(L), INK, 1)
                img.outline(ox - 1, oy + 11, ox + pw, oy + 12 + ph, INK2)
                for (x, y), mv in data.get(L, {}).items():
                    if ax0 <= x < ax1 and ay0 <= y < ay1:
                        px, py = ox + (x - ax0) * s, oy + 12 + (y - ay0) * s
                        if (x, y, L) in in_place:
                            for yy in range(s):
                                for xx in range(s):
                                    img.put(px + xx, py + yy, MOVED if (x + y + xx + yy) % 2 else OW_FILL)
                        else:
                            img.rect(px, py, px + s - 1, py + s - 1, MOVED if mv else OW_FILL)
                if row == 1:
                    for (x, y) in stairs.get(L, ()):
                        if ax0 <= x < ax1 and ay0 <= y < ay1:
                            img.cross(ox + (x - ax0) * s + s // 2, oy + 12 + (y - ay0) * s + s // 2, 2 + s // 2, INK)
                    here = [(x, y, sid) for (x, y, sid, _role) in sw_tiles.get(L, ())
                            if ax0 <= x < ax1 and ay0 <= y < ay1]
                    for (x, y, _sid) in here:
                        img.diamond(ox + (x - ax0) * s + s // 2, oy + 12 + (y - ay0) * s + s // 2, 1 + s // 2,
                                    INK, ring=SURFACE)
                    _stair_labels(img, lambda x, y: (ox + (x - ax0) * s + s // 2, oy + 12 + (y - ay0) * s + s // 2),
                                  here, taken, 1, near=max(3, 12 // s))
                    for rid in rids:
                        r = region_box[rid]
                        for Ls, cnt in r["final_layers"].items():
                            if int(Ls) == L and cnt:
                                bx, by = r["bbox"][0], r["bbox"][1]
                                img.text(ox + max(0, (bx - ax0)) * s + 2, oy + 14 + max(0, (by - ay0)) * s,
                                         rid, INK, 1, halo=SURFACE)
        name = "relayer_{}.png".format(n)
        (out / name).write_bytes(img.png())
        written.append(name)
    return written
