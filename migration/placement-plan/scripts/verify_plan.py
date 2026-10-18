#!/usr/bin/env python3
"""Independent check of placement-plan.json (uses only the JSON).

    python3 -I scripts/verify_plan.py [placement-plan.json]

Groups:
  * overworld maps are 24 x 24 (settings.overworld_map_size, every rows grid)
  * one overworld group; group ids and member maps (overworld cells
    included) are unique; every group's classification and reason are known
    values; the summary's group, map and classification counts are re-derived
    (the overworld's map and per-layer counts from its cells)
Placement (every placed group, proposed and tentative):
  * every place group has a proposed placement and every uncertain group a
    tentative one (an unreachable group may have one from an alignment hint);
    a failed placement fails the plan; the summary's placement order, unplaced
    groups and failed placements are re-derived
  * offsets = ideal + shift_from_ideal and extra_layers_from_ideal in the
    orientation's direction; the ideal follows from the main connection (its
    far end, one layer away in the exit's direction, within the recorded
    vertical bounds), which is one of the group's exits to the overworld or
    an earlier placed group; only an alignment-hinted tentative placement
    has none (a proposed placement carries no hint), and its hint is one of
    settings.tentative_alignments: its ideal is the anchor's offsets, the
    configured layers away; every configured alignment is applied (its
    group's tentative placement carries it)
  * no placement goes further from its ideal spot than max_extra_layers and
    max_shift_tiles allow
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
    one-way stairwell's far end is a landing); every replaces entry equals its
    exit record (tiles, target, archetype, plane endpoints, direction)
  * a column stairwell leaves its column by at most stairwell_max_bend_tiles
    (the same limit the blocked-site chain search uses)
  * kind, shape (from its ends), id series, max_bend, owners (group, owner,
    other, connects: from the exits it replaces) and the intermediate tiles'
    decoration (the exits' stair family) are re-derived; so are the summary's
    stairwell and blocked-site counts
Exits:
  * exit_inventory lists every exit object of the content once, with values
    of the right types, a known form and direction, a target exactly when its
    form resolves one, the data-problem facts its form or problem needs,
    each resolved target on a tile of its target map; the exit
    records are exactly the copies it calls for (one per group a crossing exit
    touches, the overworld's own exits in internal_exits; a wired exit as
    data_problems lists it); every content field of a record (tiles, target,
    archetype, form, direction, flags) equals its inventory entry, and a record
    holds no field the verifier does not know; the exit data problems (every
    entry, field by field) and the summary and per-group crossing counts are
    re-derived
  * every other copy of an exit (blocked sites, relayering exits, wired and
    kept dangling exits, data problems, main connections) equals its record
    or inventory entry; a blocked site lists at least one exit, only exits
    that need a stairwell, between its own two owners (a relayering site:
    overworld exits between its top and bottom maps), and has the shape of
    its ends; a kept decoration sits at its tile on the plane;
    every placement has a main connection to something placed before it,
    except one forced by an alignment hint
  * the records of one exit (one per group it touches) are identical; an
    exit labelled stairwell is listed in exactly that stairwell's replaces
  * an exit record has its placement fields (plane ends, distances, label)
    exactly when both ends are placed (or overworld); the plane ends and
    overworld_plane_source / _target equal origin + tile on the tile's final
    layer, xy_distance and layer_delta follow from them, the "becomes" label
    matches the geometry, and a portal's reason, its text and slope_reachable
    are re-derived from the blocked sites, the layers and the later placed end;
    wired exits are plain stairs and their verdict follows from their evidence;
    exits the owner keeps as decoration have no exit record
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
Not re-derived: the generator's explanations and evidence, which the
migration does not act on. That is classification evidence, reach depth and
script references; the placement rationale beyond the consistency above
(orientation and its evidence, the vertical bounds and constraint note,
conflicts_resolved, main_stairwell_blocked, bounding boxes, formulas); the
evidence of blocked sites (column_occupied, layers, reason), of wiring and of
kept decorations (would_land_on) other than the verdict and target; the
relayering's descriptions (why, resolution, judgement calls, cave options, the
maps, boxes and z ranges listed per region and unit) beyond the re-derived
regions, units and final layers; placement_changes_from_v1,
view_radius_sensitivity and conventions; and the tile-link, layout and
connectivity data problems.
This check uses only the JSON, so it cannot see an edit that keeps the JSON
consistent with itself (an exit dropped from exit_inventory together with
summary.exit_objects, say). `build_plan.py --check` covers that: it
regenerates from the content and fails unless every artifact is
byte-identical.
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
OW_SIZE = 24  # overworld maps are 24 x 24; plane tile = 24 * filename coordinate + tile
N8 = [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dy) != (0, 0)]
# Fields of an exit record that come from the content: equal to its inventory entry.
SOURCE_FIELDS = ("source_map", "source_tile", "target_map", "target_tile", "arch", "line", "form", "vertical",
                 "authored_target", "target_is_map_default_entry", "per_player_last_eat_1", "event_scripts",
                 "vertical_inferred_from")
# Fields a record has exactly when both of its ends are placed, and those that depend on its label.
PLACED_FIELDS = ("plane_source", "plane_target", "xy_distance", "layer_delta", "becomes")
LABEL_FIELDS = ("stairwell", "portal_reason", "portal_reason_text", "slope_reachable")
RECORD_FIELDS = set(SOURCE_FIELDS) | set(PLACED_FIELDS) | set(LABEL_FIELDS) | {
    "source_group", "target_group", "wired", "overworld_plane_source", "overworld_plane_target"}
# Stair families: a stairwell's intermediate tiles show the down archetype of the first family
# (in this order) that one of its exit archetypes belongs to (build_plan.STAIR_FAMILIES).
STAIR_FAMILIES = (("stairs_spiral_stone_", "stairs_spiral_stone_down"), ("stairs_stone_", "stairs_stone_down"),
                  ("stairs_wood_", "stairs_wood_down"), ("stairs_red_", "stairs_red_down"),
                  ("stairs_", "stairs_down"), ("ladder_", "ladder_down"))
SW_PREFIX = {"placement": "SW", "sloped": "SL", "relayering": "SR"}
# The reader's exit forms: those that resolve to a target, and those that never do.
TARGET_FORMS = {"explicit", "tiled", "same-map", "automatic-link", "shop-mat"}
NO_TARGET_FORMS = {"tiled-unresolved", "unresolved", "dynamic"}
# Tile-link slot names of the map format (the reader's SLOT_NAMES), used in data-problem details.
SLOT_NAMES = {1: "N", 2: "E", 3: "S", 4: "W", 5: "NE", 6: "SE", 7: "SW", 8: "NW", 9: "up", 10: "down"}


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
    bend = st["stairwell_max_bend_tiles"]
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
    if st.get("overworld_map_size") != OW_SIZE:
        errors.append("settings.overworld_map_size {} is not the {}-tile grid of the overworld coordinates".format(
            st.get("overworld_map_size"), OW_SIZE))
    for path, by_layer in sorted(ow_rows.items()):
        X, Y, Z = world_cell(path)
        for Ls, rows in sorted(by_layer.items(), key=lambda kv: int(kv[0])):
            if len(rows) != OW_SIZE or any(len(r) != OW_SIZE for r in rows):
                errors.append("rows of {} on layer {} are not {} x {}".format(path, Ls, OW_SIZE, OW_SIZE))
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
    placed_ids = {g["id"] for g in placed}
    summ = plan["summary"]

    # ----------------------------------------------------- placement status
    # A failed placement must not be published; every place group is placed
    # (proposed) and every uncertain group tentatively. The summary's order and
    # unplaced lists are re-derived from the placement records.
    others = [g for g in plan["groups"] if g["classification"] != "overworld"]
    reasons = {"overworld": {"overworld"}, "place": {"place"}, "uncertain": {"uncertain"},
               "separate": {"guild", "other_world", "per_player", "ship", "system", "unreachable"}}
    for g in plan["groups"]:
        if g.get("reason") not in reasons.get(g["classification"], ()):
            errors.append("group {} has classification {} with reason {}".format(
                g["id"], g["classification"], g.get("reason")))
    if sum(1 for g in plan["groups"] if g["classification"] == "overworld") != 1:
        errors.append("the plan has no single overworld group")
    # identities are unique before anything is counted
    for what, items in (
            ("group id", [g["id"] for g in plan["groups"]]),
            ("member map", [m["path"] for g in others for m in g["maps"]] + [
                "{}_{}_{}{}".format(ow_id, X, Y, "" if int(z) == 0 else "_{}".format(z))
                for z, cells in ow["cells_by_layer"].items() for X, Y in cells])):
        dup = sorted(k for k, n in collections.Counter(items).items() if n > 1)
        if dup:
            errors.append("{} {} {} listed {} times".format(what, dup[0], "and {} more are".format(len(dup) - 1)
                                                             if len(dup) > 1 else "is", "several"))
    if summ.get("groups_total") != len(plan["groups"]) or summ.get("non_overworld_groups") != len(others) or \
            summ.get("maps_total") != sum(g["map_count"] for g in plan["groups"]) or \
            any(g["map_count"] != len(g["maps"]) for g in others) or \
            ow["map_count"] != sum(len(c) for c in ow["cells_by_layer"].values()) or \
            ow.get("layers") != {z: len(c) for z, c in sorted(ow["cells_by_layer"].items(), key=lambda kv: int(kv[0]))} or \
            summ.get("by_classification") != dict(sorted(collections.Counter(
                "{}:{}".format(g["classification"], g["reason"]) for g in others).items())):
        errors.append("summary group and map counts differ from the groups")
    status = {g["id"]: (g.get("placement") or {}).get("status") for g in others}
    for g in others:
        gid, s = g["id"], status[g["id"]]
        if g.get("placement") is not None and s is None:
            errors.append("placement of {} has no status".format(gid))
        if s == "failed":
            errors.append("placement of {} failed; a plan with a failed group must not be published".format(gid))
        elif s not in (None, "proposed", "tentative"):
            errors.append("placement of {} has an unknown status {}".format(gid, s))
        want = {"place": "proposed", "uncertain": "tentative"}.get(g["classification"])
        if want is not None and s != want:
            errors.append("{} group {} has no {} placement".format(g["classification"], gid, want))
        if s == "proposed" and g["classification"] != "place":
            errors.append("{} group {} has a proposed placement".format(g["classification"], gid))
        # tentative: an uncertain group, or an unreachable group placed only by an alignment hint
        if s == "tentative" and g["classification"] != "uncertain" and not (
                g["classification"] == "separate" and g["reason"] == "unreachable"
                and g["placement"].get("tentative_reason")):
            errors.append("{} group {} ({}) has a tentative placement without an alignment hint".format(
                g["classification"], gid, g["reason"]))
    index = {g["id"]: g["placement"].get("placement_index") for g in placed}
    order = summ["placement_order"] + summ["tentative_placement_order"]
    if sorted(order) != sorted(index) or [index[gid] for gid in order] != list(range(len(order))) or \
            sorted(summ["placement_order"]) != sorted(gid for gid, s in status.items() if s == "proposed"):
        errors.append("placement order in the summary differs from the placed groups")
    for key, cls, s_ok in (("unplaced_place_groups", "place", "proposed"),
                           ("unplaced_uncertain_groups", "uncertain", "tentative")):
        want = sorted(g["id"] for g in others if g["classification"] == cls and status[g["id"]] != s_ok)
        if summ.get(key) != want:
            errors.append("summary {} {} != {}".format(key, summ.get(key), want))
    if summ.get("failed_placements") != sorted(gid for gid, s in status.items() if s == "failed"):
        errors.append("summary failed_placements differs from the failed placement records")

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
    copies = collections.Counter()  # (group, list, exit key) -> copies
    for g in plan["groups"]:
        for key in ("entrances_from_overworld", "exits_to_overworld", "entrances_from_other_groups",
                    "exits_to_other_groups", "internal_exits"):
            for e in g.get(key, []):
                k = (e["source_map"], e["line"])
                copies[(g["id"], key, k)] += 1
                if k in records and records[k] != e:
                    errors.append("exit {}:{} has two different records".format(*k))
                records[k] = e

    def same_as(what, copy, ref, fields, ref_name="exit record"):
        """Every listed field of a copy of an exit equals the field of its reference."""
        bad = [f for f in fields if copy.get(f) != ref.get(f)]
        if bad:
            errors.append("{} of exit {}:{} differs from its {} in {}".format(
                what, ref["source_map"], ref["line"], ref_name, ", ".join(bad)))

    def owner_of_end(e, side):
        """The owner of one end of an exit: the overworld map itself, else the group."""
        return e[side + "_map"] if e[side + "_group"] == ow_id else e[side + "_group"]

    # The exit inventory is the expected set: every exit object read from the
    # content. Each one with a destination has a record in every group it touches
    # (the overworld's own exits in its internal_exits, none for an exit inside
    # one other group), and nothing else has a record.
    group_of = {}
    for g in others:
        for m in g["maps"]:
            if group_of.setdefault(m["path"], g["id"]) != g["id"]:
                errors.append("map {} is a member of two groups".format(m["path"]))
    for z, cells in ow["cells_by_layer"].items():
        for X, Y in cells:
            group_of.setdefault("{}{}_{}{}".format(prefix, X, Y, "" if int(z) == 0 else "_{}".format(z)), ow_id)
    inv = {}
    for x in plan["exit_inventory"]:
        k = (x["source_map"], x["line"])
        if k in inv:
            errors.append("exit {}:{} is listed twice in the exit inventory".format(*k))
        inv[k] = x
        # value types: map paths and archetypes are text, lines and tiles integers, flags true when present
        def tile_ok(t_):
            return isinstance(t_, list) and len(t_) == 2 and all(type(c) is int for c in t_)

        if not (isinstance(x["source_map"], str) and type(x["line"]) is int and tile_ok(x.get("source_tile"))
                and isinstance(x.get("arch"), str) and x["arch"]
                and ("target_map" not in x or isinstance(x["target_map"], str) and tile_ok(x.get("target_tile")))
                and all(x.get(f, True) is True for f in ("target_is_map_default_entry", "per_player_last_eat_1",
                                                         "explicit_last_heal_0"))
                and ("event_scripts" not in x or isinstance(x["event_scripts"], list) and x["event_scripts"]
                     and all(isinstance(v, str) for v in x["event_scripts"]))
                and all(f not in x or isinstance(x[f], str) and x[f] for f in ("vertical_inferred_from", "problem",
                                                                                "dynamic_target"))
                # the facts behind data problems are there exactly for their form or problem
                and ("dynamic_target" in x) == (x.get("form") == "dynamic")
                and ("problem_target" in x) == ("problem" in x)
                and ("problem_target" not in x or isinstance(x["problem_target"], list)
                     and len(x["problem_target"]) == 3)):
            errors.append("exit {!r}:{!r} has a value of the wrong type".format(*k))
        # the tile-link slot of a tiled exit without a link: 1..10, and only on that form; wired ones go up or down
        if ("tiled_slot" in x) != (x.get("form") == "tiled-unresolved") or "tiled_slot" in x and \
                x["tiled_slot"] not in SLOT_NAMES:
            errors.append("exit {}:{} has form {} and tile-link slot {}".format(k[0], k[1], x.get("form"),
                                                                              x.get("tiled_slot")))
        # a known form and direction; a target exactly when the form resolves one and the map exists
        if x.get("form") not in TARGET_FORMS | NO_TARGET_FORMS or x.get("vertical") not in ("up", "down", "none") or \
                ("target_map" in x) != ("target_tile" in x) or \
                "target_map" in x and x["form"] not in TARGET_FORMS or \
                "target_map" not in x and x["form"] in TARGET_FORMS and not str(x.get("problem", "")).startswith(
                    "missing-target-map"):
            errors.append("exit {}:{} has form {}, direction {} and {} target".format(
                k[0], k[1], x.get("form"), x.get("vertical"), "a" if "target_map" in x else "no"))
    if len(plan["exit_inventory"]) != summ["exit_objects"]:
        errors.append("the exit inventory lists {} exit objects, the summary {}".format(
            len(plan["exit_inventory"]), summ["exit_objects"]))
    wired_dp = {}
    for d in plan["data_problems"]["dangling_exits_wired"]:
        k = (d["source_map"], d["line"])
        if k in wired_dp:
            errors.append("wired exit {}:{} is listed twice".format(*k))
        wired_dp[k] = d
    if len(plan["data_problems"]["dangling_exits_wired"]) != summ["wired_exits"]:
        errors.append("{} wired exits listed, the summary says {}".format(
            len(plan["data_problems"]["dangling_exits_wired"]), summ["wired_exits"]))
    size = {m["path"]: (m["width"], m["height"]) for g in others for m in g["maps"]}
    expected_copies = set()
    want_rec = {}  # exit key -> the record fields the inventory (and wiring) fix
    for k, x in sorted(inv.items()):
        want = {f: x.get(f) for f in SOURCE_FIELDS}
        if k in wired_dp:
            if x["form"] != "tiled-unresolved" or "target_map" in x or x.get("tiled_slot") not in (9, 10):
                errors.append("wired exit {}:{} is not a legacy exit that leads nowhere".format(*k))
            want.update(form="wired", target_map=wired_dp[k].get("target_map"),
                        target_tile=wired_dp[k].get("target_tile"))
        sg = group_of.get(x["source_map"])
        if sg is None:
            errors.append("exit {}:{} is on a map that is in no group".format(*k))
            continue
        if want["target_map"] is None:
            continue
        tg = group_of.get(want["target_map"])
        if tg is None:
            errors.append("exit {}:{} leads to {}, which is in no group".format(k[0], k[1], want["target_map"]))
            continue
        # a target (resolved by the reader, or wired) is a tile of its map
        tw, th = size.get(want["target_map"], (OW_SIZE,) * 2)
        tt = want.get("target_tile")
        if not (isinstance(tt, list) and len(tt) == 2 and all(isinstance(c, int) for c in tt)
                and 0 <= tt[0] < tw and 0 <= tt[1] < th):
            errors.append("exit {}:{} targets tile {} outside {} ({} x {})".format(
                k[0], k[1], tt, want["target_map"], tw, th))
        want.update(source_group=sg, target_group=tg)
        want_rec[k] = want
        if sg == tg:
            if sg == ow_id:
                expected_copies.add((ow_id, "internal_exits", k))
            continue
        if sg != ow_id:
            expected_copies.add((sg, "exits_to_overworld" if tg == ow_id else "exits_to_other_groups", k))
        if tg != ow_id:
            expected_copies.add((tg, "entrances_from_overworld" if sg == ow_id else "entrances_from_other_groups", k))
    missing = sorted(expected_copies - set(copies))
    extra = sorted(c for c, n in copies.items() if c not in expected_copies or n > 1)
    if missing:
        errors.append("{} exit record(s) missing, e.g. exit {}:{} from {} of {}".format(
            len(missing), missing[0][2][0], missing[0][2][1], missing[0][1], missing[0][0]))
    if extra:
        errors.append("{} exit record(s) the exit inventory does not call for, e.g. exit {}:{} in {} of {}".format(
            len(extra), extra[0][2][0], extra[0][2][1], extra[0][1], extra[0][0]))
    for k, e in sorted(records.items()):
        unknown = sorted(set(e) - RECORD_FIELDS)
        if unknown:
            errors.append("exit {}:{} has fields the verifier does not know: {}".format(k[0], k[1], ", ".join(unknown)))
        if k in want_rec:
            same_as("record", e, want_rec[k], SOURCE_FIELDS + ("source_group", "target_group"), "inventory entry")
            if (e.get("form") == "wired") != ("wired" in e):
                errors.append("exit {}:{} has form {} but {} wiring evidence".format(
                    k[0], k[1], e.get("form"), "carries" if "wired" in e else "no"))
    for k, d in sorted(wired_dp.items()):
        e = records.get(k)
        if e is None:
            errors.append("wired exit {}:{} has no exit record".format(*k))
            continue
        same_as("wired exit", d, e, ("source_tile", "arch", "target_map", "target_tile"))
        if d.get("verdict") != (e.get("wired") or {}).get("verdict"):
            errors.append("wired exit {}:{} verdict differs from its record".format(*k))
    # data problems and kept decoration describe exits without a record: compare with the inventory
    for what, lst in (("data problem", plan["data_problems"]["exits"]),
                      ("kept decoration", plan.get("exits_kept_as_decoration", [])),
                      ("kept decoration problem", plan["data_problems"].get("dangling_exits_kept_as_decoration", []))):
        for d in lst:
            x = inv.get((d["source_map"], d["line"]))
            if x is None:
                errors.append("{} names exit {}:{}, which is not in the exit inventory".format(
                    what, d["source_map"], d["line"]))
                continue
            same_as(what, d, x, ("source_tile", "arch"), "inventory entry")
            if what != "data problem" and x["form"] != "tiled-unresolved":
                errors.append("{} exit {}:{} is not a legacy exit that leads nowhere".format(
                    what, d["source_map"], d["line"]))
    # the exit data problems, re-derived entry by entry from the inventory
    working = {(x["source_map"], tuple(x["source_tile"])) for x in inv.values() if "target_map" in x}
    want_dp = []
    for k, x in sorted(inv.items()):
        form, tile = x["form"], tuple(x["source_tile"])
        base = {f: x[f] for f in ("source_map", "source_tile", "arch", "line")}
        if form in ("tiled-unresolved", "unresolved") and not x.get("event_scripts"):
            if (x["source_map"], tile) in working:
                base.update(severity="info", note="another exit object on the same tile has a destination "
                                                  "(decorative duplicate)")
            elif x.get("explicit_last_heal_0"):
                base.update(severity="info", note="explicitly disabled with last_heal 0 (decorative)")
            else:
                base["severity"] = "problem"
        w, h = size.get(x["source_map"], (OW_SIZE,) * 2)
        if not (0 <= tile[0] < w and 0 <= tile[1] < h):
            want_dp.append(dict(base, kind="exit-outside-map-bounds"))
        if form == "tiled-unresolved":
            want_dp.append(dict(base, kind="tiled-exit-without-link", detail="last_heal {} ({}) but the map has no "
                                "such tile link; the exit leads nowhere".format(x.get("tiled_slot"),
                                                                                 SLOT_NAMES.get(x.get("tiled_slot")))))
        elif form == "unresolved":
            if not x.get("event_scripts"):
                want_dp.append(dict(base, kind="exit-without-destination",
                                    detail="no slaying, no tiled slot, hp/sp -1 and no event script"))
        elif x.get("problem"):
            want_dp.append(dict(base, kind=x["problem"], target=x.get("problem_target")))
        elif form == "dynamic":
            want_dp.append(dict(base, kind="dynamic-random-map-exit", detail=x.get("dynamic_target"), severity="info"))
    want_dp = collections.Counter(json.dumps(d, sort_keys=True) for d in want_dp)
    got_dp = collections.Counter(json.dumps(d, sort_keys=True) for d in plan["data_problems"]["exits"])
    if got_dp != want_dp:
        diff = sorted((want_dp - got_dp) + (got_dp - want_dp))
        errors.append("{} exit data problem(s) differ from the exit inventory, e.g. {}".format(len(diff), diff[0]))

    # ----------------------------------------------------------- stairwells
    chain = collections.defaultdict(dict)  # layer -> owner -> Rows
    sw_by_id = {}
    for sw in plan["stairwells"]:
        if sw["id"] in sw_by_id:
            errors.append("stairwell id {} is used twice".format(sw["id"]))
        sw_by_id[sw["id"]] = sw
        for x in sw["tiles"][1:-1]:
            chain[x["layer"]].setdefault(sw["owner"], Rows()).add(x["x"], x["y"])
    for layer in chain.values():
        for rr in layer.values():
            rr.done()
    n_kind = collections.Counter()
    for sw in plan["stairwells"]:
        n_kind[sw["kind"]] += 1
        if sw["id"] != "{}{}".format(SW_PREFIX.get(sw["kind"], "?"), n_kind[sw["kind"]]):
            errors.append("stairwell {} of kind {} is out of its series".format(sw["id"], sw["kind"]))
    if summ.get("stairwells") != len(plan["stairwells"]) or summ.get("stairwells_by_kind") != dict(sorted(
            n_kind.items())) or summ.get("stairwell_spans_layers") != {str(k): v for k, v in sorted(collections.Counter(
                sw["top_layer"] - sw["bottom_layer"] for sw in plan["stairwells"]).items())} or \
            summ.get("stairwell_sites_blocked") != len(plan["stairwell_sites_blocked"]) or \
            summ.get("sloped_stairwell_sites_blocked") != len(plan["sloped_stairwell_sites_blocked"]) or \
            summ.get("relayering_stairwell_sites_blocked") != len(rl["stairwells_blocked"]):
        errors.append("summary stairwell and blocked-site counts differ from the lists")
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
        if sw["shape"] != ("column" if off <= tol else "sloped") or off > span * tol:
            errors.append("{} shape {} does not fit ends {} tiles apart over {} layers".format(
                sw["id"], sw["shape"], off, span))
        if sw["horizontal_offset"] != off:
            errors.append("{} horizontal_offset {} != {}".format(sw["id"], sw["horizontal_offset"], off))
        if sw["kind"] not in SW_PREFIX or sw["kind"] == "placement" and sw["shape"] != "column" or \
                sw["kind"] == "sloped" and sw["shape"] != "sloped":
            errors.append("{} kind {} does not match shape {}".format(sw["id"], sw["kind"], sw["shape"]))
        if sw.get("max_bend") != max([cheb((x_["x"], x_["y"]), top) for x_ in t[1:-1]] or [0]):
            errors.append("{} max_bend {} does not match its tiles".format(sw["id"], sw.get("max_bend")))
        if sw["shape"] == "column" and sw.get("max_bend", 0) > bend:
            errors.append("{} bends {} tile(s) from its column, more than stairwell_max_bend_tiles {}".format(
                sw["id"], sw.get("max_bend"), bend))
        if not sw["replaces"]:
            errors.append("{} replaces no exit".format(sw["id"]))
        # owners, re-derived from the exits it replaces: a placed group's stairwell joins that group
        # (placed after the other end) and the other owner; a relayering one two overworld maps
        pairs_ = set()
        for r in sw["replaces"]:
            e = records.get((r["source_map"], r["line"]))
            if e is not None and "plane_source" in e and "plane_target" in e:
                if sw["kind"] == "relayering":
                    up_src = e["plane_source"][2] > e["plane_target"][2]
                    pairs_.add((e["source_map"], e["target_map"]) if up_src else (e["target_map"], e["source_map"]))
                else:
                    pairs_.add(tuple(sorted([owner_of_end(e, "source"), owner_of_end(e, "target")])))
        if sw["kind"] == "relayering":
            want_o = None if len(pairs_) != 1 else {
                "group": None, "owner": "stairwell@{},{},{}".format(*top), "other": next(iter(pairs_))[1],
                "connects": sorted(set(next(iter(pairs_))))}
        else:
            g_, o_ = sw.get("group"), sw.get("other")
            want_o = None if len(pairs_) != 1 or tuple(sorted([g_, o_])) not in pairs_ or g_ not in placed_ids or \
                o_ in placed_ids and not (index.get(o_, len(index)) < index.get(g_, -1)) else {
                    "group": g_, "owner": g_, "other": o_, "connects": sorted({g_, o_})}
        if want_o is None or any(sw.get(f) != v for f, v in want_o.items()):
            errors.append("{} owners (group {}, owner {}, other {}, connects {}) do not follow from the exits it "
                          "replaces".format(sw["id"], sw.get("group"), sw.get("owner"), sw.get("other"),
                                            sw.get("connects")))
        # intermediate tiles show the down archetype of the exits' stair family; they are not exits
        ends_arch = sorted({r["arch"] for r in sw["replaces"] if r["goes"] == "down"}) + \
            sorted({r["arch"] for r in sw["replaces"] if r["goes"] == "up"})
        fam = next((down for a in ends_arch for prefix, down in STAIR_FAMILIES if a.casefold().startswith(prefix)),
                   None)
        app_src = ("appearance of the down archetype of the {} family used by this stairwell's exits; the "
                   "intermediate objects are decorations, not exits".format(fam.rsplit("_", 1)[0]) if fam else
                   "no stair archetype among {}; owner to choose".format(ends_arch))
        want_objs_mid = [{"archetype": fam, "exit": False, "walkable": False, "use": "appearance only"}] if fam else []
        if sw.get("intermediate_appearance") != fam or sw.get("intermediate_appearance_source") != app_src or \
                any(x_.get("objects") != want_objs_mid or x_.get("existing") is not False for x_ in t[1:-1]):
            errors.append("{} intermediate tiles do not show {} as a decoration".format(sw["id"], fam))
        for r in sw["replaces"]:
            e = records.get((r["source_map"], r["line"]))
            if e is None:
                errors.append("{} replaces an unknown exit {}:{}".format(sw["id"], r["source_map"], r["line"]))
                continue
            if e.get("becomes") != "stairwell" or e.get("stairwell") != sw["id"]:
                errors.append("{} exit {}:{} is not marked as this stairwell".format(sw["id"], r["source_map"], r["line"]))
            # the copy in replaces is what the migration reads: it must be the record
            same_as("{} replaces entry".format(sw["id"]), r, e,
                    ("source_tile", "target_map", "target_tile", "arch", "plane_source", "plane_target"))
            s, d = e.get("plane_source"), e.get("plane_target")
            if s is None or d is None:
                continue  # reported with the exit records
            if r.get("goes") != ("down" if s[2] > d[2] else "up"):
                errors.append("{} exit {}:{} goes {}, but its record goes {}".format(
                    sw["id"], r["source_map"], r["line"], r.get("goes"), "down" if s[2] > d[2] else "up"))
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
        if sw.get("mode") != ("clear" if not sw["view_conflicts"] and not sw["contacts"] else "void-only"):
            errors.append("{} mode {} disagrees with its view conflicts and contacts".format(sw["id"], sw["mode"]))

    # ---------------------------------------------------------------- exits
    replaced_by = collections.defaultdict(list)  # exit key -> stairwells listing it in replaces
    for sw in plan["stairwells"]:
        for r in sw["replaces"]:
            replaced_by[(r["source_map"], r["line"])].append(sw["id"])
    def needs_stairwell(e):
        """Ends 2+ layers and at most one tile per layer apart, unless a legacy overworld shaft kept as it was."""
        s_, d_ = e["plane_source"], e["plane_target"]
        dl, dist = abs(d_[2] - s_[2]), cheb(s_, d_)
        if dl < 2 or dist > dl * tol:
            return False
        if e["source_group"] == ow_id and e["target_group"] == ow_id:
            return abs(world_cell(e["target_map"])[2] - world_cell(e["source_map"])[2]) != dl
        return True

    blocked_keys = {}
    for key_, sites in (("column", plan["stairwell_sites_blocked"]), ("sloped", plan["sloped_stairwell_sites_blocked"]),
                        ("relayering", rl["stairwells_blocked"])):
        for b in sites:
            shape_ = "sloped" if cheb(b["top"], b["bottom"]) > tol else "column"
            if not b["exits"] or b.get("shape") != shape_ or key_ != "relayering" and shape_ != key_:
                errors.append("blocked {} stairwell site at {} lists {} exits with shape {} ({} by its ends)".format(
                    key_, b["top"], len(b["exits"]), b.get("shape"), shape_))
            if key_ != "relayering" and (b.get("group") not in placed_ids or b.get("other") in placed_ids and not (
                    index.get(b["other"], len(index)) < index.get(b["group"], -1))):
                errors.append("blocked {} stairwell site at {} belongs to {}, which is not the later placed "
                              "end".format(key_, b["top"], b.get("group")))
            for x in b["exits"]:
                k = (x["source_map"], x["line"])
                if k in blocked_keys:
                    errors.append("exit {}:{} is listed at two blocked stairwell sites".format(*k))
                r = records.get(k)
                if r is None:
                    errors.append("blocked {} stairwell site lists exit {}:{}, which has no exit record".format(key_, *k))
                else:
                    same_as("blocked {} site entry".format(key_), x, r, ("source_tile", "arch"))
                    if "plane_source" not in r or "plane_target" not in r or not needs_stairwell(r):
                        errors.append("blocked {} stairwell site lists exit {}:{}, which needs no stairwell".format(
                            key_, *k))
                    elif key_ == "relayering" and not r["source_group"] == r["target_group"] == ow_id:
                        errors.append("blocked relayering site lists exit {}:{}, which is not an overworld "
                                      "exit".format(*k))
                    elif key_ == "relayering":
                        up_src = r["plane_source"][2] > r["plane_target"][2]
                        ends_ = [r["source_map"], r["target_map"]] if up_src else [r["target_map"], r["source_map"]]
                        if ends_ != [b.get("top_map"), b.get("bottom_map")]:
                            errors.append("blocked relayering site {} -> {} lists exit {}:{} between {}".format(
                                b.get("top_map"), b.get("bottom_map"), k[0], k[1], ends_))
                    elif sorted([owner_of_end(r, "source"), owner_of_end(r, "target")]) != \
                            sorted([b.get("group"), b.get("other")]):
                        errors.append("blocked {} site of {} / {} lists exit {}:{} of other owners".format(
                            key_, b.get("group"), b.get("other"), *k))
                blocked_keys[k] = (key_, b)

    def portal_reason(e, k, s, dist, dl):
        """The generator's reason an exit stays a portal, its text, and whether a sloped stairwell would reach."""
        if k in blocked_keys and blocked_keys[k][0] == "sloped":
            return "slope-blocked", "{} tiles and {} layers apart, but no void sloped chain exists".format(
                dist, dl), True
        if k in blocked_keys:
            return "stairwell-blocked", "aligned over {} layers, but no void stair chain exists".format(dl), None
        if dl == 0:
            return "same-layer", "both ends on layer {}, {} tiles apart".format(s[2], dist), None
        ends_ = [x for x in (e["source_group"], e["target_group"]) if x != ow_id]
        if not ends_:
            return "overworld", "overworld exit whose ends are {} tiles and {} layer(s) apart".format(dist, dl), None
        later = max(ends_, key=lambda x: index.get(x, -1))
        pl = placed_by_id[later]["placement"]
        mc = pl.get("main_connection") or {}
        slope = True if 2 <= dist <= dl * tol else None
        if pl.get("shift_from_ideal") != [0, 0]:
            return "group-shifted", "{} was shifted {} from its ideal spot".format(later, pl.get("shift_from_ideal")), \
                slope
        if mc.get("source_map") == e["source_map"] and mc.get("source_tile") == e["source_tile"]:
            return "main-entrance-offset", "the main connection itself lands {} tiles away".format(dist), slope
        return "other-entrance-aligned", "{} is aligned to its main connection; this exit's ends are {} tiles and {} " \
            "layer(s) apart".format(later, dist, dl), slope

    placed_by_id = {g["id"]: g for g in placed}
    aligns = {}
    for a in st.get("tentative_alignments", []):
        if a.get("group") in aligns:
            errors.append("tentative alignment of {} is configured twice".format(a.get("group")))
        aligns[a.get("group")] = a
        # a configured alignment names real groups and was applied: its group's tentative placement carries
        # it (checked against the anchor below), so a stale or made-up entry fails
        g_ = placed_by_id.get(a.get("group"))
        if g_ is None or a.get("anchor") not in placed_by_id or not isinstance(a.get("layer_delta"), int) or \
                not isinstance(a.get("why"), str) or not a["why"] or g_["placement"].get("status") != "tentative" or \
                g_["placement"].get("tentative_reason") != a["why"]:
            errors.append("tentative alignment of {} to {} is not the hint of a tentative placement".format(
                a.get("group"), a.get("anchor")))
    exits_checked = 0
    for (src_map, line), e in sorted(records.items()):
        live = all(x == ow_id or x in placed_ids for x in (e["source_group"], e["target_group"]))
        have = [f for f in PLACED_FIELDS + LABEL_FIELDS if f in e]
        lacks = [f for f in PLACED_FIELDS if f not in e]
        if live and lacks or not live and have:
            errors.append("exit {}:{} {} placement fields {}, although {} of its ends are placed".format(
                src_map, line, "lacks" if live else "has", lacks if live else have, "both" if live else "not both"))
            continue
        ends = {}
        for end, ow_end, mkey, tkey, gkey in (
                ("plane_source", "overworld_plane_source", "source_map", "source_tile", "source_group"),
                ("plane_target", "overworld_plane_target", "target_map", "target_tile", "target_group")):
            mp, (tx, ty) = e[mkey], e[tkey]
            if (e[gkey] == ow_id) != (ow_end in e):
                errors.append("exit {}:{} {} {}".format(src_map, line, "lacks" if ow_end not in e else "has", ow_end))
            if e[gkey] == ow_id:
                if world_cell(mp) is None:
                    errors.append("exit {}:{} has an overworld end on {}, which is not an overworld map".format(
                        src_map, line, mp))
                    continue
                X, Y, _Z = world_cell(mp)
                exp = [24 * X + tx, 24 * Y + ty, ow_layer(mp, tx, ty)]
                if ow_end in e and e[ow_end] != exp:
                    errors.append("exit {}:{} {} {} != {}".format(src_map, line, ow_end, e[ow_end], exp))
            elif mp in origin and e[gkey] in placed_ids:
                o = origin[mp]
                exp = [o[0] + tx, o[1] + ty, o[2]]
            else:
                continue
            if not live:
                continue
            exits_checked += 1
            ends[end] = exp
            if exp != e[end]:
                errors.append("exit {}:{} {} {} != {}".format(src_map, line, end, e[end], exp))
        if len(ends) == 2:
            s, d = ends["plane_source"], ends["plane_target"]
            dist, dl = cheb(s, d), abs(d[2] - s[2])
            if e["xy_distance"] != dist or e["layer_delta"] != d[2] - s[2]:
                errors.append("exit {}:{} records {} tiles / {} layers, its ends are {} / {} apart".format(
                    src_map, line, e["xy_distance"], e["layer_delta"], dist, d[2] - s[2]))
            b = e.get("becomes")
            want_reason, want_text, want_slope = portal_reason(e, (src_map, line), s, dist, dl) if b == "portal" \
                else (None, None, None)
            if ("stairwell" in e) != (b == "stairwell") or e.get("portal_reason") != want_reason or \
                    e.get("portal_reason_text") != want_text or e.get("slope_reachable") != want_slope:
                errors.append("exit {}:{} labelled {} records stairwell {}, portal reason {} (slope reachable {}); "
                              "expected portal reason {} (slope reachable {})".format(
                                  src_map, line, b, e.get("stairwell"), e.get("portal_reason"),
                                  e.get("slope_reachable"), want_reason, want_slope))
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

    # exits the owner keeps as decoration are not wired and carry their decision; the
    # list matches the owner decision recorded in settings and its data-problem copy
    decided = {(m, tuple(t), a) for m, t, a in st.get("owner_keep_as_decoration", [])}
    listed_kept = {(d["source_map"], tuple(d["source_tile"]), d["arch"]) for d in plan.get("exits_kept_as_decoration", [])}
    copied = {(d["source_map"], tuple(d["source_tile"]), d["arch"])
              for d in plan["data_problems"].get("dangling_exits_kept_as_decoration", [])}
    dangling = {(x["source_map"], tuple(x["source_tile"]), x["arch"]) for x in plan["data_problems"]["exits"]
                if x["kind"] == "tiled-exit-without-link"}
    if not decided == listed_kept == copied:
        errors.append("exits kept as decoration ({}) differ from the owner decision ({}) or its data-problem copy "
                      "({})".format(len(listed_kept), len(decided), len(copied)))
    if listed_kept - dangling:
        errors.append("an exit kept as decoration is not a legacy dangling exit: {}".format(sorted(listed_kept - dangling)[0]))
    kept = [(d["source_map"], d["line"]) for d in plan.get("exits_kept_as_decoration", [])]
    if len(kept) != len(set(kept)):
        errors.append("an exit is listed twice as kept decoration")
    for d in plan.get("exits_kept_as_decoration", []):
        if (d["source_map"], d["line"]) in records:
            errors.append("exit {}:{} is kept as decoration but has an exit record".format(d["source_map"], d["line"]))
        if not d.get("decision") or "no exit" not in d.get("migrate_as", ""):
            errors.append("exit {}:{} is kept as decoration without its decision".format(d["source_map"], d["line"]))
        # where the kept object ends up: its tile on the plane, like an exit's source end
        mp, (tx, ty) = d["source_map"], d["source_tile"]
        if group_of.get(mp) == ow_id:
            X, Y, _Z = world_cell(mp)
            exp = [24 * X + tx, 24 * Y + ty, ow_layer(mp, tx, ty)]
        else:
            o = origin.get(mp)
            exp = None if o is None else [o[0] + tx, o[1] + ty, o[2]]
        if d.get("plane_source") != exp:
            errors.append("exit {}:{} kept as decoration is at {}, its tile is at {}".format(
                d["source_map"], d["line"], d.get("plane_source"), exp))

    # crossing counts, re-derived from the records (one per exit object)
    def counted(es):
        return dict(sorted(collections.Counter(str(e.get("becomes")) for e in es).items()))

    crossing = [e for e in records.values() if e["source_group"] != e["target_group"]]
    live_crossing = [e for e in crossing if "plane_source" in e and "plane_target" in e]
    if counted(live_crossing) != summ["crossing_exit_objects_after_placement"]:
        errors.append("summary crossing exits {} differ from the exit records {}".format(
            summ["crossing_exit_objects_after_placement"], counted(live_crossing)))
    if len(crossing) - len(live_crossing) != summ["crossing_exit_objects_into_separate_planes"]:
        errors.append("summary crossing exits into separate planes {} differ from the exit records {}".format(
            summ["crossing_exit_objects_into_separate_planes"], len(crossing) - len(live_crossing)))
    reasons = collections.Counter(str(e.get("portal_reason")) for e in live_crossing if e.get("becomes") == "portal")
    if dict(sorted(reasons.items())) != summ["portal_reasons"]:
        errors.append("summary portal reasons differ from the exit records")
    for g in placed:
        mine = counted(e for e in live_crossing if g["id"] in (e["source_group"], e["target_group"]))
        if mine != g["placement"].get("crossing_exits_after_placement"):
            errors.append("crossing exits of {} {} differ from its exit records {}".format(
                g["id"], g["placement"].get("crossing_exits_after_placement"), mine))
        # the offsets are the ideal spot plus the recorded shift and extra layers (deeper when the group
        # is oriented below, higher when above)
        pl = g["placement"]
        ideal, orient = pl.get("ideal") or {}, pl.get("orientation")
        step = {"below": -1, "above": 1}.get(orient)
        sh, extra = pl.get("shift_from_ideal") or [None, None], pl.get("extra_layers_from_ideal")
        if None not in sh and isinstance(extra, int) and (
                extra > st["max_extra_layers"] or max(abs(sh[0]), abs(sh[1])) > st["max_shift_tiles"]):
            errors.append("placement of {} goes {} layer(s) and {} beyond its ideal spot, past the search limits "
                          "(max_extra_layers {}, max_shift_tiles {})".format(g["id"], extra, sh,
                                                                             st["max_extra_layers"],
                                                                             st["max_shift_tiles"]))
        if step is None or not isinstance(extra, int) or extra < 0 or None in (ideal.get("dx"), ideal.get("dy"),
                                                                                 ideal.get("L"), *sh) or \
                [pl["x_offset"], pl["y_offset"], pl["layer_offset"]] != [
                    ideal["dx"] + sh[0], ideal["dy"] + sh[1], ideal["L"] + step * extra]:
            errors.append("offsets of {} are not its ideal spot {} shifted by {} and {} layer(s) {}".format(
                g["id"], ideal, sh, extra, orient))
            continue
        # the main connection is one of the group's exit records, to something placed before it;
        # only a placement forced by an alignment hint has none
        mc = pl.get("main_connection")
        hinted = bool(pl.get("tentative_reason"))
        if "tentative_reason" in pl and not (isinstance(pl["tentative_reason"], str) and pl["tentative_reason"]):
            errors.append("tentative_reason of {} is not a text".format(g["id"]))
        if hinted and pl.get("status") != "tentative":
            errors.append("{} placement of {} carries an alignment hint".format(pl.get("status"), g["id"]))
            hinted = False
        if hinted:
            # a configured alignment hint: the group's ideal spot is its anchor's, the given layers away
            a = aligns.get(g["id"])
            anchor = placed_by_id.get(a["anchor"]) if a else None
            if anchor is None or pl["tentative_reason"] != a["why"] or ideal != {
                    "dx": anchor["placement"]["x_offset"], "dy": anchor["placement"]["y_offset"],
                    "L": anchor["placement"]["layer_offset"] + a["layer_delta"]} or \
                    not index.get(a["anchor"], len(index)) < index.get(g["id"], -1):
                errors.append("alignment hint of {} is not a configured alignment to a placed anchor".format(g["id"]))
        if mc is None or hinted:
            if (mc is None) != hinted:
                errors.append("placement of {} has {}".format(
                    g["id"], "no main connection" if mc is None else "both a main connection and an alignment hint"))
            continue
        if mc.get("kind") not in ("in", "out"):
            errors.append("main connection of {} has kind {}".format(g["id"], mc.get("kind")))
            continue
        keys = ("entrances_from_overworld", "entrances_from_other_groups") if mc["kind"] == "in" else \
            ("exits_to_overworld", "exits_to_other_groups")
        match = [e for key in keys for e in g.get(key, [])
                 if [e["source_map"], e["source_tile"], e["target_map"], e["target_tile"], e["arch"], e["vertical"]]
                 == [mc.get("source_map"), mc.get("source_tile"), mc.get("target_map"), mc.get("target_tile"),
                     mc.get("arch"), mc.get("vdir")]]
        if not match:
            errors.append("main connection of {} is not one of its exit records".format(g["id"]))
            continue
        other = match[0]["source_group"] if mc["kind"] == "in" else match[0]["target_group"]
        oi, gi = index.get(other), index.get(g["id"])
        if other != ow_id and not (isinstance(oi, int) and isinstance(gi, int) and oi < gi):
            errors.append("main connection of {} leads to {}, which is not placed before it".format(g["id"], other))
            continue
        # the ideal spot puts the connection's end in this group under (or over) its other end: one
        # layer away in the exit's direction (or the orientation's), within the vertical bounds
        e = match[0]
        if "plane_source" not in e or "plane_target" not in e:
            continue  # reported with the exit records
        far, (near_map, near_tile) = (e["plane_source"], (e["target_map"], e["target_tile"])) if mc["kind"] == "in" \
            else (e["plane_target"], (e["source_map"], e["source_tile"]))
        lo_ = next((m["local_offset"] for m in g["maps"] if m["path"] == near_map), None)
        if lo_ is None:
            continue  # reported with the exit records
        lx, ly, lz = lo_[0] + near_tile[0], lo_[1] + near_tile[1], lo_[2]
        d_ = mc["vdir"] if mc["vdir"] != "none" else (
            ("down" if orient == "below" else "up") if mc["kind"] == "in" else ("up" if orient == "below" else "down"))
        below = d_ == ("down" if mc["kind"] == "in" else "up")
        L_ = far[2] - 1 - lz if below else far[2] + 1 - lz
        lo_b, hi_b = (pl.get("vertical_bounds_from_exit_semantics") or [None, None])
        L_ = max(L_, lo_b) if lo_b is not None else L_
        L_ = min(L_, hi_b) if hi_b is not None else L_
        if ideal != {"dx": far[0] - lx, "dy": far[1] - ly, "L": L_}:
            errors.append("ideal spot {} of {} does not follow from its main connection ({})".format(
                ideal, g["id"], {"dx": far[0] - lx, "dy": far[1] - ly, "L": L_}))

    # ------------------------------------------------- required stairwells
    # Every exit whose ends are 2+ layers apart and at most one tile apart per
    # layer must be a stairwell, unless a blocked site lists it: crossing exits
    # (placed groups) always, overworld exits only when the relayering changed
    # their layer distance. Blocked sites must really have no void chain.
    required = 0
    for (src_map, line), e in sorted(records.items()):
        if "plane_source" not in e or "plane_target" not in e or not needs_stairwell(e):
            continue
        s_, d_ = e["plane_source"], e["plane_target"]
        dl, dist = abs(d_[2] - s_[2]), cheb(s_, d_)
        required += 1
        if (src_map, line) in blocked_keys:
            if e.get("becomes") != "portal":
                errors.append("exit {}:{} is listed as blocked but becomes {}".format(src_map, line, e.get("becomes")))
            b = blocked_keys[(src_map, line)][1]
            up, low = (s_, d_) if s_[2] > d_[2] else (d_, s_)
            if up[2] != b["top"][2] or low[2] != b["bottom"][2] or cheb(up, b["top"]) > tol or \
                    cheb(low, b["bottom"]) > tol:
                errors.append("exit {}:{} does not end at its blocked site {} -> {}".format(
                    src_map, line, b["top"], b["bottom"]))
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
                            ok = cheb(p_, top) <= bend
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
    heights, blocked, ground = {}, set(), {}
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
                if "/" in tok:
                    tok, g = tok.split("/")
                    ground[k] = int(g)
                heights[k] = int(tok)
    split = {}
    for sp in rl["split_tiles"]:
        X, Y, Z = world_cell(sp["map"])
        split[(Z, 24 * X + sp["tile"][0], 24 * Y + sp["tile"][1])] = sp
    # a split tile's raised part is its upper layer; which tiles split is derived below
    main = {k: max(v[1]) for k, v in leg.items()}
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
    # split tiles: a relayered tile whose sub-layer 0 floor stays below its raised floor keeps
    # that ground part on its unit's layer; derived from the tile heights, not the records
    derived_split = {}
    for k in relayered:
        g = ground.get(k)
        base_layer = k[0] + (du[unit_of[k]] if k in unit_of else 0)
        if g is not None and g // HZ < q[k] and base_layer != main[k]:
            derived_split[k] = base_layer
    if set(derived_split) != set(split):
        errors.append("split tiles differ: {} derived from the tile heights, {} recorded".format(
            len(derived_split), len(split)))
    for k, v in leg.items():
        want = sorted({derived_split[k], main[k]}) if k in derived_split else [main[k]]
        if sorted(v[1]) != want:
            errors.append("legacy tile {} of {} appears on layers {}, expected {}".format(k, v[0], sorted(v[1]), want))
    for k, sp in split.items():
        if k not in derived_split or sp["ground_layer"] != derived_split[k] or sp["upper_layer"] != main[k] or \
                sp["floors"].get("0") != ground.get(k):
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
            if k in derived_split:
                gpos[(derived_split[k], k[1], k[2])].append((("ground",) + k, ground[k]))
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
    if len(listed) != len(rl["exits"]):
        errors.append("an exit is listed twice in the relayering exits")
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
        same_as("relayering exit", e, r, ("source_tile", "target_map", "target_tile", "arch", "xy_distance",
                                          "stairwell", "portal_reason"))
        crosses = None if r["source_group"] == r["target_group"] else (
            r["target_group"] if r["source_group"] == ow_id else r["source_group"])
        if e.get("crosses_to_group") != crosses:
            errors.append("relayering exit {}:{} crosses to {}, its record to {}".format(
                e["source_map"], e["line"], e.get("crosses_to_group"), crosses))
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
            "exit_inventory_objects": len(inv), "exit_record_copies_checked": sum(copies.values()),
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
