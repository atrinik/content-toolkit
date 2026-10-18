"""Read-only loader for Atrinik legacy archetypes and maps.

Mirrors the Classic server / content tooling semantics that matter for map
placement:

* a map file is any regular file under maps/ whose bytes start with
  ``arch map`` (excluded trees: python, interfaces, celestial-fixtures,
  styles, schemas);
* ``msg``/``endmsg`` bodies are skipped; ``#`` comment lines are skipped;
  ``arch NAME`` opens and ``end`` closes a block only when anchored at the
  start of the line (same rule as tools/content_core/parser.py);
* object attributes = archetype defaults overridden by object fields
  (last value wins, keys case-folded);
* explicit ``tile_path_N`` values resolve relative to the map directory;
  missing slots are derived from the filename coordinate suffix exactly like
  server/src/server/map.c:derive_map_filename_tiles() /
  tools/validate_exits.py:_add_filename_links() (only existing files).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

EXCLUDED_TOP = ("python", "interfaces", "celestial-fixtures", "styles", "schemas")
EXIT_TYPE = "66"
SIGNED_INTEGER_RE = re.compile(r"^-?[0-9]+$")
# slot -> (dx, dy, dz) in *filename coordinate* units (server map_tiled_coords)
FILENAME_TILE_OFFSETS = {
    1: (0, -1, 0),
    2: (1, 0, 0),
    3: (0, 1, 0),
    4: (-1, 0, 0),
    5: (1, -1, 0),
    6: (1, 1, 0),
    7: (-1, 1, 0),
    8: (-1, -1, 0),
    9: (0, 0, 1),
    10: (0, 0, -1),
}
REVERSE_SLOT = {1: 3, 3: 1, 2: 4, 4: 2, 5: 7, 7: 5, 6: 8, 8: 6, 9: 10, 10: 9}
SLOT_NAMES = {1: "N", 2: "E", 3: "S", 4: "W", 5: "NE", 6: "SE", 7: "SW", 8: "NW", 9: "up", 10: "down"}
# freearr_x/freearr_y for directions 1..8 (server/common)
DIR_OFFSETS = {1: (0, -1), 2: (1, -1), 3: (1, 0), 4: (1, 1), 5: (0, 1), 6: (-1, 1), 7: (-1, 0), 8: (-1, -1)}


@dataclass
class Obj:
    name: str
    depth: int
    line: int
    fields: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    has_msg: bool = False


@dataclass
class MapRec:
    path: str  # logical path, e.g. /shattered_islands/world_0_0
    header: dict
    width: int
    height: int
    links: dict  # slot -> logical path (only existing map files)
    link_source: dict  # slot -> "explicit" | "filename"
    broken_links: list  # (slot, value, normalized) explicit links whose target is missing
    objects: list  # top-level Obj
    coords: Optional[tuple]  # filename coordinates (X, Y, Z) or None
    coord_prefix: Optional[str]
    occupied: set = field(default_factory=set)  # local tiles holding at least one object
    ground: dict = field(default_factory=dict)  # local tile -> z of its top sub-layer-0 floor


def parse_blocks(text: str, header_opener: bool):
    """Parse a legacy ADS text into (header_fields, top_level_objects).

    For maps, the first block ``arch map`` is the header. For archetypes the
    top-level opener is ``Object NAME``.
    """
    header = None
    top: list[Obj] = []
    stack: list[Obj] = []
    in_msg = False
    pending_tail = False
    for lineno, raw in enumerate(text.split("\n"), start=1):
        if raw.endswith("\r"):
            raw = raw[:-1]
        anchored = not raw.startswith((" ", "\t"))
        stripped = raw.strip()
        if in_msg:
            if anchored and stripped.casefold() == "endmsg":
                in_msg = False
            continue
        if not stripped or raw.startswith("#"):
            continue
        folded = stripped.casefold()
        if anchored and folded == "msg":
            in_msg = True
            if stack:
                stack[-1].has_msg = True
            continue
        parts = stripped.split(None, 1)
        token = parts[0].casefold()
        value = parts[1].strip() if len(parts) == 2 else ""
        opener = None
        if anchored and token == "arch" and value:
            opener = value
        elif anchored and not header_opener and token == "object" and value and not stack:
            opener = value
        if opener is not None:
            obj = Obj(opener, len(stack), lineno)
            if not stack:
                obj.fields["__tail__"] = "1" if pending_tail else ""
                pending_tail = False
            if stack:
                stack[-1].children.append(obj)
            else:
                top.append(obj)
            stack.append(obj)
            continue
        if anchored and folded == "more" and not header_opener:
            pending_tail = True
            continue
        if anchored and folded == "end":
            if stack:
                stack.pop()
            continue
        if stack:
            stack[-1].fields[token] = value
    if header_opener and top and top[0].name.casefold() == "map":
        header = top[0]
        top = top[1:]
    return header, top


def load_archetypes(content_root: Path) -> dict:
    archs: dict[str, dict] = {}
    for path in sorted((content_root / "arch").rglob("*.arc")):
        if path.is_symlink() or not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        _, objs = parse_blocks(text, header_opener=False)
        head = None
        for obj in objs:
            is_tail = obj.fields.pop("__tail__", "") == "1"
            key = obj.name.casefold()
            if is_tail and head is not None:
                archs[head].setdefault("__tails__", []).append(
                    (to_int(obj.fields.get("x"), 0), to_int(obj.fields.get("y"), 0)))
                continue
            head = key
            # first definition wins if duplicates exist
            if key not in archs:
                archs[key] = dict(obj.fields)
                archs[key]["__file__"] = path.relative_to(content_root).as_posix()
            else:
                head = None
    return archs


def normalize_path(source_map: str, value: str) -> str:
    if value.startswith("/"):
        candidate = value
    else:
        candidate = posixpath.join(posixpath.dirname(source_map), value)
    return "/" + posixpath.normpath(candidate).lstrip("/")


def is_dynamic_path(value: str) -> bool:
    lowered = value.casefold()
    return lowered.startswith("/!") or lowered.startswith("/random/") or lowered in {"!", "random", "/random"}


def filename_coordinates(logical_path: str):
    """Classic filename coordinate rule (validate_exits._filename_coordinates)."""
    basename = posixpath.basename(logical_path)
    coordinates: list[int] = []
    coordinates_length = 0
    for token in basename.split("_"):
        if len(token) > 3:
            continue
        if SIGNED_INTEGER_RE.fullmatch(token):
            coordinates_length += len(token) + 1
            coordinates.append(int(token))
        if len(coordinates) >= 3:
            break
    if len(coordinates) < 2:
        return None
    z = coordinates[2] if len(coordinates) == 3 else 0
    return logical_path[:-coordinates_length], (coordinates[0], coordinates[1], z)


def discover_map_files(content_root: Path) -> dict:
    maps_root = content_root / "maps"
    found: dict[str, bytes] = {}
    all_files: set[str] = set()
    for path in sorted(maps_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(maps_root).as_posix()
        logical = "/" + rel
        all_files.add(logical)
        if rel.split("/", 1)[0] in EXCLUDED_TOP:
            continue
        data = path.read_bytes()
        if data.startswith(b"arch map"):
            found[logical] = data
    return found, all_files


def to_int(value, default=None):
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def load_maps(content_root: Path, archs: dict):
    files, all_files = discover_map_files(content_root)
    maps: dict[str, MapRec] = {}
    for logical, data in files.items():
        text = data.decode("utf-8", errors="replace")
        header_obj, objs = parse_blocks(text, header_opener=True)
        for o in objs:
            o.fields.pop("__tail__", None)
        header = dict(header_obj.fields) if header_obj else {}
        header.pop("__tail__", None)
        width = to_int(header.get("width"), 16)
        height = to_int(header.get("height"), 16)
        links: dict[int, str] = {}
        link_source: dict[int, str] = {}
        broken: list = []
        for slot in range(1, 11):
            value = header.get("tile_path_{}".format(slot))
            if value:
                norm = normalize_path(logical, value)
                if norm in files:
                    links[slot] = norm
                    link_source[slot] = "explicit"
                else:
                    broken.append({"slot": slot, "value": value, "normalized": norm,
                                   "exists_as_non_map_file": norm in all_files})
        parsed = filename_coordinates(logical)
        coords = prefix = None
        if parsed is not None:
            prefix, coords = parsed
            for slot, (dx, dy, dz) in FILENAME_TILE_OFFSETS.items():
                if slot in links or any(b["slot"] == slot for b in broken):
                    # explicit value present (even if broken) suppresses derivation
                    continue
                target = "{}_{}_{}".format(prefix, coords[0] + dx, coords[1] + dy)
                tz = coords[2] + dz
                if tz != 0:
                    target += "_{}".format(tz)
                target = "/" + posixpath.normpath(target).lstrip("/")
                if target in files:
                    links[slot] = target
                    link_source[slot] = "filename"
        occupied = set()
        ground = {}
        for o in objs:
            ox = to_int(o.fields.get("x"), 0)
            oy = to_int(o.fields.get("y"), 0)
            occupied.add((ox, oy))
            base = archs.get(o.name.casefold(), {})
            for tx, ty in base.get("__tails__", ()):
                occupied.add((ox + tx, oy + ty))
            # Terrain floor height: LAYER_FLOOR (1) on sub-layer 0; the last
            # (topmost) floor object on a tile wins, as in the client's
            # get_top_floor_height().
            layer = o.fields.get("layer", base.get("layer"))
            sub_layer = to_int(o.fields.get("sub_layer", base.get("sub_layer")), 0)
            if layer == "1" and sub_layer == 0:
                ground[(ox, oy)] = to_int(o.fields.get("z", base.get("z")), 0)
        occupied = {(x, y) for (x, y) in occupied if 0 <= x < width and 0 <= y < height}
        ground = {t: z for t, z in ground.items() if t in occupied}
        maps[logical] = MapRec(logical, header, width, height, links, link_source, broken,
                               objs, coords, prefix, occupied, ground)
    return maps, all_files


def effective_attrs(obj: Obj, archs: dict) -> dict:
    base = archs.get(obj.name.casefold(), {})
    attrs = {k: v for k, v in base.items() if not k.startswith("__")}
    attrs.update(obj.fields)
    return attrs
