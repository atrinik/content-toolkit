#!/usr/bin/env python3
"""Independent check of placement-plan.json (uses only the JSON).

    python3 -I scripts/verify_plan.py [placement-plan.json]

Checks every placed group (proposed and tentative):
  * member plane origin == local_offset + (x_offset, y_offset, layer_offset)
  * the JSON carries occupied rows for every overworld map on every layer
    that holds placed tiles or stairwell tiles
  * no occupied tile is claimed by two owners (overworld map or group)
  * gap rule: on each layer, a group's occupied tiles are more than
    view_radius_tiles (Chebyshev) from every owner it shares no exit with,
    and more than contact_margin_tiles from every owner it shares one with;
    related owners are re-derived from the exit records
  * stairwells: consecutive tiles are exactly one layer and at most
    stair_tolerance_tiles apart; the ends match every replaced exit; every
    intermediate tile is void (no owner occupies it, no other stairwell uses
    it); the recorded view conflicts equal the unrelated owners within the
    view radius, and mode "clear" means there are none
  * every exit record's plane_source / plane_target equals origin + tile, and
    its "becomes" label matches its geometry
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


def world_cell(path: str):
    m = WORLD_RE.search(path)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


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


def verify(plan: dict) -> dict:
    st = plan["settings"]
    R, M, tol = st["view_radius_tiles"], st["contact_margin_tiles"], st["stair_tolerance_tiles"]
    ow = next(g for g in plan["groups"] if g["classification"] == "overworld")
    ow_id = ow["id"]
    errors = []

    # ------------------------------------------------------------ occupancy
    owners = collections.defaultdict(dict)  # layer -> owner -> Rows
    claim = collections.defaultdict(dict)   # layer -> (x, y) -> owner
    origin = {}

    def put(owner, layer, x0, y0, rows):
        rr = owners[layer].setdefault(owner, Rows())
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                if ch == "#":
                    t = (x0 + x, y0 + y)
                    prev = claim[layer].get(t)
                    if prev is not None and prev != owner:
                        errors.append("tile {} layer {} claimed by {} and {}".format(t, layer, prev, owner))
                    claim[layer][t] = owner
                    rr.add(*t)

    for path, rows in sorted(ow["occupied_rows_on_used_layers"].items()):
        X, Y, Z = world_cell(path)
        put(path, Z, 24 * X, 24 * Y, rows)
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
            put(g["id"], got[2], got[0], got[1], m["occupied_rows"])
    for layer in owners.values():
        for rr in layer.values():
            rr.done()

    used = {z for z, by in owners.items() if any(o not in ow["occupied_rows_on_used_layers"] for o in by)}
    for sw in plan["stairwells"]:
        used.update(t["layer"] for t in sw["tiles"] if t["role"] == "intermediate")
    have = {world_cell(p)[:3] for p in ow["occupied_rows_on_used_layers"]}
    for z in sorted(used):
        for X, Y in ow["cells_by_layer"].get(str(z), []):
            if (X, Y, z) not in have:
                errors.append("occupied rows missing for overworld world_{}_{}_{} on used layer {}".format(X, Y, z, z))

    # ------------------------------------------------------ related owners
    def end_owner(group, path):
        return path if group == ow_id else group

    rel = collections.defaultdict(set)
    for g in placed:
        for key in ("entrances_from_overworld", "exits_to_overworld", "entrances_from_other_groups",
                    "exits_to_other_groups"):
            for e in g.get(key, []):
                a = end_owner(e["source_group"], e["source_map"])
                b = end_owner(e["target_group"], e["target_map"])
                rel[a].add(b)
                rel[b].add(a)
        if sorted(rel[g["id"]]) != g.get("related_owners"):
            errors.append("related owners of {} differ from the exit records".format(g["id"]))

    def related(a, b):
        return b in rel[a] or a in rel[b]

    # ------------------------------------------------------------ gap rule
    def min_distance(a, b, cap):
        """Smallest Chebyshev distance between two tile sets, or cap + 1 if larger than cap."""
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
                rel_pair = related(gid, o)
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
                    "exits_to_other_groups"):
            for e in g.get(key, []):
                records[(e["source_map"], e["line"])] = e

    # ----------------------------------------------------------- stairwells
    # Stairwell tiles belong to the group that built them for view checks.
    chain = collections.defaultdict(dict)  # layer -> group -> Rows
    for sw in plan["stairwells"]:
        for x in sw["tiles"][1:-1]:
            chain[x["layer"]].setdefault(sw["group"], Rows()).add(x["x"], x["y"])
    for layer in chain.values():
        for rr in layer.values():
            rr.done()
    sw_tiles = {}
    intermediate = 0
    for sw in plan["stairwells"]:
        t = sw["tiles"]
        if t[0]["role"] != "top" or t[-1]["role"] != "bottom" or any(x["role"] != "intermediate" for x in t[1:-1]):
            errors.append("{} tile roles are not top, intermediate..., bottom".format(sw["id"]))
        if (t[0]["x"], t[0]["y"], t[0]["layer"]) != (sw["x"], sw["y"], sw["top_layer"]) or \
                t[-1]["layer"] != sw["bottom_layer"]:
            errors.append("{} header does not match its tiles".format(sw["id"]))
        if len(t) < 3:
            errors.append("{} spans fewer than two layers".format(sw["id"]))
        for a, b in zip(t, t[1:]):
            if a["layer"] - b["layer"] != 1 or max(abs(a["x"] - b["x"]), abs(a["y"] - b["y"])) > tol:
                errors.append("{} is not continuous between layers {} and {}".format(sw["id"], a["layer"], b["layer"]))
        top = (t[0]["x"], t[0]["y"], t[0]["layer"])
        bot = (t[-1]["x"], t[-1]["y"], t[-1]["layer"])
        for r in sw["replaces"]:
            e = records.get((r["source_map"], r["line"]))
            if e is None:
                errors.append("{} replaces an unknown exit {}:{}".format(sw["id"], r["source_map"], r["line"]))
                continue
            if e.get("becomes") != "stairwell" or e.get("stairwell") != sw["id"]:
                errors.append("{} exit {}:{} is not marked as this stairwell".format(sw["id"], r["source_map"], r["line"]))
            s, d = e["plane_source"], e["plane_target"]
            up, low = (s, d) if s[2] > d[2] else (d, s)
            if up[2] != top[2] or low[2] != bot[2] or max(abs(up[0] - top[0]), abs(up[1] - top[1])) > tol \
                    or max(abs(low[0] - bot[0]), abs(low[1] - bot[1])) > tol:
                errors.append("{} ends do not match exit {}:{}".format(sw["id"], r["source_map"], r["line"]))
        rel_sw = rel[sw["group"]] | {sw["group"]}
        recorded = {(c["layer"], c["x"], c["y"]): sorted(c["visible_from"]) for c in sw["view_conflicts"]}
        for x in t[1:-1]:
            intermediate += 1
            key = (x["layer"], x["x"], x["y"])
            if (x["x"], x["y"]) in claim.get(x["layer"], {}):
                errors.append("{} tile {} is not void (occupied by {})".format(
                    sw["id"], key, claim[x["layer"]][(x["x"], x["y"])]))
            if key in sw_tiles:
                errors.append("{} tile {} is also used by {}".format(sw["id"], key, sw_tiles[key]))
            sw_tiles[key] = sw["id"]
            vis = sorted({o for o, rr in list(owners.get(x["layer"], {}).items()) + list(
                chain.get(x["layer"], {}).items()) if o not in rel_sw and rr.any_within(x["x"], x["y"], R)})
            if vis != recorded.get(key, []):
                errors.append("{} tile {} view conflicts {} != recorded {}".format(sw["id"], key, vis,
                                                                                  recorded.get(key, [])))
        if (sw["mode"] == "clear") != (not sw["view_conflicts"]):
            errors.append("{} mode {} disagrees with its view conflicts".format(sw["id"], sw["mode"]))

    # ---------------------------------------------------------------- exits
    exits_checked = 0
    for (src_map, line), e in sorted(records.items()):
        ends = {}
        for end, mkey, tkey, gkey in (("plane_source", "source_map", "source_tile", "source_group"),
                                      ("plane_target", "target_map", "target_tile", "target_group")):
            if end not in e:
                continue
            mp, (tx, ty) = e[mkey], e[tkey]
            if e[gkey] == ow_id:
                X, Y, Z = world_cell(mp)
                exp = [24 * X + tx, 24 * Y + ty, Z]
            else:
                o = origin[mp]
                exp = [o[0] + tx, o[1] + ty, o[2]]
            exits_checked += 1
            ends[end] = exp
            if exp != e[end]:
                errors.append("exit {}:{} {} {} != {}".format(src_map, line, end, e[end], exp))
        if len(ends) == 2:
            s, d = ends["plane_source"], ends["plane_target"]
            dist, dl = max(abs(s[0] - d[0]), abs(s[1] - d[1])), abs(d[2] - s[2])
            b = e.get("becomes")
            ok = {"stairs": dl == 1 and dist <= tol, "stairwell": dl >= 2 and dist <= tol and "stairwell" in e,
                  "same-tile": dl == 0 and dist == 0,
                  "portal": not (dl == 1 and dist <= tol) and not (dl == 0 and dist == 0)}.get(b, False)
            if not ok:
                errors.append("exit {}:{} labelled {} but is {} tiles / {} layers apart".format(src_map, line, b, dist, dl))
    return {"ok": not errors, "errors": errors, "member_maps": len(origin), "owner_pairs_checked": pairs,
            "closest_pairs": closest,
            "stairwells_checked": len(plan["stairwells"]), "stairwell_intermediate_tiles": intermediate,
            "exit_endpoints_checked": exits_checked}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    path = Path(argv[0]) if argv else Path(__file__).resolve().parents[1] / "placement-plan.json"
    res = verify(json.loads(path.read_text(encoding="utf-8")))
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
