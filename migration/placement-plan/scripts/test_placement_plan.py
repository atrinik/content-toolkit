#!/usr/bin/env python3
"""Negative tests for the placement-plan verifier and generator (standard library only).

    python3 -I scripts/test_placement_plan.py [--jobs N]
    PLACEMENT_PLAN_CONTENT=DIR python3 -I scripts/test_placement_plan.py

Each verifier test tampers with a copy of the committed placement-plan.json
and checks that verify_plan.verify() rejects it with the expected error. The
generator tests drive build_plan.place_pool() with stand-in callbacks. Neither
needs a content checkout. With PLACEMENT_PLAN_CONTENT naming a checkout at the
plan's content_git_head, ContentFidelity also checks that verify_content()
rejects edits that keep the JSON consistent with itself; without it, those
tests are skipped. Each test runs the full verifier, so the tests run in up
to 8 worker processes (--jobs 1 runs them in this process).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_plan  # noqa: E402
import verify_plan  # noqa: E402

PLAN = Path(__file__).resolve().parents[1] / "placement-plan.json"
INDEX = ("entrances_from_overworld", "exits_to_overworld", "entrances_from_other_groups", "exits_to_other_groups",
         "internal_exits")
MINE = "/shattered_islands/eld_woods_island/clearhaven/mine/"
CROSSING = (MINE + "mine_entrance_a", 641)  # ladder_up to mine_entrance: one copy in each group
ROCKFORGE = "/shattered_islands/strakewood_island/rockforge/"
MINES = ROCKFORGE + "rockforge_c"  # placed under rockforge_b by owner decision 2026-10-19
ZECHNA_HOLE = ("/shattered_islands/world_7_66_-1", 938)  # kept a portal by owner decision 2026-10-19


def group(plan, gid):
    return next(g for g in plan["groups"] if g["id"] == gid)


def copies(plan, key):
    """(group, list name, record) for every copy of exit `key` in the exit index."""
    return [(g, lst, e) for g in plan["groups"] for lst in INDEX for e in g.get(lst, [])
            if (e["source_map"], e["line"]) == key]


def delete_copies(plan, key, only_group=None):
    for g, lst, e in copies(plan, key):
        if only_group is None or g["id"] == only_group:
            g[lst].remove(e)


class VerifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PLAN.read_text(encoding="utf-8")

    def plan(self):
        return json.loads(self.text)

    def assertCaught(self, plan, *needles):
        res = verify_plan.verify(plan)
        self.assertFalse(res["ok"], "tampered plan verified")
        for needle in needles:
            self.assertTrue(any(needle in e for e in res["errors"]),
                            "no error mentions {!r}; errors: {}".format(needle, res["errors"][:8]))


class CommittedPlan(VerifierTest):
    def test_committed_plan_verifies(self):
        res = verify_plan.verify(self.plan())
        self.assertTrue(res["ok"], res["errors"][:8])
        self.assertEqual(res["exit_inventory_objects"], len(self.plan()["exit_inventory"]))

    def test_a_malformed_plan_fails_without_a_crash(self):
        for change in (lambda p: p["exit_inventory"][0].update(source_tile=None),
                       lambda p: p.pop("stairwells"),
                       lambda p: p["groups"][0].update(maps=None)):
            p = self.plan()
            change(p)
            res = verify_plan.verify(p)
            self.assertFalse(res["ok"])
            self.assertTrue(res["errors"])

    def test_booleans_and_floats_are_not_numbers(self):
        key = ("/shattered_islands/berri_lur/temple_loki/tol_0201", 441)
        p = self.plan()
        for _g, _lst, e in copies(p, key):
            e["layer_delta"] = True
        for r in p["relayering"]["exits"]:
            if (r["source_map"], r["line"]) == key:
                r["layer_delta_after"] = True
        self.assertCaught(p, "the plan has booleans or floats where integers belong")
        p = self.plan()
        p["stairwells"][0]["tiles"][1]["layer"] = float(p["stairwells"][0]["tiles"][1]["layer"])
        self.assertCaught(p, "the plan has booleans or floats where integers belong, e.g. /stairwells[0]/tiles[1]/layer")

    def test_strict_json_comparison(self):
        self.assertTrue(verify_plan.same_json({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]}))
        for a, b in (([1], [True]), ({"a": 1}, {"a": 1.0}), ({"a": None}, {}), ([1, 2], [2, 1])):
            self.assertFalse(verify_plan.same_json(a, b), (a, b))

    def test_an_oversized_plan_file(self):
        import contextlib
        import io

        limit, out = verify_plan.MAX_PLAN_BYTES, io.StringIO()
        try:
            verify_plan.MAX_PLAN_BYTES = 1000
            with contextlib.redirect_stdout(out):
                self.assertEqual(verify_plan.main([str(PLAN)]), 1)
        finally:
            verify_plan.MAX_PLAN_BYTES = limit
        self.assertIn("is larger than 1000 bytes", out.getvalue())

    def test_the_relayering_cache_follows_its_inputs(self):
        # a changed relayering input is recomputed, never served from the cache
        p = self.plan()
        c = p["relayering"]["new_contacts"][0]
        verify_plan.verify(self.plan())
        c["walkable"] = not c["walkable"]
        self.assertCaught(p, "new contact {} / {} records walkable".format(tuple(c["tiles"][0]), tuple(c["tiles"][1])))
        # every input is in the key: each change below computes the relayering again
        calls, rules = [], verify_plan.relayering_rules
        ow_rows = next(g for g in self.plan()["groups"] if g["classification"] == "overworld")["occupied_rows_by_layer"]
        args = [ow_rows, {}, self.plan()["relayering"], [], {("m", 1)}, [50, 50, 10, 1]]

        def counting(*a):
            calls.append(a)
            return [], {}, {}

        try:
            verify_plan.relayering_rules = counting
            verify_plan._RELAYER_CACHE.clear()
            verify_plan._relayering(*args)
            verify_plan._relayering(*args)
            self.assertEqual(len(calls), 1)
            for i, changed in ((0, dict(ow_rows, extra={})), (2, dict(args[2], seam_cell=25)),
                               (3, [{"source_map": "m"}]), (4, {("m", 2)}), (5, [50, 50, 10, 2])):
                a = list(args)
                a[i] = changed
                verify_plan._relayering(*a)
                self.assertEqual(len(calls), 2 + [0, 2, 3, 4, 5].index(i), "input {} is not in the key".format(i))
        finally:
            verify_plan.relayering_rules = rules
            verify_plan._RELAYER_CACHE.clear()

    def test_the_plan_identifies_its_input(self):
        for change in (lambda p: p.pop("content_root"), lambda p: p.update(content_root=None),
                       lambda p: p.update(content_root=""), lambda p: p.update(content_git_head="3ad2015"),
                       lambda p: p.update(content_worktree_clean="yes"),
                       lambda p: p["content_inputs_digest"].update(sha256="x")):
            p = self.plan()
            change(p)
            self.assertCaught(p, "the plan does not identify its input")

    def test_another_schema(self):
        p = self.plan()
        p["schema"] = "atrinik-overworld-placement-plan/4"
        self.assertCaught(p, "schema atrinik-overworld-placement-plan/4 is not atrinik-overworld-placement-plan/5")


class ExitInventory(VerifierTest):
    """Fix 1: the exit records are checked against the complete source exit inventory."""

    def test_deleting_every_copy_of_a_crossing_exit(self):
        p = self.plan()
        self.assertEqual(len(copies(p, CROSSING)), 2)
        delete_copies(p, CROSSING)
        self.assertCaught(p, "exit record(s) missing, e.g. exit {}:{}".format(*CROSSING),
                          "summary crossing exits", "crossing exits of {}".format(MINE + "mine_entrance_a"))

    def test_deleting_one_copy_of_a_crossing_exit(self):
        p = self.plan()
        delete_copies(p, CROSSING, only_group=MINE + "mine_entrance")
        self.assertCaught(p, "exit record(s) missing, e.g. exit {}:{} from entrances_from_other_groups of {}".format(
            CROSSING[0], CROSSING[1], MINE + "mine_entrance"))

    def test_deleting_an_overworld_entrance(self):
        p = self.plan()
        e = group(p, MINE + "mine_entrance")["entrances_from_overworld"][0]
        delete_copies(p, (e["source_map"], e["line"]))
        self.assertCaught(p, "exit record(s) missing, e.g. exit {}:{} from entrances_from_overworld".format(
            e["source_map"], e["line"]))

    def test_deleting_an_overworld_exit(self):
        p = self.plan()
        ow = next(g for g in p["groups"] if g["classification"] == "overworld")
        e = ow["internal_exits"].pop(0)
        self.assertCaught(p, "exit record(s) missing, e.g. exit {}:{} from internal_exits".format(
            e["source_map"], e["line"]))

    def test_deleting_an_exit_from_the_inventory_and_the_index(self):
        p = self.plan()
        p["exit_inventory"] = [x for x in p["exit_inventory"] if (x["source_map"], x["line"]) != CROSSING]
        delete_copies(p, CROSSING)
        self.assertCaught(p, "the exit inventory lists {} exit objects, the summary {}".format(
            len(p["exit_inventory"]), len(p["exit_inventory"]) + 1))

    def test_deleting_an_exit_from_the_inventory_only(self):
        p = self.plan()
        p["exit_inventory"] = [x for x in p["exit_inventory"] if (x["source_map"], x["line"]) != CROSSING]
        p["summary"]["exit_objects"] -= 1
        self.assertCaught(p, "exit record(s) the exit inventory does not call for, e.g. exit {}:{}".format(*CROSSING))

    def test_listing_an_exit_twice_in_the_inventory(self):
        p = self.plan()
        p["exit_inventory"].append(dict(p["exit_inventory"][0]))
        p["summary"]["exit_objects"] += 1
        self.assertCaught(p, "is listed twice in the exit inventory")

    def test_a_copy_in_the_wrong_list(self):
        p = self.plan()
        g = group(p, MINE + "mine_entrance")
        e = next(e for e in g["entrances_from_other_groups"] if (e["source_map"], e["line"]) == CROSSING)
        g["entrances_from_other_groups"].remove(e)
        g["entrances_from_overworld"].append(e)
        self.assertCaught(p, "exit record(s) missing", "exit record(s) the exit inventory does not call for")

    def test_an_extra_copy(self):
        p = self.plan()
        e = copies(p, CROSSING)[0][2]
        group(p, MINE + "mine_unique")["entrances_from_other_groups"].append(dict(e))
        self.assertCaught(p, "exit record(s) the exit inventory does not call for, e.g. exit {}:{}".format(*CROSSING))

    def test_a_record_that_differs_from_the_inventory(self):
        for field, value in (("source_tile", [0, 0]), ("target_tile", [0, 0]), ("arch", "hole"),
                             ("target_map", MINE + "mine_unique"), ("form", "tiled")):
            with self.subTest(field=field):
                p = self.plan()
                for _g, _lst, e in copies(p, CROSSING):
                    e[field] = value
                self.assertCaught(p, "record of exit {}:{} differs from its inventory entry in {}".format(
                    CROSSING[0], CROSSING[1], field))

    def test_dropping_the_plane_endpoints_of_a_placed_exit(self):
        p = self.plan()
        for _g, _lst, e in copies(p, CROSSING):
            for k in ("plane_source", "plane_target", "xy_distance", "layer_delta", "becomes"):
                e.pop(k)
        self.assertCaught(p, "exit {}:{} lacks placement fields ['plane_source', 'plane_target', 'xy_distance', "
                             "'layer_delta', 'becomes'], although both of its ends are placed".format(*CROSSING))

    def test_a_wrong_overworld_plane_end(self):
        p = self.plan()
        e = group(p, MINE + "mine_entrance")["entrances_from_overworld"][0]
        e["overworld_plane_source"] = [0, 0, 0]
        self.assertCaught(p, "exit {}:{} overworld_plane_source [0, 0, 0]".format(e["source_map"], e["line"]))

    def test_summary_and_group_counts(self):
        p = self.plan()
        p["summary"]["crossing_exit_objects_after_placement"]["stairs"] += 1
        self.assertCaught(p, "summary crossing exits")
        p = self.plan()
        p["summary"]["crossing_exit_objects_into_separate_planes"] -= 1
        self.assertCaught(p, "summary crossing exits into separate planes")
        p = self.plan()
        p["summary"]["portal_reasons"]["slope-blocked"] += 1
        self.assertCaught(p, "summary portal reasons")
        p = self.plan()
        group(p, MINE + "mine_entrance_a")["placement"]["crossing_exits_after_placement"]["stairs"] -= 1
        self.assertCaught(p, "crossing exits of {}".format(MINE + "mine_entrance_a"))

    def test_deleting_a_wired_exit(self):
        p = self.plan()
        w = p["data_problems"]["dangling_exits_wired"][0]
        delete_copies(p, (w["source_map"], w["line"]))
        self.assertCaught(p, "exit record(s) missing, e.g. exit {}:{}".format(w["source_map"], w["line"]),
                          "wired exit {}:{} has no exit record".format(w["source_map"], w["line"]))
        p = self.plan()
        w = p["data_problems"]["dangling_exits_wired"].pop(0)
        delete_copies(p, (w["source_map"], w["line"]))
        self.assertCaught(p, "wired exits listed, the summary says")

    def test_a_wired_copy_that_differs(self):
        p = self.plan()
        p["data_problems"]["dangling_exits_wired"][0]["target_tile"] = [0, 0]
        self.assertCaught(p, "differs from its inventory entry in target_tile")

    def test_a_main_connection_that_is_no_record(self):
        p = self.plan()
        group(p, MINE + "mine_entrance_a")["placement"]["main_connection"]["source_tile"] = [0, 0]
        self.assertCaught(p, "main connection of {} is not one of its exit records".format(MINE + "mine_entrance_a"))

    def test_a_main_connection_that_did_not_fix_the_placement(self):
        # the deck's other up-stairs, one tile east, would put the deck one tile further west
        deck = "/shattered_islands/deserted_tutorial_island/ship_lower_deck"
        p = self.plan()
        mc = group(p, deck)["placement"]["main_connection"]
        mc.update(source_tile=[2, 6], arch="stairs_wood_up")
        self.assertCaught(p, "ideal spot {'dx': -161, 'dy': 1832, 'L': -1} of " + deck + " does not follow from its "
                             "main connection ({'dx': -162, 'dy': 1832, 'L': -1})")

    def test_offsets_that_are_not_the_ideal_spot(self):
        # ellamin sits 4 layers below its ideal spot: flipping its orientation moves the ideal layer the other way
        ellamin = "/shattered_islands/incuna/ellamin"
        for field, value in (("shift_from_ideal", [1, 0]), ("extra_layers_from_ideal", 1), ("orientation", "above"),
                             ("orientation", "sideways")):
            with self.subTest(field=field, value=value):
                p = self.plan()
                group(p, ellamin)["placement"][field] = value
                self.assertCaught(p, "offsets of {} are not its ideal spot".format(ellamin))

    def test_main_connections(self):
        deck = "/shattered_islands/deserted_tutorial_island/ship_lower_deck"
        p = self.plan()
        group(p, deck)["placement"]["main_connection"] = None
        self.assertCaught(p, "placement of {} has no main connection".format(deck))
        p = self.plan()
        group(p, deck)["placement"]["main_connection"]["kind"] = "sideways"
        self.assertCaught(p, "main connection of {} has kind sideways".format(deck))
        p = self.plan()  # an alignment nobody configured, on a group placed by its exits
        group(p, deck)["placement"]["alignment_reason"] = "hint"
        self.assertCaught(p, "owner alignment of {} is not its configured spot under a placed anchor".format(deck),
                          "placement of {} has both a main connection and an owner alignment".format(deck))
        p = self.plan()
        group(p, deck)["placement"].update(main_connection=None, alignment_reason="hint")
        self.assertCaught(p, "owner alignment of {} is not its configured spot under a placed anchor".format(deck))
        p = self.plan()
        group(p, MINES)["placement"]["main_connection"] = dict(group(p, deck)["placement"]["main_connection"])
        self.assertCaught(p, "placement of {} has both a main connection and an owner alignment".format(MINES))
        # mine_entrance_a is entered from mine_entrance, placed before it; claim it was placed after
        p = self.plan()
        a, b = group(p, MINE + "mine_entrance_a")["placement"], group(p, MINE + "mine_entrance")["placement"]
        self.assertEqual(a["main_connection"]["source_map"], MINE + "mine_entrance")
        a["placement_index"], b["placement_index"] = b["placement_index"], a["placement_index"]
        o = p["summary"]["placement_order"]
        i, j = o.index(MINE + "mine_entrance_a"), o.index(MINE + "mine_entrance")
        o[i], o[j] = o[j], o[i]
        res = verify_plan.verify(p)
        self.assertIn("main connection of {} leads to {}, which is not placed before it".format(
            MINE + "mine_entrance_a", MINE + "mine_entrance"), res["errors"])

    def test_a_data_problem_that_differs_from_the_inventory(self):
        p = self.plan()
        p["data_problems"]["exits"][0]["source_tile"] = [99, 99]
        self.assertCaught(p, "data problem of exit")

    def test_deleting_a_data_problem(self):
        p = self.plan()
        lst = p["data_problems"]["exits"]
        d = next(d for d in lst if d["kind"] == "exit-without-destination")
        lst.remove(d)
        self.assertCaught(p, "1 exit data problem(s) differ from the exit inventory, e.g. ",
                          '"kind": "exit-without-destination", "line": {}'.format(d["line"]))
        p = self.plan()
        d = p["data_problems"]["exits"][0]
        d["severity"] = "info" if d.get("severity") == "problem" else "problem"
        self.assertCaught(p, "exit data problem(s) differ from the exit inventory")

    def test_an_exit_on_a_map_that_is_in_no_group(self):
        p = self.plan()
        key = ("/shattered_islands/deserted_tutorial_island/ship_lower_deck", 64)
        for d in p["exit_inventory"] + p["data_problems"]["exits"]:
            if (d["source_map"], d["line"]) == key:
                d["source_map"] = "/not/a/map"
        self.assertCaught(p, "exit /not/a/map:64 is on a map that is in no group")

    def test_a_dynamic_exit_target(self):
        key = ("/shattered_islands/world_18_4", 1958)
        p = self.plan()
        d = next(d for d in p["data_problems"]["exits"] if (d["source_map"], d["line"]) == key)
        self.assertEqual(d["kind"], "dynamic-random-map-exit")
        d["detail"] = "/somewhere/else"
        self.assertCaught(p, "exit data problem(s) differ from the exit inventory")
        p = self.plan()
        next(x for x in p["exit_inventory"] if (x["source_map"], x["line"]) == key)["dynamic_target"] = "/elsewhere"
        self.assertCaught(p, "exit data problem(s) differ from the exit inventory")

    def test_a_kept_decoration_in_the_wrong_place(self):
        p = self.plan()
        p["exits_kept_as_decoration"][0]["plane_source"] = [0, 0, 0]
        self.assertCaught(p, "kept as decoration is at [0, 0, 0]")

    def test_a_reversed_exit_direction(self):
        p = self.plan()
        key = ("/shattered_islands/berri_lur/temple_loki/tol_0201", 441)
        self.assertTrue(copies(p, key))
        for _g, _lst, e in copies(p, key):
            e["vertical"] = "down" if e["vertical"] == "up" else "up"
        self.assertCaught(p, "record of exit {}:{} differs from its inventory entry in vertical".format(*key))

    def test_partial_plane_endpoints_on_an_unplaced_exit(self):
        p = self.plan()
        key = ("/emergency", 17)
        for _g, _lst, e in copies(p, key):
            self.assertNotIn("plane_source", e)
            e["plane_source"] = [0, 0, 0]
        self.assertCaught(p, "exit {}:{} has placement fields ['plane_source'], although not both of its ends "
                             "are placed".format(*key))

    def test_placement_fields(self):
        cases = (("xy_distance", 5, "records 5 tiles"), ("layer_delta", 3, "/ 3 layers"),
                 ("stairwell", "SW1", "labelled stairs records stairwell SW1"),
                 ("portal_reason", "overworld", "portal reason overworld"),
                 ("bogus", 1, "has fields the verifier does not know: bogus"))
        for field, value, needle in cases:
            with self.subTest(field=field):
                p = self.plan()
                for _g, _lst, e in copies(p, CROSSING):
                    e[field] = value
                self.assertCaught(p, needle)

    def test_a_wrong_portal_reason_text(self):
        p = self.plan()
        e = next(e for g in p["groups"] for e in g.get("entrances_from_other_groups", []) if e.get("portal_reason"))
        for _g, _lst, c in copies(p, (e["source_map"], e["line"])):
            c["portal_reason_text"] = "no reason"
        self.assertCaught(p, "exit {}:{} labelled portal".format(e["source_map"], e["line"]))

    def test_classifications(self):
        p = self.plan()
        group(p, "/emergency")["classification"] = "seperate"
        self.assertCaught(p, "group /emergency has classification seperate with reason system")
        p = self.plan()
        group(p, "/emergency")["reason"] = "place"
        self.assertCaught(p, "group /emergency has classification separate with reason place")
        p = self.plan()
        p["summary"]["by_classification"]["separate:system"] += 1
        self.assertCaught(p, "summary group and map counts differ from the groups")

    def test_duplicate_identities(self):
        p = self.plan()
        apt = "/shattered_islands/eld_woods_island/apartments/apartment_cheap"
        dup = json.loads(json.dumps(group(p, apt)))
        p["groups"].append(dup)
        s = p["summary"]
        s["groups_total"] += 1
        s["non_overworld_groups"] += 1
        s["maps_total"] += dup["map_count"]
        s["by_classification"]["{}:{}".format(dup["classification"], dup["reason"])] += 1
        self.assertCaught(p, "group id {} is listed several times".format(apt))
        p = self.plan()
        ow = next(g for g in p["groups"] if g["classification"] == "overworld")
        ow["cells_by_layer"]["0"].append(list(ow["cells_by_layer"]["0"][0]))
        ow["map_count"] += 1
        ow["layers"]["0"] += 1
        p["summary"]["maps_total"] += 1
        self.assertCaught(p, "member map /shattered_islands/world_{}_{} is listed several times".format(
            *ow["cells_by_layer"]["0"][0]))

    def test_the_overworld_map_count(self):
        p = self.plan()
        next(g for g in p["groups"] if g["classification"] == "overworld")["map_count"] += 1
        p["summary"]["maps_total"] += 1
        self.assertCaught(p, "summary group and map counts differ from the groups")

    def test_unknown_source_values(self):
        for field, value, needle in (("form", "banana", "has form banana"),
                                     ("vertical", "sideways", "has form explicit, direction sideways")):
            with self.subTest(field=field):
                p = self.plan()
                next(x for x in p["exit_inventory"] if (x["source_map"], x["line"]) == CROSSING)[field] = value
                for _g, _lst, e in copies(p, CROSSING):
                    e[field] = value
                self.assertCaught(p, "exit {}:{} {}".format(CROSSING[0], CROSSING[1], needle))
        p = self.plan()
        x = next(x for x in p["exit_inventory"] if x["form"] == "unresolved")
        x.update(target_map="/emergency", target_tile=[0, 0])
        self.assertCaught(p, "exit {}:{} has form unresolved, direction {} and a target".format(
            x["source_map"], x["line"], x["vertical"]))

    def test_value_types(self):
        for field, value in (("arch", 7), ("source_tile", [1.5, 2]), ("per_player_last_eat_1", "yes"),
                             ("event_scripts", "script.py"), ("vertical_inferred_from", 3)):
            with self.subTest(field=field):
                p = self.plan()
                next(x for x in p["exit_inventory"] if (x["source_map"], x["line"]) == CROSSING)[field] = value
                for _g, _lst, e in copies(p, CROSSING):
                    e[field] = value
                self.assertCaught(p, "exit {!r}:{!r} has a value of the wrong type".format(*CROSSING))

    def test_a_same_group_exit_with_bad_values(self):
        key = ("/planes/creation/creation_0501", 427)
        for change in ({"target_map": None, "target_tile": None}, {"source_tile": [2.5, 1]}):
            with self.subTest(change=change):
                p = self.plan()
                next(x for x in p["exit_inventory"] if (x["source_map"], x["line"]) == key).update(change)
                self.assertCaught(p, "exit {!r}:{!r} has a value of the wrong type".format(*key))

    def test_a_dynamic_exit_without_its_target(self):
        key = ("/shattered_islands/world_18_4", 1958)
        p = self.plan()
        next(x for x in p["exit_inventory"] if (x["source_map"], x["line"]) == key).pop("dynamic_target")
        next(d for d in p["data_problems"]["exits"] if (d["source_map"], d["line"]) == key)["detail"] = None
        self.assertCaught(p, "exit {!r}:{!r} has a value of the wrong type".format(*key))

    def test_a_wired_target_outside_its_map(self):
        p = self.plan()
        key = (MINE + "mine_entrance", 181)
        d = next(d for d in p["data_problems"]["dangling_exits_wired"] if (d["source_map"], d["line"]) == key)
        self.assertEqual((d["target_map"], d["target_tile"]), ("/shattered_islands/world_8_79", [8, 1]))
        d.update(target_map="/shattered_islands/world_7_79", target_tile=[32, 1])
        for _g, _lst, e in copies(p, key):
            e.update(target_map="/shattered_islands/world_7_79", target_tile=[32, 1])
        self.assertCaught(p, "exit {}:{} targets tile [32, 1] outside /shattered_islands/world_7_79".format(*key))

    def test_a_boolean_wired_coordinate(self):
        p = self.plan()
        key = (MINE + "mine_entrance", 181)
        next(d for d in p["data_problems"]["dangling_exits_wired"] if (d["source_map"], d["line"]) == key)[
            "target_tile"] = [8, True]
        for _g, _lst, e in copies(p, key):
            e["target_tile"] = [8, True]
        self.assertCaught(p, "exit {}:{} targets tile [8, True] outside".format(*key))

    def test_an_invalid_tile_link_slot(self):
        p = self.plan()
        x = next(x for x in p["exit_inventory"] if x["form"] == "tiled-unresolved")
        x["tiled_slot"] = 999
        for d in p["data_problems"]["exits"]:
            if (d["source_map"], d["line"]) == (x["source_map"], x["line"]) and d["kind"] == "tiled-exit-without-link":
                d["detail"] = "last_heal 999 (None) but the map has no such tile link; the exit leads nowhere"
        self.assertCaught(p, "exit {}:{} has form tiled-unresolved and tile-link slot 999".format(
            x["source_map"], x["line"]))
        p = self.plan()
        next(x for x in p["exit_inventory"] if x["form"] == "explicit")["tiled_slot"] = 9
        self.assertCaught(p, "has form explicit and tile-link slot 9")

    def test_a_target_outside_its_map(self):
        p = self.plan()
        key = ("/shattered_islands/ships/ship_1", 122)
        x = next(x for x in p["exit_inventory"] if (x["source_map"], x["line"]) == key)
        x.update(target_map="/emergency", target_tile=[999, 999])
        for _g, _lst, e in copies(p, key):
            e.update(target_map="/emergency", target_tile=[999, 999])
        self.assertCaught(p, "exit {}:{} targets tile [999, 999] outside /emergency".format(*key))

    def test_a_wrong_portal_reason(self):
        p = self.plan()
        e = next(e for g in p["groups"] for e in g.get("entrances_from_other_groups", [])
                 if e.get("portal_reason") == "other-entrance-aligned")
        for _g, _lst, c in copies(p, (e["source_map"], e["line"])):
            c["portal_reason"] = "group-shifted"
        self.assertCaught(p, "portal reason group-shifted (slope reachable None); expected portal reason "
                             "other-entrance-aligned")


class StairwellReplaces(VerifierTest):
    """Fix 2: every stairwell replaces entry equals its indexed exit record."""

    def test_each_copied_field(self):
        for field, value in (("source_tile", [0, 0]), ("target_map", "/shattered_islands/world_0_0"),
                             ("target_tile", [0, 0]), ("arch", "hole"), ("plane_source", [0, 0, 0]),
                             ("plane_target", [0, 0, 0])):
            with self.subTest(field=field):
                p = self.plan()
                r = p["stairwells"][0]["replaces"][0]
                r[field] = value
                self.assertCaught(p, "SW1 replaces entry of exit {}:{} differs from its exit record in {}".format(
                    r["source_map"], r["line"], field))

    def test_all_fields_at_once(self):
        p = self.plan()
        for r in p["stairwells"][0]["replaces"]:
            r.update(source_tile=[0, 0], target_map="/shattered_islands/world_0_0", target_tile=[0, 0],
                     plane_source=[0, 0, 0], plane_target=[0, 0, 0])
        self.assertCaught(p, "SW1 replaces entry")

    def test_shape(self):
        p = self.plan()
        sr1 = next(sw for sw in p["stairwells"] if sw["id"] == "SR1")
        sr1["shape"] = "banana"
        self.assertCaught(p, "SR1 shape banana does not fit")
        p = self.plan()
        sr1 = next(sw for sw in p["stairwells"] if sw["id"] == "SR1")
        sr1["shape"] = "sloped" if sr1["shape"] == "column" else "column"
        self.assertCaught(p, "SR1 shape {} does not fit".format(sr1["shape"]))

    def test_owners(self):
        p = self.plan()
        sw = p["stairwells"][0]
        sw["other"], sw["connects"] = "/nowhere", sorted([sw["group"], "/nowhere"])
        self.assertCaught(p, "SW1 owners")
        p = self.plan()
        sr1 = next(sw for sw in p["stairwells"] if sw["id"] == "SR1")
        sr1["owner"] = "stairwell@0,0,0"
        self.assertCaught(p, "SR1 owners")

    def test_intermediate_appearance(self):
        p = self.plan()
        sw = p["stairwells"][0]
        sw["intermediate_appearance"] = "stairs_red_down"
        self.assertCaught(p, "SW1 intermediate tiles do not show")
        p = self.plan()
        p["stairwells"][0]["tiles"][1]["objects"][0]["walkable"] = True
        self.assertCaught(p, "SW1 intermediate tiles do not show")

    def test_the_bend_limit(self):
        p = self.plan()
        p["settings"]["stairwell_max_bend_tiles"] = 0
        bent = [sw["id"] for sw in p["stairwells"] if sw["shape"] == "column" and sw["max_bend"]]
        self.assertTrue(bent)
        self.assertCaught(p, *("{} bends 1 tile(s) from its column".format(i) for i in bent))

    def test_stairwell_mode(self):
        p = self.plan()
        p["stairwells"][0]["mode"] = "banana"
        self.assertCaught(p, "SW1 mode banana disagrees")

    def test_series_bend_and_counts(self):
        p = self.plan()
        p["stairwells"][0]["id"], p["stairwells"][1]["id"] = p["stairwells"][1]["id"], p["stairwells"][0]["id"]
        self.assertCaught(p, "is out of its series")
        p = self.plan()
        p["stairwells"][0]["max_bend"] += 1
        self.assertCaught(p, "SW1 max_bend")
        p = self.plan()
        p["summary"]["stairwells_by_kind"]["placement"] += 1
        self.assertCaught(p, "summary stairwell and blocked-site counts differ from the lists")

    def test_an_unreachable_blocked_slope(self):
        p = self.plan()
        site = p["sloped_stairwell_sites_blocked"][0]
        self.assertEqual((site["top"], site["bottom"]), ([198, 1895, 0], [194, 1895, -4]))
        site["top"], site["bottom"] = [199, 1895, 0], [193, 1895, -4]
        self.assertCaught(p, "blocked sloped stairwell site [199, 1895, 0] -> [193, 1895, -4] is no stairwell site")

    def test_an_empty_blocked_site(self):
        for key in ("stairwell_sites_blocked", "sloped_stairwell_sites_blocked"):
            with self.subTest(key=key):
                p = self.plan()
                p[key].append(dict(p[key][0], exits=[]))
                self.assertCaught(p, "lists 0 exits")
        p = self.plan()
        p["stairwell_sites_blocked"][0]["shape"] = "sloped"
        self.assertCaught(p, "with shape sloped (column by its ends)")

    def test_direction(self):
        p = self.plan()
        r = p["stairwells"][0]["replaces"][0]
        r["goes"] = "up" if r["goes"] == "down" else "down"
        self.assertCaught(p, "SW1 exit {}:{} goes {}, but its record goes".format(r["source_map"], r["line"], r["goes"]))

    def test_other_copies(self):
        p = self.plan()
        p["stairwell_sites_blocked"][0]["exits"][0]["source_tile"] = [0, 0]
        self.assertCaught(p, "blocked column site entry of exit")
        p = self.plan()
        p["sloped_stairwell_sites_blocked"][0]["exits"][0]["arch"] = "hole"
        self.assertCaught(p, "blocked sloped site entry of exit")
        p = self.plan()
        p["relayering"]["exits"][0]["target_tile"] = [0, 0]
        self.assertCaught(p, "relayering exit of exit")
        p = self.plan()
        p["relayering"]["exits"][0]["crosses_to_group"] = "/emergency"
        self.assertCaught(p, "crosses to /emergency")

    def test_a_placement_site_moved_to_the_relayering_sites(self):
        p = self.plan()
        site = p["stairwell_sites_blocked"].pop(0)
        x = site["exits"][0]
        r = copies(p, (x["source_map"], x["line"]))[0][2]
        up_src = r["plane_source"][2] > r["plane_target"][2]
        top, bottom = (r["source_map"], r["target_map"]) if up_src else (r["target_map"], r["source_map"])
        p["relayering"]["stairwells_blocked"].append(
            {"top_map": top, "bottom_map": bottom, "top": site["top"], "bottom": site["bottom"],
             "shape": site["shape"], "exits": site["exits"]})
        p["summary"]["stairwell_sites_blocked"] -= 1
        p["summary"]["relayering_stairwell_sites_blocked"] += 1
        self.assertCaught(p, "blocked relayering site lists exit {}:{}, which is not an overworld exit".format(
            x["source_map"], x["line"]))

    def test_blocked_site_owners(self):
        p = self.plan()
        p["stairwell_sites_blocked"][0]["other"] = "/shattered_islands/world_0_0"
        self.assertCaught(p, "lists exit {}:{} of other owners".format(
            p["stairwell_sites_blocked"][0]["exits"][0]["source_map"], p["stairwell_sites_blocked"][0]["exits"][0]["line"]))

    def test_an_unrelated_exit_at_a_blocked_site(self):
        p = self.plan()
        key = ("/shattered_islands/world_11_61", 668)
        e = copies(p, key)[0][2]
        self.assertEqual(e["becomes"], "stairs")
        p["stairwell_sites_blocked"][0]["exits"].append(
            {"source_map": e["source_map"], "source_tile": e["source_tile"], "arch": e["arch"], "line": e["line"]})
        self.assertCaught(p, "blocked column stairwell site lists exit {}:{}, which needs no stairwell".format(*key))


class FailedPlacement(VerifierTest):
    """Fix 3: a plan with a failed group does not verify, so it is not published."""

    GID = "/shattered_islands/eld_woods_island/clearhaven/treehome_4"

    def fail(self, p, gid):
        g = group(p, gid)
        g["placement"] = {"status": "failed", "ideal": g["placement"]["ideal"]}
        return g

    def test_failed_group_left_in_the_placement_order(self):
        # what the generator used to publish: the failed group stayed in placement_order
        p = self.plan()
        self.fail(p, self.GID)
        self.assertCaught(p, "placement of {} failed; a plan with a failed group must not be published".format(
            self.GID), "place group {} has no proposed placement".format(self.GID))

    def test_failed_group_kept_unplaced(self):
        # what the generator writes now: unplaced and listed as failed; still refused
        p = self.plan()
        idx = group(p, self.GID)["placement"]["placement_index"]
        self.fail(p, self.GID)
        s = p["summary"]
        s["placement_order"].remove(self.GID)
        s["unplaced_place_groups"] = [self.GID]
        s["failed_placements"] = [self.GID]
        for g in p["groups"]:
            pl = g.get("placement") or {}
            if pl.get("placement_index") is not None and pl["placement_index"] > idx:
                pl["placement_index"] -= 1
        res = verify_plan.verify(p)
        self.assertFalse(res["ok"])
        self.assertIn("placement of {} failed; a plan with a failed group must not be published".format(self.GID),
                      res["errors"])
        self.assertFalse(any("summary unplaced" in e or "failed_placements" in e or "placement order" in e
                             for e in res["errors"]), res["errors"][:8])

    def test_failed_aligned_group(self):
        p = self.plan()
        self.fail(p, MINES)
        self.assertCaught(p, "placement of {} failed".format(MINES))

    def test_a_tentative_placement_only_for_an_uncertain_group(self):
        # what version 2 published for the Rockforge mines: unreachable, tentative, hinted
        p = self.plan()
        g = group(p, MINES)
        self.assertEqual((g["classification"], g["placement"]["status"]), ("place", "proposed"))
        g.update(classification="separate", reason="unreachable")
        g["placement"]["status"] = "tentative"
        s = p["summary"]
        s["placement_order"].remove(MINES)
        s["tentative_placement_order"] = [MINES]
        s["by_classification"]["place:place"] -= 1
        s["by_classification"]["separate:unreachable"] = 1
        self.assertCaught(p, "separate group {} (unreachable) has a tentative placement".format(MINES),
                          "tentative placement of {} carries an owner alignment".format(MINES),
                          "owner alignment of {} to {} is not applied".format(MINES, ROCKFORGE + "rockforge_b"))
        p = self.plan()
        group(p, MINES)["placement"]["status"] = "tentative"
        self.assertCaught(p, "place group {} has no proposed placement".format(MINES),
                          "place group {} (place) has a tentative placement".format(MINES))

    def test_a_fabricated_owner_alignment(self):
        # a group placed by its exits claimed to be an owner alignment instead
        p = self.plan()
        group(p, self.GID)["placement"].update(main_connection=None, alignment_reason="made up")
        self.assertCaught(p, "owner alignment of {} is not its configured spot under a placed anchor".format(self.GID))
        # a configured alignment whose layer distance does not match the placement
        p = self.plan()
        p["settings"]["owner_alignments"][0]["layer_delta"] -= 1
        self.assertCaught(p, "owner alignment of {} is not its configured spot".format(MINES))
        # the decided spot is exact: no extra layer, even one the search might have taken
        p = self.plan()
        pl = group(p, MINES)["placement"]
        pl["layer_offset"] -= 1
        pl["extra_layers_from_ideal"] = 1
        for o in pl["member_origins"].values():
            o[2] -= 1
        self.assertCaught(p, "owner alignment of {} is not its configured spot".format(MINES))
        # an alignment with no decision date
        p = self.plan()
        p["settings"]["owner_alignments"][0]["decided"] = "soon"
        self.assertCaught(p, "owner alignment of {} to {} is not applied".format(MINES, ROCKFORGE + "rockforge_b"))

    def test_a_layer_outside_the_vertical_bounds(self):
        p = self.plan()
        g = group(p, MINE + "mine_unique_a")["placement"]
        self.assertEqual((g["ideal"]["L"], g["layer_offset"]), (-4, -5))
        g["vertical_bounds_from_exit_semantics"] = [-4, -4]
        self.assertCaught(p, "layer offset -5 of {} is outside its vertical bounds [-4, -4]".format(
            MINE + "mine_unique_a"))

    def test_placement_numbers_are_integers(self):
        for field, value in (("extra_layers_from_ideal", True), ("shift_from_ideal", [False, 0]),
                             ("layer_offset", -5.0)):
            with self.subTest(field=field):
                p = self.plan()
                group(p, MINE + "mine_unique_a")["placement"][field] = value
                self.assertCaught(p, "placement of {} has offsets, ideal, shift or extra layers that are not "
                                     "integers".format(MINE + "mine_unique_a"))

    def test_search_limits(self):
        p = self.plan()
        p["settings"]["max_extra_layers"] = 0
        self.assertCaught(p, "placement of /shattered_islands/incuna/ellamin goes 4 layer(s)")
        p = self.plan()
        p["settings"]["max_shift_tiles"] = -1
        self.assertCaught(p, "past the search limits")

    def test_kept_placed_without_a_footprint(self):
        p = self.plan()
        self.assertEqual(group(p, MINES)["placement"]["occupied_tiles_by_layer"], {})
        self.assertEqual([d["group"] for d in p["settings"]["owner_no_footprint"]], [MINES])
        for change in (lambda d: d.update(group="/shattered_islands/everlink/everlink"),  # not placed
                       lambda d: d.update(decided="later")):
            p = self.plan()
            change(p["settings"]["owner_no_footprint"][0])
            self.assertCaught(p, "to keep a group placed without a footprint does not hold in the plan")
        p = self.plan()
        p["settings"]["owner_no_footprint"].append(dict(p["settings"]["owner_no_footprint"][0]))
        self.assertCaught(p, "to keep a group placed without a footprint does not hold in the plan")

    def test_an_alignment_of_a_group_with_exits(self):
        p = self.plan()
        deck = "/shattered_islands/deserted_tutorial_island/ship_lower_deck"
        group(p, MINES)["entrances_from_other_groups"] = [group(p, deck)["entrances_from_overworld"][0]]
        self.assertCaught(p, "owner alignment of {} applies to a group with exits of its own".format(MINES))
        # the decision was made on the sheets as they were laid out
        p = self.plan()
        group(p, MINES)["maps"][0]["tile_links"].pop("N")
        self.assertCaught(p, "owner alignment of {} was decided on another layout of its maps".format(MINES))

    def test_a_dangling_alignment(self):
        p = self.plan()
        p["settings"]["owner_alignments"].append({"group": "/not/a/group", "anchor": "/also/missing", "layer_delta": 0,
                                                  "decided": "2026-10-19", "why": "fabricated"})
        self.assertCaught(p, "owner alignment of /not/a/group to /also/missing is not applied")
        # an entry between two ordinary proposed groups is not applied anywhere
        p = self.plan()
        a, b = p["summary"]["placement_order"][:2]
        p["settings"]["owner_alignments"].append({"group": b, "anchor": a, "layer_delta": -1, "decided": "2026-10-19",
                                                  "why": "stale"})
        self.assertCaught(p, "owner alignment of {} to {} is not applied".format(b, a))

    def test_the_overworld_grid(self):
        p = self.plan()
        p["settings"]["overworld_map_size"] = 25
        self.assertCaught(p, "settings.overworld_map_size 25 is not the 24-tile grid")
        p = self.plan()
        ow = next(g for g in p["groups"] if g["classification"] == "overworld")
        path = sorted(ow["occupied_rows_by_layer"])[0]
        layer = sorted(ow["occupied_rows_by_layer"][path])[0]
        ow["occupied_rows_by_layer"][path][layer][0] += "."
        self.assertCaught(p, "rows of {} on layer {} are not 24 x 24".format(path, layer))

    def test_a_placement_without_status(self):
        p = self.plan()
        group(p, "/emergency")["placement"] = {"status": None, "x_offset": 123}
        self.assertCaught(p, "placement of /emergency has no status")

    def test_alignment_reasons_are_text(self):
        p = self.plan()
        a = p["settings"]["owner_alignments"][0]
        a["why"] = ["x"]
        group(p, a["group"])["placement"]["alignment_reason"] = ["x"]
        self.assertCaught(p, "alignment_reason of {} is not a text".format(a["group"]),
                          "owner alignment of {} to {} is not applied".format(a["group"], a["anchor"]))

    def test_place_group_without_a_placement(self):
        p = self.plan()
        group(p, self.GID).pop("placement")
        self.assertCaught(p, "place group {} has no proposed placement".format(self.GID))

    def test_summary_lists(self):
        p = self.plan()
        p["summary"]["unplaced_place_groups"] = [self.GID]
        self.assertCaught(p, "summary unplaced_place_groups")
        p = self.plan()
        p["summary"]["failed_placements"] = [self.GID]
        self.assertCaught(p, "summary failed_placements")
        p = self.plan()
        p["summary"]["placement_order"].reverse()
        self.assertCaught(p, "placement order in the summary differs")
        p = self.plan()
        p["summary"].pop("failed_placements")
        self.assertCaught(p, "summary failed_placements")


class OwnerDecisions(VerifierTest):
    """The owner decisions of 2026-10-19 are re-checked from the JSON (settings.owner_* and their effects)."""

    def sw(self, p, sid):
        return next(sw for sw in p["stairwells"] if sw["id"] == sid)

    def test_a_decided_portal_stays_a_portal(self):
        p = self.plan()
        for _g, _lst, e in copies(p, ZECHNA_HOLE):
            self.assertEqual((e["becomes"], e["portal_reason"]), ("portal", "stairwell-blocked"))
            e.update(becomes="stairwell", stairwell="SW1")
            for k in ("portal_reason", "portal_reason_text", "slope_reachable"):
                e.pop(k, None)
        self.assertCaught(p, "exit {}:{} is kept a portal by the owner but its record is stairwell".format(*ZECHNA_HOLE))

    def test_the_blocked_site_carries_the_decision(self):
        p = self.plan()
        site = p["stairwell_sites_blocked"][0]
        decision = site.pop("owner_decision")
        self.assertEqual(decision["decided"], "2026-10-19")
        self.assertCaught(p, "blocked column stairwell site at [181, 1599, -1] records owner decision None, "
                             "settings.owner_portals give")
        p = self.plan()
        p["settings"]["owner_portals"] = []
        self.assertCaught(p, "blocked column stairwell site at [181, 1599, -1] records owner decision {",
                          "settings.owner_portals give None")
        p = self.plan()
        p["stairwell_sites_blocked"][0]["owner_decision"]["decision"] = "made up"
        self.assertCaught(p, "blocked column stairwell site at [181, 1599, -1] records owner decision")
        # the decision was made on this site record: another obstruction in the column, another target end or
        # another owner is another premise
        for change in (lambda b: b["column_occupied"][0].update(map=b["column_occupied"][0]["map"] + "_x"),
                       lambda b: b["bottom"].__setitem__(2, -5),
                       lambda b: b.update(other="/shattered_islands/world_7_66")):
            p = self.plan()
            change(p["stairwell_sites_blocked"][0])
            self.assertCaught(p, "is kept a portal for only some of its exits or on another site record")

    def test_a_portal_decision_for_an_exit_at_no_blocked_site(self):
        p = self.plan()
        e = copies(p, CROSSING)[0][2]
        p["settings"]["owner_portals"].append(dict(p["settings"]["owner_portals"][0], source_map=e["source_map"],
                                                   line=e["line"], source_tile=e["source_tile"], arch=e["arch"]))
        self.assertCaught(p, "owner portal {} {}:{} is no exit at a blocked stairwell site".format(e["arch"], *CROSSING),
                          "exit {}:{} is kept a portal by the owner but its record is stairs".format(*CROSSING))
        # the decision names one exit object: another line (a duplicate hole on the tile, say) is not decided
        p = self.plan()
        p["settings"]["owner_portals"][0]["line"] += 1
        self.assertCaught(p, "owner portal hole {}:{} is no exit at a blocked stairwell site".format(
            ZECHNA_HOLE[0], ZECHNA_HOLE[1] + 1), "records owner decision {")
        p = self.plan()
        p["settings"]["owner_portals"][0]["decided"] = ""
        self.assertCaught(p, "is not an exit at a blocked site with a dated owner decision")
        p = self.plan()
        p["settings"]["owner_portals"].append(dict(p["settings"]["owner_portals"][0]))
        self.assertCaught(p, "is configured twice")

    def test_the_stairwell_appearance_rule(self):
        p = self.plan()
        self.assertEqual([self.sw(p, i)["intermediate_appearance"] for i in ("SW6", "SR17", "SR18")],
                         ["stairs_stone_down", "ladder_down", "ladder_down"])
        sw6 = self.sw(p, "SW6")
        sw6["intermediate_appearance"] = "ladder_down"
        for t in sw6["tiles"][1:-1]:
            t["objects"][0]["archetype"] = "ladder_down"
        self.assertCaught(p, "SW6 intermediate tiles do not show stairs_stone_down")
        # the verifier follows the recorded rule; a stair family still wins over a fallback (SW10: invis_exit and
        # stairs_stone_* exits keeps stairs_stone_down)
        p = self.plan()
        next(f for f in p["settings"]["stairwell_appearance_fallbacks"]
             if f["exit_arch"] == "invis_exit")["appearance"] = "ladder_down"
        res = verify_plan.verify(p)
        self.assertIn("SW6 intermediate tiles do not show ladder_down as a decoration", res["errors"])
        self.assertFalse([e for e in res["errors"] if "SW10" in e], res["errors"][:8])
        p = self.plan()
        p["settings"]["stairwell_appearance_fallbacks"] = []
        self.assertCaught(p, *("{} intermediate tiles do not show None".format(i) for i in ("SW6", "SR17", "SR18")))
        # a fallback no stairwell uses is a stale decision
        p = self.plan()
        p["settings"]["stairwell_appearance_fallbacks"].append(
            dict(p["settings"]["stairwell_appearance_fallbacks"][0], exit_arch="teleporter"))
        self.assertCaught(p, "stairwell appearance fallback for teleporter dresses [], the owner chose it for "
                             "['SW6:f2adfb3c19b1']")
        # each fallback is bound to the stairwells the owner chose it for, by premise, not by positional id
        p = self.plan()
        next(f for f in p["settings"]["stairwell_appearance_fallbacks"]
             if f["exit_arch"] == "well_bucket")["stairwells"].append({"id": "SR17", "premise": "0" * 64})
        self.assertCaught(p, "stairwell appearance fallback for well_bucket dresses ['SR18:7c7394a44511'], the owner "
                             "chose it for ['SR18:7c7394a44511', 'SR17:000000000000']")
        p = self.plan()  # another stairwell in SW6's place: same id, other exits
        self.sw(p, "SW6")["direction"] = "down"
        self.assertCaught(p, "stairwell appearance fallback for invis_exit dresses ['SW6:")

    def test_appearance_fallbacks_are_dated_exit_archetypes(self):
        for change, needle in ((lambda f: f.update(exit_arch="Invis_Exit"), "is not an exit archetype"),
                               (lambda f: f.update(appearance=""), "is not an exit archetype"),
                               (lambda f: f.pop("decided"), "is not an exit archetype")):
            p = self.plan()
            change(p["settings"]["stairwell_appearance_fallbacks"][0])
            self.assertCaught(p, needle)
        p = self.plan()
        fbs = p["settings"]["stairwell_appearance_fallbacks"]
        fbs.append(dict(fbs[0]))
        self.assertCaught(p, "stairwell appearance fallback for {} is configured twice".format(fbs[0]["exit_arch"]))

    def test_confirmed_orientations_and_classifications(self):
        dark = "/shattered_islands/strakewood_island/dark_cave/dark_cave"
        p = self.plan()
        group(p, dark)["placement"]["orientation"] = "above"
        self.assertCaught(p, "owner orientation decision {{'group': '{}'".format(dark))
        p = self.plan()
        p["settings"]["owner_orientations"].append(dict(p["settings"]["owner_orientations"][0]))
        self.assertCaught(p, "owner orientation decision", "does not hold in the plan")
        # 'below' still, but no longer from a name keyword alone: a directional exit voted
        p = self.plan()
        group(p, dark)["placement"]["orientation_evidence"].insert(
            0, "exit semantics with upstream groups/overworld: {'below': 1}")
        self.assertCaught(p, "owner orientation decision {{'group': '{}'".format(dark))
        # the same classification on other evidence
        p = self.plan()
        group(p, "/shattered_islands/everlink/everlink")["evidence"][1] = "1 exit object(s) from the overworld lead here"
        self.assertCaught(p, "owner classification decision {'group': '/shattered_islands/everlink/everlink'")
        # a confirmation the plan carries needs its settings entry
        p = self.plan()
        p["settings"]["owner_orientations"].pop(0)
        p["settings"]["owner_classifications"] = []
        self.assertCaught(p, "/shattered_islands/eld_woods_island/cave_morlarg carries an owner orientation "
                             "confirmation without its settings entry",
                          "/hall_of_dms carries an owner classification confirmation without its settings entry")
        p = self.plan()
        group(p, "/shattered_islands/everlink/everlink")["reason"] = "unreachable"
        self.assertCaught(p, "owner classification decision {'group': '/shattered_islands/everlink/everlink'")
        p = self.plan()
        p["settings"]["owner_classifications"][0]["decided"] = "2026-19-10x"
        self.assertCaught(p, "owner classification decision")

    def test_confirmed_relayering_judgement_calls(self):
        p = self.plan()
        p["relayering"]["judgement_calls"][0]["text"] += " (changed)"
        self.assertCaught(p, "the relayering judgement calls differ from the ones the owner confirmed")
        p = self.plan()
        p["settings"]["owner_relayering_judgement_calls"]["calls"].pop()
        self.assertCaught(p, "the relayering judgement calls differ from the ones the owner confirmed")
        p = self.plan()
        p["placement_changes_from_v1"].pop()
        self.assertCaught(p, "the placement changes from version 1 differ from the ones the owner confirmed")
        # both copies agree, but the group is not placed there
        p = self.plan()
        p["placement_changes_from_v1"][0]["v2"][2] = 999
        p["settings"]["owner_relayering_judgement_calls"]["placement_changes_from_v1"][0]["v2"][2] = 999
        self.assertCaught(p, "the placement changes from version 1 differ from the ones the owner confirmed")

    def test_the_seam_stair_archetypes(self):
        p = self.plan()
        ns = p["relayering"]["new_stairs"][0]
        self.assertEqual([(o["tile"], o["archetype"]) for o in ns["objects"]],
                         [("low", "stairs_stone_up"), ("high", "stairs_stone_down")])
        ns["objects"][0]["archetype"] = "stairs_wood_up"  # still an up stair, but not the decided one
        self.assertCaught(p, "new stairs {} do not use the seam stair archetypes stairs_stone_up/stairs_stone_down"
                          .format(ns["id"]))
        p = self.plan()
        p["settings"]["seam_stair_archetypes"]["decided"] = None
        self.assertCaught(p, "settings.seam_stair_archetypes records no dated owner decision")


@unittest.skipUnless(os.environ.get("PLACEMENT_PLAN_CONTENT"), "set PLACEMENT_PLAN_CONTENT to a content checkout")
class ContentFidelity(VerifierTest):
    """Edits that keep the JSON consistent with itself pass verify() but not verify_content()."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = Path(os.environ["PLACEMENT_PLAN_CONTENT"]).resolve()
        cls.fresh = verify_plan.rebuild(cls.content)

    def assertUnfaithful(self, p):
        self.assertTrue(verify_plan.verify(p)["ok"], "the JSON-only check should not see this edit")
        res = verify_plan.verify_content(p, self.content, self.fresh)
        self.assertFalse(res["ok"])
        self.assertIn("the plan differs from a rebuild from the content", res["errors"][0])

    def test_the_committed_plan_is_the_rebuild(self):
        self.assertTrue(verify_plan.verify_content(self.plan(), self.content, self.fresh)["ok"])

    def test_edits_the_json_only_check_cannot_see(self):
        key = ("/planes/creation/creation_0501", 427)
        p = self.plan()  # an exit dropped from the inventory together with its count
        p["exit_inventory"] = [x for x in p["exit_inventory"] if (x["source_map"], x["line"]) != key]
        p["summary"]["exit_objects"] -= 1
        self.assertUnfaithful(p)
        p = self.plan()  # a recordless exit moved to another source line
        next(x for x in p["exit_inventory"] if (x["source_map"], x["line"]) == key)["line"] = -1
        self.assertUnfaithful(p)
        p = self.plan()  # an optional flag present as null in every copy
        for _g, _lst, e in copies(p, CROSSING):
            e["target_is_map_default_entry"] = None
        self.assertUnfaithful(p)
        p = self.plan()  # explanatory text the JSON-only check does not re-derive
        group(p, "/emergency")["evidence"][0] = "made up"
        self.assertUnfaithful(p)

    def test_type_strict_comparison(self):
        p = self.plan()  # caught by both checks, and by the rebuild comparison type for type
        p["summary"]["wired_exits"] = 5.0
        self.assertFalse(verify_plan.verify(p)["ok"])
        self.assertFalse(verify_plan.verify_content(p, self.content, self.fresh)["ok"])


class Report(VerifierTest):
    """The report counts wired exits from the records."""

    def test_an_overworld_wired_exit_is_no_crossing_stairs(self):
        import tempfile

        import report

        p = self.plan()
        ow = next(g for g in p["groups"] if g["classification"] == "overworld")
        wired = next(e for g in p["groups"] for key in INDEX for e in g.get(key, []) if e.get("form") == "wired")
        ow["internal_exits"].append(dict(ow["internal_exits"][0], form="wired", wired=wired["wired"]))
        p["summary"]["wired_exits"] += 1  # as the generator counts it: every wired exit
        with tempfile.TemporaryDirectory() as tmp:
            report.write_report(p, Path(tmp))
            text = (Path(tmp) / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("**66 become plain stairs** (5 of them wired dangling exits)", text)
        self.assertIn("**Wired exits**: 6 legacy exits", text)

    def write(self, p):
        import tempfile

        import report

        with tempfile.TemporaryDirectory() as tmp:
            report.write_report(p, Path(tmp))
            return (Path(tmp) / "REPORT.md").read_text(encoding="utf-8")

    def test_open_decisions_follow_the_plan(self):
        p = self.plan()
        text = self.write(p)
        self.assertIn("### Decided 2026-10-19", text)
        self.assertIn("7. **#26 `rockforge_c` stays placed without a footprint**", text)
        self.assertIn("Open owner decisions: none.", text)
        self.assertIn("## Open owner decisions\n\nNone.", text)
        # without the owner's answers, the empty mines, an undecided blocked site and a stairwell without stair art
        # are open decisions again
        p["settings"]["owner_no_footprint"] = []
        p["stairwell_sites_blocked"][0].pop("owner_decision")
        next(sw for sw in p["stairwells"] if sw["id"] == "SW6")["intermediate_appearance"] = None
        text = self.write(p)
        self.assertIn("1. **Blocked stairwell at #28 `zechna_temple`**", text)
        self.assertIn("2. **Stair archetype for stairwells without stairs**: SW6 (#17 `dark_cave`: invis_exit)", text)
        self.assertIn("3. **New: placed group without a footprint, #26 `strakewood_island/rockforge/rockforge_c`**",
                      text)
        self.assertIn("3 owner decisions are open", text)


class OwnerDecisionRules(unittest.TestCase):
    """The generator's owner decisions of 2026-10-19 apply as recorded and fail closed when they no longer do."""

    HOLE = ("/m", 7, (1, 2), "hole")

    @staticmethod
    def site(*exits, bottom=(0, 0, -3)):
        return {"top": [0, 0, 0], "bottom": list(bottom),
                "exits": [{"source_map": m, "line": ln, "source_tile": list(t), "arch": a} for m, ln, t, a in exits]}

    def decision(self, site):
        return {"premise": build_plan.premise(site), "decision": "kept"}

    def test_stairwell_appearance(self):
        app = build_plan.stairwell_appearance
        self.assertEqual(app(["invis_exit", "stairs_stone_down", "invis_exit"])[0], "stairs_stone_down")
        self.assertEqual(app(["invis_exit", "stairs_wood_up"])[0], "stairs_wood_down")  # a family wins
        self.assertEqual(app(["well_stone", "well_bucket"])[0], "ladder_down")
        self.assertEqual(app(["invis_exit", "invis_exit"]), (
            "stairs_stone_down", "owner decision 2026-10-19: no stair archetype among ['invis_exit'], so the "
                                 "invis_exit exits show stairs_stone_down; the intermediate objects are decorations, "
                                 "not exits", "invis_exit"))
        self.assertEqual(app(["Well_Bucket"])[2], "well_bucket")  # the fallback used, for the staleness check
        self.assertIsNone(app(["invis_exit", "stairs_stone_up"])[2])
        self.assertEqual(app(["hole", "teleporter"]),
                         (None, "no stair archetype among ['hole', 'teleporter']; owner to choose", None))

    def test_portal_decisions_mark_their_sites(self):
        sites = [self.site(self.HOLE), self.site(("/m", 9, (5, 5), "ladder_down"))]
        build_plan.apply_owner_portals(sites, {self.HOLE: self.decision(self.site(self.HOLE))})
        self.assertEqual(sites[0]["owner_decision"], {"decided": "2026-10-19", "decision": "kept"})
        self.assertNotIn("owner_decision", sites[1])

    def test_a_portal_decision_that_no_longer_applies(self):
        decisions = {self.HOLE: self.decision(self.site(self.HOLE))}
        for sites, needle in (([self.site(("/m", 9, (5, 5), "ladder_down"))], "not at a blocked stairwell site"),
                              # another object on the same tile, with the same archetype: not the decided exit
                              ([self.site(("/m", 8, (1, 2), "hole"))], "not at a blocked stairwell site"),
                              ([self.site(self.HOLE, ("/m", 8, (1, 3), "hole"))], "only for some of its exits"),
                              # the same exit at a site whose record changed (here its bottom end)
                              ([self.site(self.HOLE, bottom=(0, 0, -4))], "or for another site record")):
            with self.subTest(needle=needle), self.assertRaisesRegex(SystemExit, needle):
                build_plan.apply_owner_portals(sites, decisions)

    def test_the_confirmed_judgement_calls(self):
        confirmed = (("k", "U1", "t"), ("j", None, "u"))
        calls = build_plan.check_judgement_calls([{"kind": "k", "unit": "U1", "text": "t"}, {"kind": "j", "text": "u"}],
                                                 confirmed)
        self.assertEqual(calls, [{"kind": "k", "unit": "U1", "text": "t"}, {"kind": "j", "text": "u"}])
        with self.assertRaisesRegex(SystemExit, "they now differ"):
            build_plan.check_judgement_calls([{"kind": "k", "unit": "U1", "text": "t2"}, {"kind": "j", "text": "u"}],
                                             confirmed)

    def test_the_committed_plan_records_the_constants(self):
        st = json.loads(PLAN.read_text(encoding="utf-8"))["settings"]
        self.assertEqual({(d["source_map"], d["line"], tuple(d["source_tile"]), d["arch"]): {
            "premise": d["premise"], "decision": d["decision"]} for d in st["owner_portals"]}, build_plan.OWNER_PORTALS)
        self.assertEqual({f["exit_arch"]: (f["appearance"], tuple((x["id"], x["premise"]) for x in f["stairwells"]))
                          for f in st["stairwell_appearance_fallbacks"]}, build_plan.STAIRWELL_APPEARANCE_FALLBACKS)
        self.assertEqual(verify_plan.STAIRWELL_PREMISE_FIELDS, build_plan.STAIRWELL_PREMISE_FIELDS)
        self.assertEqual({a["group"]: (a["anchor"], a["layer_delta"], a["why"], a["premise"])
                          for a in st["owner_alignments"]}, build_plan.OWNER_ALIGN)
        self.assertEqual({d["group"]: (d["orientation"], tuple(d["evidence"])) for d in st["owner_orientations"]},
                         build_plan.OWNER_ORIENTATIONS)
        self.assertEqual({d["group"]: (d["classification"], d["reason"], d["premise"])
                          for d in st["owner_classifications"]}, build_plan.OWNER_CLASSIFICATIONS)
        self.assertEqual({d["group"]: d["decision"] for d in st["owner_no_footprint"]}, build_plan.OWNER_NO_FOOTPRINT)
        self.assertEqual(verify_plan.ALIGN_LAYOUT_FIELDS, build_plan.ALIGN_LAYOUT_FIELDS)
        self.assertEqual(verify_plan.premise({"b": [1, "x"], "a": None}), build_plan.premise({"a": None, "b": [1, "x"]}))
        self.assertEqual(st["owner_relayering_judgement_calls"]["calls"], build_plan.check_judgement_calls(
            st["owner_relayering_judgement_calls"]["calls"], build_plan.OWNER_RELAYERING_JUDGEMENT_CALLS))
        self.assertEqual({k: st["seam_stair_archetypes"][k] for k in ("low", "high")}, build_plan.SEAM_STAIR_ARCHETYPES)


class PlacePool(unittest.TestCase):
    """Fix 3 in the generator: a failed group stays unplaced and nothing is placed through it."""

    # a -> b -> c, and d reachable from the overworld directly
    LINKS = {"a": {"ow"}, "b": {"a"}, "c": {"b"}, "d": {"ow"}}

    def run_pool(self, fails, pool=("a", "b", "c", "d")):
        placed, calls = {"ow"}, []

        def place(gid):
            calls.append(gid)
            if gid in fails:
                return fails[gid]
            placed.add(gid)
            return {"chosen": gid}

        res = build_plan.place_pool(pool, lambda gid: bool(self.LINKS[gid] & placed), lambda gid, rem: set(),
                                    lambda gid: 0, place)
        return res, calls

    def test_all_placed(self):
        (order, failed, left), calls = self.run_pool({})
        self.assertEqual((order, failed, left), (["a", "b", "c", "d"], [], []))
        self.assertEqual(calls, order)

    def test_failed_group_stays_unplaced(self):
        (order, failed, left), calls = self.run_pool({"b": {"failed": True, "ideal": {}}})
        self.assertEqual(order, ["a", "d"])
        self.assertEqual(failed, ["b"])
        self.assertEqual(left, ["c"])          # reachable only through b
        self.assertEqual(calls.count("b"), 1)  # never retried

    def test_no_placement_record_is_a_failure(self):
        (order, failed, left), _calls = self.run_pool({"a": None})
        self.assertEqual((order, failed, left), (["d"], ["a"], ["b", "c"]))


def _run_one(test_id):
    """Run one test in a worker process; return (id, outcome, report)."""
    import io

    out = io.StringIO()
    result = unittest.TextTestRunner(stream=out, verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromName(test_id, sys.modules[__name__]))
    outcome = "ok" if result.wasSuccessful() else "fail"
    if result.wasSuccessful() and result.skipped:
        outcome = "skip"
    return test_id, outcome, "" if result.wasSuccessful() else out.getvalue()


def main(argv=None):
    """Run the suite, by default in parallel worker processes (one test per task; results are the same)."""
    import argparse
    import multiprocessing
    import time
    from concurrent.futures import ProcessPoolExecutor

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8),
                    help="worker processes (1 runs the tests in this process)")
    args = ap.parse_args(argv)
    if args.jobs <= 1 or "fork" not in multiprocessing.get_all_start_methods():
        return 0 if unittest.main(argv=[sys.argv[0]], exit=False).result.wasSuccessful() else 1

    def ids(suite):
        for t in suite:
            if isinstance(t, unittest.TestSuite):
                yield from ids(t)
            else:
                yield t.id().split(".", 1)[1]  # Class.method, looked up in this module by the worker

    tests = sorted(ids(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])))
    start = time.monotonic()
    verify_plan.verify(json.loads(PLAN.read_text(encoding="utf-8")))  # warm the relayering cache the workers fork
    with ProcessPoolExecutor(args.jobs, mp_context=multiprocessing.get_context("fork")) as pool:
        results = list(pool.map(_run_one, tests))
    failed = [(t, report) for t, outcome, report in results if outcome == "fail"]
    for t, report in failed:
        sys.stderr.write("{}\nFAIL: {}\n{}\n".format("=" * 70, t, report))
    skipped = sum(1 for _t, outcome, _r in results if outcome == "skip")
    sys.stderr.write("{}\nRan {} tests in {:.3f}s ({} workers)\n\n{}\n".format(
        "-" * 70, len(results), time.monotonic() - start, args.jobs,
        "FAILED (failures={})".format(len(failed)) if failed else
        "OK" + (" (skipped={})".format(skipped) if skipped else "")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
