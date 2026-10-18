"""Layer-height relayering, applied (owner decision 2026-10-18: fix the conflicts, then relayer).

Height of a tile: its support height, the highest floor z over all sub-layers
(classic server map_space_support_height(), server/src/socket/request.c:1012-1022).
Rule: H = 50 * q + rest with q = H // 50 for H >= 50 and q = 0 below; a
relayered tile moves q layers up and keeps rest (0..49) as its tile height.

Steps, all deterministic (sorted iteration, lexicographic tie-breaks):

1. Regions: 8-connected tiles of one layer with the same q >= 1. A region of at
   least MIN_REGION_TILES tiles is relayered; a smaller one only when it touches
   a relayered region on its layer (another step of the same slope). Any other
   small region keeps its z as tile height (single raised tiles, decoration).
2. Units: 8-connected occupied tiles of one layer. On the overworld the surface
   (layer 0) is never a unit; only its regions move.
3. Conflicts, resolved before the layers are final and repeated until none is
   left:
   - column order: in every column (x, y) the occupied tiles keep their
     vertical order. When a lower tile would reach the tile above it, the unit
     above moves up (storeys stand on the ground under them); when the lower
     tile is underground, its cave moves down instead;
   - caves stay underground: no tile from a negative layer may reach layer 0;
   - a cave that must move down is lowered as a whole (its raised regions land
     back on their layer and its internal slopes stay intact) when that breaks
     no more stairs and makes no more steep steps than lowering only the raised
     regions in place, and pushes nothing below it. Otherwise only the raised
     regions are lowered in place: they keep their layer and their floor keeps
     z - 50 q.
4. Continuity: a walkable edge (both tiles have a floor, neither holds a
   no_pass object, |dz| < WALKABLE_DZ so the classic server counts no fall,
   round(dz / 50) = 0 in server/src/server/move.c:151) whose ends end on
   different layers is cut. Cut edges form seams; each seam piece (the part of
   a seam inside one 24x24 cell) keeps continuity through an existing exit
   between its two sides, or gets one new pair of plain stairs.
5. Reports: steep steps (walkable edges that stay on one layer but become a
   fall), new contacts (tiles that become neighbours on a layer although they
   were not neighbours before), and every tile's final layer.
"""

from __future__ import annotations

import collections

N8 = [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dy) != (0, 0)]
MIN_REGION_TILES = 24
WALKABLE_DZ = 25
SEAM_CELL = 24
MAX_ROUNDS = 64


def flood(keys, same):
    """8-connected components (one layer) of `keys` linked by `same(a, b)`, sorted."""
    keyset = set(keys)
    seen = set()
    out = []
    for k in sorted(keyset):
        if k in seen:
            continue
        comp = [k]
        seen.add(k)
        stack = [k]
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


def cheb(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


class Relayering:
    """Relayer one coordinate frame (the overworld plane or one group's local layout).

    tiles:     {(layer, x, y): (map, floors {sub_layer: z}, blocked)} for every occupied tile
    exits:     [{"key", "src": (layer, x, y), "dst": (layer, x, y)}] with both ends in this frame
    overworld: enables the surface rules (layer 0 is the surface, caves stay below it)
    """

    def __init__(self, tiles, exits, height, overworld):
        self.tiles = tiles
        self.exits = exits
        self.height = height
        self.overworld = overworld
        self.H, self.H0 = {}, {}
        for k, (_m, floors, _b) in tiles.items():
            if floors:
                self.H[k] = max(floors.values())
                if 0 in floors:
                    self.H0[k] = floors[0]
        self.q = {k: h // height for k, h in self.H.items() if h >= height}
        # tiles at the layer height only because of a floor above sub-layer 0
        self.raised_by_upper = sum(1 for k in self.q if self.H0.get(k, -1) < height)
        self._regions()
        self._units()
        self.du = collections.Counter()
        self.dr = collections.Counter()
        self.moves = []
        self.caves = []
        self.unresolved = []
        self._resolve()
        self._finish()

    # ------------------------------------------------------------- regions
    def _regions(self):
        comps = flood(self.q, lambda a, b: self.q[a] == self.q[b])
        comps.sort(key=lambda c: (-len(c), c[0]))
        self.regions = []
        self.region_of = {}
        for comp in comps:
            r = {"index": len(self.regions), "layer": comp[0][0], "q": self.q[comp[0]], "tiles": comp,
                 "relayered": len(comp) >= MIN_REGION_TILES,
                 "why": "at least {} tiles".format(MIN_REGION_TILES) if len(comp) >= MIN_REGION_TILES else None}
            self.regions.append(r)
            for k in comp:
                self.region_of[k] = r["index"]
        changed = True
        while changed:
            changed = False
            for r in self.regions:
                if r["relayered"]:
                    continue
                touch = sorted({self.region_of[n] for k in r["tiles"] for n in self._n8(k)
                                if n in self.region_of and self.regions[self.region_of[n]]["relayered"]})
                if touch:
                    r["relayered"] = True
                    r["why"] = "touches relayered region(s) {}".format(touch)
                    r["joins"] = touch
                    changed = True
        self.relayered = {k for r in self.regions if r["relayered"] for k in r["tiles"]}

    @staticmethod
    def _n8(k):
        return [(k[0], k[1] + dx, k[2] + dy) for dx, dy in N8]

    # --------------------------------------------------------------- units
    def _units(self):
        bylayer = collections.defaultdict(list)
        for k in self.tiles:
            bylayer[k[0]].append(k)
        self.units = []
        self.unit_of = {}
        for L in sorted(bylayer):
            if self.overworld and L == 0:
                continue
            for comp in flood(bylayer[L], lambda a, b: True):
                u = {"index": len(self.units), "layer": L, "tiles": comp}
                self.units.append(u)
                for k in comp:
                    self.unit_of[k] = u["index"]
        cols = collections.defaultdict(list)
        for k in self.tiles:
            cols[(k[1], k[2])].append(k[0])
        self.columns = {c: sorted(v) for c, v in cols.items() if len(v) > 1}
        self.underground = sorted(k for k in self.tiles if k[0] < 0) if self.overworld else []

    # -------------------------------------------------------------- shifts
    def shift(self, k, du=None, dr=None):
        du = self.du if du is None else du
        dr = self.dr if dr is None else dr
        s = du.get(self.unit_of[k], 0) if k in self.unit_of else 0
        if k in self.relayered:
            s += self.q[k] + dr.get(self.region_of[k], 0)
        return s

    def final(self, k, du=None, dr=None):
        return k[0] + self.shift(k, du, dr)

    def rest(self, k):
        h = self.H.get(k)
        if h is None:
            return None
        return h - self.height * self.q[k] if k in self.relayered else h

    # ----------------------------------------------------------- conflicts
    def _violations(self, du=None, dr=None, cols=None, tiles=None):
        up = {}
        down = collections.defaultdict(dict)
        bad = []
        for (x, y) in (sorted(self.columns) if cols is None else cols):
            Ls = self.columns[(x, y)]
            for lb, lt in zip(Ls, Ls[1:]):
                b, t = (lb, x, y), (lt, x, y)
                fb, ft = self.final(b, du, dr), self.final(t, du, dr)
                if ft > fb:
                    continue
                short = fb - ft + 1
                if self.overworld and lb < 0:
                    u = self.unit_of[b]
                    down[u][b] = max(down[u].get(b, 0), short)
                elif t in self.unit_of:
                    u = self.unit_of[t]
                    up[u] = max(up.get(u, 0), short)
                else:
                    bad.append((b, t))
        for k in (self.underground if tiles is None else [t for t in tiles if t[0] < 0 and self.overworld]):
            f = self.final(k, du, dr)
            if f >= 0:
                u = self.unit_of[k]
                down[u][k] = max(down[u].get(k, 0), f + 1)
        return up, dict(down), bad

    def _exit_changes(self, unit_tiles, du, dr):
        """Exits with exactly one end in the unit whose layer distance differs from the legacy one."""
        n = 0
        for e in self.exits:
            ins = (e["src"] in unit_tiles) + (e["dst"] in unit_tiles)
            if ins != 1:
                continue
            before = e["dst"][0] - e["src"][0]
            after = self._final_any(e["dst"], du, dr) - self._final_any(e["src"], du, dr)
            if abs(before) != abs(after):
                n += 1
        return n

    def _final_any(self, k, du=None, dr=None):
        return self.final(k, du, dr) if k in self.tiles else k[0]

    def _steep(self, keys, du, dr):
        """Walkable edges touching `keys` that stay on one layer but become a fall."""
        n = 0
        seen = set()
        for k in sorted(keys):
            for nb in self._n8(k):
                pair = (min(k, nb), max(k, nb))
                if pair in seen or not self.walkable(k, nb):
                    continue
                seen.add(pair)
                if self.final(k, du, dr) == self.final(nb, du, dr) and abs(self.rest(k) - self.rest(nb)) >= WALKABLE_DZ:
                    n += 1
        return n

    def walkable(self, a, b):
        """Legacy walkable edge: both tiles have a floor, no no_pass object, and |dz| < WALKABLE_DZ."""
        if a not in self.H or b not in self.H or self.tiles[a][2] or self.tiles[b][2]:
            return False
        return abs(self.H[a] - self.H[b]) < WALKABLE_DZ

    def _lower_cave(self, u, needs, rnd):
        unit = self.units[u]
        utiles = set(unit["tiles"])
        amount = max(needs.values())
        duA = dict(self.du)
        duA[u] = duA.get(u, 0) - amount
        need_r = {}
        for k, a in needs.items():
            if k in self.relayered:
                r = self.region_of[k]
                need_r[r] = max(need_r.get(r, 0), a)
        feasible_b = all(k in self.relayered for k in needs)
        drB = dict(self.dr)
        for r, a in need_r.items():
            drB[r] = drB.get(r, 0) - a
        ucols = sorted({(k[1], k[2]) for k in utiles if (k[1], k[2]) in self.columns})
        _upA, downA, _badA = self._violations(du=duA, cols=ucols, tiles=unit["tiles"])
        cascade = sorted(v for v in downA if v != u)
        brokenA = self._exit_changes(utiles, duA, self.dr)
        steepA = self._steep(utiles, duA, self.dr)
        brokenB = self._exit_changes(utiles, self.du, drB) if feasible_b else None
        steepB = self._steep(utiles, self.du, drB) if feasible_b else None
        regions = sorted(need_r)
        if feasible_b and (cascade or (brokenA, steepA) > (brokenB, steepB)
                           or (brokenA, steepA) == (brokenB, steepB)):
            choice = "lower-regions-in-place"
            self.dr = collections.Counter(drB)
            for r, a in sorted(need_r.items()):
                self.moves.append({"round": rnd, "kind": "region-in-place", "region": r, "unit": u, "shift": -a})
        else:
            choice = "lower-whole-cave"
            self.du[u] -= amount
            self.moves.append({"round": rnd, "kind": "cave-down", "unit": u, "shift": -amount})
        why = []
        if choice == "lower-whole-cave":
            if not feasible_b:
                why.append("tiles that are not relayered must move, so the cave moves as a whole")
            else:
                why.append("lowering the whole cave breaks {} stair(s) and makes {} steep step(s); lowering "
                           "only the raised regions breaks {} and makes {}".format(brokenA, steepA, brokenB, steepB))
        else:
            if cascade:
                why.append("lowering the whole cave would push {} unit(s) below it down".format(len(cascade)))
            why.append("lowering the whole cave breaks {} stair(s) and makes {} steep step(s); lowering only the "
                       "raised regions breaks {} and makes {}".format(brokenA, steepA, brokenB, steepB))
        self.caves.append({"round": rnd, "unit": u, "layer": unit["layer"], "tiles": len(unit["tiles"]),
                           "regions": regions, "lowered_by": amount, "choice": choice,
                           "option_whole_cave": {"stairs_changed": brokenA, "steep_steps": steepA,
                                                 "pushes_units_below": cascade},
                           "option_in_place": None if not feasible_b else {"stairs_changed": brokenB,
                                                                            "steep_steps": steepB},
                           "why": "; ".join(why)})

    def _resolve(self):
        for rnd in range(MAX_ROUNDS):
            up, down, bad = self._violations()
            if bad:
                self.unresolved = [{"lower": list(b), "upper": list(t)} for b, t in bad]
            if not up and not down:
                return
            for u in sorted(up):
                self.du[u] += up[u]
                self.moves.append({"round": rnd, "kind": "storey-up", "unit": u, "shift": up[u]})
            for u in sorted(down):
                self._lower_cave(u, down[u], rnd)
        raise RuntimeError("relayering conflicts did not converge in {} rounds".format(MAX_ROUNDS))

    # ------------------------------------------------------------- results
    def _finish(self):
        self.final_layer = {k: self.final(k) for k in self.tiles}
        # Split tiles: a relayered tile whose sub-layer 0 floor is lower than a layer
        # keeps that floor (and the objects on sub-layer 0) on the unit's layer.
        self.split = {}
        for k in sorted(self.relayered):
            h0 = self.H0.get(k)
            if h0 is not None and h0 // self.height < self.q[k] and len(self.tiles[k][1]) > 1:
                base = k[0] + (self.du.get(self.unit_of[k], 0) if k in self.unit_of else 0)
                if base != self.final_layer[k]:
                    self.split[k] = base
        moved = [k for k in sorted(self.tiles) if self.final_layer[k] != k[0]]
        self.moved = moved
        # pieces: map -> final layer -> tiles (frame coordinates)
        self.pieces = collections.defaultdict(lambda: collections.defaultdict(set))
        for k, (m, _f, _b) in self.tiles.items():
            self.pieces[m][self.final_layer[k]].add((k[1], k[2]))
            if k in self.split:
                self.pieces[m][self.split[k]].add((k[1], k[2]))

    def final_positions(self):
        """{(final_layer, x, y): [legacy tile, ...]} for every occupied tile; split tiles on both layers."""
        out = collections.defaultdict(list)
        for k in sorted(self.tiles):
            out[(self.final_layer[k], k[1], k[2])].append(k)
            if k in self.split:
                out[(self.split[k], k[1], k[2])].append(k)
        return out

    def overlaps(self):
        """Final positions claimed by two legacy tiles (the void rule forbids them)."""
        return sorted((pos, ks) for pos, ks in self.final_positions().items() if len(ks) > 1)

    def cut_edges(self):
        """Walkable legacy edges whose two tiles end on different layers."""
        cut = set()
        for k in self.moved:
            for nb in self._n8(k):
                if nb in self.tiles and self.walkable(k, nb) and self.final_layer[k] != self.final_layer[nb]:
                    cut.add((min(k, nb), max(k, nb)))
        out = []
        for a, b in sorted(cut):
            lo, hi = (a, b) if self.final_layer[a] < self.final_layer[b] else (b, a)
            out.append((lo, hi))
        return out

    def steep_steps(self):
        out = []
        seen = set()
        for k in sorted(set(self.moved) | {k for k in self.relayered}):
            for nb in self._n8(k):
                pair = (min(k, nb), max(k, nb))
                if pair in seen or nb not in self.tiles or not self.walkable(k, nb):
                    continue
                seen.add(pair)
                if self.final_layer[k] == self.final_layer[nb] and \
                        abs(self.rest(k) - self.rest(nb)) >= WALKABLE_DZ:
                    out.append(pair)
        return sorted(out)

    def new_contacts(self):
        """Pairs of tiles that are 8-neighbours on their final layer but came from different legacy layers."""
        final = self.final_positions()
        out = set()
        for k in self.moved:
            fx = (self.final_layer[k], k[1], k[2])
            for dx, dy in N8:
                for t in final.get((fx[0], fx[1] + dx, fx[2] + dy), ()):
                    if t[0] != k[0]:
                        out.add((min(k, t), max(k, t)))
        return sorted(out)

    def seams(self):
        """Group cut edges into seams and keep continuity on every seam piece.

        Two cut edges belong to one seam when they join the same pair of final
        layers and their low tiles or their high tiles are 8-neighbours (or the
        same tile). A seam piece is the part of a seam whose low tiles lie in one
        SEAM_CELL x SEAM_CELL cell. A piece keeps continuity through an existing
        exit when one end of that exit lies within one tile of a low tile of the
        piece on the low layer and the other end within one tile of a high tile on
        the high layer; otherwise the piece gets one new pair of plain stairs on
        its representative edge (orthogonal edges first, then the edge nearest to
        the piece's centre, then coordinates).
        """
        edges = self.cut_edges()
        parent = list(range(len(edges)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            a, b = find(i), find(j)
            if a != b:
                parent[max(a, b)] = min(a, b)

        layers = [(self.final_layer[lo], self.final_layer[hi]) for lo, hi in edges]
        for side in (0, 1):
            at = collections.defaultdict(list)
            for i, e in enumerate(edges):
                at[(layers[i], e[side][1], e[side][2])].append(i)
            for i, e in enumerate(edges):
                t = e[side]
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for j in at.get((layers[i], t[1] + dx, t[2] + dy), ()):
                            union(i, j)
        groups = collections.defaultdict(list)
        for i in range(len(edges)):
            groups[find(i)].append(i)
        ends = collections.defaultdict(list)  # (final layer, x, y) -> exits with an end there
        for e in self.exits:
            for end in ("src", "dst"):
                k = e[end]
                L = self._final_any(k)
                ends[(L, k[1], k[2])].append(e)
        seams = []
        for sid, idx in enumerate(sorted(groups.values(), key=lambda v: edges[v[0]])):
            pieces = collections.defaultdict(list)
            for i in idx:
                lo = edges[i][0]
                pieces[(lo[1] // SEAM_CELL, lo[2] // SEAM_CELL)].append(i)
            lo_layer, hi_layer = layers[idx[0]]
            seam = {"index": sid, "low_layer": lo_layer, "high_layer": hi_layer, "edges": len(idx), "pieces": []}
            for cell in sorted(pieces):
                pe = [edges[i] for i in pieces[cell]]
                lows = {(e[0][1], e[0][2]) for e in pe}
                highs = {(e[1][1], e[1][2]) for e in pe}
                joined = None
                for (x, y) in sorted(lows):
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            for ex in ends.get((lo_layer, x + dx, y + dy), ()):
                                other = ex["dst"] if self._final_any(ex["src"]) == lo_layer and \
                                    cheb(ex["src"][1:], (x, y)) <= 1 else ex["src"]
                                L = self._final_any(other)
                                if L == hi_layer and any(cheb(other[1:], h) <= 1 for h in highs):
                                    joined = joined or ex
                n = len(pe)
                sx = sum(e[0][1] + e[1][1] for e in pe)
                sy = sum(e[0][2] + e[1][2] for e in pe)

                def rank(e):
                    diag = int(e[0][1] != e[1][1] and e[0][2] != e[1][2])
                    d = (n * (e[0][1] + e[1][1]) - sx) ** 2 + (n * (e[0][2] + e[1][2]) - sy) ** 2
                    return (diag, d, e)

                rep = min(pe, key=rank)
                piece = {"cell": list(cell), "edges": n,
                         "low_tiles": len(lows), "high_tiles": len(highs),
                         "representative_edge": [list(rep[0]), list(rep[1])]}
                if joined is not None:
                    piece["continuity"] = "existing exit"
                    piece["exit"] = list(joined["key"])
                else:
                    piece["continuity"] = "new stairs"
                    piece["stairs"] = {"low": [rep[0][1], rep[0][2], lo_layer], "high": [rep[1][1], rep[1][2], hi_layer],
                                       "low_tile": list(rep[0]), "high_tile": list(rep[1])}
                seam["pieces"].append(piece)
            seams.append(seam)
        return seams
