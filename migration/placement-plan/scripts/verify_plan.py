#!/usr/bin/env python3
"""Independent check of placement-plan.json (uses only the JSON).

    python3 -I scripts/verify_plan.py [placement-plan.json]

Placement (every placed group, proposed and tentative):
  * member plane origin == local_offset + (x_offset, y_offset, layer_offset);
    no placed group has a relayered region (its member maps are recorded on
    one layer each; the generator refuses to publish otherwise)
  * the JSON carries rows for every overworld map near a tile of a placed
    group or stairwell (same layer, same or neighbouring 24x24 cell), every map
    off the surface layer, every surface map under or over such a map, and
    every map the relayering touches
  * no occupied tile is claimed by two maps on one layer
  * gap rule: on each layer, a group's occupied tiles are more than
    view_radius_tiles (Chebyshev) from every owner it shares no exit with, and
    more than contact_margin_tiles from every owner it shares one with;
    related owners are re-derived from the exit records, an overworld map only
    on the layer where the shared exit ends
Stairwells (SW placement, SL sloped, SR relayering):
  * consecutive tiles are exactly one layer and at most stair_tolerance_tiles
    apart; a column stairwell's ends are at most that far apart, a sloped one's
    at most that far per layer; the ends match every replaced exit
  * every intermediate tile is void, used by no other stairwell and holds no
    exit object; the recorded view conflicts equal the unrelated owners within
    the view radius and the recorded contacts the related areas within the
    contact margin; mode "clear" means there are none of either; every
    stairwell carries the engine rule segments_drawn_only_on_player_layer
  * a stairwell's direction matches the exits it replaces, and each end holds
    exactly the exit objects (archetype, exit flag) that start there (a
    one-way stairwell's far end is a landing)
Exits:
  * the records of one exit (one per group it touches) are identical; an
    exit labelled stairwell is listed in exactly that stairwell's replaces
  * every exit record's plane_source / plane_target equals origin + tile on
    the tile's final layer, and its "becomes" label matches its geometry;
    wired exits are plain stairs and their verdict follows from their evidence
  * every exit 2+ layers and at most one tile per layer apart is a stairwell
    (crossing exits always, overworld exits when the relayering changed their
    span), unless a blocked site lists it; a blocked site has no void chain
Relayering (re-derived from the overworld rows and tile heights):
  * regions: 8-connected tiles of one layer with the same q = H // layer
    height >= 1; relayered when large enough or touching a relayered region
  * every unit (8-connected tiles of one layer off the surface) moves as one;
    surface tiles move only with their region; storeys move only up, caves
    only down; a relayered tile ends on layer + q + unit shift + region
    adjustment (adjustment <= 0), split tiles keep their ground part
  * column order is kept in every column, no cave tile reaches layer 0 and no
    moved tile ends on layer 0
  * cut walkable edges, seams, seam pieces and their new stairs; steep steps;
    new contacts; moved maps and the before/after layer deltas of exits
Exit status 0 when every check passes.
"""

from __future__ import annotations

import bisect
import collections
import json
import re
import sys
from pathlib import Path

WORLD_RE = re.compile(r"/world_(-?\d+)_(-?\d+)(?:_(-?\d+))?$")
N8 = [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dy) != (0, 0)]


def world_cell(path: str):
    m = WORLD_RE.search(path)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def cheb(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


class Rows:
    """Occupied tiles of one owner on one layer, indexed by row for range queries."""

    def __init__(self):
        self.rows = collections.defaultdict(list)
        self.box = None
        self.count = 0

    def add(self, x, y):
        self.rows[y].append(x)
        self.count += 1
        b = self.box
        self.box = (x, y, x, y) if b is None else (min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y))

    def done(self):
        for xs in self.rows.values():
            xs.sort()

    def any_within(self, x, y, r) -> bool:
        b = self.box
        if b is None or x < b[0] - r or x > b[2] + r or y < b[1] - r or y > b[3] + r:
            return False
        for yy in range(y - r, y + r + 1):
            xs = self.rows.get(yy)
            if xs:
                i = bisect.bisect_left(xs, x - r)
                if i < len(xs) and xs[i] <= x + r:
                    return True
        return False

    def tiles(self):
        for y, xs in self.rows.items():
            for x in xs:
                yield x, y


def flood(keys, same):
    keyset = set(keys)
    seen, out = set(), []
    for k in sorted(keyset):
        if k in seen:
            continue
        comp, stack = [k], [k]
        seen.add(k)
        while stack:
            c = stack.pop()
            for dx, dy in N8:
                n = (c[0], c[1] + dx, c[2] + dy)
                if n in keyset and n not in seen and same(c, n):
                    seen.add(n)
                    stack.append(n)
                    comp.append(n)
        out.append(sorted(comp))
    return out


def verify(plan: dict) -> dict:
    st = plan["settings"]
    R, M, tol = st["view_radius_tiles"], st["contact_margin_tiles"], st["stair_tolerance_tiles"]
    HZ, MINR, WDZ = st["layer_height_z"], st["relayer_min_region_tiles"], st["walkable_dz"]
    ow = next(g for g in plan["groups"] if g["classification"] == "overworld")
    ow_id = ow["id"]
    rl = plan["relayering"]
    errors = []

    # ------------------------------------------------------------ occupancy
    owners = collections.defaultdict(dict)  # layer -> owner -> Rows
    claim = collections.defaultdict(dict)   # layer -> (x, y) -> (owner, map)
    origin = {}
    leg = {}                                # (legacy layer, x, y) -> [map, final layers]

    def put(owner, mapname, layer, x0, y0, rows):
        rr = owners[layer].setdefault(owner, Rows())
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                if ch == "#":
                    t = (x0 + x, y0 + y)
                    prev = claim[layer].get(t)
                    if prev is not None and prev != (owner, mapname):
                        errors.append("tile {} layer {} claimed by {} and {}".format(t, layer, prev[1], mapname))
                    claim[layer][t] = (owner, mapname)
                    rr.add(*t)

    ow_rows = ow["occupied_rows_by_layer"]
    for path, by_layer in sorted(ow_rows.items()):
        X, Y, Z = world_cell(path)
        for Ls, rows in sorted(by_layer.items(), key=lambda kv: int(kv[0])):
            put(path, path, int(Ls), 24 * X, 24 * Y, rows)
            for y, row in enumerate(rows):
                for x, ch in enumerate(row):
                    if ch == "#":
                        leg.setdefault((Z, 24 * X + x, 24 * Y + y), [path, []])[1].append(int(Ls))
    placed = []
    for g in plan["groups"]:
        p = g.get("placement")
        if not p or p.get("status") not in ("proposed", "tentative"):
            continue
        placed.append(g)
        for m in g["maps"]:
            lo = m["local_offset"]
            exp = [lo[0] + p["x_offset"], lo[1] + p["y_offset"], lo[2] + p["layer_offset"]]
            got = p["member_origins"][m["path"]]
            if exp != got:
                errors.append("origin mismatch {} {} != {}".format(m["path"], got, exp))
            origin[m["path"]] = got
            if "occupied_rows" not in m:
                errors.append("no occupied rows for placed map {}".format(m["path"]))
                continue
            put(g["id"], m["path"], got[2], got[0], got[1], m["occupied_rows"])
    for layer in owners.values():
        for rr in layer.values():
            rr.done()

    # rows coverage near every placed or stairwell tile
    used_cells = collections.defaultdict(set)
    for g in placed:
        for z, by in owners.items():
            rr = by.get(g["id"])
            if rr is not None:
                used_cells[z].update((x // 24, y // 24) for x, y in rr.tiles())
    for sw in plan["stairwells"]:
        for t in sw["tiles"][1:-1]:
            used_cells[t["layer"]].add((t["x"] // 24, t["y"] // 24))
    prefix = ow_id + "_"
    for z, cells in sorted(used_cells.items()):
        have = {tuple(c) for c in ow["cells_by_layer"].get(str(z), [])}
        for cx, cy in sorted(cells):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if (cx + dx, cy + dy) in have:
                        path = "{}{}_{}{}".format(prefix, cx + dx, cy + dy, "" if z == 0 else "_{}".format(z))
                        if path not in ow_rows:  # an included map carries every one of its final layers
                            errors.append("rows missing for {} near used layer {}".format(path, z))
    for mm in rl["maps_moved"]:
        if mm["path"] not in ow_rows:
            errors.append("rows missing for relayered map {}".format(mm["path"]))
    stacked = set()
    for z, cells in ow["cells_by_layer"].items():
        if int(z) != 0:
            stacked.update(tuple(c) for c in cells)
    for z, cells in ow["cells_by_layer"].items():
        for X, Y in cells:
            path = "{}{}_{}{}".format(prefix, X, Y, "" if int(z) == 0 else "_{}".format(z))
            if (int(z) != 0 or (X, Y) in stacked) and path not in ow_rows:
                errors.append("rows missing for {} (off the surface or stacked)".format(path))

    # ---------------------------------------------------- final overworld layer
    void_ends = {(v["map"], tuple(v["tile"])): v["layer"] for v in rl["exit_ends_on_void_tiles"]}

    def ow_layer(path, tx, ty):
        X, Y, Z = world_cell(path)
        k = (Z, 24 * X + tx, 24 * Y + ty)
        if k in leg:
            return max(leg[k][1])
        return void_ends.get((path, (tx, ty)), Z)

    # ------------------------------------------------------ related owners
    def end_of(e, side):
        g = e["source_group"] if side == "s" else e["target_group"]
        m = e["source_map"] if side == "s" else e["target_map"]
        t = e["source_tile"] if side == "s" else e["target_tile"]
        if g == ow_id:
            return m, ow_layer(m, *t)
        return g, None

    rel = collections.defaultdict(set)  # owner -> {(owner, layer or None)}
    for g in placed:
        for key in ("entrances_from_overworld", "exits_to_overworld", "entrances_from_other_groups",
                    "exits_to_other_groups"):
            for e in g.get(key, []):
                a, b = end_of(e, "s"), end_of(e, "t")
                rel[a[0]].add(b)
                rel[b[0]].add(a)
    for g in placed:
        got = rel[g["id"]]
        if sorted({o for o, _L in got}) != g.get("related_owners"):
            errors.append("related owners of {} differ from the exit records".format(g["id"]))
        lay = collections.defaultdict(set)
        for o, L in got:
            if L is not None:
                lay[o].add(L)
        if {m: sorted(v) for m, v in sorted(lay.items())} != g.get("related_overworld_layers"):
            errors.append("related overworld layers of {} differ from the exit records".format(g["id"]))
    group_ids = {g["id"] for g in plan["groups"] if g["classification"] != "overworld"}

    def related(a, b, z):
        """a is a placed group; b an owner on layer z."""
        if b in group_ids:
            return (b, None) in rel[a] or (a, None) in rel[b]
        return (b, z) in rel[a]

    # ------------------------------------------------------------ gap rule
    def min_distance(a, b, cap):
        small, big = (a, b) if a.count <= b.count else (b, a)

        def hit(r):
            return any(big.any_within(x, y, r) for (x, y) in small.tiles())

        if not hit(cap):
            return cap + 1
        lo, hi = 0, cap
        while lo < hi:
            mid = (lo + hi) // 2
            if hit(mid):
                hi = mid
            else:
                lo = mid + 1
        return lo

    pairs = 0
    closest = {"unrelated": None, "related": None}
    for g in placed:
        gid = g["id"]
        for z, by in sorted(owners.items()):
            mine = by.get(gid)
            if mine is None or mine.box is None:
                continue
            for o, theirs in sorted(by.items()):
                if o == gid or theirs.box is None:
                    continue
                rel_pair = related(gid, o, z)
                need = M if rel_pair else R
                band = need + R
                a, b = mine.box, theirs.box
                if a[0] - band > b[2] or b[0] - band > a[2] or a[1] - band > b[3] or b[1] - band > a[3]:
                    continue
                pairs += 1
                d = min_distance(mine, theirs, band)
                kind = "related" if rel_pair else "unrelated"
                if d <= band and (closest[kind] is None or d < closest[kind][0]):
                    closest[kind] = [d, gid, o, z]
                if d <= need:
                    errors.append("{} {} rule broken on layer {} with {}: distance {} <= {}".format(
                        gid, "contact" if rel_pair else "gap", z, o, d, need))

    # ----------------------------------------------------------- exit index
    records = {}
    for g in plan["groups"]:
        for key in ("entrances_from_overworld", "exits_to_overworld", "entrances_from_other_groups",
                    "exits_to_other_groups", "internal_exits"):
            for e in g.get(key, []):
                k = (e["source_map"], e["line"])
                if k in records and records[k] != e:
                    errors.append("exit {}:{} has two different records".format(*k))
                records[k] = e

    # ----------------------------------------------------------- stairwells
    chain = collections.defaultdict(dict)  # layer -> owner -> Rows
    sw_by_id = {}
    for sw in plan["stairwells"]:
        sw_by_id[sw["id"]] = sw
        for x in sw["tiles"][1:-1]:
            chain[x["layer"]].setdefault(sw["owner"], Rows()).add(x["x"], x["y"])
    for layer in chain.values():
        for rr in layer.values():
            rr.done()
    sw_tiles = {}
    intermediate = 0
    kinds = collections.Counter()
    sr_owners = {sw["owner"] for sw in plan["stairwells"] if sw["kind"] == "relayering"}
    for sw in plan["stairwells"]:
        kinds[sw["kind"]] += 1
        t = sw["tiles"]
        if t[0]["role"] != "top" or t[-1]["role"] != "bottom" or any(x["role"] != "intermediate" for x in t[1:-1]):
            errors.append("{} tile roles are not top, intermediate..., bottom".format(sw["id"]))
        if (t[0]["x"], t[0]["y"], t[0]["layer"]) != (sw["x"], sw["y"], sw["top_layer"]) or \
                t[-1]["layer"] != sw["bottom_layer"]:
            errors.append("{} header does not match its tiles".format(sw["id"]))
        if len(t) < 3:
            errors.append("{} spans fewer than two layers".format(sw["id"]))
        if sw.get("segments_drawn_only_on_player_layer") is not True:
            errors.append("{} lacks the engine rule segments_drawn_only_on_player_layer".format(sw["id"]))
        for a, b in zip(t, t[1:]):
            if a["layer"] - b["layer"] != 1 or max(abs(a["x"] - b["x"]), abs(a["y"] - b["y"])) > tol:
                errors.append("{} is not continuous between layers {} and {}".format(sw["id"], a["layer"], b["layer"]))
        top = (t[0]["x"], t[0]["y"], t[0]["layer"])
        bot = (t[-1]["x"], t[-1]["y"], t[-1]["layer"])
        span = top[2] - bot[2]
        off = cheb(top, bot)
        if sw["shape"] == "column" and off > tol or sw["shape"] == "sloped" and not tol < off <= span * tol:
            errors.append("{} shape {} does not fit ends {} tiles apart over {} layers".format(
                sw["id"], sw["shape"], off, span))
        if sw["horizontal_offset"] != off:
            errors.append("{} horizontal_offset {} != {}".format(sw["id"], sw["horizontal_offset"], off))
        if sw["kind"] == "placement" and sw["shape"] != "column" or sw["kind"] == "sloped" and sw["shape"] != "sloped":
            errors.append("{} kind {} does not match shape {}".format(sw["id"], sw["kind"], sw["shape"]))
        for r in sw["replaces"]:
            e = records.get((r["source_map"], r["line"]))
            if e is None:
                errors.append("{} replaces an unknown exit {}:{}".format(sw["id"], r["source_map"], r["line"]))
                continue
            if e.get("becomes") != "stairwell" or e.get("stairwell") != sw["id"]:
                errors.append("{} exit {}:{} is not marked as this stairwell".format(sw["id"], r["source_map"], r["line"]))
            s, d = e["plane_source"], e["plane_target"]
            up, low = (s, d) if s[2] > d[2] else (d, s)
            if up[2] != top[2] or low[2] != bot[2] or cheb(up, top) > tol or cheb(low, bot) > tol:
                errors.append("{} ends do not match exit {}:{}".format(sw["id"], r["source_map"], r["line"]))
        goes = {r["goes"] for r in sw["replaces"]}
        want = "both" if goes == {"down", "up"} else ("down" if goes == {"down"} else "up")
        if sw.get("direction") != want or sw.get("landing_only_end") != {"both": None, "down": "bottom",
                                                                         "up": "top"}[want]:
            errors.append("{} direction {} does not match its exits ({})".format(sw["id"], sw.get("direction"),
                                                                             sorted(goes)))
        for end, way in ((t[0], "down"), (t[-1], "up")):
            want_objs = sorted({r["arch"] for r in sw["replaces"] if r["goes"] == way})
            if sorted(o.get("archetype") for o in end["objects"]) != want_objs or \
                    any(o.get("exit") is not True for o in end["objects"]) or end.get("existing") is not True:
                errors.append("{} {} end objects are not the exits it replaces ({})".format(sw["id"], end["role"],
                                                                                         want_objs))
        recorded = {(c["layer"], c["x"], c["y"]): sorted(c["visible_from"]) for c in sw["view_conflicts"]}
        touched = {(c["layer"], c["x"], c["y"]): sorted(c["touches"]) for c in sw["contacts"]}
        for x in t[1:-1]:
            intermediate += 1
            key = (x["layer"], x["x"], x["y"])
            if (x["x"], x["y"]) in claim.get(x["layer"], {}):
                errors.append("{} tile {} is not void (occupied by {})".format(
                    sw["id"], key, claim[x["layer"]][(x["x"], x["y"])][1]))
            if any(o.get("exit") is not False for o in x["objects"]):
                errors.append("{} tile {} holds an exit object; intermediate tiles must not be exits".format(
                    sw["id"], key))
            if key in sw_tiles:
                errors.append("{} tile {} is also used by {}".format(sw["id"], key, sw_tiles[key]))
            sw_tiles[key] = sw["id"]
            z = x["layer"]
            cand = list(owners.get(z, {}).items()) + list(chain.get(z, {}).items())
            if sw["kind"] == "relayering":
                vis = sorted({o for o, rr in cand if o not in sr_owners and rr.any_within(x["x"], x["y"], R)})
            else:
                gid = sw["group"]
                vis = sorted({o for o, rr in cand
                              if o != gid and not related(gid, o, z) and rr.any_within(x["x"], x["y"], R)})
            if vis != recorded.get(key, []):
                errors.append("{} tile {} view conflicts {} != recorded {}".format(sw["id"], key, vis,
                                                                                  recorded.get(key, [])))
            touch = [] if sw["kind"] == "relayering" else sorted(
                {o for o, rr in cand if o != sw["group"] and related(sw["group"], o, z)
                 and rr.any_within(x["x"], x["y"], M)})
            if touch != touched.get(key, []):
                errors.append("{} tile {} contacts {} != recorded {}".format(sw["id"], key, touch, touched.get(key, [])))
        if (sw["mode"] == "clear") != (not sw["view_conflicts"] and not sw["contacts"]):
            errors.append("{} mode {} disagrees with its view conflicts and contacts".format(sw["id"], sw["mode"]))

    # ---------------------------------------------------------------- exits
    replaced_by = collections.defaultdict(list)  # exit key -> stairwells listing it in replaces
    for sw in plan["stairwells"]:
        for r in sw["replaces"]:
            replaced_by[(r["source_map"], r["line"])].append(sw["id"])
    exits_checked = 0
    for (src_map, line), e in sorted(records.items()):
        ends = {}
        for end, mkey, tkey, gkey in (("plane_source", "source_map", "source_tile", "source_group"),
                                      ("plane_target", "target_map", "target_tile", "target_group")):
            if end not in e:
                continue
            mp, (tx, ty) = e[mkey], e[tkey]
            if e[gkey] == ow_id:
                X, Y, _Z = world_cell(mp)
                exp = [24 * X + tx, 24 * Y + ty, ow_layer(mp, tx, ty)]
            else:
                o = origin[mp]
                exp = [o[0] + tx, o[1] + ty, o[2]]
            exits_checked += 1
            ends[end] = exp
            if exp != e[end]:
                errors.append("exit {}:{} {} {} != {}".format(src_map, line, end, e[end], exp))
        if len(ends) == 2:
            s, d = ends["plane_source"], ends["plane_target"]
            dist, dl = cheb(s, d), abs(d[2] - s[2])
            b = e.get("becomes")
            sw = sw_by_id.get(e.get("stairwell"))
            ok = {"stairs": dl == 1 and dist <= tol,
                  "stairwell": sw is not None and dl >= 2 and dist <= (tol if sw["shape"] == "column" else dl * tol),
                  "same-tile": dl == 0 and dist == 0,
                  "portal": not (dl == 1 and dist <= tol) and not (dl == 0 and dist == 0)}.get(b, False)
            if not ok:
                errors.append("exit {}:{} labelled {} but is {} tiles / {} layers apart".format(src_map, line, b, dist, dl))
            if b == "stairwell" and replaced_by.get((src_map, line)) != [e.get("stairwell")]:
                errors.append("exit {}:{} is labelled {} but is replaced by {}".format(
                    src_map, line, e.get("stairwell"), replaced_by.get((src_map, line), "no stairwell")))
        if e.get("form") == "wired":
            w = e.get("wired") or {}
            if e.get("becomes") != "stairs" or w.get("plane_target") != e.get("plane_target"):
                errors.append("wired exit {}:{} is not plain stairs to its recorded target".format(src_map, line))
            # the verdict follows from the recorded evidence
            if not w.get("landing_floor_terrain") or w.get("landing_blocked") or w["landing_floor_terrain"] != ["land"]:
                want = "doubtful"
            elif w.get("return_exits_near_landing") or w.get("target_from", "").startswith("the destination"):
                want = "confirmed"
            else:
                want = "plausible"
            if w.get("verdict") != want:
                errors.append("wired exit {}:{} verdict {} does not follow from its evidence ({})".format(
                    src_map, line, w.get("verdict"), want))

    # ------------------------------------------------- required stairwells
    # Every exit whose ends are 2+ layers apart and at most one tile apart per
    # layer must be a stairwell, unless a blocked site lists it: crossing exits
    # (placed groups) always, overworld exits only when the relayering changed
    # their layer distance. Blocked sites must really have no void chain.
    blocked_keys = {}
    for key_, sites in (("column", plan["stairwell_sites_blocked"]), ("sloped", plan["sloped_stairwell_sites_blocked"]),
                        ("relayering", rl["stairwells_blocked"])):
        for b in sites:
            for x in b["exits"]:
                blocked_keys[(x["source_map"], x["line"])] = (key_, b)
    required = 0
    for (src_map, line), e in sorted(records.items()):
        if "plane_source" not in e or "plane_target" not in e:
            continue
        s_, d_ = e["plane_source"], e["plane_target"]
        dl, dist = abs(d_[2] - s_[2]), cheb(s_, d_)
        if dl < 2 or dist > dl * tol:
            continue
        if e["source_group"] == ow_id and e["target_group"] == ow_id:
            legacy = abs(world_cell(e["target_map"])[2] - world_cell(e["source_map"])[2])
            if legacy == dl:
                continue  # a legacy overworld shaft the relayering leaves alone
        required += 1
        if (src_map, line) in blocked_keys:
            if e.get("becomes") != "portal":
                errors.append("exit {}:{} is listed as blocked but becomes {}".format(src_map, line, e.get("becomes")))
        elif e.get("becomes") != "stairwell":
            errors.append("exit {}:{} needs a stairwell ({} layers, {} tiles apart) but becomes {}".format(
                src_map, line, dl, dist, e.get("becomes")))

    def void_at(z, x, y):
        return (x, y) not in claim.get(z, {}) and (z, x, y) not in sw_tiles

    def chain_exists(top, bottom, shape):
        n = top[2] - bottom[2]
        reach = {(top[0], top[1])}
        for i in range(1, n):
            z = top[2] - i
            nxt = set()
            for (x, y) in reach:
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        p_ = (x + dx, y + dy)
                        if shape == "column":
                            ok = cheb(p_, top) <= 2  # STAIRWELL_MAX_BEND
                        else:
                            ok = cheb(p_, top) <= i and cheb(p_, bottom) <= n - i
                        if ok and void_at(z, *p_):
                            nxt.add(p_)
            reach = nxt
        return any(cheb(p_, bottom) <= tol for p_ in reach)

    for key_, b in sorted({id(b): (k_, b) for k_, b in blocked_keys.values()}.values(), key=lambda kb: kb[1]["top"]):
        shape = "sloped" if key_ == "sloped" or b.get("shape") == "sloped" else "column"
        if chain_exists(tuple(b["top"]), tuple(b["bottom"]), shape):
            errors.append("blocked {} stairwell site at {} has a void chain after all".format(shape, b["top"]))

    # ----------------------------------------------------------- relayering
    heights, blocked = {}, set()
    for path, rows in sorted(rl["tile_heights"].items()):
        X, Y, Z = world_cell(path)
        for y, row in enumerate(rows):
            for x, tok in enumerate(row.split(" ")):
                if tok == ".":
                    continue
                k = (Z, 24 * X + x, 24 * Y + y)
                if tok.endswith("b"):
                    blocked.add(k)
                    tok = tok[:-1]
                heights[k] = int(tok)
    split = {}
    for sp in rl["split_tiles"]:
        X, Y, Z = world_cell(sp["map"])
        split[(Z, 24 * X + sp["tile"][0], 24 * Y + sp["tile"][1])] = sp
    main = {k: (max(v[1]) if k in split else v[1][0]) for k, v in leg.items()}
    for k, v in leg.items():
        if len(v[1]) != (2 if k in split else 1):
            errors.append("legacy tile {} of {} appears on layers {}".format(k, v[0], v[1]))
    # regions
    q = {k: h // HZ for k, h in heights.items() if h >= HZ}
    comps = flood(q, lambda a, b: q[a] == q[b])
    comps.sort(key=lambda c: (-len(c), c[0]))
    relay_comp = [len(c) >= MINR for c in comps]
    comp_of = {k: i for i, c in enumerate(comps) for k in c}
    changed = True
    while changed:
        changed = False
        for i, c in enumerate(comps):
            if relay_comp[i]:
                continue
            if any((k[0], k[1] + dx, k[2] + dy) in comp_of and relay_comp[comp_of[(k[0], k[1] + dx, k[2] + dy)]]
                   for k in c for dx, dy in N8):
                relay_comp[i] = True
                changed = True
    relayered = {k for i, c in enumerate(comps) if relay_comp[i] for k in c}
    got_r = sorted((c[0][0], q[c[0]], len(c)) for i, c in enumerate(comps) if relay_comp[i])
    got_k = sorted((c[0][0], q[c[0]], len(c)) for i, c in enumerate(comps) if not relay_comp[i])
    if got_r != sorted((r["layer_before"], r["q"], r["tiles"]) for r in rl["regions"]):
        errors.append("relayered regions differ from the tile heights")
    if got_k != sorted((r["layer_before"], r["q"], r["tiles"]) for r in rl["kept_regions"]):
        errors.append("kept regions differ from the tile heights")
    # units and shifts
    units = []
    unit_of = {}
    bylayer = collections.defaultdict(list)
    for k in leg:
        if k[0] != 0:
            bylayer[k[0]].append(k)
    for L in sorted(bylayer):
        for c in flood(bylayer[L], lambda a, b: True):
            for k in c:
                unit_of[k] = len(units)
            units.append(c)
    du = {}
    for i, c in enumerate(units):
        offs = {main[k] - k[0] for k in c if k not in relayered}
        if len(offs) > 1:
            errors.append("unit with {} tiles on layer {} does not move as one: shifts {}".format(
                len(c), c[0][0], sorted(offs)))
        du[i] = min(offs) if offs else 0
        if du[i] > 0 and c[0][0] < 1 or du[i] < 0 and c[0][0] >= 0:
            errors.append("unit on layer {} moves {} (storeys move only up, caves only down)".format(c[0][0], du[i]))
    dr = {}
    for k in sorted(relayered):
        d = main[k] - k[0] - q[k] - (du[unit_of[k]] if k in unit_of else 0)
        ci = comp_of[k]
        if dr.setdefault(ci, d) != d:
            errors.append("region tile {} adjustment {} differs from its region's {}".format(k, d, dr[ci]))
        if d > 0:
            errors.append("region tile {} is adjusted up by {}".format(k, d))
    for k in leg:
        if k in relayered:
            continue
        if k[0] == 0 and main[k] != 0:
            errors.append("surface tile {} of {} moved to layer {} without a region".format(k, leg[k][0], main[k]))
    for k, sp in split.items():
        g_layer = k[0] + (du[unit_of[k]] if k in unit_of else 0)
        if k not in relayered or sp["ground_layer"] != g_layer or sp["upper_layer"] != main[k] or \
                sorted(leg[k][1]) != sorted({g_layer, main[k]}):
            errors.append("split tile {} is inconsistent".format(k))
    if sorted((r["layer_before"], r["q"], r["tiles"], r["in_place_adjustment"]) for r in rl["regions"]) != \
            sorted((comps[i][0][0], q[comps[i][0]], len(comps[i]), dr.get(i, 0)) for i in range(len(comps))
                   if relay_comp[i]):
        errors.append("region in-place adjustments differ from the final layers")
    got_u = sorted((units[i][0][0], d, len(units[i])) for i, d in du.items() if d)
    if got_u != sorted((u["layer_before"], u["shift"], u["tiles"]) for u in rl["units_moved"]):
        errors.append("moved units differ from the final layers")
    # column order, caves underground, nothing lands on the surface
    cols = collections.defaultdict(list)
    for k, v in leg.items():
        cols[(k[1], k[2])].append((k[0], min(v[1]), max(v[1])))
    for (x, y), lst in cols.items():
        lst.sort()
        for (la, _mna, mxa), (lb, mnb, _mxb) in zip(lst, lst[1:]):
            if not mxa < mnb:
                errors.append("column ({}, {}): layer {} ends at or above layer {}".format(x, y, la, lb))
    for k, v in leg.items():
        if k[0] < 0 and max(v[1]) >= 0:
            errors.append("cave tile {} of {} reaches layer {}".format(k, v[0], max(v[1])))
        if main[k] != k[0] and main[k] == 0:
            errors.append("moved tile {} of {} ends on the surface layer".format(k, v[0]))
    # maps moved
    mm = collections.defaultdict(collections.Counter)
    moved_maps = set()
    for k, v in leg.items():
        mm[v[0]][main[k]] += 1
        if main[k] != k[0]:
            moved_maps.add(v[0])
    exp_maps = sorted(moved_maps)
    if exp_maps != sorted(m["path"] for m in rl["maps_moved"]):
        errors.append("moved maps differ from the final layers")
    for m in rl["maps_moved"]:
        if {str(L): n for L, n in sorted(mm[m["path"]].items())} != m["final_layers"]:
            errors.append("final layers of {} differ".format(m["path"]))

    def walkable(a, b):
        return a in heights and b in heights and a not in blocked and b not in blocked and \
            abs(heights[a] - heights[b]) < WDZ

    def rest(k):
        return heights[k] - HZ * q[k] if k in relayered else heights[k]

    moved = sorted(k for k in leg if main[k] != k[0])
    moved_set = set(moved)
    cut = set()
    for k in moved:
        for dx, dy in N8:
            n = (k[0], k[1] + dx, k[2] + dy)
            if n in leg and walkable(k, n) and main[k] != main[n]:
                a, b = min(k, n), max(k, n)
                cut.add((a, b) if main[a] < main[b] else (b, a))
    edges = sorted(cut)
    parent = list(range(len(edges)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    lay = [(main[lo], main[hi]) for lo, hi in edges]
    for side in (0, 1):
        at = collections.defaultdict(list)
        for i, e in enumerate(edges):
            at[(lay[i], e[side][1], e[side][2])].append(i)
        for i, e in enumerate(edges):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in at.get((lay[i], e[side][1] + dx, e[side][2] + dy), ()):
                        a, b = find(i), find(j)
                        if a != b:
                            parent[max(a, b)] = min(a, b)
    seams = collections.defaultdict(list)
    for i in range(len(edges)):
        seams[find(i)].append(i)
    cell = rl.get("seam_cell", 24)
    pieces = {}
    for idx in seams.values():
        sid = min(edges[i] for i in idx)
        for i in idx:
            lo = edges[i][0]
            pieces.setdefault((sid, lo[1] // cell, lo[2] // cell), set()).add(edges[i])
    if len(seams) != len(rl["seams"]) or sum(s["cut_edges"] for s in rl["seams"]) != len(edges):
        errors.append("seams differ: {} seams with {} cut edges recomputed, {} with {} recorded".format(
            len(seams), len(edges), len(rl["seams"]), sum(s["cut_edges"] for s in rl["seams"])))
    piece_by_edge = {}
    for key, es in pieces.items():
        for e in es:
            piece_by_edge[e] = key
    seen_pieces = set()
    ns_by_id = {n["id"]: n for n in rl["new_stairs"]}
    for s_ in rl["seams"]:
        for pc in s_["pieces"]:
            e = (tuple(pc["low_tile"]), tuple(pc["high_tile"]))
            key = piece_by_edge.get(e)
            if key is None:
                errors.append("seam piece edge {} of {} is not a cut walkable edge".format(e, s_["id"]))
                continue
            if key in seen_pieces or len(pieces[key]) != pc["edges"] or [key[1], key[2]] != pc["cell"]:
                errors.append("seam piece {} of {} does not match the recomputed piece".format(pc["cell"], s_["id"]))
            seen_pieces.add(key)
            if pc["continuity"] == "new stairs" and pc["stairs"] and pc["stairs"][0] in ns_by_id:
                first = ns_by_id[pc["stairs"][0]]
                if "exit" not in pc and (first["low_tile_legacy"], first["high_tile_legacy"]) != \
                        (pc["low_tile"], pc["high_tile"]):
                    errors.append("seam piece {} of {} does not put its first stairs on its representative edge".format(
                        pc["cell"], s_["id"]))
            elif pc["continuity"] != "existing exit" or tuple(pc.get("exit", ())) not in records:
                errors.append("seam piece {} of {} has no continuity".format(pc["cell"], s_["id"]))
            for sid in pc["stairs"]:
                n = ns_by_id.get(sid)
                objs = {o.get("tile"): o for o in (n or {}).get("objects", [])}
                lo_o, hi_o = objs.get("low", {}), objs.get("high", {})
                if n is None or len(n["objects"]) != 2 or not (
                        lo_o.get("exit") is True and lo_o.get("exit_to") == "high"
                        and re.match(r"^(stairs|ladder)_.*up\d*$", lo_o.get("archetype", ""))
                        and hi_o.get("exit") is True and hi_o.get("exit_to") == "low"
                        and re.match(r"^(stairs|ladder)_.*down\d*$", hi_o.get("archetype", ""))):
                    errors.append("new stairs {} are not an up stair on the low tile and a down stair on the high "
                                  "tile targeting each other".format(sid))
                    continue
                lo, hi = (tuple(n["low_tile_legacy"]), tuple(n["high_tile_legacy"])) if n else (None, None)
                if n is None or piece_by_edge.get((lo, hi)) != key or n["low"] != [lo[1], lo[2], main[lo]] \
                        or n["high"] != [hi[1], hi[2], main[hi]] or n["high"][2] - n["low"][2] != 1 \
                        or cheb(n["low"], n["high"]) > tol:
                    errors.append("new stairs {} of seam piece {} of {} are not plain stairs across one of its "
                                  "cut edges".format(sid, pc["cell"], s_["id"]))
    if len(seen_pieces) != len(pieces):
        errors.append("{} seam pieces recomputed, {} recorded".format(len(pieces), len(seen_pieces)))
    if sorted(n["id"] for n in rl["new_stairs"]) != sorted(i for s_ in rl["seams"] for pc in s_["pieces"]
                                                           for i in pc["stairs"]):
        errors.append("new stairs and seam pieces do not list the same stairs")
    # continuity: the two tiles of every cut edge stay connected on the final layers
    # (walking, legacy overworld exits, new stairs) within the maps with tile heights
    hmaps = set(rl["tile_heights"])
    parent = {}

    def uf_find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def uf_union(a, b):
        ra, rb = uf_find(a), uf_find(b)
        if ra != rb:
            parent[rb] = ra

    gpos = collections.defaultdict(list)
    for k, v in leg.items():
        if v[0] in hmaps and k in heights and k not in blocked:
            gpos[(main[k], k[1], k[2])].append((k, rest(k)))
            if k in split:
                gpos[(split[k]["ground_layer"], k[1], k[2])].append((("ground",) + k, split[k]["floors"]["0"]))
    for (L, x, y), here in gpos.items():
        for dx, dy in ((1, 0), (-1, 1), (0, 1), (1, 1)):
            for a, ra in here:
                for b, rb in gpos.get((L, x + dx, y + dy), ()):
                    if abs(ra - rb) < WDZ:
                        uf_union(a, b)
    for r in ow.get("internal_exits", []):
        X, Y, Z = world_cell(r["source_map"])
        X2, Y2, Z2 = world_cell(r["target_map"])
        uf_union((Z, 24 * X + r["source_tile"][0], 24 * Y + r["source_tile"][1]),
                 (Z2, 24 * X2 + r["target_tile"][0], 24 * Y2 + r["target_tile"][1]))
    for n in rl["new_stairs"]:
        uf_union(tuple(n["low_tile_legacy"]), tuple(n["high_tile_legacy"]))
    apart = [e for e in edges if uf_find(e[0]) != uf_find(e[1])]
    if apart:
        errors.append("{} cut walkable edges are left without continuity, e.g. {}".format(len(apart), apart[0]))
    steep = set()
    for k in sorted(set(moved) | relayered):
        for dx, dy in N8:
            n = (k[0], k[1] + dx, k[2] + dy)
            if n in leg and walkable(k, n) and main[k] == main[n] and abs(rest(k) - rest(n)) >= WDZ:
                steep.add((min(k, n), max(k, n)))
    rec_steep = {tuple(sorted(((s_["layer"], *s_["tiles"][0]), (s_["layer"], *s_["tiles"][1])))) for s_ in rl["steep_steps"]}
    if steep != rec_steep:
        errors.append("steep steps differ: {} recomputed, {} recorded".format(len(steep), len(rec_steep)))
    finals = collections.defaultdict(list)
    for k, v in leg.items():
        for L in v[1]:
            finals[(L, k[1], k[2])].append(k)
    contacts = set()
    for k in moved:
        for dx, dy in N8:
            for t in finals.get((main[k], k[1] + dx, k[2] + dy), ()):
                if t[0] != k[0]:
                    contacts.add((min(k, t), max(k, t)))
    if contacts != {(tuple(c["tiles"][0]), tuple(c["tiles"][1])) for c in rl["new_contacts"]}:
        errors.append("new contacts differ: {} recomputed, {} recorded".format(len(contacts), len(rl["new_contacts"])))
    for c in rl["new_contacts"]:
        a, b = tuple(c["tiles"][0]), tuple(c["tiles"][1])
        walk = a in heights and b in heights and a not in blocked and b not in blocked and \
            abs(rest(a) - rest(b)) < WDZ
        if c["walkable"] != walk or c["final_layer"] != main.get(a if a in moved_set else b):
            errors.append("new contact {} / {} records walkable {} on layer {}; recomputed {}".format(
                a, b, c["walkable"], c["final_layer"], walk))
    # every exit with an overworld end on a moved tile must be listed, and nothing else
    def moved_end(g, mp, t):
        if g != ow_id:
            return False
        X, Y, Z = world_cell(mp)
        k = (Z, 24 * X + t[0], 24 * Y + t[1])
        return k in main and main[k] != Z

    expected = {key for key, r in records.items() if "plane_source" in r and "plane_target" in r and (
        moved_end(r["source_group"], r["source_map"], r["source_tile"]) or
        moved_end(r["target_group"], r["target_map"], r["target_tile"]))}
    listed = {(e["source_map"], e["line"]) for e in rl["exits"]}
    if expected != listed:
        errors.append("relayering exits differ: {} expected but not listed, {} listed but not expected".format(
            len(expected - listed), len(listed - expected)))
    for e in rl["exits"]:
        r = records.get((e["source_map"], e["line"]))
        if r is None:
            errors.append("relayering exit {}:{} has no exit record".format(e["source_map"], e["line"]))
            continue
        ls = [world_cell(r["source_map"])[2] if r["source_group"] == ow_id else r["plane_source"][2],
              world_cell(r["target_map"])[2] if r["target_group"] == ow_id else r["plane_target"][2]]
        if e["layer_delta_before"] != ls[1] - ls[0] or e["layer_delta_after"] != r["layer_delta"] or \
                e["becomes"] != r["becomes"]:
            errors.append("relayering exit {}:{} before/after does not match".format(e["source_map"], e["line"]))
    for g in placed:
        if any(r["relayered"] for r in rl["group_regions"].get(g["id"], [])):
            errors.append("placed group {} has a relayered region, but its maps are recorded on one layer "
                          "each".format(g["id"]))
    if rl["unresolved"] or rl["overlaps"]:
        errors.append("relayering left unresolved conflicts or overlaps")
    return {"ok": not errors, "errors": errors, "member_maps": len(origin), "owner_pairs_checked": pairs,
            "closest_pairs": closest,
            "stairwells_checked": len(plan["stairwells"]), "stairwells_by_kind": dict(sorted(kinds.items())),
            "stairwell_exits_required": required,
            "stairwell_intermediate_tiles": intermediate, "exit_endpoints_checked": exits_checked,
            "relayering": {"overworld_tiles": len(leg), "tile_heights": len(heights), "regions": len(got_r),
                           "kept_regions": len(got_k), "units": len(units), "moved_units": len(got_u),
                           "moved_tiles": len(moved), "cut_edges": len(edges), "seam_pieces": len(pieces),
                           "steep_steps": len(steep), "new_contacts": len(contacts)}}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    path = Path(argv[0]) if argv else Path(__file__).resolve().parents[1] / "placement-plan.json"
    res = verify(json.loads(path.read_text(encoding="utf-8")))
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
