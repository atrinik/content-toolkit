"""Occupied-tile footprints, the view-gap rule and stairwell chains.

Footprint rule (owner decision 3): a map occupies only the tiles that hold
at least one object; every other legacy tile becomes void. On one layer a
placed thing must keep

* the view gap from every unrelated owner: Chebyshev distance between any two
  occupied tiles > ``radius`` (so at least ``radius`` void tiles between them,
  and a player standing on one can never be sent a tile of the other);
* the contact margin from every related owner (one it shares an exit with):
  Chebyshev distance > ``margin``, so the two areas do not merge into one
  walkable area.

Owners are overworld map paths, placed group ids, and (for stairwell tiles)
the group whose placement built the stairwell.
"""

from __future__ import annotations

import collections

CELL = 64  # spatial hash cell for owner bounding boxes


def dilate(tiles, r: int) -> set:
    """All tiles within Chebyshev distance r of `tiles`."""
    if r == 0:
        return set(tiles)
    rows = collections.defaultdict(list)
    for x, y in tiles:
        rows[y].append(x)
    merged = {}
    for y, xs in rows.items():
        xs.sort()
        ivs = []
        for x in xs:
            a, b = x - r, x + r
            if ivs and a <= ivs[-1][1] + 1:
                ivs[-1][1] = max(ivs[-1][1], b)
            else:
                ivs.append([a, b])
        merged[y] = ivs
    out = set()
    for y, ivs in merged.items():
        for yy in range(y - r, y + r + 1):
            for a, b in ivs:
                out.update((x, yy) for x in range(a, b + 1))
    return out


def bbox_of(tiles):
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_near(a, b, r: int) -> bool:
    return a[0] - r <= b[2] and b[0] - r <= a[2] and a[1] - r <= b[3] and b[1] - r <= a[3]


class Occupancy:
    """Per-layer occupied tiles by owner, with lazily cached dilations."""

    def __init__(self, radius: int, margin: int):
        self.radius, self.margin = radius, margin
        self.bbox = {}                      # (layer, owner) -> bbox
        self.tiles = {}                     # (layer, owner) -> set or callable (lazy)
        self.grid = collections.defaultdict(set)  # (layer, cx, cy) -> owners
        self.at = collections.defaultdict(dict)   # layer -> {(x, y): owner}, materialised lazily
        self._materialised = set()
        self._dil = {}

    def add(self, owner: str, layer: int, tiles, bbox=None):
        """Register owner tiles; `tiles` may be a callable returning them (lazy)."""
        key = (layer, owner)
        if callable(tiles):
            if bbox is None:
                raise ValueError("lazy tiles need a bbox")
        else:
            tiles = set(tiles)
            if not tiles:
                return
            bbox = bbox_of(tiles)
            if key in self.tiles:
                old = self._get(layer, owner)
                tiles = old | tiles
                bbox = bbox_of(tiles)
                self._dil = {k: v for k, v in self._dil.items() if k[:2] != key}
                self._materialised.discard(key)
        self.tiles[key] = tiles
        self.bbox[key] = bbox
        for cx in range(bbox[0] // CELL, bbox[2] // CELL + 1):
            for cy in range(bbox[1] // CELL, bbox[3] // CELL + 1):
                self.grid[(layer, cx, cy)].add(owner)
        if not callable(tiles):
            self._materialise(layer, owner)

    def _get(self, layer, owner) -> set:
        t = self.tiles[(layer, owner)]
        if callable(t):
            t = set(t())
            self.tiles[(layer, owner)] = t
        return t

    def _materialise(self, layer, owner):
        key = (layer, owner)
        if key in self._materialised:
            return
        d = self.at[layer]
        for t in self._get(layer, owner):
            d.setdefault(t, owner)
        self._materialised.add(key)

    def owners_near(self, layer: int, bbox, r: int) -> list:
        found = set()
        for cx in range((bbox[0] - r) // CELL, (bbox[2] + r) // CELL + 1):
            for cy in range((bbox[1] - r) // CELL, (bbox[3] + r) // CELL + 1):
                found |= self.grid.get((layer, cx, cy), set())
        return sorted(o for o in found if bbox_near(self.bbox[(layer, o)], bbox, r))

    def dilated(self, layer: int, owner: str, r: int) -> set:
        key = (layer, owner, r)
        if key not in self._dil:
            self._dil[key] = dilate(self._get(layer, owner), r)
        return self._dil[key]

    def occupant(self, layer: int, xy):
        """Owner holding an occupied tile at (layer, xy), or None."""
        for o in self.owners_near(layer, (xy[0], xy[1], xy[0], xy[1]), 0):
            self._materialise(layer, o)
        return self.at[layer].get(xy)

    def check(self, layer: int, tiles, bbox, related, own=None):
        """First violation of the gap/contact rule for `tiles`, or None.

        Returns (owner, kind) where kind is "gap" (unrelated owner within the
        view radius) or "contact" (related owner within the contact margin).
        """
        for o in self.owners_near(layer, bbox, self.radius):
            if o == own:
                continue
            r = self.margin if o in related else self.radius
            if not bbox_near(self.bbox[(layer, o)], bbox, r):
                continue
            d = self.dilated(layer, o, r)
            for t in tiles:
                if t in d:
                    return o, ("contact" if o in related else "gap")
        return None

    def unrelated_within(self, layer: int, xy, related, own=None) -> list:
        """Unrelated owners whose occupied tiles lie within the view radius of xy."""
        out = []
        b = (xy[0], xy[1], xy[0], xy[1])
        for o in self.owners_near(layer, b, self.radius):
            if o == own or o in related:
                continue
            if xy in self.dilated(layer, o, self.radius):
                out.append(o)
        return out


def find_chain(top, bottom, free_void, free_strict, max_bend: int):
    """Stair tiles for every layer strictly between `top` and `bottom`.

    top/bottom are plane tiles (x, y, layer) with top layer >= bottom + 2 and
    Chebyshev xy distance <= 1. The chain starts at the column under `top`;
    every consecutive pair (top, chain..., bottom) is exactly one layer and at
    most one tile apart horizontally. A tile may bend away from the column by
    at most `max_bend` tiles, but only when the straight spot is not free.

    Returns (mode, tiles) where mode is "clear" (every tile passes
    free_strict: void and outside every unrelated area's view radius),
    "void-only" (every tile is void, some are within view of an unrelated
    area) or None (no chain exists).
    """
    cx, cy = top[0], top[1]
    layers = list(range(top[2] - 1, bottom[2], -1))
    offsets = sorted(((dx, dy) for dx in range(-max_bend, max_bend + 1) for dy in range(-max_bend, max_bend + 1)),
                     key=lambda d: (max(abs(d[0]), abs(d[1])), abs(d[0]) + abs(d[1]), d[1], d[0]))
    for mode, ok in (("clear", free_strict), ("void-only", free_void)):
        # best[pos] = (cost, path) for the current layer
        prev = {(top[0], top[1]): (0, [])}
        for z in layers:
            cur = {}
            for dx, dy in offsets:
                p = (cx + dx, cy + dy)
                if not ok((p[0], p[1], z)):
                    continue
                dev = max(abs(dx), abs(dy))
                best = None
                for q, (cost, path) in prev.items():
                    if max(abs(p[0] - q[0]), abs(p[1] - q[1])) <= 1:
                        cand = (cost + dev, path + [(p[0], p[1], z)])
                        if best is None or cand < best:
                            best = cand
                if best is not None:
                    cur[p] = best
            prev = cur
            if not prev:
                break
        if prev:
            ends = [v for p, v in prev.items()
                    if max(abs(p[0] - bottom[0]), abs(p[1] - bottom[1])) <= 1]
            if ends:
                return mode, min(ends)[1]
    return None, []


def find_slope_chain(top, bottom, free_void, free_strict):
    """Sloped stairwell tiles for every layer strictly between `top` and `bottom`.

    top/bottom are plane tiles (x, y, layer) with n = top layer - bottom layer
    >= 2 and Chebyshev xy distance between them <= n. Intermediate tile i
    (i = 1 .. n-1, on layer top - i) lies within i tiles of `top` and within
    n - i tiles of `bottom`, so every consecutive pair (top, chain..., bottom)
    is exactly one layer and at most one tile apart: the staircase shifts at
    most one tile sideways per layer.

    Returns (mode, tiles) like find_chain(): "clear" when every tile passes
    free_strict, "void-only" when every tile is void, None when no chain
    exists. Among chains of one mode the one closest to the straight line
    between the ends wins (sum of Chebyshev deviations, scaled by n to stay
    integral), then the lexicographically smallest.
    """
    n = top[2] - bottom[2]
    layers = [top[2] - i for i in range(1, n)]
    for mode, ok in (("clear", free_strict), ("void-only", free_void)):
        prev = {(top[0], top[1]): (0, [])}
        for i, z in enumerate(layers, 1):
            cur = {}
            x_lo, x_hi = max(top[0] - i, bottom[0] - (n - i)), min(top[0] + i, bottom[0] + (n - i))
            y_lo, y_hi = max(top[1] - i, bottom[1] - (n - i)), min(top[1] + i, bottom[1] + (n - i))
            for y in range(y_lo, y_hi + 1):
                for x in range(x_lo, x_hi + 1):
                    if not ok((x, y, z)):
                        continue
                    dev = max(abs(n * x - (n - i) * top[0] - i * bottom[0]),
                              abs(n * y - (n - i) * top[1] - i * bottom[1]))
                    best = None
                    for q, (cost, path) in prev.items():
                        if max(abs(x - q[0]), abs(y - q[1])) <= 1:
                            cand = (cost + dev, path + [(x, y, z)])
                            if best is None or cand < best:
                                best = cand
                    if best is not None:
                        cur[(x, y)] = best
            prev = cur
            if not prev:
                break
        if prev:
            ends = [v for p, v in prev.items() if max(abs(p[0] - bottom[0]), abs(p[1] - bottom[1])) <= 1]
            if ends:
                return mode, min(ends)[1]
    return None, []
