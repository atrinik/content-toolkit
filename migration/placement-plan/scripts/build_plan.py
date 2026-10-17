#!/usr/bin/env python3
"""Build the Atrinik overworld-plane placement plan.

Read-only against the content checkout. Writes placement-plan.json,
REPORT.md and layer_<n>.png into the output directory.

    python3 -I scripts/build_plan.py [--content DIR] [--out DIR]

Everything is deterministic: inputs are iterated in sorted order and every
tie is broken lexicographically.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis as an  # noqa: E402
import mapdata as md  # noqa: E402

DEFAULT_CONTENT = Path("/home/zoey/dev/atrinik/content")
DEFAULT_OUT = Path(__file__).resolve().parents[1]

# ----------------------------------------------------------------- settings
MARGIN = 1             # empty tiles required between distinct placed things
MAX_EXTRA_LAYERS = 3   # how many layers past the ideal layer to try
MAX_SHIFT = 96         # max Chebyshev horizontal shift (tiles) to try
STAIR_TOLERANCE = 1    # Chebyshev distance allowed for "becomes stairs"

# ------------------------------------------------- classification knowledge
# Owner directives / system knowledge that cannot be derived from exits alone.
# Each entry is applied only after the generic evidence rules and is printed
# as evidence so the owner can audit it.
SYSTEM_MAPS = {
    "/emergency": "Server emergency fallback map (EMERGENCY_MAPPATH in "
                  "classic/server object_enter_map); used by scripts as a fallback.",
    "/hall_of_dms": "DM hall; hub with exits to the Plane of Creation, guild oracle and "
                    "several overworld towns (a DM/system map).",
}
OTHER_WORLD_PATH_PREFIXES = ("/planes/",)
OTHER_WORLD_PATH_PARTS = ("/desert_plane/",)  # owner: "desert planes" stay separate
GUILD_PREFIXES = ("/guilds/",)
SHIP_PREFIXES = ("/shattered_islands/ships/",)
# Tentative alignment for groups that have no exit connection at all but whose
# naming shows they belong under another group (used only for a tentative
# placement the owner may accept or reject).
TENTATIVE_ALIGN = {
    "/shattered_islands/strakewood_island/rockforge/rockforge_c": (
        "/shattered_islands/strakewood_island/rockforge/rockforge_b", -1,
        "rockforge_a/_b/_c share the same 5x5 sheet naming (0101..ab05) and "
        "rockforge_a<->rockforge_b stairs connect identical local coordinates; "
        "rockforge_c ('Rockforge: Mines') is assumed to be the next sheet below "
        "rockforge_b at the same x/y."),
}

ABOVE_WORDS = ("tower", "treehome", "treehouse", "attic", "loft", "spire", "upper", "roof")
BELOW_WORDS = ("cave", "cavern", "mine", "sewer", "dungeon", "crypt", "lair", "abyss", "below",
               "underground", "cellar", "catacomb", "tunnel", "jail", "pit", "ruins", "outpost",
               "stronghold", "lab", "temple", "rockforge")


# ------------------------------------------------------------------ helpers
def git_head(content: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(content), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:  # pragma: no cover
        return "unknown"


def tree_digest(maps: dict, archs_files: int, content: Path) -> str:
    h = hashlib.sha256()
    for m in sorted(maps):
        h.update(m.encode())
        h.update(b"\0")
        h.update(hashlib.sha256((content / "maps" / m.lstrip("/")).read_bytes()).digest())
    return h.hexdigest()


def nice_group_id(members: list, taken: set) -> str:
    if len(members) == 1:
        gid = members[0]
    else:
        p = posixpath.commonprefix(members)
        if p in members:
            pass
        elif not p.endswith(("_", "/")):
            cut = max(p.rfind("_"), p.rfind("/"))
            p = p[:cut + 1]
        gid = p if p in members else p.rstrip("_/")
    base, n = gid, 2
    while gid in taken:
        gid = "{}~{}".format(base, n)
        n += 1
    taken.add(gid)
    return gid


def exit_direction(e: dict, archs: dict) -> str:
    arch = e["arch"].casefold()
    a = archs.get(arch, {})
    text = "{} {}".format(arch, (e.get("name") or "")).casefold()
    if a.get("last_heal") == "9" or re.search(r"(^|_| )up(\d|_|$| )", text) or "rope" in text:
        return "up"
    if a.get("last_heal") == "10" or "down" in text or "hole" in text or "well_bucket" in arch \
            or "trapdoor" in text or re.search(r"\bpit\b", text):
        return "down"
    if e.get("form") == "tiled" and e.get("tiled_slot") in (9, 10):
        return "up" if e["tiled_slot"] == 9 else "down"
    return "none"


def scan_scripts(content: Path, map_paths: set) -> dict:
    """Find literal references to map paths in Python scripts and dialogs."""
    refs = collections.defaultdict(list)
    maps_root = content / "maps"
    path_re = re.compile(r"[\"'](/(?:shattered_islands|planes|guilds|hall_of_dms|emergency)[^\"' ]*)")
    teleport_re = re.compile(r'teleport="([^" ]+)')
    files = sorted(list(maps_root.rglob("*.py")) + list((maps_root / "interfaces").rglob("*.xml")))
    for f in files:
        rel = f.relative_to(maps_root).as_posix()
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.split("\n"), start=1):
            for rx in (path_re, teleport_re):
                for mt in rx.finditer(line):
                    p = "/" + posixpath.normpath(mt.group(1)).lstrip("/")
                    if p in map_paths:
                        tup = re.search(r"\(\s*[\"']" + re.escape(mt.group(1)) + r"[\"']\s*,\s*-?\d+\s*,\s*-?\d+\s*,\s*(\d)\s*\)", line)
                        refs[p].append({"file": "maps/" + rel, "line": lineno,
                                        "text": line.strip()[:200],
                                        "unique_getpath": bool(re.search(r"GetPath\([^)]*True", line)),
                                        "house_tuple_unique": None if tup is None else tup.group(1) == "1",
                                        "test_only": rel.startswith("python/tests/")})
    return refs


def scan_exit_script_targets(content: Path, exits: list, maps: dict) -> list:
    """Event scripts on exits that redirect to `slaying + suffix` with unique=True."""
    found = []
    rx = re.compile(r"GetPath\(\s*me\.slaying\s*\+\s*[\"']([^\"']+)[\"']\s*,\s*True")
    for e in exits:
        for script in e.get("event_scripts", []):
            if script.startswith("/"):
                f = content / "maps" / script.lstrip("/")
            else:
                f = content / "maps" / posixpath.dirname(e["source_map"]).lstrip("/") / script
            if not f.is_file():
                continue
            for mt in rx.finditer(f.read_text(encoding="utf-8", errors="replace")):
                if e.get("target_map"):
                    tgt = e["target_map"] + mt.group(1)
                    if tgt in maps:
                        found.append({"exit_source": [e["source_map"], e["source_x"], e["source_y"]],
                                      "script": f.relative_to(content).as_posix(),
                                      "per_player_target": tgt})
    return found


# ---------------------------------------------------------------- occupancy
class Occupancy:
    """Collision index for one placement run.

    mode "rect": every map occupies its full width x height rectangle.
    mode "tiles": a map occupies only tiles holding at least one object
    (multi-part tails included); overworld layer 0 is always treated as full.
    A placed thing must keep `margin` empty tiles to everything else.
    """

    def __init__(self, maps: dict, ow_members: list, mode: str, margin: int):
        self.mode, self.margin = mode, margin
        self.ow = collections.defaultdict(set)
        self.ow_tiles = collections.defaultdict(dict)
        for m in ow_members:
            X, Y, Z = maps[m].coords
            self.ow[Z].add((X, Y))
            if mode == "tiles" and Z != 0:
                d = self.ow_tiles[Z]
                for (tx, ty) in maps[m].occupied:
                    px, py = 24 * X + tx, 24 * Y + ty
                    for ax in range(-margin, margin + 1):
                        for ay in range(-margin, margin + 1):
                            d.setdefault((px + ax, py + ay), m)
        self.rects = collections.defaultdict(list)  # layer -> (x0,y0,x1,y1,gid)
        self.tiles = collections.defaultdict(dict)  # layer -> {(x,y): gid} (dilated)

    def collisions(self, members: list):
        margin = self.margin
        for (x0, y0, x1, y1, layer, tiles) in members:
            ex0, ey0, ex1, ey1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
            if self.mode == "rect" or layer == 0:
                cells = self.ow.get(layer)
                if cells:
                    for X in range(ex0 // 24, (ex1 - 1) // 24 + 1):
                        for Y in range(ey0 // 24, (ey1 - 1) // 24 + 1):
                            if (X, Y) in cells:
                                return [("overworld", "/shattered_islands/world_{}_{}{}".format(
                                    X, Y, "" if layer == 0 else "_{}".format(layer)), layer)]
            if self.mode == "rect":
                for (a0, b0, a1, b1, gid) in self.rects.get(layer, ()):
                    if ex0 < a1 and a0 < ex1 and ey0 < b1 and b0 < ey1:
                        return [("group", gid, layer)]
            else:
                owt = self.ow_tiles.get(layer, {})
                grt = self.tiles.get(layer, {})
                for t in tiles:
                    if t in owt:
                        return [("overworld", owt[t], layer)]
                    if t in grt:
                        return [("group", grt[t], layer)]
        return []

    def add(self, members: list, gid: str):
        margin = self.margin
        for (x0, y0, x1, y1, layer, tiles) in members:
            self.rects[layer].append((x0, y0, x1, y1, gid))
            if self.mode == "tiles":
                d = self.tiles[layer]
                for (px, py) in tiles:
                    for ax in range(-margin, margin + 1):
                        for ay in range(-margin, margin + 1):
                            d.setdefault((px + ax, py + ay), gid)


def ring(r: int):
    if r == 0:
        return [(0, 0)]
    pts = [(x, y) for x in range(-r, r + 1) for y in range(-r, r + 1) if max(abs(x), abs(y)) == r]
    return sorted(pts, key=lambda p: (abs(p[0]) + abs(p[1]), p[1], p[0]))


# --------------------------------------------------------------------- main
def build(content: Path, out: Path, render: bool = True):
    archs = md.load_archetypes(content)
    maps, all_files = md.load_maps(content, archs)
    comps = an.components(maps)
    taken: set = set()
    groups = []
    gof = {}
    for c in comps:
        gid = nice_group_id(c, taken)
        groups.append({"id": gid, "maps": c})
        for m in c:
            gof[m] = gid
    ow_id = gof[an.OVERWORLD_ROOT]
    G = {g["id"]: g for g in groups}

    link_problems = an.link_reciprocity(maps)
    for g in groups:
        g["layout"] = an.layout(g["maps"], maps, overworld=(g["id"] == ow_id))

    exits, inventory_exits = an.collect_exits(maps, archs, all_files)
    for e in exits:
        e["dir"] = exit_direction(e, archs)
        e["source_group"] = gof[e["source_map"]]
        e["target_group"] = gof.get(e.get("resolved_map")) if e.get("resolved_map") else None
    # infer direction of neutral exits from co-located exits / reciprocal exits
    by_tile = collections.defaultdict(list)
    for e in exits:
        by_tile[(e["source_map"], e["source_x"], e["source_y"])].append(e)
    for e in exits:
        if e["dir"] != "none" or not e.get("target_group"):
            continue
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for o in by_tile.get((e["source_map"], e["source_x"] + dx, e["source_y"] + dy), ()):
                    if o is not e and o["dir"] != "none" and o.get("target_group") == e["target_group"]:
                        e["dir_inferred"] = o["dir"]
                        e["dir_inferred_from"] = "co-located {}".format(o["arch"])
        if "dir_inferred" in e:
            continue
        # reciprocal: an exit in the target group leading back near our source tile
        for o in exits:
            if o.get("target_group") == e["source_group"] and o["source_group"] == e["target_group"] \
                    and o.get("resolved_map") == e["source_map"] and o["dir"] != "none" \
                    and abs(o["resolved_x"] - e["source_x"]) <= 2 and abs(o["resolved_y"] - e["source_y"]) <= 2:
                e["dir_inferred"] = "down" if o["dir"] == "up" else "up"
                e["dir_inferred_from"] = "reciprocal {} at {} ({},{})".format(
                    o["arch"], o["source_map"], o["source_x"], o["source_y"])
                break
    for e in exits:
        e["vdir"] = e["dir"] if e["dir"] != "none" else e.get("dir_inferred", "none")

    exit_redirects = scan_exit_script_targets(content, exits, maps)
    script_refs = scan_scripts(content, set(maps))
    start_map = archs.get("map", {}).get("slaying")

    # ------------------------------------------------ per-group connections
    cross = [e for e in exits if e.get("target_group") and e["target_group"] != e["source_group"]]
    for g in groups:
        gid = g["id"]
        g["entrances"] = [e for e in cross if e["target_group"] == gid]
        g["exits_out"] = [e for e in cross if e["source_group"] == gid]
        g["script_refs"] = sorted((dict(r, map=m) for m in g["maps"] for r in script_refs.get(m, ())),
                                  key=lambda r: (r["file"], r["line"], r["map"]))
        g["redirect_refs"] = [r for r in exit_redirects if gof[r["per_player_target"]] == gid]

    # ------------------------------------------------------ classification
    def classify(g):
        gid, mem = g["id"], g["maps"]
        ev = []
        names = sorted({maps[m].header.get("name", "") for m in mem})
        regions = sorted({maps[m].header.get("region", "") for m in mem})
        ev.append("names={}; regions={}".format(names, regions))
        ow_in = [e for e in g["entrances"] if e["source_group"] == ow_id]
        pp_in = [e for e in g["entrances"] if e["per_player"]]
        if ow_in:
            ev.append("{} exit object(s) from the overworld lead here".format(len(ow_in)))
        else:
            ev.append("no exit object in the overworld leads here")
        if gid in SYSTEM_MAPS:
            return "separate", "system", ev + [SYSTEM_MAPS[gid]]
        if gid.startswith(GUILD_PREFIXES):
            return "separate", "guild", ev + ["path under /guilds/; guild maps are entered through "
                                              "python/Guild.py / guild scripts"]
        if gid.startswith(OTHER_WORLD_PATH_PREFIXES):
            return "separate", "other_world", ev + ["path under /planes/ (separate world)"]
        if any(p in gid + "/" for p in OTHER_WORLD_PATH_PARTS):
            return "separate", "other_world", ev + [
                "part of the Temple of Loki 'Desert Plane' sequence (path contains /desert_plane/); "
                "owner directive: desert planes stay separate"]
        if gid.startswith(SHIP_PREFIXES):
            return "separate", "ship", ev + ["path under /shattered_islands/ships/; one ship map is entered "
                                             "from {} different overworld docks".format(
                                                 len({e['source_map'] for e in ow_in}))]
        if pp_in:
            return "separate", "per_player", ev + [
                "{} entrance exit(s) carry last_eat 1 (MAP_PLAYER_MAP: per-player copy), e.g. {} ({},{})".format(
                    len(pp_in), pp_in[0]["source_map"], pp_in[0]["source_x"], pp_in[0]["source_y"])]
        if g["redirect_refs"]:
            r = g["redirect_refs"][0]
            return "separate", "per_player", ev + [
                "entered only via {} which teleports to slaying+suffix with GetPath(..., True) "
                "(per-player copy)".format(r["script"])]
        apts = [r for r in g["script_refs"] if r["file"] == "maps/python/Apartments.py"]
        if apts:
            return "separate", "per_player", ev + [
                "listed in {}:{} (apartment registry); python/generic/apartment_teleport.py enters it with "
                "GetPath(path, True, player) = per-player copy".format(apts[0]["file"], apts[0]["line"])]
        houses = [r for r in g["script_refs"] if r["file"] == "maps/python/House.py" and r["house_tuple_unique"]]
        if houses:
            return "separate", "per_player", ev + [
                "listed in {}:{} with unique flag 1; python/houses/exit.py enters it with "
                "GetPath(path, 1, player) = per-player house".format(houses[0]["file"], houses[0]["line"])]
        base = posixpath.basename(gid)
        if base.startswith("ship_") or "/ship_" in gid:
            dialog = [r for r in g["script_refs"] if r["file"].startswith("maps/interfaces/")]
            if start_map and start_map in mem:
                return "uncertain", "uncertain", ev + [
                    "a ship's lower deck, but it is the NEW-CHARACTER START MAP (arch/intern/map.arc "
                    "slaying {}); its only exits lead up to world_-7_76 where the beached ship is; the "
                    "overworld stairs at world_-7_76 (8,14) lead nowhere (no _-1 map)".format(start_map)]
            outs = sorted({e["resolved_map"] for e in g["exits_out"] if e["target_group"] == ow_id})
            ins = sorted({e["source_map"] for e in ow_in})
            return "separate", "ship", ev + [
                "ship lower deck used for sea travel: entered from {} but exits to {}{}".format(
                    ins, outs, "; NPC dialog teleports here ({})".format(dialog[0]["file"]) if dialog else "")]
        if "unique" in gid:
            return "uncertain", "uncertain", ev + [
                "name contains 'unique' and the owner listed 'unique mines' as separate, but no "
                "per-player mechanism exists in the data (no last_eat 1 exit, no `unique` header, no "
                "GetPath(..., True) script); physically it is reached by ladders/exits inside the "
                "Clearhaven mine"]
        return None, None, ev

    for g in groups:
        if g["id"] == ow_id:
            g.update(classification="overworld", reason="overworld", evidence=[
                "{} maps world_X_Y[_Z], 24x24, X {}..{}, Y {}..{}".format(
                    len(g["maps"]), min(maps[m].coords[0] for m in g["maps"]),
                    max(maps[m].coords[0] for m in g["maps"]), min(maps[m].coords[1] for m in g["maps"]),
                    max(maps[m].coords[1] for m in g["maps"]))])
            continue
        c, r, ev = classify(g)
        g.update(classification=c, reason=r, evidence=ev)

    # reachability: from the overworld through groups that are not separate.
    # pass 1 traverses only undecided groups (definite places); pass 2 also
    # traverses uncertain groups, so anything only reachable through an
    # uncertain group inherits "uncertain".
    def bfs(allowed):
        reach = {ow_id: 0}
        frontier = [ow_id]
        while frontier:
            nxt = []
            for src in frontier:
                for e in cross:
                    if e["source_group"] != src:
                        continue
                    t = e["target_group"]
                    if t in reach or G[t]["classification"] not in allowed:
                        continue
                    reach[t] = reach[src] + 1
                    nxt.append(t)
            frontier = sorted(set(nxt))
        return reach

    reach_definite = bfs({None})
    reach = bfs({None, "uncertain"})
    for g in groups:
        g["reach_depth"] = reach.get(g["id"])
        if g["classification"] is None:
            if g["id"] in reach_definite:
                g["classification"], g["reason"] = "place", "place"
                via = sorted({e["source_group"] for e in g["entrances"] if e["source_group"] in reach_definite
                              and reach_definite[e["source_group"]] < reach_definite[g["id"]]})
                g["evidence"].append("physical place reachable from the overworld via exits (depth {}; "
                                     "entered from {})".format(reach_definite[g["id"]],
                                                               ["overworld" if v == ow_id else v for v in via]))
            elif g["id"] in reach:
                g["classification"], g["reason"] = "uncertain", "uncertain"
                via = sorted({e["source_group"] for e in g["entrances"] if e["source_group"] in reach})
                g["evidence"].append("physical, but only reachable through uncertain group(s) {}".format(via))
            else:
                g["classification"], g["reason"] = "separate", "unreachable"
                srcs = sorted({e["source_group"] for e in g["entrances"]})
                g["evidence"].append("not reachable from the overworld through exit objects{}".format(
                    "; only entered from {}".format(srcs) if srcs else "; no exit object leads here at all"))
                if g["script_refs"]:
                    g["evidence"].append("referenced by scripts: {}".format(sorted({r["file"] for r in g["script_refs"]})))
    # treat everlink-like script-only hubs: region text says location unknown
    regions_text = (content / "maps" / "regions.reg").read_text(encoding="utf-8", errors="replace")
    region_msgs = {}
    for blk in re.finditer(r"^region (\S+)\n(.*?)^end$", regions_text, re.S | re.M):
        region_msgs[blk.group(1)] = blk.group(2)
    for g in groups:
        if g["reason"] == "unreachable":
            regs = {maps[m].header.get("region") for m in g["maps"]}
            for rg in sorted(regs):
                txt = region_msgs.get(rg, "")
                if "No one knows where" in txt:
                    g["reason"] = "other_world"
                    g["evidence"].append("region '{}' description: {!r}; only entered by scripts "
                                         "(python/houses/exit.py via House.py)".format(
                                             rg, re.search(r"msg\n(.*?)\nendmsg", txt, re.S).group(1)))

    # ---------------------------------------------------------- placement
    class S:  # mutable state of the current placement run
        placed: dict = {}
        occ = None

    def local_tile(gid, m, x, y):
        ox, oy, oz = G[gid]["layout"]["offsets"][m]
        return ox + x, oy + y, oz

    def plane_tile(gid, m, x, y):
        lx, ly, lz = local_tile(gid, m, x, y)
        if gid == ow_id:
            return lx, ly, lz
        p = S.placed.get(gid)
        if p is None:
            return None
        return lx + p["dx"], ly + p["dy"], lz + p["L"]

    def member_rects(gid, dx, dy, L):
        out = []
        for m, (ox, oy, oz) in sorted(G[gid]["layout"]["offsets"].items()):
            bx, by = ox + dx, oy + dy
            tiles = [(bx + tx, by + ty) for (tx, ty) in sorted(maps[m].occupied)] if S.occ.mode == "tiles" else ()
            out.append((bx, by, bx + maps[m].width, by + maps[m].height, oz + L, tiles))
        return out

    def orientation(g):
        """below/above from exit semantics, preferring connections to upstream groups."""
        depth = g.get("reach_depth")

        def votes_for(filter_fn):
            v = collections.Counter()
            for e in g["entrances"]:
                if filter_fn(e["source_group"]):
                    if e["vdir"] == "down":
                        v["below"] += 1
                    elif e["vdir"] == "up":
                        v["above"] += 1
            for e in g["exits_out"]:
                if filter_fn(e["target_group"]):
                    if e["vdir"] == "up":
                        v["below"] += 1
                    elif e["vdir"] == "down":
                        v["above"] += 1
            return v

        def upstream(other):
            d = G[other].get("reach_depth") if other != ow_id else 0
            return d is not None and depth is not None and d < depth

        why = []
        votes = votes_for(upstream)
        if votes:
            why.append("exit semantics with upstream groups/overworld: {}".format(dict(sorted(votes.items()))))
        else:
            votes = votes_for(lambda other: other == ow_id or G[other]["classification"] in ("place", "uncertain"))
            if votes:
                why.append("exit semantics with connected groups: {}".format(dict(sorted(votes.items()))))
        text = (g["id"] + " " + " ".join(sorted({maps[m].header.get("name", "") for m in g["maps"]}))).casefold()
        if not votes or votes["above"] == votes["below"]:
            if any(w in text for w in ABOVE_WORDS):
                why.append("name keyword (above)")
                return "above", why
            if any(w in text for w in BELOW_WORDS):
                why.append("name keyword (below)")
                return "below", why
        if votes["above"] > votes["below"]:
            return "above", why
        if votes["below"] > votes["above"]:
            return "below", why
        why.append("no decisive evidence; default dungeon-like below")
        return "below", why

    def connections_to_placed(gid):
        """Exits between gid and already placed things (overworld or groups)."""
        conns = []
        for e in G[gid]["entrances"]:
            s = e["source_group"]
            if s == ow_id or s in S.placed:
                conns.append(("in", e))
        for e in G[gid]["exits_out"]:
            t = e["target_group"]
            if t == ow_id or t in S.placed:
                conns.append(("out", e))
        return conns

    def eval_conn(kind, e, gid, dx, dy, L):
        """Return (plane_src, plane_dst) for a connection given a candidate placement of gid."""
        if kind == "in":
            src = plane_tile(e["source_group"], e["source_map"], e["source_x"], e["source_y"])
            lx, ly, lz = local_tile(gid, e["resolved_map"], e["resolved_x"], e["resolved_y"])
            dst = (lx + dx, ly + dy, lz + L)
        else:
            lx, ly, lz = local_tile(gid, e["source_map"], e["source_x"], e["source_y"])
            src = (lx + dx, ly + dy, lz + L)
            dst = plane_tile(e["target_group"], e["resolved_map"], e["resolved_x"], e["resolved_y"])
        return src, dst

    def is_stairs(src, dst):
        return abs(src[2] - dst[2]) == 1 and max(abs(src[0] - dst[0]), abs(src[1] - dst[1])) <= STAIR_TOLERANCE

    def layer_bounds(gid, conns):
        lo, hi = -10 ** 9, 10 ** 9
        reasons = []
        for kind, e in conns:
            if e["vdir"] == "none":
                continue
            if kind == "in":
                sl = plane_tile(e["source_group"], e["source_map"], e["source_x"], e["source_y"])[2]
                tl = local_tile(gid, e["resolved_map"], e["resolved_x"], e["resolved_y"])[2]
                if e["vdir"] == "down":
                    hi = min(hi, sl - 1 - tl)
                else:
                    lo = max(lo, sl + 1 - tl)
            else:
                ql = local_tile(gid, e["source_map"], e["source_x"], e["source_y"])[2]
                pl = plane_tile(e["target_group"], e["resolved_map"], e["resolved_x"], e["resolved_y"])[2]
                if e["vdir"] == "up":
                    hi = min(hi, pl - 1 - ql)
                else:
                    lo = max(lo, pl + 1 - ql)
        return lo, hi

    def site_key(kind, e):
        return (kind, e["source_map"], e["source_x"], e["source_y"], e["resolved_map"])

    def place_group(gid, tentative=False, forced=None):
        g = G[gid]
        conns = connections_to_placed(gid)
        orient, why = orientation(g)
        lo, hi = layer_bounds(gid, conns)
        constraint_note = None
        if lo > hi:
            constraint_note = "vertical constraints from exit semantics conflict (need L>={} and L<={}); " \
                              "used the main-entrance constraint only".format(lo, hi)
            lo, hi = -10 ** 9, 10 ** 9
        candidates = []
        if forced is not None:
            candidates.append(dict(forced, conn=None))
        for kind, e in conns:
            if kind == "in":
                src = plane_tile(e["source_group"], e["source_map"], e["source_x"], e["source_y"])
                lx, ly, lz = local_tile(gid, e["resolved_map"], e["resolved_x"], e["resolved_y"])
                d = e["vdir"] if e["vdir"] != "none" else ("down" if orient == "below" else "up")
                L = src[2] - 1 - lz if d == "down" else src[2] + 1 - lz
            else:
                dst = plane_tile(e["target_group"], e["resolved_map"], e["resolved_x"], e["resolved_y"])
                lx, ly, lz = local_tile(gid, e["source_map"], e["source_x"], e["source_y"])
                src = dst
                d = e["vdir"] if e["vdir"] != "none" else ("up" if orient == "below" else "down")
                L = dst[2] - 1 - lz if d == "up" else dst[2] + 1 - lz
            L = max(lo, min(hi, L))
            candidates.append({"dx": src[0] - lx, "dy": src[1] - ly, "L": L, "conn": (kind, e)})
        if not candidates:
            return None

        def score(c):
            sites = set()
            for kind, e in conns:
                s, t = eval_conn(kind, e, gid, c["dx"], c["dy"], c["L"])
                if is_stairs(s, t):
                    sites.add(site_key(kind, e))
            kind_e = c["conn"]
            ow_src = 1 if kind_e and kind_e[0] == "in" and kind_e[1]["source_group"] == ow_id else 0
            entr = 1 if kind_e and kind_e[0] == "in" else 0
            stairs_like = 1 if kind_e and kind_e[1]["vdir"] != "none" else 0
            key = (kind_e[1]["source_map"], kind_e[1]["source_x"], kind_e[1]["source_y"]) if kind_e else ("",)
            return (-len(sites), -entr, -ow_src, -stairs_like, key, c["dx"], c["dy"], c["L"])

        if forced is None:
            candidates.sort(key=score)
        best = candidates[0]
        ideal = dict(dx=best["dx"], dy=best["dy"], L=best["L"])
        step = -1 if orient == "below" else 1
        tried = []
        chosen = None
        for r in range(0, MAX_SHIFT + 1):
            for k in range(0, MAX_EXTRA_LAYERS + 1):
                L = ideal["L"] + step * k
                for sx, sy in ring(r):
                    rects = member_rects(gid, ideal["dx"] + sx, ideal["dy"] + sy, L)
                    hits = S.occ.collisions(rects)
                    if not hits:
                        chosen = dict(dx=ideal["dx"] + sx, dy=ideal["dy"] + sy, L=L, shift=[sx, sy], extra_layers=k)
                        break
                    if r == 0:
                        tried.append({"layer_offset": L, "shift": [sx, sy], "collides_with": hits[0][1],
                                      "collision_kind": hits[0][0], "plane_layer": hits[0][2]})
                if chosen:
                    break
            if chosen:
                break
        if chosen is None:
            return {"failed": True, "ideal": ideal}
        S.placed[gid] = chosen
        S.occ.add(member_rects(gid, chosen["dx"], chosen["dy"], chosen["L"]), gid)
        main = best["conn"]
        return {
            "orientation": orient, "orientation_evidence": why,
            "vertical_bounds": [None if lo == -10 ** 9 else lo, None if hi == 10 ** 9 else hi],
            "constraint_note": constraint_note,
            "main_connection": None if main is None else {
                "kind": main[0], "source_map": main[1]["source_map"],
                "source_tile": [main[1]["source_x"], main[1]["source_y"]],
                "target_map": main[1]["resolved_map"], "target_tile": [main[1]["resolved_x"], main[1]["resolved_y"]],
                "arch": main[1]["arch"], "vdir": main[1]["vdir"]},
            "ideal": ideal, "chosen": chosen, "conflicts_resolved": tried,
            "tentative": tentative,
        }

    def run_placement(mode):
        S.placed = {}
        S.occ = Occupancy(maps, G[ow_id]["maps"], mode, MARGIN)
        placements = {}
        to_place = [g["id"] for g in groups if g["classification"] == "place"]
        tentative_ids = [g["id"] for g in groups if g["classification"] == "uncertain"]

        def above_below_blockers(gid, pool):
            """Unplaced groups in `pool` that must be placed before gid (vertical precedence)."""
            orient, _ = orientation(G[gid])
            blockers = set()
            for e in G[gid]["entrances"]:
                s = e["source_group"]
                if s in pool and s not in S.placed and s != gid:
                    if (orient == "below" and e["vdir"] == "down") or (orient == "above" and e["vdir"] == "up"):
                        blockers.add(s)
            for e in G[gid]["exits_out"]:
                t = e["target_group"]
                if t in pool and t not in S.placed and t != gid:
                    if (orient == "below" and e["vdir"] == "up") or (orient == "above" and e["vdir"] == "down"):
                        blockers.add(t)
            return blockers

        def run_pool(pool, tentative):
            remaining = sorted(pool)
            order = []
            while remaining:
                ready = [gid for gid in remaining if connections_to_placed(gid)]
                if not ready:
                    break
                ok = [gid for gid in ready if not above_below_blockers(gid, set(remaining))]
                pick_from = ok or ready
                pick = min(pick_from, key=lambda gid: (G[gid]["reach_depth"] if G[gid]["reach_depth"] is not None else 99, gid))
                res = place_group(pick, tentative=tentative)
                placements[pick] = res
                order.append(pick)
                remaining.remove(pick)
            return order, remaining

        order1, left1 = run_pool(to_place, False)
        order2, left2 = run_pool(tentative_ids, True)
        order3 = []
        for gid, (anchor, dl, why) in sorted(TENTATIVE_ALIGN.items()):
            if gid in G and gid not in S.placed and anchor in S.placed:
                a = S.placed[anchor]
                res = place_group(gid, tentative=True, forced={"dx": a["dx"], "dy": a["dy"], "L": a["L"] + dl})
                if res:
                    res["tentative_reason"] = why
                    placements[gid] = res
                    order3.append(gid)
        return {"placed": dict(S.placed), "placements": placements, "order": order1, "left": left1,
                "tentative_order": order2 + order3, "tentative_left": left2}

    alt = run_placement("tiles")
    prim = run_placement("rect")
    placements = prim["placements"]
    order1, left1, left2 = prim["order"], prim["left"], prim["tentative_left"]
    order2, order3 = prim["tentative_order"], []

    # ------------------------------------------------ evaluate all crossings
    def eval_exit(e):
        s = plane_tile(e["source_group"], e["source_map"], e["source_x"], e["source_y"])
        t = plane_tile(e["target_group"], e["resolved_map"], e["resolved_x"], e["resolved_y"])
        if s is None or t is None:
            return None
        dist = max(abs(s[0] - t[0]), abs(s[1] - t[1]))
        dl = t[2] - s[2]
        if s[2] == t[2] and dist == 0:
            kind = "same-tile"
        elif abs(dl) == 1 and dist <= STAIR_TOLERANCE:
            kind = "stairs"
        elif dist <= STAIR_TOLERANCE:
            kind = "vertically-aligned-multi-layer"
        else:
            kind = "portal"
        return {"plane_source": list(s), "plane_target": list(t), "xy_distance": dist, "layer_delta": dl,
                "becomes": kind}

    def exit_record(e, with_eval=True):
        r = {
            "source_map": e["source_map"], "source_tile": [e["source_x"], e["source_y"]],
            "target_map": e["resolved_map"], "target_tile": [e["resolved_x"], e["resolved_y"]],
            "arch": e["arch"], "form": e["form"], "vertical": e["vdir"],
            "source_group": e["source_group"], "target_group": e["target_group"],
        }
        if e["resolved_map"] != e.get("target_map"):
            r["authored_target"] = [e.get("target_map"), e.get("target_x"), e.get("target_y")]
        if e.get("target_default_entry"):
            r["target_is_map_default_entry"] = True
        if e["per_player"]:
            r["per_player_last_eat_1"] = True
        if e["event_scripts"]:
            r["event_scripts"] = e["event_scripts"]
        if e.get("dir_inferred_from"):
            r["vertical_inferred_from"] = e["dir_inferred_from"]
        if with_eval:
            ev = eval_exit(e)
            if ev:
                r.update(ev)
        if e["source_group"] == ow_id:
            p = plane_tile(ow_id, e["source_map"], e["source_x"], e["source_y"])
            r["overworld_plane_source"] = list(p)
        if e["target_group"] == ow_id:
            p = plane_tile(ow_id, e["resolved_map"], e["resolved_x"], e["resolved_y"])
            r["overworld_plane_target"] = list(p)
        return r

    def becomes_stats(placed_dict):
        """Per group: counts of crossing exits by 'becomes' for a given placement."""
        saved = S.placed
        S.placed = placed_dict
        stats = collections.defaultdict(collections.Counter)
        total = collections.Counter()
        for e in cross:
            ev = eval_exit(e)
            if ev is None:
                continue
            total[ev["becomes"]] += 1
            for gid in {e["source_group"], e["target_group"]} - {ow_id}:
                stats[gid][ev["becomes"]] += 1
        S.placed = saved
        stats["__total__"] = total
        return stats

    stats_rect = becomes_stats(prim["placed"])
    stats_tiles = becomes_stats(alt["placed"])

    # ---------------------------------------------------------- data problems
    problems = {"tile_links": link_problems, "layouts": [], "exits": []}
    for g in groups:
        for p in g["layout"]["problems"]:
            problems["layouts"].append(dict(p, group=g["id"]))
    working_tiles = {(e["source_map"], e["source_x"], e["source_y"]) for e in exits if e.get("resolved_map")}
    for e in exits:
        base = {"source_map": e["source_map"], "source_tile": [e["source_x"], e["source_y"]],
                "arch": e["arch"], "line": e["line"]}
        if e["form"] in ("tiled-unresolved", "unresolved") and not e["event_scripts"]:
            if (e["source_map"], e["source_x"], e["source_y"]) in working_tiles:
                base["severity"] = "info"
                base["note"] = "another exit object on the same tile has a destination (decorative duplicate)"
            elif e.get("explicit_last_heal_0"):
                base["severity"] = "info"
                base["note"] = "explicitly disabled with last_heal 0 (decorative)"
            else:
                base["severity"] = "problem"
        rec = maps[e["source_map"]]
        if not (0 <= e["source_x"] < rec.width and 0 <= e["source_y"] < rec.height):
            problems["exits"].append(dict(base, kind="exit-outside-map-bounds"))
        if e["form"] == "tiled-unresolved":
            problems["exits"].append(dict(base, kind="tiled-exit-without-link",
                                          detail="last_heal {} ({}) but the map has no such tile link; the exit "
                                                 "leads nowhere".format(e["tiled_slot"], md.SLOT_NAMES[e["tiled_slot"]])))
        elif e["form"] == "unresolved":
            if e["event_scripts"]:
                continue  # destination supplied by a script; not a data problem
            problems["exits"].append(dict(base, kind="exit-without-destination",
                                          detail="no slaying, no tiled slot, hp/sp -1 and no event script"))
        elif e.get("problem"):
            problems["exits"].append(dict(base, kind=e["problem"], target=[e.get("target_map"), e.get("target_x"),
                                                                           e.get("target_y")]))
        elif e["form"] == "dynamic":
            problems["exits"].append(dict(base, kind="dynamic-random-map-exit", detail=e.get("raw_target"),
                                          severity="info"))
    # one-way crossings (informational)
    oneway = []
    for g in groups:
        if g["id"] == ow_id:
            continue
        if g["entrances"] and not g["exits_out"]:
            oneway.append({"group": g["id"], "kind": "no-exit-back-out"})
        if g["exits_out"] and not g["entrances"]:
            oneway.append({"group": g["id"], "kind": "no-exit-object-leads-in",
                           "script_or_dialog_refs": sorted({r["file"] for r in g["script_refs"]})})
    problems["connectivity"] = oneway

    # -------------------------------------------------------------- output
    def bbox_layers(gid, dx=0, dy=0, L=0):
        bl = {}
        for m, (ox, oy, oz) in G[gid]["layout"]["offsets"].items():
            z = oz + L
            b = bl.setdefault(z, [10 ** 9, 10 ** 9, -10 ** 9, -10 ** 9])
            b[0] = min(b[0], ox + dx)
            b[1] = min(b[1], oy + dy)
            b[2] = max(b[2], ox + dx + maps[m].width - 1)
            b[3] = max(b[3], oy + dy + maps[m].height - 1)
        return {str(z): {"x_min": b[0], "y_min": b[1], "x_max": b[2], "y_max": b[3],
                         "maps": sum(1 for m, o in G[gid]["layout"]["offsets"].items() if o[2] + L == z)}
                for z, b in sorted(bl.items())}

    groups_out = []
    for g in groups:
        gid = g["id"]
        rec = {
            "id": gid,
            "map_count": len(g["maps"]),
            "classification": g["classification"],
            "reason": g["reason"],
            "evidence": g["evidence"],
            "reach_depth_from_overworld": g.get("reach_depth"),
        }
        if gid == ow_id:
            rec["layers"] = {str(z): n for z, n in sorted(collections.Counter(
                maps[m].coords[2] for m in g["maps"]).items())}
            rec["plane_formula"] = "tile (x,y) of world_X_Y_Z -> plane (24*X+x, 24*Y+y) layer Z"
            rec["bounding_boxes"] = bbox_layers(gid)
            rec["maps"] = "{} maps (omitted; /shattered_islands/world_X_Y[_Z])".format(len(g["maps"]))
            cbl = collections.defaultdict(list)
            for m in g["maps"]:
                X, Y, Z = maps[m].coords
                cbl[Z].append([X, Y])
            rec["cells_by_layer"] = {str(z): sorted(v) for z, v in sorted(cbl.items())}
            groups_out.append(rec)
            continue
        rec["layout_root"] = g["layout"]["root"]
        rec["maps"] = [{"path": m, "width": maps[m].width, "height": maps[m].height,
                        "name": maps[m].header.get("name"), "region": maps[m].header.get("region"),
                        "local_offset": list(g["layout"]["offsets"][m]),
                        "tile_links": {md.SLOT_NAMES[s]: t for s, t in sorted(maps[m].links.items())}}
                       for m in g["maps"]]
        rec["local_bounding_boxes"] = bbox_layers(gid)
        rec["layout_problems"] = g["layout"]["problems"]
        rec["entrances_from_overworld"] = [exit_record(e) for e in g["entrances"] if e["source_group"] == ow_id]
        rec["exits_to_overworld"] = [exit_record(e) for e in g["exits_out"] if e["target_group"] == ow_id]
        rec["entrances_from_other_groups"] = [exit_record(e) for e in g["entrances"] if e["source_group"] != ow_id]
        rec["exits_to_other_groups"] = [exit_record(e) for e in g["exits_out"] if e["target_group"] != ow_id]
        if g["script_refs"]:
            rec["script_and_dialog_references"] = g["script_refs"]
        if g["redirect_refs"]:
            rec["script_redirects"] = g["redirect_refs"]
        p = placements.get(gid)
        if p and not p.get("failed"):
            c = p["chosen"]
            rec["placement"] = {
                "status": "tentative" if p["tentative"] else "proposed",
                "layer_offset": c["L"], "x_offset": c["dx"], "y_offset": c["dy"],
                "plane_tile_formula": "plane = local_offset(map) + (x,y) + ({}, {}); plane layer = local layer + ({})".format(
                    c["dx"], c["dy"], c["L"]),
                "plane_bounding_boxes": bbox_layers(gid, c["dx"], c["dy"], c["L"]),
                "member_origins": {m: [o[0] + c["dx"], o[1] + c["dy"], o[2] + c["L"]]
                                   for m, o in sorted(g["layout"]["offsets"].items())},
                "orientation": p["orientation"], "orientation_evidence": p["orientation_evidence"],
                "vertical_bounds_from_exit_semantics": p["vertical_bounds"],
                "constraint_note": p["constraint_note"],
                "main_connection": p["main_connection"],
                "ideal": p["ideal"], "shift_from_ideal": c["shift"], "extra_layers_from_ideal": c["extra_layers"],
                "conflicts_resolved": p["conflicts_resolved"],
            }
            if p.get("tentative_reason"):
                rec["placement"]["tentative_reason"] = p["tentative_reason"]
        elif p and p.get("failed"):
            rec["placement"] = {"status": "failed", "ideal": p["ideal"]}
        if "placement" in rec and rec["placement"].get("status") != "failed":
            rec["placement"]["crossing_exits_after_placement"] = dict(sorted(stats_rect[gid].items()))
            ap = alt["placements"].get(gid)
            if ap and not ap.get("failed"):
                c2 = ap["chosen"]
                rec["placement"]["alternative_tile_footprint"] = {
                    "layer_offset": c2["L"], "x_offset": c2["dx"], "y_offset": c2["dy"],
                    "shift_from_ideal": c2["shift"], "extra_layers_from_ideal": c2["extra_layers"],
                    "ideal": ap["ideal"],
                    "conflicts_resolved": ap["conflicts_resolved"],
                    "crossing_exits_after_placement": dict(sorted(stats_tiles[gid].items())),
                }
        groups_out.append(rec)

    counts = collections.Counter()
    for g in groups:
        if g["id"] == ow_id:
            continue
        counts["{}:{}".format(g["classification"], g["reason"])] += 1
    plan = {
        "schema": "atrinik-overworld-placement-plan/1",
        "generated_by": "scripts/build_plan.py",
        "content_root": str(content),
        "content_git_head": git_head(content),
        "content_maps_digest_sha256": tree_digest(maps, 0, content),
        "settings": {"margin_tiles": MARGIN, "max_extra_layers": MAX_EXTRA_LAYERS, "max_shift_tiles": MAX_SHIFT,
                     "stair_tolerance_tiles": STAIR_TOLERANCE, "overworld_map_size": 24,
                     "excluded_trees": list(md.EXCLUDED_TOP)},
        "conventions": {
            "plane": "overworld plane; layer 0 surface, negative below, positive above",
            "local_layout": "each member map gets a group-local origin (x, y, layer) by BFS over tile links from "
                            "layout_root (root origin = filename coordinates*size when present, else 0,0,0); N/S/E/W/"
                            "diagonals offset by neighbour/own width/height exactly as get_map_from_coord(); "
                            "up/down = layer +1/-1",
            "placement": "plane tile = local origin + tile + (x_offset, y_offset); plane layer = local layer + "
                         "layer_offset",
            "stairs": "an exit 'becomes stairs' when after placement source and destination differ by exactly one "
                      "layer and by <= {} tile(s) horizontally".format(STAIR_TOLERANCE),
        },
        "summary": {"maps_total": len(maps), "groups_total": len(groups), "non_overworld_groups": len(groups) - 1,
                    "by_classification": dict(sorted(counts.items())),
                    "exit_objects": len(exits), "inventory_exit_objects_ignored": inventory_exits,
                    "crossing_exit_objects_after_placement": {
                        "rect_footprint_primary": dict(sorted(stats_rect["__total__"].items())),
                        "tile_footprint_alternative": dict(sorted(stats_tiles["__total__"].items())),
                    },
                    "placement_order": order1, "tentative_placement_order": order2 + order3,
                    "unplaced_place_groups": left1, "unplaced_uncertain_groups": left2},
        "start_map": start_map,
        "groups": groups_out,
        "data_problems": problems,
    }
    import verify_plan  # noqa: E402
    plan["verification"] = verify_plan.verify(json.loads(json.dumps(plan)))
    out.mkdir(parents=True, exist_ok=True)
    (out / "placement-plan.json").write_text(json.dumps(plan, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return plan, maps, G, ow_id


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--content", type=Path, default=DEFAULT_CONTENT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args(argv)
    plan, maps, G, ow_id = build(args.content.resolve(), args.out.resolve())
    import report  # noqa: E402
    report.write_report(plan, args.out.resolve())
    if not args.no_render:
        import render  # noqa: E402
        render.render_layers(plan, maps, G, ow_id, args.out.resolve())
    print("wrote", args.out / "placement-plan.json")
    print(json.dumps(plan["summary"], indent=1))
    print("verification:", "OK" if plan["verification"]["ok"] else plan["verification"]["errors"][:10])
    return 0 if plan["verification"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
