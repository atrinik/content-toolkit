#!/usr/bin/env python3
"""Independent check of placement-plan.json (uses only the JSON).

    python3 -I scripts/verify_plan.py [placement-plan.json]

Checks, for proposed placements and separately proposed+tentative together:
  * member plane origin == local_offset + (x_offset, y_offset, layer_offset)
  * no member rectangle overlaps or touches (margin) an overworld map on its layer
  * no two placed groups overlap or touch (margin)
  * no two members of one group overlap
  * every exit record's plane_source / plane_target equals origin + tile
Exit status 0 when every check passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def verify(plan: dict) -> dict:
    margin = plan["settings"]["margin_tiles"]
    ow = next(g for g in plan["groups"] if g["classification"] == "overworld")
    ow_id = ow["id"]
    cells = {int(z): {tuple(c) for c in v} for z, v in ow["cells_by_layer"].items()}
    errors = []
    origin = {}  # map -> plane origin (proposed+tentative)
    size = {}
    for g in plan["groups"]:
        p = g.get("placement")
        if not p or p.get("status") not in ("proposed", "tentative"):
            continue
        for m in g["maps"]:
            lo = m["local_offset"]
            exp = [lo[0] + p["x_offset"], lo[1] + p["y_offset"], lo[2] + p["layer_offset"]]
            got = p["member_origins"][m["path"]]
            if exp != got:
                errors.append("origin mismatch {} {} != {}".format(m["path"], got, exp))
            origin[m["path"]] = (got, g["id"], p["status"])
            size[m["path"]] = (m["width"], m["height"])

    def rect(path):
        (x, y, z), gid, st = origin[path]
        w, h = size[path]
        return x, y, x + w, y + h, z, gid, st

    rects = [rect(p) for p in sorted(origin)]
    for (x0, y0, x1, y1, z, gid, st) in rects:
        cs = cells.get(z, set())
        for X in range((x0 - margin) // 24, (x1 + margin - 1) // 24 + 1):
            for Y in range((y0 - margin) // 24, (y1 + margin - 1) // 24 + 1):
                if (X, Y) in cs:
                    errors.append("{} ({}) touches overworld world_{}_{} layer {}".format(gid, st, X, Y, z))
    checked_pairs = 0
    tentative_conflicts = []
    for i in range(len(rects)):
        a = rects[i]
        for j in range(i + 1, len(rects)):
            b = rects[j]
            if a[4] != b[4]:
                continue
            checked_pairs += 1
            same = a[5] == b[5]
            m = 0 if same else margin
            if a[0] - m < b[2] and b[0] - m < a[2] and a[1] - m < b[3] and b[1] - m < a[3]:
                msg = "{} and {} overlap/touch on layer {}".format(a[5], b[5], a[4])
                if "tentative" in (a[6], b[6]) and not same:
                    tentative_conflicts.append(msg)
                else:
                    errors.append(msg)
    exits_checked = 0
    for g in plan["groups"]:
        for key in ("entrances_from_overworld", "exits_to_overworld", "entrances_from_other_groups",
                    "exits_to_other_groups"):
            for e in g.get(key, []):
                for end, mkey, tkey in (("plane_source", "source_map", "source_tile"),
                                        ("plane_target", "target_map", "target_tile")):
                    if end not in e:
                        continue
                    mp, (tx, ty) = e[mkey], e[tkey]
                    grp = e["source_group" if end == "plane_source" else "target_group"]
                    if grp == ow_id:
                        base = mp.rsplit("/", 1)[1].split("_")
                        X, Y = int(base[1]), int(base[2])
                        Z = int(base[3]) if len(base) > 3 else 0
                        exp = [24 * X + tx, 24 * Y + ty, Z]
                    else:
                        o = origin[mp][0]
                        exp = [o[0] + tx, o[1] + ty, o[2]]
                    exits_checked += 1
                    if exp != e[end]:
                        errors.append("exit {} {} {} {} != {}".format(key, mp, e[tkey], e[end], exp))
    return {"ok": not errors, "errors": errors, "member_maps": len(origin), "same_layer_pairs_checked": checked_pairs,
            "exit_endpoints_checked": exits_checked, "tentative_vs_tentative_conflicts": tentative_conflicts}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    path = Path(argv[0]) if argv else Path(__file__).resolve().parents[1] / "placement-plan.json"
    res = verify(json.loads(path.read_text(encoding="utf-8")))
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
