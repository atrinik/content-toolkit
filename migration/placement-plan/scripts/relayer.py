"""Layer-height (relayering) proposal: floor z of 50 is about one map layer.

For every placed map (overworld maps on every layer and placed group
members), each occupied tile's terrain floor z (LAYER_FLOOR, sub-layer 0) is
split as ``z = 50 * q + rest`` with ``q = trunc(z / 50)``, so ``|rest| < 50``
and the remainder keeps z's sign. Tiles with ``q != 0`` would move ``q``
layers; 8-connected tiles with the same (layer, q) form one region. The
analysis is data only: nothing here changes the placement.

Per region it reports the maps and tile counts, the layer before and after,
tiles that would land on occupied tiles of the target layer (overlap
conflicts), content on the target layer within the view radius, neighbours on
the target layer at about the same height (joins), the same-layer edges the
move would cut, and how every exit with an end in the region changes.
"""

from __future__ import annotations

import collections

NEIGHBOURS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]
MIN_LISTED_TILES = 24  # smaller regions are aggregated (single raised tiles, decoration)


def split(z: int, height: int):
    q = int(z / height)
    return q, z - q * height


def analyse(maps, map_origins, ground_at, occupant, unrelated_within, exits, owner_of_map,
            height: int, client_level_px: int):
    tiles = {}  # (layer, x, y) -> (z, q, map)
    scanned = 0
    near_threshold = 0
    negative_moves = 0
    for m in sorted(map_origins):
        ox, oy, oz = map_origins[m]
        for (tx, ty), z in sorted(maps[m].ground.items()):
            scanned += 1
            if client_level_px <= abs(z) < height:
                near_threshold += 1
            q, _ = split(z, height)
            if q:
                tiles[(oz, ox + tx, oy + ty)] = (z, q, m)
                if q < 0:
                    negative_moves += 1

    # regions: 8-connected, same layer and same q
    seen = set()
    regions = []
    for key in sorted(tiles, key=lambda k: (k[0], k[2], k[1])):
        if key in seen:
            continue
        L, q = key[0], tiles[key][1]
        comp = []
        stack = [key]
        seen.add(key)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for dx, dy in NEIGHBOURS:
                nb = (L, cur[1] + dx, cur[2] + dy)
                if nb in tiles and nb not in seen and tiles[nb][1] == q:
                    seen.add(nb)
                    stack.append(nb)
        regions.append((L, q, sorted(comp)))
    regions.sort(key=lambda r: (-len(r[2]), r[0], r[2][0][2], r[2][0][1]))

    # connections between owners, for "related" content in view checks
    owner_links = collections.defaultdict(set)
    for e in exits:
        if e.get("resolved_map") in map_origins and e["source_map"] in map_origins:
            a, b = owner_of_map(e["source_map"]), owner_of_map(e["resolved_map"])
            owner_links[a].add(b)
            owner_links[b].add(a)

    def plane(m, x, y):
        ox, oy, oz = map_origins[m]
        return ox + x, oy + y, oz

    listed, small = [], {"regions": 0, "tiles": 0, "maps": set()}
    for L, q, comp in regions:
        cset = set(comp)
        if len(comp) < MIN_LISTED_TILES:
            small["regions"] += 1
            small["tiles"] += len(comp)
            small["maps"].update(tiles[k][2] for k in comp)
            continue
        target = L + q
        zs = [tiles[k][0] for k in comp]
        rests = [split(z, height)[1] for z in zs]
        by_map = collections.Counter(tiles[k][2] for k in comp)
        owners = sorted({owner_of_map(m) for m in by_map})
        related = set(owners)
        for o in owners:
            related |= owner_links.get(o, set())
        overlap = collections.Counter()
        joins = collections.Counter()
        cut, gentle = 0, 0
        for (_, x, y) in comp:
            o = occupant(target, (x, y))
            if o is not None:
                overlap[o] += 1
            z, _q, _m = tiles[(L, x, y)]
            rest = split(z, height)[1]
            for dx, dy in NEIGHBOURS:
                nx, ny = x + dx, y + dy
                if (L, nx, ny) not in cset:
                    # an occupied neighbour with a floor that stays behind on this layer
                    gz = ground_at(L, nx, ny)
                    if gz is not None:
                        cut += 1
                        if abs(gz - z) < height // 2:
                            gentle += 1
                if (target, nx, ny) not in cset:
                    tz = ground_at(target, nx, ny)
                    if tz is not None and abs(tz - rest) < height // 2:
                        jo = occupant(target, (nx, ny))
                        if jo is not None and jo not in owners:
                            joins[jo] += 1
        view = set()
        for (_, x, y) in comp:
            view.update(unrelated_within(target, (x, y), related))
        ex = []
        for e in exits:
            if not e.get("resolved_map") or e["source_map"] not in map_origins or e["resolved_map"] not in map_origins:
                continue
            s = plane(e["source_map"], e["source_x"], e["source_y"])
            t = plane(e["resolved_map"], e["resolved_x"], e["resolved_y"])
            s_in, t_in = (s[2], s[0], s[1]) in cset, (t[2], t[0], t[1]) in cset
            if not (s_in or t_in):
                continue
            s2 = (s[0], s[1], s[2] + (q if s_in else 0))
            t2 = (t[0], t[1], t[2] + (q if t_in else 0))
            d = max(abs(s[0] - t[0]), abs(s[1] - t[1]))
            before, after = t[2] - s[2], t2[2] - s2[2]
            if d > 1:
                change = "portal either way"
            elif abs(after) == 0 and abs(before) >= 1:
                change = "aligns: becomes a level walk on layer {}".format(s2[2])
            elif abs(after) == 1 and abs(before) != 1:
                change = "aligns: becomes plain stairs"
            elif abs(before) == 1 and abs(after) >= 2:
                change = "breaks: plain stairs become a {}-layer stairwell".format(abs(after))
            elif before == after:
                change = "unchanged"
            else:
                change = "layer span {} -> {}".format(abs(before), abs(after))
            ex.append({"source_map": e["source_map"], "source_tile": [e["source_x"], e["source_y"]],
                       "target_map": e["resolved_map"], "target_tile": [e["resolved_x"], e["resolved_y"]],
                       "arch": e["arch"], "layer_delta_before": before, "layer_delta_after": after,
                       "xy_distance": d, "change": change})
        ex.sort(key=lambda r: (r["source_map"], r["source_tile"][1], r["source_tile"][0], r["arch"]))
        aligns = sum(1 for r in ex if r["change"].startswith("aligns"))
        breaks = sum(1 for r in ex if r["change"].startswith("breaks"))
        conflict_free = not overlap
        listed.append({
            "id": "R{}".format(len(listed) + 1),
            "layer_before": L, "layer_after": target, "layers_moved": q,
            "tiles": len(comp),
            "maps": [{"path": m, "tiles": n} for m, n in sorted(by_map.items())],
            "z_min": min(zs), "z_max": max(zs), "remainder_min": min(rests), "remainder_max": max(rests),
            "bbox": [min(k[1] for k in comp), min(k[2] for k in comp), max(k[1] for k in comp),
                     max(k[2] for k in comp)],
            "overlap_conflicts": {"tiles": sum(overlap.values()),
                                  "owners": {o: n for o, n in sorted(overlap.items())}},
            "unrelated_content_in_view_on_target_layer": sorted(view),
            "same_height_edges_on_target_layer": {o: n for o, n in sorted(joins.items())},
            "same_layer_edges_cut": cut, "gentle_edges_cut": gentle,
            "exits": ex, "exits_aligned": aligns, "exits_broken": breaks,
            "conflict_free": conflict_free,
            "clearly_better": conflict_free and aligns > breaks and gentle == 0,
        })
    return {
        "status": "proposal for owner approval; NOT applied to the placement",
        "rule": "tile floor z = {h} * q + rest with q = trunc(z / {h}); a tile with q != 0 moves q layers and keeps "
                "rest as its tile height; 8-connected tiles with the same (layer, q) form one region".format(h=height),
        "layer_height_z": height,
        "client_level_pixel_height": client_level_px,
        "tiles_scanned": scanned,
        "tiles_that_would_move": len(tiles),
        "tiles_that_would_move_down": negative_moves,
        "tiles_between_client_level_and_threshold": near_threshold,
        "regions_total": len(regions),
        "regions_listed_min_tiles": MIN_LISTED_TILES,
        "regions": listed,
        "small_regions": {"regions": small["regions"], "tiles": small["tiles"], "maps": sorted(small["maps"])},
        "applied": False,
    }
