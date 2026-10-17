"""Group (component) computation, internal layout, and exit resolution."""

from __future__ import annotations

import collections
import os
from typing import Optional

import mapdata as md

OVERWORLD_PREFIX = "/shattered_islands/world_"
OVERWORLD_ROOT = "/shattered_islands/world_0_0"
OW_TILE = 24


# ---------------------------------------------------------------- components
def components(maps: dict) -> list:
    parent = {m: m for m in maps}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for m in sorted(maps):
        for _slot, target in sorted(maps[m].links.items()):
            a, b = find(m), find(target)
            if a != b:
                if a < b:
                    parent[b] = a
                else:
                    parent[a] = b
    comps = collections.defaultdict(list)
    for m in sorted(maps):
        comps[find(m)].append(m)
    return sorted((sorted(v) for v in comps.values()), key=lambda v: v[0])


def group_id(members: list, taken: set) -> str:
    if len(members) == 1:
        gid = members[0]
    else:
        gid = os.path.commonprefix(members).rstrip("_-")
        if gid.endswith("/"):
            gid = gid.rstrip("/")
    base = gid
    n = 2
    while gid in taken:
        gid = "{}~{}".format(base, n)
        n += 1
    taken.add(gid)
    return gid


# ------------------------------------------------------------------- layout
def slot_offset(slot: int, cur, nb):
    """Offset of neighbour origin relative to current origin (dx, dy, dz)."""
    w, h = cur.width, cur.height
    nw, nh = nb.width, nb.height
    return {
        1: (0, -nh, 0),
        2: (w, 0, 0),
        3: (0, h, 0),
        4: (-nw, 0, 0),
        5: (w, -nh, 0),
        6: (w, h, 0),
        7: (-nw, h, 0),
        8: (-nw, -nh, 0),
        9: (0, 0, 1),
        10: (0, 0, -1),
    }[slot]


def layout(members: list, maps: dict, overworld: bool) -> dict:
    """Assign each member a local (x, y, layer) origin by walking tile links."""
    problems = []
    if overworld:
        offsets = {}
        for m in members:
            X, Y, Z = maps[m].coords
            offsets[m] = (OW_TILE * X, OW_TILE * Y, Z)
        # verify BFS agrees with the formula
        for m in members:
            cur = maps[m]
            for slot, t in sorted(cur.links.items()):
                if t not in offsets:
                    continue
                dx, dy, dz = slot_offset(slot, cur, maps[t])
                exp = (offsets[m][0] + dx, offsets[m][1] + dy, offsets[m][2] + dz)
                if exp != offsets[t]:
                    problems.append({"kind": "inconsistent-link-offset", "map": m, "slot": slot,
                                     "neighbour": t, "expected": list(exp), "actual": list(offsets[t])})
        return {"root": OVERWORLD_ROOT, "offsets": offsets, "problems": problems}

    with_coords = [m for m in members if maps[m].coords is not None]
    root = min(with_coords) if with_coords else min(members)
    r = maps[root]
    if r.coords is not None:
        start = (r.coords[0] * r.width, r.coords[1] * r.height, r.coords[2])
    else:
        start = (0, 0, 0)
    offsets = {root: start}
    queue = collections.deque([root])
    while queue:
        m = queue.popleft()
        cur = maps[m]
        for slot, t in sorted(cur.links.items()):
            dx, dy, dz = slot_offset(slot, cur, maps[t])
            exp = (offsets[m][0] + dx, offsets[m][1] + dy, offsets[m][2] + dz)
            if t not in offsets:
                offsets[t] = exp
                queue.append(t)
            elif offsets[t] != exp:
                problems.append({"kind": "inconsistent-link-offset", "map": m, "slot": slot,
                                 "slot_name": md.SLOT_NAMES[slot], "neighbour": t,
                                 "expected": list(exp), "actual": list(offsets[t])})
    # filename coordinate cross-check (only meaningful for uniform sizes)
    sizes = {(maps[m].width, maps[m].height) for m in members}
    prefixes = {maps[m].coord_prefix for m in with_coords}
    if with_coords and len(sizes) == 1 and len(prefixes) == 1:
        w, h = next(iter(sizes))
        for m in with_coords:
            X, Y, Z = maps[m].coords
            if offsets.get(m) != (X * w, Y * h, Z):
                problems.append({"kind": "filename-coordinate-mismatch", "map": m,
                                 "from_links": list(offsets.get(m, ())), "from_filename": [X * w, Y * h, Z]})
    # overlaps between members on the same local layer
    by_layer = collections.defaultdict(list)
    for m, (x, y, z) in offsets.items():
        by_layer[z].append((x, y, x + maps[m].width, y + maps[m].height, m))
    for z, rects in by_layer.items():
        rects.sort()
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    problems.append({"kind": "member-overlap", "layer": z, "maps": [a[4], b[4]]})
    return {"root": root, "offsets": offsets, "problems": problems}


def link_reciprocity(maps: dict) -> list:
    problems = []
    for m in sorted(maps):
        rec = maps[m]
        for slot, t in sorted(rec.links.items()):
            back = maps[t].links.get(md.REVERSE_SLOT[slot])
            if back != m:
                problems.append({"kind": "non-reciprocal-tile-link", "map": m, "slot": slot,
                                 "slot_name": md.SLOT_NAMES[slot], "link_source": rec.link_source[slot],
                                 "target": t, "target_reverse_slot": md.REVERSE_SLOT[slot],
                                 "target_reverse_value": back})
            if slot in (1, 3) and maps[t].width != rec.width:
                problems.append({"kind": "edge-size-mismatch", "map": m, "slot": slot, "target": t,
                                 "detail": "N/S neighbour width {} != {}".format(maps[t].width, rec.width)})
            if slot in (2, 4) and maps[t].height != rec.height:
                problems.append({"kind": "edge-size-mismatch", "map": m, "slot": slot, "target": t,
                                 "detail": "E/W neighbour height {} != {}".format(maps[t].height, rec.height)})
        for b in rec.broken_links:
            problems.append(dict({"kind": "broken-tile-link", "map": m}, **b))
    return problems


# -------------------------------------------------------------------- exits
LANDING = md.DIR_OFFSETS


def resolve_coordinate(maps: dict, path: str, x: int, y: int):
    """Mirror server get_map_from_coord(); returns (path, x, y) or None."""
    seen = set()
    for _ in range(128):
        if (path, x, y) in seen:
            return None
        seen.add((path, x, y))
        rec = maps.get(path)
        if rec is None:
            return None
        if 0 <= x < rec.width and 0 <= y < rec.height:
            return path, x, y
        if x < 0:
            slot = 8 if y < 0 else (7 if y >= rec.height else 4)
        elif x >= rec.width:
            slot = 5 if y < 0 else (6 if y >= rec.height else 2)
        elif y < 0:
            slot = 1
        else:
            slot = 3
        nxt = rec.links.get(slot)
        if nxt is None:
            return None
        nb = maps[nxt]
        if slot == 8:
            x += nb.width; y += nb.height
        elif slot == 7:
            x += nb.width; y -= rec.height
        elif slot == 4:
            x += nb.width
        elif slot == 5:
            x -= rec.width; y += nb.height
        elif slot == 6:
            x -= rec.width; y -= rec.height
        elif slot == 2:
            x -= rec.width
        elif slot == 1:
            y += nb.height
        else:
            y -= rec.height
        path = nxt
    return None


def collect_exits(maps: dict, archs: dict, all_files: set) -> tuple:
    exits = []
    inventory_exits = 0
    for m in sorted(maps):
        rec = maps[m]
        for obj in rec.objects:
            # count exits hidden in inventories (inactive on the map)
            stack = list(obj.children)
            while stack:
                c = stack.pop()
                if md.effective_attrs(c, archs).get("type") == md.EXIT_TYPE:
                    inventory_exits += 1
                stack.extend(c.children)
            attrs = md.effective_attrs(obj, archs)
            if attrs.get("type") != md.EXIT_TYPE:
                continue
            x = md.to_int(attrs.get("x"), 0)
            y = md.to_int(attrs.get("y"), 0)
            slaying = attrs.get("slaying", "").strip()
            e = {
                "source_map": m, "source_x": x, "source_y": y, "arch": obj.name,
                "name": attrs.get("name"), "line": obj.line,
                "per_player": attrs.get("last_eat") == "1",
                "explicit_last_heal_0": obj.fields.get("last_heal") == "0",
                "fixed_pos": attrs.get("use_fix_pos") == "1",
                "has_events": any(md.effective_attrs(c, archs).get("type") == "118" for c in obj.children),
                "event_scripts": sorted({c.fields.get("race", "") for c in obj.children
                                         if md.effective_attrs(c, archs).get("type") == "118"} - {""}),
            }
            target = tx = ty = None
            if slaying:
                if md.is_dynamic_path(slaying):
                    e.update(form="dynamic", raw_target=slaying)
                    exits.append(e)
                    continue
                target = md.normalize_path(m, slaying)
                tx = md.to_int(attrs.get("hp"), -1)
                ty = md.to_int(attrs.get("sp"), -1)
                e.update(form="explicit", raw_target=slaying)
            else:
                tiled = md.to_int(attrs.get("last_heal"))
                if tiled is not None and 1 <= tiled <= 10 and (tiled in rec.links or any(
                        b["slot"] == tiled for b in rec.broken_links)):
                    if tiled in rec.links:
                        target = rec.links[tiled]
                    else:
                        target = next(b["normalized"] for b in rec.broken_links if b["slot"] == tiled)
                    tx, ty = x, y
                    if attrs.get("xrays") == "1":
                        d = md.to_int(attrs.get("direction"))
                        if d is not None and 1 <= d <= 8:
                            if tiled == 9:
                                d = ((d + 3) % 8) + 1
                            tx += LANDING[d][0]
                            ty += LANDING[d][1]
                    e.update(form="tiled", tiled_slot=tiled)
                elif tiled is not None and 1 <= tiled <= 10:
                    e.update(form="tiled-unresolved", tiled_slot=tiled)
                    exits.append(e)
                    continue
                elif md.to_int(attrs.get("hp"), -1) != -1 or md.to_int(attrs.get("sp"), -1) != -1:
                    target, tx, ty = m, md.to_int(attrs.get("hp"), -1), md.to_int(attrs.get("sp"), -1)
                    e.update(form="same-map")
                elif md.to_int(attrs.get("sub_type"), 0) not in (0, None) or obj.name.casefold() == "shop_mat":
                    target, tx, ty = m, x, y
                    e.update(form="automatic-link" if obj.name.casefold() != "shop_mat" else "shop-mat")
                else:
                    e.update(form="unresolved")
                    exits.append(e)
                    continue
            e.update(target_map=target, target_x=tx, target_y=ty)
            if target not in maps:
                e["problem"] = "missing-target-map" + (" (exists but not a map file)" if target in all_files else "")
                exits.append(e)
                continue
            res = resolve_coordinate(maps, target, tx, ty)
            if res is None:
                trec = maps[target]
                ex = md.to_int(trec.header.get("enter_x"), 0)
                ey = md.to_int(trec.header.get("enter_y"), 0)
                res = (target, ex, ey)
                e["target_default_entry"] = True
                if not (tx == -1 and ty == -1):
                    e["problem"] = "invalid-target-coordinates (server falls back to enter_x/enter_y)"
            e.update(resolved_map=res[0], resolved_x=res[1], resolved_y=res[2])
            exits.append(e)
    return exits, inventory_exits
