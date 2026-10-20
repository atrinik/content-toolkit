#!/usr/bin/env python3
"""Build the Atrinik overworld-plane placement plan.

Read-only against the content checkout, which must be clean under arch/ and
maps/. Relayers the overworld (owner decision 2026-10-18: fix the conflicts,
then relayer), places the groups on the result, builds stairwells (aligned,
sloped and relayering ones) and wires dangling exits. Applies the owner
decisions of 2026-10-19 (the OWNER_* constants) and refuses to publish when one
no longer matches what it decides. Generates placement-plan.json, REPORT.md,
layer_<n>[_zoom].png and relayer_<n>.png in a staging directory, verifies the
staged plan, and only then replaces the artifacts in the output directory.
Standard library only.

    python3 -I scripts/build_plan.py [--content DIR] [--out DIR] [--allow-dirty] [--check]

--check regenerates in a temporary directory and fails unless every artifact
in --out is byte-identical; it writes nothing.

Everything is deterministic: inputs are iterated in sorted order and every
tie is broken lexicographically.
"""

from __future__ import annotations

import argparse
import collections
import filecmp
import functools
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis as an  # noqa: E402
import footprint as fp  # noqa: E402
import mapdata as md  # noqa: E402
import relayer  # noqa: E402

DEFAULT_CONTENT = Path("/home/zoey/dev/atrinik/content")
DEFAULT_OUT = Path(__file__).resolve().parents[1]

# ----------------------------------------------------------------- settings
# View radius: the most tiles a classic server sends around the player in any
# direction. MAP_CLIENT_X/Y is 32 (classic server/src/include/config.h:60-61),
# the server accepts any requested size up to that (socket/request.c:154-161)
# and sends x in [x - mapx_2, x + (mapx + 1) / 2 - 1] (socket/request.c:
# 1305-1312), so 16 tiles. The shipped client asks for at most 21 tiles
# (MAP_LOOK_SIZE_MAX 17 + 2 * MAP_RENDER_OVERSCAN, client/src/include/map.h:
# 68-83), i.e. radius 10; the server bound is used so no client can see across.
VIEW_RADIUS = 16
CLIENT_VIEW_RADIUS = 10  # shipped client's largest view, reported as a sensitivity run
CONTACT_MARGIN = 1     # related (exit-connected) areas: no view gap, but no touching
MAX_EXTRA_LAYERS = 6   # how many layers past the ideal layer to try
MAX_SHIFT = 96         # max Chebyshev horizontal shift (tiles) to try
STAIR_TOLERANCE = 1    # Chebyshev distance allowed between consecutive stair tiles
STAIRWELL_MAX_BEND = 2 # how far a stairwell tile may leave its column to find a free spot
LAYER_HEIGHT_Z = 50    # owner rule: floor z 50 ~ one map layer (see relayer.py)
CLIENT_LEVEL_PIXEL_HEIGHT = 46  # classic client MAP_LEVEL_PIXEL_HEIGHT (gui/widgets/map.c:1307)

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

# Settled owner decisions (2026-10): these groups are placed in the world.
OWNER_PLACE = {
    "/shattered_islands/eld_woods_island/clearhaven/mine/mine_unique":
        "owner decision 1 (settled): place the Clearhaven 'unique' mines in the world, not as separate planes",
    "/shattered_islands/eld_woods_island/clearhaven/mine/mine_unique_a":
        "owner decision 1 (settled): place the Clearhaven 'unique' mines in the world, not as separate planes",
    "/shattered_islands/eld_woods_island/clearhaven/mine/mine_unique_tipper":
        "owner decision 1 (settled): place the Clearhaven 'unique' mines in the world, not as separate planes",
    "/shattered_islands/deserted_tutorial_island/ship_lower_deck":
        "owner decision 2 (settled): the new-character start map is placed under world_-7_76, where the "
        "existing stairs line up",
}
# Owner decision 2026-10-18: the six Greyton jail cell ladders (each on a bed
# tile, five of them over water) stay bunk-bed decoration. They are not wired;
# the migration keeps their look as objects without an exit.
JAIL = "/shattered_islands/strakewood_island/greyton/jail/jail"
OWNER_KEEP_AS_DECORATION = {
    (JAIL, (x, y), "ladder_up"): "owner decision 2026-10-18: keep as bunk-bed decoration, an object without an exit"
    for (x, y) in ((2, 1), (2, 7), (2, 13), (13, 1), (13, 7), (13, 13))
}

# ------------------------------------------------ owner decisions 2026-10-19
# The owner's answers to the six questions version 2 left open. Each is an
# input here and is recorded with its date in the JSON settings. Decisions 1-3
# change the plan; decisions 4-6 confirm what the rules derived. A decision is
# bound to the evidence it was made on (the record itself, or its premise(): a
# SHA-256 of the record's canonical JSON); the generator refuses to publish when
# that evidence changed (the owner must be asked again; the refusal names the
# new premise), and verify_plan.py re-checks the binding in the JSON.
DECIDED_2026_10_19 = "2026-10-19"
ROCKFORGE = "/shattered_islands/strakewood_island/rockforge/"
# Decision 1: these exits stay portals where their stairwell is blocked; no
# stairwell is forced through the content in its way. Each is one exit object
# (source map, line, tile, archetype); its premise covers portal_evidence(): the
# blocked site record (owners, ends, exits, column occupants) and the records of
# its exits (targets included).
OWNER_PORTALS = {
    ("/shattered_islands/world_7_66_-1", 938, (13, 15), "hole"): {
        "premise": "ad3f43acaee60f6c1459523c8af820f3fdbe384f780a3468ef8f0fab23e0cf20",
        "decision": "Zechna's first hole stays a portal: no stairwell is built through zechna_temple_1_1, the floor "
                    "in its column"},
}
# Decision 2: a stairwell whose exits belong to no stair family (STAIR_FAMILIES)
# shows, on its intermediate tiles, the archetype chosen for the first of its
# exit archetypes listed here (down-going exits first, each set sorted). Each
# fallback is bound to the stairwells the owner chose it for: (id as the owner
# saw it, stairwell_premise()). An id is positional; the premise is the identity.
STAIRWELL_APPEARANCE_FALLBACKS = {
    "invis_exit": ("stairs_stone_down", (("SW6", "f2adfb3c19b1fb4bee44f296e5071e1ddb6c7390cd0e8539bec6c4f5cb0f55f4"),)),
    "well_bucket": ("ladder_down", (("SR18", "7c7394a4451176950e74cbf647c9038d0b8ba5d30d6aa0668e9db95ec5733adc"),)),
    "well_stone": ("ladder_down", (("SR17", "696b3636a020a3d31e0e6af411d9429923edcde41d65a6da37560788f1b0c5a7"),)),
}
# The evidence a stairwell's premise covers: its kind, owners, direction and every exit it replaces (tiles,
# targets, archetypes, plane ends).
STAIRWELL_PREMISE_FIELDS = ("kind", "group", "owner", "other", "connects", "direction", "replaces")
STAIRWELL_APPEARANCE_DECISION = (
    "stairwells whose exits have no stair archetype: the wells (SR17, SR18) show ladder_down and the invisible "
    "exit of #17 dark_cave (SW6) shows stairs_stone_down on their intermediate tiles")
# Decision 3: groups without an exit connection placed at a fixed layer from an
# anchor group, at the anchor's x/y (no search: the spot is the decision). The
# premise covers alignment_evidence() of the evidence groups the reason rests
# on: their sheet layouts and the exit records between them.
OWNER_ALIGN = {
    ROCKFORGE + "rockforge_c": {
        "anchor": ROCKFORGE + "rockforge_b", "layer_delta": -1,
        "evidence_groups": (ROCKFORGE + "rockforge_a", ROCKFORGE + "rockforge_b", ROCKFORGE + "rockforge_c"),
        "premise": "b5e862b1b9db3eaa0363e85151637719769b24f8f3f5aba55654db8474b86ccb",
        "why": "owner decision 2026-10-19: the Rockforge mines ('Rockforge: Mines') are placed on the layer directly "
               "under rockforge_b at the same x/y (rockforge_a/_b/_c share the same 5x5 sheet naming 0101..ab05, and "
               "the rockforge_a<->rockforge_b stairs connect identical local coordinates)"},
}
ALIGN_LAYOUT_FIELDS = ("path", "width", "height", "local_offset", "tile_links")  # per member map, in group order
# Decision 7 (2026-10-19, the owner's answer to the question this plan raised):
# these placed groups stay placed although no member map holds an object, so
# they occupy no tile. By the owner's reasoning the decision stays valid when
# content is added: the footprint then appears when the plan is regenerated,
# and the spot is fixed (decision 3), so a collision makes the generator refuse.
# Any other placed group without a footprint needs an owner decision first.
OWNER_NO_FOOTPRINT = {
    ROCKFORGE + "rockforge_c":
        "keep the Rockforge mines placed with no footprint: their 25 maps are empty headers, a footprint appears "
        "automatically when content is added and the plan is regenerated, and the spot is fixed, so a later "
        "collision makes the generator refuse",
}
# Decision 4: the orientation derived for these groups is confirmed, with the
# evidence it came from: a name keyword, no exit with a direction.
# (orientation, derived evidence, premise of the group's content: group_evidence())
KEYWORD_BELOW = ("name keyword (below)",)
OWNER_ORIENTATIONS = {
    "/shattered_islands/eld_woods_island/cave_morlarg": ("below", KEYWORD_BELOW, "927d5882eee6c9f761a0f044dd7b25623338f55ecebdaeda5658c9a83997bd3d"),
    "/shattered_islands/strakewood_island/brynknot/sewers/lab": ("below", KEYWORD_BELOW, "1b7505d0b301c9d656d628251139e1078688dccb2590d5eceb663f10683670b6"),
    "/shattered_islands/strakewood_island/dark_cave/dark_cave": ("below", KEYWORD_BELOW, "77a1804f4a83e6b4083293c1fa74e2b5893afa281bc0914bc64520fbbf46dafd"),
    "/shattered_islands/strakewood_island/giant_stronghold/giant_stronghold": ("below", KEYWORD_BELOW, "74e02789aba87bb33804b7ce5c4574786d5bb8118930b8b9b1783f7a0e0ea8a9"),
}
ORIENTATION_DECISION = "'below' confirmed for a group entered only through neutral exits"
# Decision 5: version 1's classification judgement calls are confirmed, each
# with the premise of {"evidence": the evidence the rules gave for it, "group":
# group_evidence() of its record} (its maps, exits and script references).
LOKI_DESERT = "/shattered_islands/berri_lur/temple_loki/desert_plane/"
OWNER_CLASSIFICATIONS = {
    "/shattered_islands/everlink/everlink":
        ("separate", "other_world", "be278e9eeea6d8524a0c9f962507b450958575d20694d59dc1dd74776472fb3c"),
    LOKI_DESERT + "desert_plane":
        ("separate", "other_world", "25be0f8f36831865c4d557607e1b483fb981ff5bc31ab8acf6bd6f56d1e888d9"),
    LOKI_DESERT + "entrance":
        ("separate", "other_world", "3d319765bc0e62816c33067f7c10ac845f05802a5f5e602dd9c0463f5e6ad252"),
    LOKI_DESERT + "passage":
        ("separate", "other_world", "1e5bc4e079a004ba0c4c4969ca55ce1d8caa784f6e9922fe382fcec539569ef2"),
    LOKI_DESERT + "unusual_garden":
        ("separate", "other_world", "7bf7f6471a24f33c0a01463f3f058f97533f88d2eb164d2fde80473c0c09125b"),
    "/shattered_islands/incuna/ship_lower_deck":
        ("separate", "ship", "1f2cc9c8cb181ba6308e2fb69db18f6b936e0afedad44941cbb3a822e9c77d2f"),
    "/shattered_islands/incuna/ship_lower_deck_to_incuna":
        ("separate", "ship", "d74a65d957119c1216d6a783b634e504f42a0165439c56e39eabb1b32117f00b"),
    "/shattered_islands/incuna/ship_lower_deck_to_strakewood":
        ("separate", "ship", "21ee6f53b9f6c430728cfae798ef365ea15d379f03c991dcb430f536b9c2aa53"),
    "/hall_of_dms": ("separate", "system", "f62856ef0a335de5becf7234da9ea37232a19522b6df6a98133aad8cc796a224"),
}
CLASSIFICATION_DECISION = ("version 1 judgement call confirmed (Everlink and the Temple of Loki desert-plane chain "
                           "are other_world, the Incuna lower decks are ships, hall_of_dms is a system map)")
# Decision 6: the relayering judgement calls are confirmed as listed, and with
# them the placements the lowered caves push one layer deeper. The new seam
# stairs keep their default archetypes.
SEAM_STAIR_ARCHETYPES = {"low": "stairs_stone_up", "high": "stairs_stone_down"}
RELAYERING_DECISION = ("relayering judgement calls confirmed: R3's cave is lowered in place; U64 and U65 move down as "
                       "whole caves, pushing ellamin and rockforge_a, _b and _c one layer deeper; the new seam stairs "
                       "use the default archetypes; the storeys U2 and U3, partly over unmoved ground, move up")
OWNER_RELAYERING_JUDGEMENT_CALLS = (
    ("storey partly over unmoved ground", "U2",
     "U2 (world_3_45_1) moves up 1 layer(s); 17 of its 87 tiles stand on ground that stays"),
    ("storey partly over unmoved ground", "U3",
     "U3 (world_3_45_1, world_3_46_1) moves up 1 layer(s); 18 of its 96 tiles stand on ground that stays"),
    ("cave resolution", "U64",
     "U64 (3411 tiles, regions R7): lower whole cave; lowering the whole cave breaks 0 stair(s) and makes 0 steep "
     "step(s); lowering only the raised regions breaks 0 and makes 14"),
    ("cave resolution", "U65",
     "U65 (2453 tiles, regions R8, R10, R12): lower whole cave; lowering the whole cave breaks 0 stair(s) and makes "
     "0 steep step(s); lowering only the raised regions breaks 0 and makes 23"),
    ("cave resolution", "R3's cave",
     "R3's cave (3047 tiles, regions R3): lower regions in place; lowering the whole cave would push 1 unit(s) below "
     "it down; lowering the whole cave breaks 22 stair(s) and makes 0 steep step(s); lowering only the raised "
     "regions breaks 18 and makes 4"),
    ("one-way stairwells", None,
     "12 (SR3, SR5, SR6, SR7, SR9, SR10, SR11, SR13, SR14, SR18, SR19, SR20) replace legacy exits that only go one "
     "way, so each has exits at one end and a landing at the other; making them two-way would add a way in that "
     "legacy does not have"),
    ("steep steps", None, "4 walkable edge(s) become a fall on the same layer"),
    ("new contacts", None, "2 tile pair(s) become neighbours on a layer"),
    ("split tiles", None,
     "200 tile(s) carry a low floor on sub-layer 0 and a raised floor on a higher sub-layer; the raised part moves, "
     "the ground part stays"),
    ("kept regions", None, "7 small raised region(s) (58 tiles) keep z as tile height"),
    ("new stairs", None,
     "46 new stair pair(s) keep the 44 cut seam piece(s) and every pocket they leave walkable; archetypes default to "
     "stairs_stone_up/down"),
)
# The premise of relayering_evidence(): the calls and every record they summarise.
OWNER_RELAYERING_PREMISE = "b98a773ceeb3cd3acb1a88967274cf0ddbdc168a977600979ccbc53dda92dce1"
# (x_offset, y_offset, layer_offset) of version 1 -> this plan, under /shattered_islands/
OWNER_PLACEMENT_CHANGES_FROM_V1 = (
    ("incuna/ellamin", (105, 1991, -4), (105, 1991, -5)),
    ("strakewood_island/rockforge/rockforge_a", (-13, 1347, -4), (-13, 1347, -5)),
    ("strakewood_island/rockforge/rockforge_b", (-13, 1347, -5), (-13, 1347, -6)),
    ("strakewood_island/rockforge/rockforge_c", (-13, 1347, -6), (-13, 1347, -7)),
)

# Placements of plan v1 (PR #44, toolkit 7ba54b0, content b9657299a6e7):
# (x_offset, y_offset, layer_offset) per placed group id under /shattered_islands/.
# Used only to report which placements this version changes.
PLAN_V1_PLACEMENTS = {
    "berri_lur/temple_loki/tol": (240, 1464, -1),
    "deserted_tutorial_island/ship_lower_deck": (-161, 1832, -1),
    "eld_woods_island/cave_morlarg": (250, 1899, -1),
    "eld_woods_island/clearhaven/mine/mine_entrance": (197, 1894, -1),
    "eld_woods_island/clearhaven/mine/mine_entrance_a": (195, 1885, -2),
    "eld_woods_island/clearhaven/mine/mine_unique": (166, 1870, -3),
    "eld_woods_island/clearhaven/mine/mine_unique_a": (153, 1890, -5),
    "eld_woods_island/clearhaven/mine/mine_unique_tipper": (192, 1894, -4),
    "eld_woods_island/clearhaven/treehome_1": (182, 1865, 1),
    "eld_woods_island/clearhaven/treehome_2": (183, 1866, 2),
    "eld_woods_island/clearhaven/treehome_3": (182, 1865, 3),
    "eld_woods_island/clearhaven/treehome_4": (181, 1864, 4),
    "eld_woods_island/hemlock_cave/hemlock_cave": (348, 1686, -1),
    "eld_woods_island/vielumin_ruins/ruins": (423, 1710, -2),
    "incuna/ellamin": (105, 1991, -4),
    "strakewood_island/brynknot/sewers/lab": (76, 1583, -3),
    "strakewood_island/dark_cave/dark_cave": (77, 1704, -3),
    "strakewood_island/giant_stronghold/giant_stronghold": (-28, 1547, -1),
    "strakewood_island/greyton/jail/jail": (279, 1596, -1),
    "strakewood_island/greyton/jail/jail_a": (286, 1604, -2),
    "strakewood_island/old_outpost/old_outpost": (126, 1370, -3),
    "strakewood_island/old_outpost/old_outpost_a": (159, 1370, -4),
    "strakewood_island/rockforge/rockforge": (-13, 1348, -1),
    "strakewood_island/rockforge/rockforge_a": (-13, 1347, -4),
    "strakewood_island/rockforge/rockforge_b": (-13, 1347, -5),
    "strakewood_island/rockforge/rockforge_c": (-13, 1347, -6),
    "strakewood_island/underground_city/underground_city": (-19, 1574, -4),
    "strakewood_island/zechna_temple/zechna_temple": (155, 1558, -3),
}

# Stair families: the intermediate tiles of a stairwell reuse the "down"
# archetype of the family the stairwell's own exit objects belong to.
STAIR_FAMILIES = (
    ("stairs_spiral_stone_", "stairs_spiral_stone_down"),
    ("stairs_stone_", "stairs_stone_down"),
    ("stairs_wood_", "stairs_wood_down"),
    ("stairs_red_", "stairs_red_down"),
    ("stairs_", "stairs_down"),
    ("ladder_", "ladder_down"),
)

ABOVE_WORDS = ("tower", "treehome", "treehouse", "attic", "loft", "spire", "upper", "roof")
BELOW_WORDS = ("cave", "cavern", "mine", "sewer", "dungeon", "crypt", "lair", "abyss", "below",
               "underground", "cellar", "catacomb", "tunnel", "jail", "pit", "ruins", "outpost",
               "stronghold", "lab", "temple", "rockforge")


# ------------------------------------------------------------------ helpers
INPUT_TREES = ("arch", "maps")  # every file the generator reads lives under these


def git(content: Path, *args) -> str:
    out = subprocess.run(["git", "-C", str(content), *args], capture_output=True, text=True, check=True)
    return out.stdout


def content_revision(content: Path) -> dict:
    """Commit and cleanliness of the input trees; the plan is only evidence for a clean checkout."""
    head = git(content, "rev-parse", "--verify", "HEAD^{commit}").strip()
    dirty = git(content, "status", "--porcelain", "--untracked-files=all", "--", *INPUT_TREES).splitlines()
    return {"head": head, "clean": not dirty, "dirty_paths": sorted(dirty)[:50]}


def inputs_digest(content: Path) -> dict:
    """SHA-256 over (path, sha256(bytes)) of every regular file under the input trees, in sorted order."""
    h = hashlib.sha256()
    n = 0
    for tree in INPUT_TREES:
        for path in sorted((content / tree).rglob("*"), key=lambda p: p.as_posix()):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(content).as_posix()
            h.update(rel.encode())
            h.update(b"\0")
            h.update(hashlib.sha256(path.read_bytes()).digest())
            n += 1
    return {"trees": list(INPUT_TREES), "files": n, "sha256": h.hexdigest()}


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


def ring(r: int):
    if r == 0:
        return [(0, 0)]
    pts = [(x, y) for x in range(-r, r + 1) for y in range(-r, r + 1) if max(abs(x), abs(y)) == r]
    return sorted(pts, key=lambda p: (abs(p[0]) + abs(p[1]), p[1], p[0]))


def cheb(a, b) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def base_name(path) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


def stair_family_down(arch: str):
    a = arch.casefold()
    for prefix, down in STAIR_FAMILIES:
        if a.startswith(prefix):
            return down
    return None


def premise(evidence) -> str:
    """Fingerprint of the evidence an owner decision was made on: SHA-256 of its canonical JSON (taken as the
    plan's JSON holds it, so the generator and the verifier hash the same text)."""
    canon = json.loads(json.dumps(evidence))
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# What a premise covers, from the plan's JSON records (verify_plan.py has its own copy of each).
EXIT_LISTS = ("entrances_from_overworld", "exits_to_overworld", "entrances_from_other_groups", "exits_to_other_groups",
              "internal_exits")
EXIT_CONTENT_FIELDS = ("source_map", "line", "source_tile", "target_map", "target_tile", "arch", "form", "vertical",
                       "vertical_inferred_from")
GROUP_MAP_FIELDS = ("path", "name", "region", "width", "height", "local_offset", "tile_links", "occupied_tiles")


def group_evidence(rec: dict) -> dict:
    """A group's content as the rules see it: its maps (names, regions, sizes, layout, tile counts), the content
    fields of every exit record it holds, and its script references."""
    exits = {}
    for key in EXIT_LISTS[:4]:
        for e in rec.get(key, []):
            exits[(e["source_map"], e["line"])] = {f: e.get(f) for f in EXIT_CONTENT_FIELDS}
    return {"maps": [{k: m.get(k) for k in GROUP_MAP_FIELDS} for m in rec["maps"]],
            "exits": [exits[k] for k in sorted(exits)],
            "scripts": rec.get("script_and_dialog_references", []), "redirects": rec.get("script_redirects", [])}


def portal_evidence(site: dict, records: dict) -> dict:
    """A blocked stairwell site as a portal decision sees it: the site record (owners, ends, exits, column
    occupants, reason) and the full exit record of each of its exits."""
    return {"site": {k: v for k, v in site.items() if k != "owner_decision"},
            "exits": [records.get((x["source_map"], x["line"])) for x in site["exits"]]}


def relayering_evidence(rl: dict, stairwells: list) -> dict:
    """What the relayering judgement calls summarise: the calls and every record behind them (regions, kept
    regions, moved units, caves, split tiles, seams, new stairs, steep steps, new contacts) and the one-way
    relayering stairwells."""
    out = {k: rl[k] for k in ("judgement_calls", "regions", "kept_regions", "units_moved", "caves", "split_tiles",
                              "seams", "new_stairs", "steep_steps", "new_contacts")}
    out["one_way_stairwells"] = [{k: sw[k] for k in STAIRWELL_PREMISE_FIELDS} for sw in stairwells
                                 if sw["kind"] == "relayering" and sw["direction"] != "both"]
    return out


def alignment_evidence(recs: dict, gids) -> dict:
    """What an owner alignment rests on, from the JSON group records `recs` (id -> record) of the groups `gids`:
    each one's sheet layout (ALIGN_LAYOUT_FIELDS per member map) and the content fields of every exit record
    between two of them."""
    gids = list(gids)
    exits = {}
    for gid in gids:
        for key in ("entrances_from_other_groups", "exits_to_other_groups"):
            for e in recs[gid].get(key, []):
                if e["source_group"] in gids and e["target_group"] in gids:
                    exits[(e["source_map"], e["line"])] = [e["source_map"], e["source_tile"], e["target_map"],
                                                           e["target_tile"], e["arch"]]
    return {"layouts": {gid: [{k: m[k] for k in ALIGN_LAYOUT_FIELDS} for m in recs[gid]["maps"]] for gid in gids},
            "exits": [exits[k] for k in sorted(exits)]}


def stairwell_premise(sw: dict) -> str:
    """The premise of a stairwell record: what a decision about it was made on (STAIRWELL_PREMISE_FIELDS)."""
    return premise({k: sw[k] for k in STAIRWELL_PREMISE_FIELDS})


def stairwell_appearance(exit_archs: list):
    """(archetype, source text) shown on a stairwell's intermediate tiles, or (None, text) when there is none.

    `exit_archs` are the archetypes of the stairwell's exits, the down-going ones
    first, each set sorted. The first one that belongs to a stair family gives the
    family's down archetype. Failing that, the first one with an owner-chosen
    fallback (decision 2026-10-19) gives that fallback. The third value is the
    exit archetype whose fallback was used, else None."""
    exit_archs = list(dict.fromkeys(exit_archs))  # an archetype at both ends is named once
    for a in exit_archs:
        fam = stair_family_down(a)
        if fam:
            return fam, ("appearance of the down archetype of the {} family used by this stairwell's exits; the "
                         "intermediate objects are decorations, not exits".format(fam.rsplit("_", 1)[0])), None
    for a in exit_archs:
        app = STAIRWELL_APPEARANCE_FALLBACKS.get(a.casefold(), (None,))[0]
        if app:
            return app, ("owner decision {}: no stair archetype among {}, so the {} exits show {}; the intermediate "
                         "objects are decorations, not exits".format(DECIDED_2026_10_19, exit_archs, a, app)), \
                a.casefold()
    return None, "no stair archetype among {}; owner to choose".format(exit_archs), None


def seam_stairs_text() -> str:
    """The seam stair archetypes as the judgement call names them (stairs_stone_up/down)."""
    low, high = SEAM_STAIR_ARCHETYPES["low"], SEAM_STAIR_ARCHETYPES["high"]
    common = os.path.commonprefix([low, high])
    common = common[:common.rfind("_") + 1]
    return "{}/{}".format(low, high[len(common):])


def portal_key(x: dict) -> tuple:
    """The identity an owner portal decision names: one exit object (source map, line, tile, archetype)."""
    return x["source_map"], x["line"], tuple(x["source_tile"]), x["arch"]


def apply_owner_portals(sites: list, decisions: dict, records: dict):
    """Owner decision 1 (2026-10-19): mark the blocked stairwell sites whose exits the owner keeps portals.

    `decisions` maps portal_key() to the decision: its text and the premise() of
    the portal_evidence() it was made on (the site record and the exit records,
    from `records`, keyed by source map and line). A site whose exits are all
    decided, on that same evidence, carries the decision (owner_decision). A
    site decided for only some of its exits or whose evidence changed (other
    ends, owners, exits, targets or column occupants), or a decision for an exit
    at no blocked site (no longer blocked, or not the same object), no longer
    applies: SystemExit."""
    decided_at = set()
    for site in sites:
        keys = [portal_key(x) for x in site["exits"]]
        found = [decisions.get(k) for k in keys]
        if not any(found):
            continue
        texts = {d["decision"] if d else None for d in found}
        now = premise(portal_evidence(site, records))
        if len(texts) != 1 or any(d["premise"] != now for d in found):
            raise SystemExit("owner decision {} keeps the blocked stairwell site {} -> {} a portal only for some of "
                             "its exits {} or for another site record (its premise is now {}); ask the owner "
                             "again".format(DECIDED_2026_10_19, site["top"], site["bottom"], keys, now))
        site["owner_decision"] = {"decided": DECIDED_2026_10_19, "decision": texts.pop()}
        decided_at.update(keys)
    stale = sorted(k for k in decisions if k not in decided_at)
    if stale:
        raise SystemExit("owner decision {} keeps exits a portal that are not at a blocked stairwell site (no longer "
                         "blocked, or not the same exit object): {}; ask the owner again".format(
                             DECIDED_2026_10_19, stale))


def check_judgement_calls(judgement: list, confirmed: tuple) -> list:
    """Owner decision 6 (2026-10-19): the relayering judgement calls must be exactly the confirmed ones.

    `confirmed` holds (kind, unit or None, text); returns them as judgement-call
    records, or raises SystemExit naming the calls that differ."""
    calls = [dict({"kind": k}, **({"unit": u} if u is not None else {}), text=t) for k, u, t in confirmed]
    if judgement != calls:
        new_ = [j for j in judgement if j not in calls]
        gone = [j for j in calls if j not in judgement]
        raise SystemExit("owner decision {} confirmed the relayering judgement calls as they were; they now differ "
                         "(new or changed: {}; no longer made: {}); ask the owner again".format(
                             DECIDED_2026_10_19, new_[:3], gone[:3]))
    return calls


def occupied_rows(rec) -> list:
    return ["".join("#" if (x, y) in rec.occupied else "." for x in range(rec.width)) for y in range(rec.height)]


def placement_failed(res) -> bool:
    return res is None or bool(res.get("failed"))


def place_pool(pool, connected, blockers, depth, place):
    """Place the groups of `pool` one at a time, in reach order; return (order, failed, unplaced).

    connected(gid): gid has an exit to something already placed. blockers(gid,
    remaining): unplaced groups that must be placed before gid. depth(gid): reach
    depth, the first sort key. place(gid): the placement record, or None or
    {"failed": True} when gid cannot be placed. A failed group stays unplaced: it
    is not in `order`, it is never tried again, and nothing is placed through it,
    so groups reachable only through it end up in `unplaced`.
    """
    remaining = sorted(pool)
    order, failed = [], []
    while remaining:
        ready = [gid for gid in remaining if connected(gid)]
        if not ready:
            break
        ok = [gid for gid in ready if not blockers(gid, set(remaining))]
        pick = min(ok or ready, key=lambda gid: (depth(gid), gid))
        remaining.remove(pick)
        (failed if placement_failed(place(pick)) else order).append(pick)
    return order, failed, remaining


def exit_source_fields(e: dict) -> dict:
    """The fields of an exit that come from the content alone, in record order.

    The exit records and the exit inventory both use them, so the verifier can
    compare every record with the exit object it was made from."""
    r = {"source_map": e["source_map"], "source_tile": [e["source_x"], e["source_y"]]}
    if e.get("resolved_map"):
        r["target_map"] = e["resolved_map"]
        r["target_tile"] = [e["resolved_x"], e["resolved_y"]]
    r.update(arch=e["arch"], line=e["line"], form=e["form"], vertical=e["vdir"])
    if e.get("target_map") is not None and e["target_map"] != e.get("resolved_map"):
        r["authored_target"] = [e["target_map"], e.get("target_x"), e.get("target_y")]
    if e.get("target_default_entry"):
        r["target_is_map_default_entry"] = True
    if e["per_player"]:
        r["per_player_last_eat_1"] = True
    if e["event_scripts"]:
        r["event_scripts"] = e["event_scripts"]
    if e.get("dir_inferred_from"):
        r["vertical_inferred_from"] = e["dir_inferred_from"]
    return r


def inventory_entry(e: dict) -> dict:
    """One exit object as read from the content, before placement or wiring.

    Besides the record fields it carries the reader's facts the data problems
    are derived from: the tile-link slot of a tiled exit without a link, the
    authored path of a dynamic exit, a target problem with its authored target,
    and an explicit last_heal 0."""
    rec = exit_source_fields(e)
    if e["form"] == "tiled-unresolved":
        rec["tiled_slot"] = e["tiled_slot"]
    if e["form"] == "dynamic":
        rec["dynamic_target"] = e.get("raw_target")
    if e.get("problem"):
        rec["problem"] = e["problem"]
        rec["problem_target"] = [e.get("target_map"), e.get("target_x"), e.get("target_y")]
    if e.get("explicit_last_heal_0"):
        rec["explicit_last_heal_0"] = True
    return rec


# --------------------------------------------------------------------- main
def build(content: Path, allow_dirty: bool = False):
    revision = content_revision(content)
    if not revision["clean"] and not allow_dirty:
        raise SystemExit("content checkout {} has uncommitted changes under {}; the plan must come from a clean "
                         "commit (use --allow-dirty only for experiments):\n  {}".format(
                             content, "/".join(INPUT_TREES), "\n  ".join(revision["dirty_paths"])))
    digest_before = inputs_digest(content)
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
        e["key"] = (e["source_map"], e["line"])

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
                "{} entrance exit(s) carry last_eat 1 (MAP_PLAYER_MAP: per-player copy), e.g. {} ({},{}); "
                "owner decision 5 (settled): per-player copies stay separate planes".format(
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
        if gid in OWNER_PLACE:
            extra = []
            if start_map and start_map in mem:
                extra.append("NEW-CHARACTER START MAP (arch/intern/map.arc slaying {})".format(start_map))
            return "place", "place", ev + extra + [OWNER_PLACE[gid]]
        if gid in OWNER_ALIGN:
            if g["entrances"] or g["exits_out"]:
                raise SystemExit("owner decision {} aligns {} under {}, but the group now has exits of its own; ask "
                                 "the owner again".format(DECIDED_2026_10_19, gid, OWNER_ALIGN[gid]["anchor"]))
            return "place", "place", ev + ["no exit object leads here or out; {}".format(OWNER_ALIGN[gid]["why"])]
        base = posixpath.basename(gid)
        if base.startswith("ship_") or "/ship_" in gid:
            dialog = [r for r in g["script_refs"] if r["file"].startswith("maps/interfaces/")]
            if start_map and start_map in mem:
                return "uncertain", "uncertain", ev + [
                    "a ship's lower deck, but it is the NEW-CHARACTER START MAP (arch/intern/map.arc "
                    "slaying {})".format(start_map)]
            outs = sorted({e["resolved_map"] for e in g["exits_out"] if e["target_group"] == ow_id})
            ins = sorted({e["source_map"] for e in ow_in})
            return "separate", "ship", ev + [
                "ship lower deck used for sea travel: entered from {} but exits to {}{}".format(
                    ins, outs, "; NPC dialog teleports here ({})".format(dialog[0]["file"]) if dialog else "")]
        if "unique" in gid:
            return "uncertain", "uncertain", ev + [
                "name contains 'unique' but no per-player mechanism exists in the data (no last_eat 1 exit, "
                "no `unique` header, no GetPath(..., True) script)"]
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
    # pass 1 traverses only undecided/placed groups (definite places); pass 2
    # also traverses uncertain groups, so anything only reachable through an
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

    reach_definite = bfs({None, "place"})
    reach = bfs({None, "place", "uncertain"})
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
    # Owner decision 5 (2026-10-19): the classifications the owner confirmed must still be what the rules derive
    # (on the same evidence: checked with the group's JSON record below).
    for gid, (cls, rsn, _premise) in sorted(OWNER_CLASSIFICATIONS.items()):
        got = (G[gid]["classification"], G[gid]["reason"]) if gid in G else None
        if got != (cls, rsn):
            raise SystemExit("owner decision {} confirmed {} as {}/{}, but the rules now give {}; ask the owner "
                             "again".format(DECIDED_2026_10_19, gid, cls, rsn, "/".join(got) if got else
                                            "no such group"))
        G[gid]["evidence"].append("owner decision {}: {} confirmed (a version 1 judgement call)".format(
            DECIDED_2026_10_19, rsn))

    # ---------------------------------------------------------- relayering
    # Owner decision (2026-10-18): fix the conflicts, then relayer. Every group
    # (the overworld in plane coordinates, other groups in their local layout)
    # is relayered before placement, so placement, stairs and stairwells see the
    # final layers. Groups whose layout cannot be laid flat keep their layers.
    relay = {}
    for g in groups:
        if g["id"] != ow_id and g["layout"]["problems"]:
            continue
        offs = g["layout"]["offsets"]
        tiles = {}
        for m, (ox, oy, oz) in sorted(offs.items()):
            r = maps[m]
            for (tx, ty) in sorted(r.occupied):
                tiles.setdefault((oz, ox + tx, oy + ty), (m, r.floors.get((tx, ty)), (tx, ty) in r.blocked))
        mem = set(g["maps"])
        internal = []
        for e in exits:
            if e["source_map"] in mem and e.get("resolved_map") in mem:
                so, to = offs[e["source_map"]], offs[e["resolved_map"]]
                internal.append({"key": e["key"], "arch": e["arch"],
                                 "src": (so[2], so[0] + e["source_x"], so[1] + e["source_y"]),
                                 "dst": (to[2], to[0] + e["resolved_x"], to[1] + e["resolved_y"])})
        relay[g["id"]] = relayer.Relayering(tiles, internal, LAYER_HEIGHT_Z, overworld=(g["id"] == ow_id))
    void_exit_ends = {}  # (map, x, y) -> layer, for exit ends on void tiles of a moved map

    def tile_layer(gid, m, x, y):
        """Final layer of tile (x, y) of member map m after relayering."""
        ox, oy, oz = G[gid]["layout"]["offsets"][m]
        R = relay.get(gid)
        if R is None:
            return oz
        k = (oz, ox + x, oy + y)
        if k in R.final_layer:
            return R.final_layer[k]
        # An exit end on a void tile follows the nearest occupied tile of its map.
        for rad in (1, 2):
            near = sorted((max(abs(dx), abs(dy)), dy, dx, R.final_layer[n])
                          for dx in range(-rad, rad + 1) for dy in range(-rad, rad + 1)
                          for n in [(oz, ox + x + dx, oy + y + dy)]
                          if n in R.final_layer and R.tiles[n][0] == m)
            if near:
                if near[0][3] != oz:
                    void_exit_ends[(m, x, y)] = near[0][3]
                return near[0][3]
        return oz

    # ---------------------------------------------------------- footprints
    # Occupied tiles per group in group-local coordinates, per local layer.
    local_tiles, local_set, local_bbox = {}, {}, {}
    for g in groups:
        if g["id"] == ow_id:
            continue
        per = collections.defaultdict(set)
        R = relay.get(g["id"])
        if R is not None:
            for (L, x, y) in R.final_positions():
                per[L].add((x, y))
        else:
            for m, (ox, oy, oz) in g["layout"]["offsets"].items():
                for (tx, ty) in maps[m].occupied:
                    per[oz].add((ox + tx, oy + ty))
        per = {z: v for z, v in per.items() if v}
        local_set[g["id"]] = per
        local_tiles[g["id"]] = {z: sorted(v) for z, v in sorted(per.items())}
        local_bbox[g["id"]] = {z: fp.bbox_of(v) for z, v in per.items()}

    def owner_of(group_id, map_path):
        return map_path if group_id == ow_id else group_id

    # Related owners share an exit. An overworld map can span several layers after
    # relayering, so it is related only on the layer(s) where a shared exit ends.
    related, related_ow_layers = {}, {}
    for g in groups:
        if g["id"] == ow_id:
            continue
        rel = set()
        lay = collections.defaultdict(set)
        for e in g["entrances"]:
            rel.add(owner_of(e["source_group"], e["source_map"]))
            if e["source_group"] == ow_id:
                lay[e["source_map"]].add(tile_layer(ow_id, e["source_map"], e["source_x"], e["source_y"]))
        for e in g["exits_out"]:
            rel.add(owner_of(e["target_group"], e["resolved_map"]))
            if e["target_group"] == ow_id:
                lay[e["resolved_map"]].add(tile_layer(ow_id, e["resolved_map"], e["resolved_x"], e["resolved_y"]))
        related[g["id"]] = rel
        related_ow_layers[g["id"]] = lay

    def rel_on(gid, z):
        """Owners related to gid on layer z (groups on every layer, overworld maps on their exit layers)."""
        lay = related_ow_layers.get(gid, {})
        return {o for o in related.get(gid, ()) if o not in lay or z in lay[o]}

    # ---------------------------------------------------------- placement
    class S:  # mutable state of the current placement run
        placed: dict = {}
        placements: dict = {}
        occ = None
        order: list = []
        stairwells: list = []
        blocked_sites: list = []
        sw_of_exit: dict = {}
        slope_failed: list = []
        slope_failed_exit: dict = {}
        relayer_blocked: list = []
        fallback_of: dict = {}

    def local_tile(gid, m, x, y):
        ox, oy, _oz = G[gid]["layout"]["offsets"][m]
        return ox + x, oy + y, tile_layer(gid, m, x, y)

    def plane_tile(gid, m, x, y):
        lx, ly, lz = local_tile(gid, m, x, y)
        if gid == ow_id:
            return lx, ly, lz
        p = S.placed.get(gid)
        if p is None:
            return None
        return lx + p["dx"], ly + p["dy"], lz + p["L"]

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

        orient, why = derived_orientation(g, votes_for, upstream)
        # Owner decision 4 (2026-10-19): a confirmed orientation must still be the derived one, from the same
        # evidence (a name keyword, no exit with a direction).
        decided = OWNER_ORIENTATIONS.get(g["id"])
        if decided is not None:
            if decided[:2] != (orient, tuple(why)):
                raise SystemExit("owner decision {} confirmed '{}' for {} from {}, but the rules now give '{}' from "
                                 "{}; ask the owner again".format(DECIDED_2026_10_19, decided[0], g["id"],
                                                                  list(decided[1]), orient, why))
            why = why + ["owner decision {}: {}".format(DECIDED_2026_10_19, ORIENTATION_DECISION)]
        return orient, why

    def derived_orientation(g, votes_for, upstream):
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
        return abs(src[2] - dst[2]) == 1 and cheb(src, dst) <= STAIR_TOLERANCE

    def is_aligned_multi(src, dst):
        return abs(src[2] - dst[2]) >= 2 and cheb(src, dst) <= STAIR_TOLERANCE

    def layer_bounds(gid, conns):
        lo, hi = -10 ** 9, 10 ** 9
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

    def footprint_violation(gid, dx, dy, L):
        """First gap/contact violation of gid at (dx, dy, L), or None."""
        for lz, pts in local_tiles[gid].items():
            z = lz + L
            lb = local_bbox[gid][lz]
            bbox = (lb[0] + dx, lb[1] + dy, lb[2] + dx, lb[3] + dy)
            if not S.occ.owners_near(z, bbox, S.occ.radius):
                continue
            hit = S.occ.check(z, [(x + dx, y + dy) for (x, y) in pts], bbox, rel_on(gid, z), own=gid)
            if hit:
                return hit[0], hit[1], z
        return None

    def chain_between(gid, upper, lower, cand=None, slope=False):
        """Stairwell chain from upper to lower end for a (candidate) placement of gid.

        slope=True searches a sloped chain (one tile sideways per layer at most)
        for ends that are more than one tile apart."""

        def own(p):
            if cand is None:
                return False
            dx, dy, L = cand
            return (p[0] - dx, p[1] - dy) in local_set[gid].get(p[2] - L, ())

        def void(p):
            return not own(p) and S.occ.occupant(p[2], (p[0], p[1])) is None

        def strict(p):
            rel = rel_on(gid, p[2])
            return void(p) and not S.occ.unrelated_within(p[2], (p[0], p[1]), rel | {gid}, own=gid) and \
                not S.occ.related_within(p[2], (p[0], p[1]), rel - {gid}, own=gid)

        if slope:
            return fp.find_slope_chain(upper, lower, void, strict)
        return fp.find_chain(upper, lower, void, strict, STAIRWELL_MAX_BEND)

    def map_at(owner, plane):
        """Member map of `owner` (overworld map path or placed group) holding plane tile (x, y, layer)."""
        x, y, z = plane
        if owner in maps:
            return owner
        p = S.placed.get(owner)
        if p is None:
            return None
        for m, (ox, oy, oz) in sorted(G[owner]["layout"]["offsets"].items()):
            lx, ly = x - ox - p["dx"], y - oy - p["dy"]
            if (lx, ly) in maps[m].occupied and tile_layer(owner, m, lx, ly) + p["L"] == z:
                return m
        return None

    def column_blockers(gid, upper, lower, cand=None):
        """What occupies the straight stairwell column between the two ends (for reporting)."""
        out = []
        for z in range(upper[2] - 1, lower[2], -1):
            if cand is not None and (upper[0] - cand[0], upper[1] - cand[1]) in local_set[gid].get(z - cand[2], ()):
                out.append({"layer": z, "x": upper[0], "y": upper[1], "occupied_by": gid, "map": None})
                continue
            o = S.occ.occupant(z, (upper[0], upper[1]))
            if o is not None:
                out.append({"layer": z, "x": upper[0], "y": upper[1], "occupied_by": o,
                            "map": map_at(o, (upper[0], upper[1], z))})
        return out

    def ends(src, dst):
        return (src, dst) if src[2] > dst[2] else (dst, src)

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
        main = best["conn"]
        ideal = dict(dx=best["dx"], dy=best["dy"], L=best["L"])
        step = -1 if orient == "below" else 1
        tried = []
        chosen = None
        # Preference: (1) unshifted with a buildable stairwell (any extra
        # depth); (2) unshifted, footprint fits but the main stairwell is
        # blocked (reported; a shift would turn it into a portal anyway and
        # lose the x/y alignment); (3) horizontal shifts. A forced spot (an
        # owner alignment) is the decision itself: it is tried alone.
        fallback = None
        for r in range(0, (0 if forced is not None else MAX_SHIFT) + 1):
            for k in range(0, (0 if forced is not None else MAX_EXTRA_LAYERS) + 1):
                L = ideal["L"] + step * k
                if not lo <= L <= hi:
                    continue  # never cross a vertical bound from the exit semantics
                for sx, sy in ring(r):
                    dx, dy = ideal["dx"] + sx, ideal["dy"] + sy
                    hit = footprint_violation(gid, dx, dy, L)
                    if hit is None and main is not None:
                        s, t = eval_conn(main[0], main[1], gid, dx, dy, L)
                        if is_aligned_multi(s, t):
                            up, low = ends(s, t)
                            mode, _ = chain_between(gid, up, low, cand=(dx, dy, L))
                            if mode is None:
                                blk = column_blockers(gid, up, low, cand=(dx, dy, L))
                                hit = (", ".join(sorted({b["occupied_by"] for b in blk})) or "no free spot",
                                       "stairwell-blocked", None)
                                if fallback is None and r == 0:
                                    fallback = dict(dx=dx, dy=dy, L=L, shift=[sx, sy], extra_layers=k,
                                                    main_stairwell_blocked=True)
                    if hit is None:
                        chosen = dict(dx=dx, dy=dy, L=L, shift=[sx, sy], extra_layers=k)
                        break
                    if r == 0:
                        tried.append({"layer_offset": L, "shift": [sx, sy], "blocked_by": hit[0],
                                      "rule": hit[1], "plane_layer": hit[2]})
                if chosen:
                    break
            if chosen:
                break
            if r == 0 and fallback is not None:
                chosen = fallback
                break
        if chosen is None:
            return {"failed": True, "ideal": ideal}
        S.placed[gid] = chosen
        S.order.append(gid)
        for lz, pts in local_tiles[gid].items():
            S.occ.add(gid, lz + chosen["L"], [(x + chosen["dx"], y + chosen["dy"]) for (x, y) in pts])
        build_stairwells(gid, conns)
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

    def is_slope(src, dst):
        """Ends 2+ layers apart and at most one tile apart per layer, but not aligned (sloped stairwell)."""
        dl = abs(src[2] - dst[2])
        return dl >= 2 and STAIR_TOLERANCE < cheb(src, dst) <= dl * STAIR_TOLERANCE

    def slope_blockers(gid, top, bottom):
        """Why no sloped chain exists: layers whose whole corridor is occupied, else 'no connected path'."""
        n = top[2] - bottom[2]
        out = []
        for i in range(1, n):
            z = top[2] - i
            spots = [(x, y) for y in range(max(top[1] - i, bottom[1] - (n - i)), min(top[1] + i, bottom[1] + (n - i)) + 1)
                     for x in range(max(top[0] - i, bottom[0] - (n - i)), min(top[0] + i, bottom[0] + (n - i)) + 1)]
            occ = sorted({S.occ.occupant(z, xy) for xy in spots} - {None})
            free = sum(1 for xy in spots if S.occ.occupant(z, xy) is None)
            out.append({"layer": z, "corridor_tiles": len(spots), "void_tiles": free,
                        "occupied_by": [base_name(o) for o in occ]})
        return out

    def make_stairwell(kind, owner, other, top, bottom, site, mode, chain):
        """Stairwell record (and occupancy of its intermediate tiles) for one site of exit items."""
        exits_rec = [{"source_map": o["e"]["source_map"], "source_tile": [o["e"]["source_x"], o["e"]["source_y"]],
                      "target_map": o["e"]["resolved_map"], "target_tile": [o["e"]["resolved_x"], o["e"]["resolved_y"]],
                      "arch": o["e"]["arch"], "line": o["e"]["line"],
                      "goes": "down" if o["src_is_upper"] else "up",
                      "plane_source": list(o["src"]), "plane_target": list(o["dst"])} for o in site]
        top_objs = sorted({o["e"]["arch"] for o in site if o["src_is_upper"]})
        bot_objs = sorted({o["e"]["arch"] for o in site if not o["src_is_upper"]})
        fam, fam_source, fallback = stairwell_appearance(top_objs + bot_objs)
        # Top and bottom keep their existing exit objects. Intermediate tiles get
        # a decoration that only borrows the chosen archetype's appearance: it is
        # not an exit (no exit type, destination or tiled link) and not walkable,
        # so the stairwell stays ONE exit.
        tiles = [{"layer": top[2], "x": top[0], "y": top[1], "role": "top", "existing": True,
                  "objects": [{"archetype": a, "exit": True} for a in top_objs]}]
        for (x, y, z) in chain:
            tiles.append({"layer": z, "x": x, "y": y, "role": "intermediate", "existing": False,
                          "objects": [{"archetype": fam, "exit": False, "walkable": False,
                                       "use": "appearance only"}] if fam else []})
        tiles.append({"layer": bottom[2], "x": bottom[0], "y": bottom[1], "role": "bottom", "existing": True,
                      "objects": [{"archetype": a, "exit": True} for a in bot_objs]})
        for (x, y, z) in chain:
            S.occ.add(owner, z, [(x, y)])
        goes = {o["src_is_upper"] for o in site}
        direction = "both" if len(goes) == 2 else ("down" if True in goes else "up")
        sw = {"kind": kind, "group": owner, "owner": owner, "connects": sorted({owner, other}), "other": other,
              "direction": direction,
              "landing_only_end": {"both": None, "down": "bottom", "up": "top"}[direction],
              "x": top[0], "y": top[1], "top_layer": top[2], "bottom_layer": bottom[2],
              "shape": "sloped" if cheb(top, bottom) > STAIR_TOLERANCE else "column",
              "horizontal_offset": cheb(top, bottom),
              "mode": mode, "max_bend": max([cheb((x, y), top) for (x, y, _z) in chain] or [0]),
              "segments_drawn_only_on_player_layer": True,
              "intermediate_appearance": fam,
              "intermediate_appearance_source": fam_source,
              "tiles": tiles, "view_conflicts": [], "replaces": exits_rec}
        S.stairwells.append(sw)
        if fallback:
            S.fallback_of[id(sw)] = fallback  # the stairwell's id is given after the run
        for o in site:
            S.sw_of_exit[o["e"]["key"]] = sw
        return sw

    def group_sites(items):
        """One site (stairwell) per tile holding a down-going stair object, see build_stairwells()."""
        for it in items:
            it["stair"] = stair_family_down(it["e"]["arch"]) is not None
        items.sort(key=lambda it: (0 if it["src_is_upper"] and it["stair"] else 1 if it["src_is_upper"] else 2,
                                   it["e"]["source_map"], it["e"]["source_y"], it["e"]["source_x"], it["e"]["line"]))
        sites = []
        for it in items:
            best = None
            for site in sites:
                c = site[0]
                if c["other"] != it["other"] or c["up"][2] != it["up"][2] or c["low"][2] != it["low"][2] \
                        or c["slope"] != it["slope"]:
                    continue
                if it["src_is_upper"] and it["stair"]:
                    fits = it["up"] == c["up"] and all(cheb(o["low"], it["low"]) <= 1 for o in site)
                elif it["src_is_upper"]:
                    fits = c["stair"] and c["src_is_upper"] and cheb(c["up"], it["up"]) <= 1 and \
                        all(cheb(o["low"], it["low"]) <= 1 for o in site)
                else:
                    fits = cheb(c["up"], it["up"]) <= 1 and all(cheb(o["low"], it["low"]) <= 1 for o in site)
                if fits:
                    k = (cheb(c["up"], it["up"]), cheb(c["low"], it["low"]))
                    if best is None or k < best[0]:
                        best = (k, site)
            if best is None:
                sites.append([it])
            else:
                best[1].append(it)
        return sites

    def site_ends(site):
        canon = site[0]
        top = canon["up"]
        n_ok = (lambda b: cheb(b, top) <= (top[2] - b[2]) * STAIR_TOLERANCE) if canon["slope"] else \
            (lambda b: cheb(b, top) <= STAIR_TOLERANCE)
        bottoms = sorted({tuple(o["src"]) for o in site if not o["src_is_upper"] and n_ok(tuple(o["src"]))})
        bottom = min(bottoms, key=lambda b: (cheb(b, top), b[1], b[0])) if bottoms else tuple(canon["low"])
        return top, bottom

    def build_stairwells(gid, conns):
        """Build one stairwell per aligned or sloped multi-layer exit site of the just placed gid."""
        p = S.placed[gid]
        items = []
        for kind, e in conns:
            s, t = eval_conn(kind, e, gid, p["dx"], p["dy"], p["L"])
            aligned, slope = is_aligned_multi(s, t), is_slope(s, t)
            if not (aligned or slope):
                continue
            other = owner_of(e["source_group"], e["source_map"]) if kind == "in" else \
                owner_of(e["target_group"], e["resolved_map"])
            up, low = ends(s, t)
            items.append({"e": e, "src": s, "dst": t, "up": up, "low": low, "other": other,
                          "src_is_upper": s[2] > t[2], "slope": slope})
        # One site (stairwell) per tile holding a down-going stair object. A
        # down-going exit without a stair archetype (an invisible exit beside a
        # multi-tile stair sprite) joins the adjacent stair's site. Return
        # (up-going) exits join the site whose top they land next to and whose
        # landing is next to them. Anything else gets a stairwell of its own.
        # Aligned and sloped exits never share a site.
        for site in group_sites(items):
            canon = site[0]
            top, bottom = site_ends(site)
            slope = cheb(top, bottom) > STAIR_TOLERANCE
            mode, chain = chain_between(gid, top, bottom, slope=slope)
            if mode is None:
                rec = {"group": gid, "other": canon["other"], "top": list(top), "bottom": list(bottom),
                       "exits": [{"source_map": o["e"]["source_map"],
                                  "source_tile": [o["e"]["source_x"], o["e"]["source_y"]], "arch": o["e"]["arch"],
                                  "line": o["e"]["line"]} for o in site]}
                if slope:
                    rec.update(shape="sloped", layers=slope_blockers(gid, top, bottom),
                               reason="no void sloped chain (one tile per layer, at most one tile sideways)")
                    S.slope_failed.append(rec)
                    for o in site:
                        S.sw_of_exit[o["e"]["key"]] = None
                        S.slope_failed_exit[o["e"]["key"]] = rec
                else:
                    rec.update(shape="column", column_occupied=column_blockers(gid, top, bottom),
                               reason="no void chain within {} tile(s) of the column".format(STAIRWELL_MAX_BEND))
                    S.blocked_sites.append(rec)
                    for o in site:
                        S.sw_of_exit[o["e"]["key"]] = None
                continue
            make_stairwell("sloped" if slope else "placement", gid, canon["other"], top, bottom, site, mode, chain)

    # groups the owner aligned to an anchor (decision 3) have no exit to place them by: they are placed after the pool
    to_place = [g["id"] for g in groups if g["classification"] == "place" and g["id"] not in OWNER_ALIGN]
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
        def place(gid):
            res = place_group(gid, tentative=tentative)
            S.placements[gid] = {"failed": True, "ideal": None} if res is None else res
            return res

        return place_pool(pool, lambda gid: bool(connections_to_placed(gid)), above_below_blockers,
                          lambda gid: G[gid]["reach_depth"] if G[gid]["reach_depth"] is not None else 99, place)

    ow_relay = relay[ow_id]
    ow_pieces = ow_relay.pieces  # map -> final layer -> plane tiles
    ow_internal = [e for e in exits if e["source_group"] == ow_id and e.get("target_group") == ow_id]

    def _piece(m, L):
        return ow_pieces[m][L]

    def sw_related(sw, z):
        """Owners whose tiles a stairwell tile on layer z may see without a view conflict.

        Relayering stairwells belong to the overworld: they do not see each other as unrelated."""
        if sw["kind"] == "relayering":
            return {x["owner"] for x in S.stairwells if x["kind"] == "relayering"}
        return rel_on(sw["group"], z) | {sw["group"]}

    def build_relayer_stairwells():
        """Stairwells for overworld exits whose ends are 2+ layers apart after relayering (decision 4 rule)."""
        items = []
        for e in ow_internal:
            s_ = plane_tile(ow_id, e["source_map"], e["source_x"], e["source_y"])
            t_ = plane_tile(ow_id, e["resolved_map"], e["resolved_x"], e["resolved_y"])
            aligned, slope = is_aligned_multi(s_, t_), is_slope(s_, t_)
            if not (aligned or slope):
                continue
            # Only exits whose layer distance the relayering changed; legacy overworld
            # shafts it leaves alone keep their legacy form.
            legacy = maps[e["resolved_map"]].coords[2] - maps[e["source_map"]].coords[2]
            if abs(legacy) == abs(t_[2] - s_[2]):
                continue
            up, low = ends(s_, t_)
            pair = (e["source_map"], e["resolved_map"]) if s_[2] > t_[2] else (e["resolved_map"], e["source_map"])
            items.append({"e": e, "src": s_, "dst": t_, "up": up, "low": low, "other": pair,
                          "src_is_upper": s_[2] > t_[2], "slope": slope})
        for site in group_sites(items):
            canon = site[0]
            top, bottom = site_ends(site)
            slope = cheb(top, bottom) > STAIR_TOLERANCE
            owner = "stairwell@{},{},{}".format(*top)

            def void(p):
                return S.occ.occupant(p[2], (p[0], p[1])) is None

            def strict(p, owner=owner):
                return void(p) and not S.occ.unrelated_within(p[2], (p[0], p[1]), {owner}, own=owner)

            mode, chain = fp.find_slope_chain(top, bottom, void, strict) if slope else \
                fp.find_chain(top, bottom, void, strict, STAIRWELL_MAX_BEND)
            if mode is None:
                S.relayer_blocked.append({"top_map": canon["other"][0], "bottom_map": canon["other"][1],
                                          "top": list(top), "bottom": list(bottom),
                                          "shape": "sloped" if slope else "column",
                                          "exits": [{"source_map": o["e"]["source_map"],
                                                     "source_tile": [o["e"]["source_x"], o["e"]["source_y"]],
                                                     "arch": o["e"]["arch"], "line": o["e"]["line"]} for o in site]})
                for o in site:
                    S.sw_of_exit[o["e"]["key"]] = None
                continue
            sw = make_stairwell("relayering", owner, canon["other"][1], top, bottom, site, mode, chain)
            sw["group"] = None
            sw["connects"] = sorted(set(canon["other"]))

    def run_placement(radius):
        S.placed, S.placements, S.order = {}, {}, []
        S.stairwells, S.blocked_sites, S.sw_of_exit = [], [], {}
        S.slope_failed, S.slope_failed_exit, S.relayer_blocked, S.fallback_of = [], {}, [], {}
        S.occ = fp.Occupancy(radius, CONTACT_MARGIN)
        for m in G[ow_id]["maps"]:
            for L in sorted(ow_pieces.get(m, {})):
                ts = ow_pieces[m][L]
                if ts:
                    S.occ.add(m, L, functools.partial(_piece, m, L), bbox=fp.bbox_of(ts))
        build_relayer_stairwells()
        o1, f1, l1 = run_pool(to_place, False)
        # Owner alignments (decision 3, 2026-10-19), right after the pool: the anchor's x/y, the decided layer
        # distance, nothing else tried. A group whose anchor or spot fails stays unplaced and fails verification.
        oa, fa = [], []
        for gid, dec in sorted(OWNER_ALIGN.items()):
            anchor, dl, why = dec["anchor"], dec["layer_delta"], dec["why"]
            a = S.placed.get(anchor)
            res = None if a is None else place_group(gid, forced={"dx": a["dx"], "dy": a["dy"], "L": a["L"] + dl})
            if placement_failed(res):
                S.placements[gid] = {"failed": True, "ideal": (res or {}).get("ideal")}
                fa.append(gid)
                continue
            res["alignment_reason"] = why
            S.placements[gid] = res
            oa.append(gid)
        o2, f2, l2 = run_pool(tentative_ids, True)
        # view conflicts against the final state (stairwell tiles count as their owner's tiles);
        # every stairwell is drawn only on the player's layer (owner decision 2026-10-18), so
        # the conflicts are listed but hidden in the engine.
        for sw in S.stairwells:
            sw["view_conflicts"], sw["contacts"] = [], []
            for t in sw["tiles"][1:-1]:
                vis = S.occ.unrelated_within(t["layer"], (t["x"], t["y"]), sw_related(sw, t["layer"]),
                                             own=sw["owner"])
                if vis:
                    sw["view_conflicts"].append({"layer": t["layer"], "x": t["x"], "y": t["y"], "visible_from": vis})
                if sw["kind"] != "relayering":
                    touch = S.occ.related_within(t["layer"], (t["x"], t["y"]),
                                                 rel_on(sw["group"], t["layer"]) - {sw["group"]}, own=sw["owner"])
                    if touch:
                        sw["contacts"].append({"layer": t["layer"], "x": t["x"], "y": t["y"], "touches": touch})
            sw["mode"] = "void-only" if sw["view_conflicts"] or sw["contacts"] else "clear"
        # stable stairwell ids: one series per kind
        lab_ids = sorted(S.placed)
        prefix = {"placement": "SW", "sloped": "SL", "relayering": "SR"}
        S.stairwells.sort(key=lambda sw: (list(prefix).index(sw["kind"]),
                                          lab_ids.index(sw["group"]) if sw["group"] in lab_ids else -1,
                                          -sw["top_layer"], sw["y"], sw["x"]))
        n = collections.Counter()
        for sw in S.stairwells:
            n[sw["kind"]] += 1
            sw["id"] = "{}{}".format(prefix[sw["kind"]], n[sw["kind"]])
        # A failed group stays unplaced (and fails verification, so the plan is not published).
        return o1 + oa, sorted(l1 + f1 + fa), o2, sorted(l2 + f2), sorted(f1 + fa + f2)

    def becomes_counts():
        c = collections.Counter()
        for e in cross:
            ev = eval_exit(e)
            if ev is not None:
                c[ev["becomes"]] += 1
        return c

    # ------------------------------------------------ evaluate all crossings
    def eval_exit(e):
        s = plane_tile(e["source_group"], e["source_map"], e["source_x"], e["source_y"])
        t = plane_tile(e["target_group"], e["resolved_map"], e["resolved_x"], e["resolved_y"])
        if s is None or t is None:
            return None
        dist = cheb(s, t)
        dl = t[2] - s[2]
        r = {"plane_source": list(s), "plane_target": list(t), "xy_distance": dist, "layer_delta": dl}
        if s[2] == t[2] and dist == 0:
            r["becomes"] = "same-tile"
        elif abs(dl) == 1 and dist <= STAIR_TOLERANCE:
            r["becomes"] = "stairs"
        elif e["key"] in S.sw_of_exit and S.sw_of_exit[e["key"]] is not None:
            r["becomes"] = "stairwell"
            r["stairwell"] = S.sw_of_exit[e["key"]]["id"]
        else:
            r["becomes"] = "portal"
            r.update(portal_reason(e, s, t, dist, dl))
        return r

    def portal_reason(e, s, t, dist, dl):
        if e["key"] in S.slope_failed_exit:
            return {"portal_reason": "slope-blocked", "slope_reachable": True,
                    "portal_reason_text": "{} tiles and {} layers apart, but no void sloped chain exists".format(
                        dist, abs(dl))}
        if e["key"] in S.sw_of_exit:
            return {"portal_reason": "stairwell-blocked",
                    "portal_reason_text": "aligned over {} layers, but no void stair chain exists".format(abs(dl))}
        if dl == 0:
            return {"portal_reason": "same-layer",
                    "portal_reason_text": "both ends on layer {}, {} tiles apart".format(s[2], dist)}
        ends_ = [x for x in (e["source_group"], e["target_group"]) if x != ow_id]
        if not ends_:
            return {"portal_reason": "overworld",
                    "portal_reason_text": "overworld exit whose ends are {} tiles and {} layer(s) apart".format(
                        dist, abs(dl))}
        later = max(ends_, key=lambda x: S.order.index(x) if x in S.order else -1)
        p = S.placed[later]
        main = (S.placements.get(later) or {}).get("main_connection")
        slope = 2 <= dist <= abs(dl) * STAIR_TOLERANCE
        if p["shift"] != [0, 0]:
            code, text = "group-shifted", "{} was shifted {} from its ideal spot".format(later, p["shift"])
        elif main and main["source_map"] == e["source_map"] and main["source_tile"] == [e["source_x"], e["source_y"]]:
            code, text = "main-entrance-offset", "the main connection itself lands {} tiles away".format(dist)
        else:
            code = "other-entrance-aligned"
            text = "{} is aligned to its main connection; this exit's ends are {} tiles and {} layer(s) apart".format(
                later, dist, abs(dl))
        out = {"portal_reason": code, "portal_reason_text": text}
        if slope:
            out["slope_reachable"] = True
        return out

    def exit_record(e, with_eval=True):
        src = exit_source_fields(e)
        r = {k: src.pop(k) for k in ("source_map", "source_tile", "target_map", "target_tile", "arch", "line",
                                     "form", "vertical")}
        r.update(source_group=e["source_group"], target_group=e["target_group"])
        r.update(src)
        if e.get("wired"):
            r["wired"] = e["wired"]
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

    # Sensitivity: the same placement with the shipped client's view radius.
    _o, sens_left1, _t, sens_left2, sens_failed = run_placement(CLIENT_VIEW_RADIUS)
    sens_placed = {gid: dict(p) for gid, p in S.placed.items()}
    sensitivity = {
        "view_radius_tiles": CLIENT_VIEW_RADIUS,
        "why": "radius of the shipped classic client's largest view (wire size 21 = MAP_LOOK_SIZE_MAX 17 + 2 * "
               "MAP_RENDER_OVERSCAN); the plan uses the server bound {} instead".format(VIEW_RADIUS),
        "crossing_exit_objects_after_placement": dict(sorted(becomes_counts().items())),
        "stairwells": len(S.stairwells),
        "stairwell_sites_blocked": len(S.blocked_sites),
        "groups_unplaced": sorted(set(sens_left1) | set(sens_left2) | set(sens_failed)),
    }
    order1, left1, order_t, left2, failed_placements = run_placement(VIEW_RADIUS)
    # Owner decision 2 (2026-10-19): every fallback dresses exactly the stairwells the owner chose it for (the
    # same records, by premise)
    dressed = collections.defaultdict(list)
    for sw in S.stairwells:
        if id(sw) in S.fallback_of:
            dressed[S.fallback_of[id(sw)]].append((sw["id"], stairwell_premise(sw)))
    for a, (_app, chosen) in sorted(STAIRWELL_APPEARANCE_FALLBACKS.items()):
        if dressed.get(a, []) != list(chosen):
            raise SystemExit("owner decision {} chose stair art for the {} exits of {}, but it now dresses {} (id, "
                             "premise); ask the owner again".format(DECIDED_2026_10_19, a, list(chosen),
                                                                    dressed.get(a, [])))
    place_index = {gid: i for i, gid in enumerate(S.order)}
    placements = S.placements
    sensitivity["groups_placed_differently"] = sorted(
        gid for gid, p in S.placed.items()
        if gid in sens_placed and (sens_placed[gid]["dx"], sens_placed[gid]["dy"], sens_placed[gid]["L"])
        != (p["dx"], p["dy"], p["L"]))

    # ------------------------------- wire dangling exits (owner decision 2026-10-18)
    # A tiled up/down exit without a tile link leads nowhere in legacy. When the
    # tile above (or below), plus the exit's xrays landing offset, is occupied by
    # other content after placement, the exit is wired to it as plain stairs.
    # A working exit on the same tile whose destination lies within one tile of
    # that landing is the same staircase, so its destination is used instead.
    def tile_objects(m, x, y):
        """Objects on tile (x, y) of map m, multipart objects included through their tails."""
        out = []
        for o in maps[m].objects:
            ox, oy = md.to_int(o.fields.get("x"), 0), md.to_int(o.fields.get("y"), 0)
            tails = archs.get(o.name.casefold(), {}).get("__tails__", ())
            if (ox, oy) == (x, y) or any((ox + tx, oy + ty) == (x, y) for tx, ty in tails):
                out.append((o.name, md.effective_attrs(o, archs)))
        return out

    terrain_names = {"0": "nothing", "1": "land", "2": "water", "4": "deep water", "8": "fire", "16": "fire",
                     "32": "cloud", "64": "shallow water"}
    wired, kept_decoration = [], []
    for e in exits:
        if e["form"] != "tiled-unresolved" or e.get("tiled_slot") not in (9, 10):
            continue
        sg = e["source_group"]
        s_ = plane_tile(sg, e["source_map"], e["source_x"], e["source_y"])
        if s_ is None:
            continue
        dz = 1 if e["tiled_slot"] == 9 else -1
        lx, ly = e["landing_offset"]
        land = (s_[0] + lx, s_[1] + ly, s_[2] + dz)
        occ_owner = S.occ.occupant(land[2], (land[0], land[1]))
        if occ_owner is None or occ_owner == owner_of(sg, e["source_map"]):
            continue
        target, how = land, "the tile {} (plus the landing offset {})".format(
            "above" if dz > 0 else "below", [lx, ly])
        co = []
        for o in by_tile[(e["source_map"], e["source_x"], e["source_y"])]:
            if o is e or not o.get("resolved_map") or o["target_group"] not in (ow_id, *S.placed):
                continue
            pt = plane_tile(o["target_group"], o["resolved_map"], o["resolved_x"], o["resolved_y"])
            co.append({"arch": o["arch"], "target_map": o["resolved_map"], "target_tile": [o["resolved_x"], o["resolved_y"]],
                       "plane_target": list(pt)})
            if pt[2] == land[2] and cheb(pt, land) <= 1 and target is land:
                target, how = tuple(pt), "the destination of the {} on the same tile, {} tile(s) from the landing".format(
                    o["arch"], cheb(pt, land))
        towner = S.occ.occupant(target[2], (target[0], target[1]))
        tm = map_at(towner, target) if towner is not None else None
        if tm is None:
            continue
        if gof[tm] == ow_id:
            X, Y, _Z = maps[tm].coords
            tx, ty = target[0] - 24 * X, target[1] - 24 * Y
        else:
            pl = S.placed[gof[tm]]
            ox, oy, _oz = G[gof[tm]]["layout"]["offsets"][tm]
            tx, ty = target[0] - ox - pl["dx"], target[1] - oy - pl["dy"]
        src_objs = [n for n, _a in tile_objects(e["source_map"], e["source_x"], e["source_y"])]
        dst = tile_objects(tm, tx, ty)
        floors_ = [a for _n, a in dst if a.get("layer") == "1"]
        terr = sorted({terrain_names.get(a.get("terrain_type", "0"), a.get("terrain_type")) for a in floors_})
        blocked_ = (tx, ty) in maps[tm].blocked or any(a.get("no_pass") == "1" for _n, a in dst)
        back = []
        for o in exits:
            if o["source_map"] == tm and o.get("resolved_map") == e["source_map"] and \
                    cheb((o["source_x"], o["source_y"]), (tx, ty)) <= 3 and \
                    cheb((o["resolved_x"], o["resolved_y"]), (e["source_x"], e["source_y"])) <= 3:
                back.append({"source_tile": [o["source_x"], o["source_y"]], "arch": o["arch"],
                             "target_tile": [o["resolved_x"], o["resolved_y"]]})
        entr = [plane_tile(x["source_group"], x["source_map"], x["source_x"], x["source_y"])
                for x in G[sg]["entrances"] if x["source_group"] in (ow_id, *S.placed)]
        near_entr = min([cheb(p_, target) for p_ in entr if p_ and p_[2] == target[2]] or [None]) \
            if any(p_ and p_[2] == target[2] for p_ in entr) else None
        if not floors_:
            verdict, why = "doubtful", "the landing tile has no floor"
        elif blocked_:
            verdict, why = "doubtful", "the landing tile holds a no_pass object"
        elif terr != ["land"]:
            verdict, why = "doubtful", ("the landing floor is {} (terrain_type {}), which a walking player cannot "
                                        "enter (classic blocked(), server/src/server/map.c:697)".format(
                                            "/".join(terr), "/".join(sorted({a.get("terrain_type", "0")
                                                                             for a in floors_}))))
        elif back or how.startswith("the destination"):
            verdict, why = "confirmed", "a working exit on the same tile or a return exit beside the landing " \
                                        "already joins the same two places"
        else:
            verdict, why = "plausible", "walkable land floor on the landing tile and no exit contradicts it"
        if near_entr is not None:
            why += "; the landing is {} tile(s) from this group's nearest entrance on that layer".format(near_entr)
        evidence = {"target_from": how, "plane_landing": list(land), "plane_target": list(target),
                    "source_tile_objects": src_objs, "landing_tile_objects": [n for n, _a in dst],
                    "landing_floor_terrain": terr, "landing_blocked": blocked_,
                    "exits_on_source_tile": co, "return_exits_near_landing": back,
                    "verdict": verdict, "why": why}
        decision = OWNER_KEEP_AS_DECORATION.get((e["source_map"], (e["source_x"], e["source_y"]), e["arch"]))
        if decision:
            kept_decoration.append({"source_map": e["source_map"], "source_tile": [e["source_x"], e["source_y"]],
                                    "arch": e["arch"], "line": e["line"], "plane_source": list(s_),
                                    "decision": decision,
                                    "migrate_as": "decoration with the {} look: no exit, no destination".format(
                                        e["arch"]),
                                    "would_land_on": {"map": tm, "tile": [tx, ty], **evidence}})
            continue
        e2 = dict(e, form="wired", resolved_map=tm, resolved_x=tx, resolved_y=ty, target_group=gof[tm])
        e2["wired"] = evidence
        wired.append(e2)
        if sg == gof[tm] == ow_id:
            ow_internal.append(e2)  # wired to another overworld map: an overworld exit, not a crossing
            continue
        cross.append(e2)
        G[sg]["exits_out"].append(e2)
        G[gof[tm]]["entrances"].append(e2)
        for a, b_end in ((sg, (gof[tm], tm, target[2])), (gof[tm], (sg, e["source_map"], s_[2]))):
            if a == ow_id:
                continue
            related[a].add(owner_of(b_end[0], b_end[1]))
            if b_end[0] == ow_id:
                related_ow_layers[a][b_end[1]].add(b_end[2])

    matched = {(d["source_map"], tuple(d["source_tile"]), d["arch"]) for d in kept_decoration}
    stale = sorted(k for k in OWNER_KEEP_AS_DECORATION if k not in matched)
    if stale:
        raise SystemExit("owner decision lists exits to keep as decoration that are no longer dangling exits "
                         "landing on content: {}".format(stale))
    # Owner decision 3 (2026-10-19) aligns groups that have no exit of their own; a wired exit would give one
    for gid in sorted(OWNER_ALIGN):
        if G[gid]["entrances"] or G[gid]["exits_out"]:
            raise SystemExit("owner decision {} aligns {} under {}, but wiring gave the group an exit; ask the owner "
                             "again".format(DECIDED_2026_10_19, gid, OWNER_ALIGN[gid]["anchor"]))

    stats = collections.defaultdict(collections.Counter)
    portal_reasons = collections.Counter()
    separate_portals = 0
    for e in cross:
        ev = eval_exit(e)
        if ev is None:
            separate_portals += 1
            continue
        stats["__total__"][ev["becomes"]] += 1
        if ev["becomes"] == "portal":
            portal_reasons[ev["portal_reason"]] += 1
        for gid in {e["source_group"], e["target_group"]} - {ow_id}:
            stats[gid][ev["becomes"]] += 1

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
    problems["dangling_exits_wired"] = [{"source_map": e["source_map"], "source_tile": [e["source_x"], e["source_y"]],
                                         "arch": e["arch"], "line": e["line"], "target_map": e["resolved_map"],
                                         "target_tile": [e["resolved_x"], e["resolved_y"]], "verdict": e["wired"]["verdict"]}
                                        for e in wired]
    problems["dangling_exits_kept_as_decoration"] = [
        {"source_map": d["source_map"], "source_tile": d["source_tile"], "arch": d["arch"], "line": d["line"],
         "decision": d["decision"]} for d in kept_decoration]

    # ------------------------------------------------------ relayering record
    for gid, Rg in sorted(relay.items()):
        if gid != ow_id and gid in S.placed and Rg.moved:
            raise SystemExit("relayering moves tiles inside placed group {}; per-layer member rows are needed "
                             "before this plan can record it".format(gid))
    R = ow_relay
    reg_ids = {}
    for r in [r for r in R.regions if r["relayered"]]:
        reg_ids[r["index"]] = "R{}".format(len(reg_ids) + 1)
    kept_ids = {}
    for r in [r for r in R.regions if not r["relayered"]]:
        kept_ids[r["index"]] = "K{}".format(len(kept_ids) + 1)

    def maps_count(keys):
        c = collections.Counter(R.tiles[k][0] for k in keys)
        return [{"path": m, "tiles": n} for m, n in sorted(c.items())]

    def finals_count(keys):
        c = collections.Counter(R.final_layer[k] for k in keys)
        return {str(L): n for L, n in sorted(c.items())}

    regions_out, kept_out = [], []
    for r in R.regions:
        ks = r["tiles"]
        zs = [R.H[k] for k in ks]
        xs, ys = [k[1] for k in ks], [k[2] for k in ks]
        rec = {"layer_before": r["layer"], "q": r["q"], "tiles": len(ks), "maps": maps_count(ks),
               "z_min": min(zs), "z_max": max(zs), "bbox": [min(xs), min(ys), max(xs), max(ys)], "why": r["why"]}
        if r["relayered"]:
            u_shift = sorted({R.du.get(R.unit_of[k], 0) for k in ks if k in R.unit_of} or {0})
            dr = R.dr.get(r["index"], 0)
            rests = [R.rest(k) for k in ks]
            if dr:
                how = "lowered in place: moved down {} layer(s), then relayered up {}; it keeps its layer".format(-dr, r["q"])
            elif u_shift != [0]:
                how = "lowered with its whole cave by {} layer(s), then relayered up {}".format(-u_shift[0], r["q"])
            else:
                how = "relayered up {} layer(s)".format(r["q"])
            rec.update(id=reg_ids[r["index"]], layer_after=sorted({R.final_layer[k] for k in ks}),
                       rest_min=min(rests), rest_max=max(rests), resolution=how,
                       in_place_adjustment=dr, final_layers=finals_count(ks))
            regions_out.append(rec)
        else:
            rec.update(id=kept_ids[r["index"]], kept="z stays as tile height: under {} tiles and touching no "
                                                   "relayered region".format(relayer.MIN_REGION_TILES))
            kept_out.append(rec)
    region_of_tile = {k: reg_ids[R.region_of[k]] for k in R.relayered}

    unit_moves = collections.defaultdict(int)
    for mv in R.moves:
        if mv["kind"] in ("storey-up", "cave-down"):
            unit_moves[mv["unit"]] += mv["shift"]
    units_out = []
    for u, sh in sorted(unit_moves.items(), key=lambda kv: (-kv[1] if kv[1] > 0 else 0, R.units[kv[0]]["layer"],
                                                            R.units[kv[0]]["tiles"][0])):
        ks = R.units[u]["tiles"]
        rec = {"id": "U{}".format(len(units_out) + 1), "kind": "storey up" if sh > 0 else "cave down",
               "layer_before": R.units[u]["layer"], "shift": sh, "tiles": len(ks), "maps": maps_count(ks),
               "final_layers": finals_count(ks), "regions": sorted({region_of_tile[k] for k in ks if k in region_of_tile},
                                                                   key=lambda t: int(t[1:])),
               "bbox": [min(k[1] for k in ks), min(k[2] for k in ks), max(k[1] for k in ks), max(k[2] for k in ks)]}
        # the regions that made it move: raised ground under a storey, raised parts inside a cave
        linked = set(rec["regions"])
        if sh > 0:
            for k in ks:
                for L in R.columns.get((k[1], k[2]), []):
                    if L < k[0] and (L, k[1], k[2]) in region_of_tile:
                        linked.add(region_of_tile[(L, k[1], k[2])])
        rec["linked_regions"] = sorted(linked, key=lambda t: int(t[1:]))
        if sh > 0:
            under = collections.Counter()
            for k in ks:
                below = [L for L in R.columns.get((k[1], k[2]), []) if L < k[0]]
                if not below:
                    under["over void"] += 1
                else:
                    b = (max(below), k[1], k[2])
                    under["over raised ground" if R.final_layer[b] != b[0] else "over unmoved ground"] += 1
            rec["stands_on"] = dict(sorted(under.items()))
            rec["why"] = "it stands on ground that is relayered up; it moves with it to keep its storey above"
        units_out.append(rec)
    unit_id = {}
    for u, rec in zip(sorted(unit_moves, key=lambda u: (-unit_moves[u] if unit_moves[u] > 0 else 0,
                                                         R.units[u]["layer"], R.units[u]["tiles"][0])), units_out):
        unit_id[u] = rec["id"]
    caves_out = []
    for c in R.caves:
        caves_out.append(dict(c, unit=unit_id.get(c["unit"], "{}'s cave".format(
                                  ", ".join(reg_ids[r] for r in c["regions"]))),
                              regions=[reg_ids[r] for r in c["regions"]]))
    for rec in units_out:
        if rec["kind"] == "cave down":
            rec["why"] = next(c["why"] for c in caves_out if c["unit"] == rec["id"])

    moved_maps = collections.defaultdict(collections.Counter)
    for k in R.moved:
        moved_maps[R.tiles[k][0]][R.final_layer[k]] += 1
    maps_moved = []
    for m in sorted(moved_maps, key=lambda m: (maps[m].coords[2], maps[m].coords[1], maps[m].coords[0])):
        allf = collections.Counter(R.final_layer[(maps[m].coords[2], 24 * maps[m].coords[0] + x,
                                                   24 * maps[m].coords[1] + y)] for (x, y) in maps[m].occupied)
        maps_moved.append({"path": m, "layer_before": maps[m].coords[2], "tiles": len(maps[m].occupied),
                           "final_layers": {str(L): n for L, n in sorted(allf.items())}})

    seams_out, new_stairs = [], []
    for sm in R.seams():
        pieces = []
        for pc in sm["pieces"]:
            lo, hi = pc["representative_edge"]
            d = {"cell": pc["cell"], "edges": pc["edges"], "continuity": pc["continuity"],
                 "low_tile": lo, "high_tile": hi, "stairs": []}
            for st_ in pc["stairs"]:
                rec = {"id": "NS{}".format(len(new_stairs) + 1), "seam": "S{}".format(sm["index"] + 1),
                       "low": st_["low"], "high": st_["high"],
                       "low_map": R.tiles[tuple(st_["low_tile"])][0], "high_map": R.tiles[tuple(st_["high_tile"])][0],
                       "low_tile_legacy": st_["low_tile"], "high_tile_legacy": st_["high_tile"], "why": st_["why"],
                       "objects": [{"tile": "low", "archetype": SEAM_STAIR_ARCHETYPES["low"], "exit": True,
                                    "exit_to": "high"},
                                   {"tile": "high", "archetype": SEAM_STAIR_ARCHETYPES["high"], "exit": True,
                                    "exit_to": "low"}]}
                new_stairs.append(rec)
                d["stairs"].append(rec["id"])
            if "exit" in pc:
                d["exit"] = pc["exit"]
            pieces.append(d)
        regs_ = sorted({region_of_tile.get(tuple(p_["low_tile"])) or region_of_tile.get(tuple(p_["high_tile"]))
                        for p_ in pieces} - {None}, key=lambda t: int(t[1:]))
        seams_out.append({"id": "S{}".format(sm["index"] + 1), "low_layer": sm["low_layer"],
                          "high_layer": sm["high_layer"], "cut_edges": sm["edges"], "regions": regs_,
                          "maps": sorted({R.tiles[tuple(p_[k])][0] for p_ in pieces for k in ("low_tile", "high_tile")}),
                          "pieces": pieces})
    steep_out = [{"layer": a[0], "tiles": [[a[1], a[2]], [b[1], b[2]]], "maps": sorted({R.tiles[a][0], R.tiles[b][0]}),
                  "z_before": [R.H[a], R.H[b]], "z_after": [R.rest(a), R.rest(b)]} for a, b in R.steep_steps()]
    contacts_out = [{"final_layer": R.final_layer[a] if a in R.moved else R.final_layer[b],
                     "tiles": [list(a), list(b)], "maps": [R.tiles[a][0], R.tiles[b][0]],
                     "walkable": (a in R.H and b in R.H and not R.tiles[a][2] and not R.tiles[b][2]
                                  and abs(R.rest(a) - R.rest(b)) < relayer.WALKABLE_DZ)}
                    for a, b in R.new_contacts()]

    # exits whose overworld ends moved, before and after
    moved_set = set(R.moved)
    relayer_exits = []
    for e in sorted(ow_internal + [x for x in cross if ow_id in (x["source_group"], x["target_group"])],
                    key=lambda x: x["key"]):
        if not e.get("resolved_map") or e["target_group"] is None:
            continue
        ends_ = []
        for gk, mk, xk, yk in (("source_group", "source_map", "source_x", "source_y"),
                               ("target_group", "resolved_map", "resolved_x", "resolved_y")):
            if e[gk] == ow_id:
                X, Y, Z = maps[e[mk]].coords
                ends_.append((Z, 24 * X + e[xk], 24 * Y + e[yk]))
        if not any(k in moved_set for k in ends_):
            continue
        ev = eval_exit(e)
        if ev is None:
            continue
        before_s = [ev["plane_source"][0], ev["plane_source"][1],
                    maps[e["source_map"]].coords[2] if e["source_group"] == ow_id else ev["plane_source"][2]]
        before_t = [ev["plane_target"][0], ev["plane_target"][1],
                    maps[e["resolved_map"]].coords[2] if e["target_group"] == ow_id else ev["plane_target"][2]]
        rec = {"source_map": e["source_map"], "source_tile": [e["source_x"], e["source_y"]],
               "target_map": e["resolved_map"], "target_tile": [e["resolved_x"], e["resolved_y"]],
               "arch": e["arch"], "line": e["line"], "layer_delta_before": before_t[2] - before_s[2],
               "layer_delta_after": ev["layer_delta"], "xy_distance": ev["xy_distance"], "becomes": ev["becomes"]}
        if e["source_group"] != ow_id or e["target_group"] != ow_id:
            rec["crosses_to_group"] = e["target_group"] if e["source_group"] == ow_id else e["source_group"]
        for k in ("stairwell", "portal_reason"):
            if k in ev:
                rec[k] = ev[k]
        relayer_exits.append(rec)
    # Every overworld exit with a destination, so the verifier can derive which
    # exits the relayering touches instead of trusting relayering.exits.
    ow_exit_records = [exit_record(e) for e in ow_internal if e.get("resolved_map")]

    affected = R.affected_maps()  # the maps the continuity graph uses; their heights go into the JSON

    def height_rows(m):
        rec_ = maps[m]
        out = []
        for y in range(rec_.height):
            row = []
            for x in range(rec_.width):
                h = rec_.support_height((x, y))
                f = rec_.floors.get((x, y), {})
                ground = "/{}".format(f[0]) if len(f) > 1 and 0 in f else ""  # sub-layer 0 floor under others
                row.append("." if h is None else "{}{}{}".format(h, ground, "b" if (x, y) in rec_.blocked else ""))
            out.append(" ".join(row))
        return out

    split_out = []
    for k in sorted(R.split):
        m = R.tiles[k][0]
        X, Y, Z = maps[m].coords
        split_out.append({"map": m, "tile": [k[1] - 24 * X, k[2] - 24 * Y], "floors": {str(sl): z for sl, z in sorted(
            R.tiles[k][1].items())}, "ground_layer": R.split[k], "upper_layer": R.final_layer[k]})

    judgement = []
    for rec in units_out:
        if rec["kind"] == "storey up" and rec["stands_on"].get("over unmoved ground"):
            judgement.append({"kind": "storey partly over unmoved ground", "unit": rec["id"],
                              "text": "{} ({}) moves up {} layer(s); {} of its {} tiles stand on ground that stays".format(
                                  rec["id"], ", ".join(base_name(x["path"]) for x in rec["maps"]), rec["shift"],
                                  rec["stands_on"]["over unmoved ground"], rec["tiles"])})
    for c in caves_out:
        judgement.append({"kind": "cave resolution", "unit": c["unit"],
                          "text": "{} ({} tiles, regions {}): {}; {}".format(
                              c["unit"], c["tiles"], ", ".join(c["regions"]), c["choice"].replace("-", " "), c["why"])})
    one_way = [sw["id"] for sw in S.stairwells if sw["kind"] == "relayering" and sw["direction"] != "both"]
    if one_way:
        judgement.append({"kind": "one-way stairwells",
                          "text": "{} ({}) replace legacy exits that only go one way, so each has exits at one end and "
                                  "a landing at the other; making them two-way would add a way in that legacy does "
                                  "not have".format(len(one_way), ", ".join(one_way))})
    if steep_out:
        judgement.append({"kind": "steep steps", "text": "{} walkable edge(s) become a fall on the same layer".format(
            len(steep_out))})
    if contacts_out:
        judgement.append({"kind": "new contacts", "text": "{} tile pair(s) become neighbours on a layer".format(
            len(contacts_out))})
    if split_out:
        judgement.append({"kind": "split tiles", "text": "{} tile(s) carry a low floor on sub-layer 0 and a raised "
                                                         "floor on a higher sub-layer; the raised part moves, the "
                                                         "ground part stays".format(len(split_out))})
    if kept_out:
        judgement.append({"kind": "kept regions", "text": "{} small raised region(s) ({} tiles) keep z as tile "
                                                          "height".format(len(kept_out),
                                                                          sum(r["tiles"] for r in kept_out))})
    if new_stairs:
        judgement.append({"kind": "new stairs", "text": "{} new stair pair(s) keep the {} cut seam piece(s) and every "
                                                        "pocket they leave walkable; archetypes default to {}".format(
                                                            len(new_stairs), sum(len(sm["pieces"]) for sm in seams_out),
                                                            seam_stairs_text())})
    # Owner decision 6 (2026-10-19): the relayering judgement calls are confirmed exactly as listed
    confirmed_calls = check_judgement_calls(judgement, OWNER_RELAYERING_JUDGEMENT_CALLS)
    group_regions = {gid: [{"layer": r["layer"], "q": r["q"], "tiles": len(r["tiles"]),
                            "maps": sorted({Rg.tiles[k][0] for k in r["tiles"]}), "relayered": r["relayered"]}
                           for r in Rg.regions] for gid, Rg in sorted(relay.items()) if gid != ow_id and Rg.regions}
    relayering = {
        "status": "applied (owner decision 2026-10-18: fix the conflicts, then relayer)",
        "height": "support height: the highest floor z over all sub-layers (classic map_space_support_height(), "
                  "server/src/socket/request.c:1012-1022)",
        "rule": "H = {h} * q + rest with q = H // {h} for H >= {h}; a relayered tile moves q layers up and keeps rest "
                "as its tile height; regions are 8-connected tiles of one layer with the same q".format(h=LAYER_HEIGHT_Z),
        "layer_height_z": LAYER_HEIGHT_Z, "client_level_pixel_height": CLIENT_LEVEL_PIXEL_HEIGHT,
        "min_region_tiles": relayer.MIN_REGION_TILES, "walkable_dz": relayer.WALKABLE_DZ,
        "seam_cell": relayer.SEAM_CELL,
        "tiles_scanned": len(R.tiles), "tiles_with_floor": len(R.H),
        "tiles_at_or_above_layer_height": len(R.q), "tiles_raised_by_upper_sub_layers": R.raised_by_upper,
        "tiles_relayered": len(R.relayered), "tiles_moved": len(R.moved),
        "regions": regions_out, "kept_regions": kept_out, "units_moved": units_out, "caves": caves_out,
        "maps_moved": maps_moved, "split_tiles": split_out, "seams": seams_out, "new_stairs": new_stairs,
        "steep_steps": steep_out, "new_contacts": contacts_out, "exits": relayer_exits,
        "stairwells_blocked": S.relayer_blocked, "unresolved": R.unresolved, "overlaps": len(R.overlaps()),
        "exit_ends_on_void_tiles": [{"map": m, "tile": [x, y], "layer": L}
                                    for (m, x, y), L in sorted(void_exit_ends.items())],
        "group_regions": group_regions, "judgement_calls": judgement,
        "tile_heights": {m: height_rows(m) for m in sorted(affected)},
        "tile_heights_format": "per tile: support height, then /G when the tile has more than one floor and G is "
                               "its sub-layer 0 floor, then b when it holds a no_pass object; . = no floor",
    }

    # Owner decision 6 (2026-10-19): the judgement calls, and every record they summarise, are as confirmed
    now = premise(relayering_evidence(relayering, S.stairwells))
    if now != OWNER_RELAYERING_PREMISE:
        raise SystemExit("owner decision {} confirmed the relayering judgement calls on other records (their premise "
                         "is now {}); ask the owner again".format(DECIDED_2026_10_19, now))

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

    used_layers = set()
    used_cells = collections.defaultdict(set)  # layer -> 24x24 cells holding placed or stairwell tiles
    for gid, p in S.placed.items():
        for lz, pts in local_tiles[gid].items():
            used_layers.add(lz + p["L"])
            used_cells[lz + p["L"]].update(((x + p["dx"]) // 24, (y + p["dy"]) // 24) for (x, y) in pts)
    for sw in S.stairwells:
        for t in sw["tiles"][1:-1]:
            used_layers.add(t["layer"])
            used_cells[t["layer"]].add((t["x"] // 24, t["y"] // 24))

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
            # Rows per final layer: every map with a tile on a layer that holds placed
            # tiles or stairwell tiles, every map off the surface layer, and every
            # map the relayering touches (so the relayering can be re-derived).
            stacked = {(maps[m].coords[0], maps[m].coords[1]) for m in g["maps"] if maps[m].coords[2] != 0}
            keep = []
            for m in sorted(g["maps"], key=lambda m: (maps[m].coords[2], m)):
                pcs = ow_pieces.get(m, {})
                X, Y, _Z = maps[m].coords
                near = any((X + i, Y + j) in used_cells.get(L, ()) for L in pcs for i in (-1, 0, 1) for j in (-1, 0, 1))
                if maps[m].coords[2] != 0 or m in affected or near or (X, Y) in stacked:
                    keep.append(m)
            rec["occupied_rows_by_layer"] = {
                m: {str(L): ["".join("#" if (24 * maps[m].coords[0] + x, 24 * maps[m].coords[1] + y) in ow_pieces[m][L]
                                     else "." for x in range(maps[m].width)) for y in range(maps[m].height)]
                    for L in sorted(ow_pieces.get(m, {})) if ow_pieces[m][L]}
                for m in keep}
            rec["internal_exits"] = ow_exit_records
            groups_out.append(rec)
            continue
        rec["layout_root"] = g["layout"]["root"]
        p = placements.get(gid)
        placed_ok = p and not p.get("failed")
        rec["maps"] = []
        for m in g["maps"]:
            mr = {"path": m, "width": maps[m].width, "height": maps[m].height,
                  "name": maps[m].header.get("name"), "region": maps[m].header.get("region"),
                  "local_offset": list(g["layout"]["offsets"][m]),
                  "tile_links": {md.SLOT_NAMES[s]: t for s, t in sorted(maps[m].links.items())},
                  "occupied_tiles": len(maps[m].occupied)}
            if placed_ok:
                mr["occupied_rows"] = occupied_rows(maps[m])
            rec["maps"].append(mr)
        rec["local_bounding_boxes"] = bbox_layers(gid)
        rec["layout_problems"] = g["layout"]["problems"]
        rec["related_owners"] = sorted(related[gid])
        rec["related_overworld_layers"] = {m: sorted(v) for m, v in sorted(related_ow_layers.get(gid, {}).items())}
        rec["entrances_from_overworld"] = [exit_record(e) for e in g["entrances"] if e["source_group"] == ow_id]
        rec["exits_to_overworld"] = [exit_record(e) for e in g["exits_out"] if e["target_group"] == ow_id]
        rec["entrances_from_other_groups"] = [exit_record(e) for e in g["entrances"] if e["source_group"] != ow_id]
        rec["exits_to_other_groups"] = [exit_record(e) for e in g["exits_out"] if e["target_group"] != ow_id]
        if g["script_refs"]:
            rec["script_and_dialog_references"] = g["script_refs"]
        if g["redirect_refs"]:
            rec["script_redirects"] = g["redirect_refs"]
        if placed_ok:
            c = p["chosen"]
            rec["placement"] = {
                "status": "tentative" if p["tentative"] else "proposed",
                "layer_offset": c["L"], "x_offset": c["dx"], "y_offset": c["dy"],
                "plane_tile_formula": "plane = local_offset(map) + (x,y) + ({}, {}); plane layer = local layer + ({})".format(
                    c["dx"], c["dy"], c["L"]),
                "plane_bounding_boxes": bbox_layers(gid, c["dx"], c["dy"], c["L"]),
                "occupied_tiles_by_layer": {str(lz + c["L"]): len(v) for lz, v in local_tiles[gid].items()},
                "member_origins": {m: [o[0] + c["dx"], o[1] + c["dy"], o[2] + c["L"]]
                                   for m, o in sorted(g["layout"]["offsets"].items())},
                "orientation": p["orientation"], "orientation_evidence": p["orientation_evidence"],
                "vertical_bounds_from_exit_semantics": p["vertical_bounds"],
                "constraint_note": p["constraint_note"],
                "main_connection": p["main_connection"],
                "ideal": p["ideal"], "shift_from_ideal": c["shift"], "extra_layers_from_ideal": c["extra_layers"],
                "conflicts_resolved": p["conflicts_resolved"],
                "placement_index": place_index[gid],
                "main_stairwell_blocked": bool(c.get("main_stairwell_blocked")),
                "crossing_exits_after_placement": dict(sorted(stats[gid].items())),
            }
            if p.get("alignment_reason"):
                rec["placement"]["alignment_reason"] = p["alignment_reason"]
        elif p and p.get("failed"):
            rec["placement"] = {"status": "failed", "ideal": p["ideal"]}
        groups_out.append(rec)

    # Owner decisions checked against the JSON records they were made on
    recs_out = {r["id"]: r for r in groups_out}
    records_out = {(e["source_map"], e["line"]): e for r in groups_out for key in EXIT_LISTS for e in r.get(key, [])}
    # 1: the blocked sites of the exits the owner keeps portals carry the decision
    apply_owner_portals(S.blocked_sites + S.slope_failed + S.relayer_blocked, OWNER_PORTALS, records_out)
    # 4 and 5: the confirmed orientations and classifications, on the same group content
    for gid, (_o, _ev, want) in sorted(OWNER_ORIENTATIONS.items()):
        now = premise(group_evidence(recs_out[gid]))
        if now != want:
            raise SystemExit("owner decision {} confirmed the orientation of {} on other content (its premise is now "
                             "{}); ask the owner again".format(DECIDED_2026_10_19, gid, now))
    for gid, (_c, _r, want) in sorted(OWNER_CLASSIFICATIONS.items()):
        now = premise({"evidence": recs_out[gid]["evidence"][:-1], "group": group_evidence(recs_out[gid])})
        if now != want:
            raise SystemExit("owner decision {} confirmed the classification of {} on other evidence (its premise is "
                             "now {}); ask the owner again".format(DECIDED_2026_10_19, gid, now))
    # 3: the alignment, on the evidence groups' sheets and the stairs between them
    for gid, dec in sorted(OWNER_ALIGN.items()):
        now = premise(alignment_evidence(recs_out, dec["evidence_groups"]))
        if gid not in dec["evidence_groups"] or dec["anchor"] not in dec["evidence_groups"] or now != dec["premise"]:
            raise SystemExit("owner decision {} aligns {} under {} on the layout of {} and the exits between them; "
                             "that evidence changed (its premise is now {}); ask the owner again".format(
                                 DECIDED_2026_10_19, gid, dec["anchor"], list(dec["evidence_groups"]), now))

    changes_v1 = []
    for gid in sorted(S.placed):
        short_id = gid[len("/shattered_islands/"):] if gid.startswith("/shattered_islands/") else gid
        c = S.placed[gid]
        now = [c["dx"], c["dy"], c["L"]]
        old = PLAN_V1_PLACEMENTS.get(short_id)
        if old is None or list(old) != now:
            pl = placements[gid]
            changes_v1.append({"group": gid, "v1": None if old is None else list(old), "v2": now,
                               "ideal_v2": [pl["ideal"]["dx"], pl["ideal"]["dy"], pl["ideal"]["L"]],
                               "blocked_by": sorted({"{} ({}, layer {})".format(base_name(x["blocked_by"]), x["rule"],
                                                                             x["plane_layer"])
                                                     for x in pl["conflicts_resolved"]})})
    # Owner decision 6 (2026-10-19) confirmed the placements the lowered caves push deeper; decision 4 the
    # orientation of four groups. Both must still be what this run produced.
    got_changes = [(x["group"][len("/shattered_islands/"):], tuple(x["v1"] or ()), tuple(x["v2"])) for x in changes_v1]
    if got_changes != list(OWNER_PLACEMENT_CHANGES_FROM_V1):
        raise SystemExit("owner decision {} confirmed the placement changes from version 1 {}, but this run makes "
                         "{}; ask the owner again".format(DECIDED_2026_10_19, list(OWNER_PLACEMENT_CHANGES_FROM_V1),
                                                          got_changes))
    # Owner decision 7 (2026-10-19) keeps groups placed that may have no footprint: they must be placed, and every
    # placed group without a footprint needs that decision
    for gid in sorted(OWNER_NO_FOOTPRINT):
        if gid not in S.placed:
            raise SystemExit("owner decision {} keeps {} placed without a footprint, but it is not placed; ask the "
                             "owner again".format(DECIDED_2026_10_19, gid))
    for gid in sorted(S.placed):
        if not local_tiles[gid] and gid not in OWNER_NO_FOOTPRINT:
            raise SystemExit("placed group {} has no footprint (none of its maps holds an object); ask the owner "
                             "whether to keep it placed (OWNER_NO_FOOTPRINT)".format(gid))
    for gid, (want, _evidence, _premise) in sorted(OWNER_ORIENTATIONS.items()):
        got = (placements.get(gid) or {}).get("orientation")
        if got != want:
            raise SystemExit("owner decision {} confirmed '{}' for {}, which is {}; ask the owner again".format(
                DECIDED_2026_10_19, want, gid, "placed '{}'".format(got) if got else "not placed"))
    # Every archetype the plan adds must exist in the content: stairwell decorations, the owner's fallbacks
    # and the seam stairs.
    added = {sw["intermediate_appearance"] for sw in S.stairwells if sw["intermediate_appearance"]}
    added |= {app for app, _ids in STAIRWELL_APPEARANCE_FALLBACKS.values()} | set(SEAM_STAIR_ARCHETYPES.values())
    missing = sorted(a for a in added if a.casefold() not in archs)
    if missing:
        raise SystemExit("the plan adds archetypes the content does not define: {}".format(missing))

    def defined_in(a):
        return archs[a.casefold()]["__file__"]

    counts = collections.Counter()
    for g in groups:
        if g["id"] == ow_id:
            continue
        counts["{}:{}".format(g["classification"], g["reason"])] += 1
    sw_spans = collections.Counter(sw["top_layer"] - sw["bottom_layer"] for sw in S.stairwells)
    plan = {
        "schema": "atrinik-overworld-placement-plan/5",
        "generated_by": "scripts/build_plan.py",
        "content_root": str(content),
        "content_git_head": revision["head"],
        "content_worktree_clean": revision["clean"],
        "content_inputs_digest": digest_before,
        "settings": {"footprint": "occupied-tiles", "view_radius_tiles": VIEW_RADIUS,
                     "contact_margin_tiles": CONTACT_MARGIN, "max_extra_layers": MAX_EXTRA_LAYERS,
                     "max_shift_tiles": MAX_SHIFT, "stair_tolerance_tiles": STAIR_TOLERANCE,
                     "stairwell_max_bend_tiles": STAIRWELL_MAX_BEND, "layer_height_z": LAYER_HEIGHT_Z,
                     "relayer_min_region_tiles": relayer.MIN_REGION_TILES, "walkable_dz": relayer.WALKABLE_DZ,
                     "owner_keep_as_decoration": [[m, list(t), a] for (m, t, a) in sorted(OWNER_KEEP_AS_DECORATION)],
                     # owner decisions of 2026-10-19 (see the constants in build_plan.py)
                     "owner_portals": [{"source_map": m, "line": ln, "source_tile": list(t), "arch": a,
                                        "premise": d["premise"], "decided": DECIDED_2026_10_19,
                                        "decision": d["decision"]}
                                       for (m, ln, t, a), d in sorted(OWNER_PORTALS.items())],
                     "stairwell_appearance_fallbacks": [
                         {"exit_arch": a, "appearance": app, "appearance_defined_in": defined_in(app),
                          "stairwells": [{"id": i, "premise": pr} for i, pr in chosen], "decided": DECIDED_2026_10_19,
                          "decision": STAIRWELL_APPEARANCE_DECISION}
                         for a, (app, chosen) in sorted(STAIRWELL_APPEARANCE_FALLBACKS.items())],
                     "owner_alignments": [{"group": gid, "anchor": d["anchor"], "layer_delta": d["layer_delta"],
                                           "evidence_groups": list(d["evidence_groups"]), "premise": d["premise"],
                                           "decided": DECIDED_2026_10_19, "why": d["why"]}
                                          for gid, d in sorted(OWNER_ALIGN.items())],
                     "owner_no_footprint": [{"group": gid, "decided": DECIDED_2026_10_19, "decision": why}
                                            for gid, why in sorted(OWNER_NO_FOOTPRINT.items())],
                     "owner_orientations": [{"group": gid, "orientation": o, "evidence": list(ev), "premise": pr,
                                             "decided": DECIDED_2026_10_19, "decision": ORIENTATION_DECISION}
                                            for gid, (o, ev, pr) in sorted(OWNER_ORIENTATIONS.items())],
                     "owner_classifications": [{"group": gid, "classification": c, "reason": r, "premise": ev,
                                                "decided": DECIDED_2026_10_19, "decision": CLASSIFICATION_DECISION}
                                               for gid, (c, r, ev) in sorted(OWNER_CLASSIFICATIONS.items())],
                     "owner_relayering_judgement_calls": {
                         "decided": DECIDED_2026_10_19, "decision": RELAYERING_DECISION, "calls": confirmed_calls,
                         "premise": OWNER_RELAYERING_PREMISE,
                         "placement_changes_from_v1": [{"group": "/shattered_islands/" + g, "v1": list(a), "v2": list(b)}
                                                       for g, a, b in OWNER_PLACEMENT_CHANGES_FROM_V1]},
                     "seam_stair_archetypes": dict(SEAM_STAIR_ARCHETYPES, decided=DECIDED_2026_10_19,
                                                   decision="the new seam stairs keep their default archetypes",
                                                   defined_in={k: defined_in(a) for k, a in
                                                               sorted(SEAM_STAIR_ARCHETYPES.items())}),
                     "overworld_map_size": 24, "excluded_trees": list(md.EXCLUDED_TOP)},
        "conventions": {
            "plane": "overworld plane; layer 0 surface, negative below, positive above",
            "local_layout": "each member map gets a group-local origin (x, y, layer) by BFS over tile links from "
                            "layout_root (root origin = filename coordinates*size when present, else 0,0,0); N/S/E/W/"
                            "diagonals offset by neighbour/own width/height exactly as get_map_from_coord(); "
                            "up/down = layer +1/-1",
            "placement": "plane tile = local origin + tile + (x_offset, y_offset); plane layer = local layer + "
                         "layer_offset",
            "footprint": "a map occupies only tiles holding at least one object (occupied_rows: '#'); every other "
                         "legacy tile becomes void",
            "gap_rule": "on one layer, occupied tiles of a placed group and of any owner it shares no exit with "
                        "(overworld map or other placed group) are more than view_radius_tiles apart (Chebyshev); "
                        "owners it shares an exit with are more than contact_margin_tiles apart",
            "stairs": "an exit 'becomes stairs' when after placement source and destination differ by exactly one "
                      "layer and by <= {} tile(s) horizontally".format(STAIR_TOLERANCE),
            "stairwell": "an exit whose ends are >= 2 layers apart and <= {} tile(s) apart horizontally becomes a "
                         "stairwell: one stair tile on every intermediate layer, each consecutive pair one layer and "
                         "<= {} tile apart; the whole stairwell is ONE exit (one use moves the player end to end, "
                         "pathfinding sees one edge)".format(STAIR_TOLERANCE, STAIR_TOLERANCE),
            "sloped_stairwell": "an exit whose ends are >= 2 layers apart and more than {t} but at most {t} tile(s) "
                                "per layer apart horizontally becomes a sloped stairwell (shape 'sloped'): the same "
                                "chain, shifting at most {t} tile sideways per layer; still ONE exit".format(
                                    t=STAIR_TOLERANCE),
            "stairwell_rendering": "segments_drawn_only_on_player_layer: clients draw a stairwell's segments only on "
                                   "the layer the player is on (owner decision 2026-10-18), so view_conflicts are "
                                   "listed but not seen",
            "stairwell_direction": "direction 'both': exits at both ends, each targeting the other end; 'down' or "
                                   "'up': the stairwell replaces a legacy one-way exit and can be used only that "
                                   "way; its landing_only_end holds no exit",
            "stairwell_contacts": "contacts: intermediate tiles within contact_margin_tiles of a related area "
                                  "(other than the stairwell's own group); the chain search avoids them where it can",
            "stairwell_ids": "SWn = stairwell built for a placed group's aligned exits, SLn = sloped stairwell, SRn = "
                             "stairwell for an overworld exit whose ends the relayering put 2+ layers apart",
            "relayering": "overworld tiles are on their final layer everywhere in this plan (see relayering); "
                          "occupied_rows_by_layer gives each map's tiles per final layer",
            "wired_exits": "an exit with form 'wired' led nowhere in legacy; it now targets the content on the "
                           "layer above or below (see its 'wired' evidence)",
            "exits_kept_as_decoration": "dangling exits the owner keeps as decoration (no exit): not wired, with the "
                                        "landing they would have had",
            "stairwell_appearance": "a stairwell's intermediate tiles show the down archetype of the first stair "
                                    "family among its exit archetypes (down-going exits first); failing that, the "
                                    "owner's fallback for the first exit archetype settings.stairwell_appearance_"
                                    "fallbacks lists (decision 2026-10-19)",
            "owner_decisions": "settings.owner_* and the seam stair and appearance settings record the owner's "
                               "decisions with their dates; a blocked stairwell site carries owner_decision when the "
                               "owner keeps its exits portals (settings.owner_portals), and a placement with "
                               "alignment_reason is the owner's fixed spot under its anchor (settings.owner_"
                               "alignments), kept even without a footprint (settings.owner_no_footprint); "
                               "confirmations (orientations, classifications, relayering judgement "
                               "calls) equal what the plan derives; each decision is bound to the evidence it was "
                               "made on (the evidence itself, or its premise: the SHA-256 of the evidence record's "
                               "canonical JSON) and no longer applies when that evidence changes",
            "exit_inventory": "every exit object read from the content (inventory exits excluded), keyed by "
                              "source_map and line: the content fields of its exit record with its legacy form "
                              "and resolved target (none for an exit that leads nowhere), plus the facts its data "
                              "problems come from; the exit records and data problems are checked against it",
        },
        "summary": {"maps_total": len(maps), "groups_total": len(groups), "non_overworld_groups": len(groups) - 1,
                    "by_classification": dict(sorted(counts.items())),
                    "exit_objects": len(exits), "inventory_exit_objects_ignored": inventory_exits,
                    "crossing_exit_objects_after_placement": dict(sorted(stats["__total__"].items())),
                    "portal_reasons": dict(sorted(portal_reasons.items())),
                    "crossing_exit_objects_into_separate_planes": separate_portals,
                    "stairwells": len(S.stairwells),
                    "stairwell_spans_layers": {str(k): v for k, v in sorted(sw_spans.items())},
                    "stairwell_sites_blocked": len(S.blocked_sites),
                    "stairwells_by_kind": dict(sorted(collections.Counter(sw["kind"] for sw in S.stairwells).items())),
                    "sloped_stairwell_sites_blocked": len(S.slope_failed),
                    "relayering_stairwell_sites_blocked": len(S.relayer_blocked),
                    "wired_exits": len(wired),
                    "dangling_exits_kept_as_decoration": len(kept_decoration),
                    "relayering": {"regions": len(regions_out), "kept_regions": len(kept_out),
                                   "tiles_relayered": len(R.relayered), "tiles_moved": len(R.moved),
                                   "maps_moved": len(maps_moved), "units_moved": len(units_out),
                                   "seams": len(seams_out), "new_stairs": len(new_stairs),
                                   "steep_steps": len(steep_out), "new_contacts": len(contacts_out)},
                    "placement_order": order1, "tentative_placement_order": order_t,
                    "unplaced_place_groups": left1, "unplaced_uncertain_groups": left2,
                    "failed_placements": failed_placements},
        "start_map": start_map,
        "view_radius_sensitivity": sensitivity,
        "stairwells": S.stairwells,
        "stairwell_sites_blocked": S.blocked_sites,
        "sloped_stairwell_sites_blocked": S.slope_failed,
        "placement_changes_from_v1": changes_v1,
        "exits_kept_as_decoration": kept_decoration,
        "exit_inventory": sorted((inventory_entry(e) for e in exits), key=lambda r: (r["source_map"], r["line"])),
        "groups": groups_out,
        "relayering": relayering,
        "data_problems": problems,
    }
    if inputs_digest(content) != digest_before:
        raise SystemExit("content inputs changed while the plan was being generated; rerun on a quiet checkout")
    import verify_plan  # noqa: E402
    plan["verification"] = verify_plan.verify(json.loads(json.dumps(plan)))
    return plan, maps, G, ow_id, ow_relay


ARTIFACT_RE = re.compile(r"^(placement-plan\.json|REPORT\.md|layer_-?\d+(_zoom)?\.png|relayer_[a-z0-9_-]+\.png)$")


def publish(stage: Path, out: Path) -> list:
    """Move the complete, verified artifact set from `stage` into `out`.

    Called only after every artifact was generated and the staged JSON
    re-verified. Each file is replaced atomically (os.replace on the same
    filesystem); stale layer images from an earlier run are removed last.
    """
    new = sorted(p.name for p in stage.iterdir() if ARTIFACT_RE.match(p.name))
    for name in new:
        os.replace(stage / name, out / name)
    stale = sorted(p.name for p in out.iterdir() if ARTIFACT_RE.match(p.name) and p.name not in new)
    for name in stale:
        (out / name).unlink()
    return new


MAX_CHECK_ENTRIES = 4096  # the output directory holds a few dozen artifacts and the scripts' folder


def compare_artifacts(stage: Path, out: Path):
    """Artifacts that differ between a fresh run in `stage` and the published set in `out`, or None if
    `out` has more than MAX_CHECK_ENTRIES entries (it is then not a plan's output directory).

    Sizes are compared first and contents in fixed-size chunks (filecmp), so an
    oversized file in `out` is a difference, not a whole-file read."""
    names = set()
    for d in (stage, out):
        with os.scandir(d) as it:
            for n_, entry in enumerate(it):
                if n_ >= MAX_CHECK_ENTRIES:
                    return None
                if ARTIFACT_RE.match(entry.name):
                    names.add(entry.name)
    return [n for n in sorted(names) if not ((stage / n).is_file() and (out / n).is_file()
                                             and filecmp.cmp(stage / n, out / n, shallow=False))]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--content", type=Path, default=DEFAULT_CONTENT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--allow-dirty", action="store_true",
                    help="allow uncommitted changes in the content checkout (experiments only; recorded in the JSON)")
    ap.add_argument("--check", action="store_true",
                    help="regenerate in a temporary directory and fail unless every artifact in --out is "
                         "byte-identical (binds the published plan, its exit inventory included, to the content; "
                         "use the --content path the plan records); writes nothing")
    args = ap.parse_args(argv)
    out = args.out.resolve()
    if not args.check:
        out.mkdir(parents=True, exist_ok=True)
    elif not out.is_dir():
        print("nothing to check: {} is not a directory".format(out))
        return 1
    plan, maps, G, ow_id, ow_relay = build(args.content.resolve(), allow_dirty=args.allow_dirty)
    print(json.dumps(plan["summary"], indent=1))
    if not plan["verification"]["ok"]:
        print("verification FAILED; nothing written:", plan["verification"]["errors"][:10])
        return 1
    import render  # noqa: E402
    import report  # noqa: E402
    import verify_plan  # noqa: E402
    with tempfile.TemporaryDirectory(prefix=".placement-plan-stage-", dir=None if args.check else out) as tmp:
        stage = Path(tmp)
        (stage / "placement-plan.json").write_text(json.dumps(plan, indent=1, sort_keys=False) + "\n",
                                                   encoding="utf-8")
        report.write_report(plan, stage)
        render.render_layers(plan, maps, G, ow_id, stage, ow_relay)
        staged = verify_plan.verify(json.loads((stage / "placement-plan.json").read_text(encoding="utf-8")))
        if not staged["ok"] or staged != plan["verification"]:
            print("staged plan failed verification; nothing written:", staged["errors"][:10])
            return 1
        if args.check:
            differ = compare_artifacts(stage, out)
            if differ is None:
                print("check FAILED: {} has more than {} entries".format(out, MAX_CHECK_ENTRIES))
                return 1
            if differ:
                print("check FAILED: {} artifact(s) in {} differ from a fresh run: {}{}".format(
                    len(differ), out, ", ".join(differ[:10]), ", ..." if len(differ) > 10 else ""))
                return 1
            print("check: all {} artifacts in {} are byte-identical to a fresh run".format(
                sum(1 for p in out.iterdir() if ARTIFACT_RE.match(p.name)), out))
            return 0
        written = publish(stage, out)
    print("wrote {} artifacts to {}".format(len(written), out))
    print("verification: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
