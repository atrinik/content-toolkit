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

    def test_another_schema(self):
        p = self.plan()
        p["schema"] = "atrinik-overworld-placement-plan/3"
        self.assertCaught(p, "schema atrinik-overworld-placement-plan/3 is not atrinik-overworld-placement-plan/4")


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
        p = self.plan()
        group(p, deck)["placement"]["tentative_reason"] = "hint"
        self.assertCaught(p, "proposed placement of {} carries an alignment hint".format(deck))
        p = self.plan()
        group(p, deck)["placement"].update(main_connection=None, tentative_reason="hint")
        self.assertCaught(p, "proposed placement of {} carries an alignment hint".format(deck),
                          "placement of {} has no main connection".format(deck))
        p = self.plan()
        tent = group(p, p["summary"]["tentative_placement_order"][0])["placement"]
        tent["main_connection"] = dict(group(p, deck)["placement"]["main_connection"])
        self.assertCaught(p, "has both a main connection and an alignment hint")
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

    def test_failed_tentative_group(self):
        p = self.plan()
        gid = p["summary"]["tentative_placement_order"][0]
        self.fail(p, gid)
        self.assertCaught(p, "placement of {} failed".format(gid))

    def test_tentative_placement_only_by_classification_or_hint(self):
        p = self.plan()
        gid = p["summary"]["tentative_placement_order"][0]
        g = group(p, gid)
        self.assertEqual((g["classification"], g["reason"]), ("separate", "unreachable"))
        g["placement"].pop("tentative_reason")
        self.assertCaught(p, "separate group {} (unreachable) has a tentative placement without an alignment "
                             "hint".format(gid))
        p = self.plan()
        group(p, gid)["reason"] = "per_player"
        self.assertCaught(p, "separate group {} (per_player) has a tentative placement".format(gid))

    def test_a_fabricated_alignment_hint(self):
        # the last proposed placement turned into an uncertain group with a made-up hint instead of a connection
        p = self.plan()
        s = p["summary"]
        gid = s["placement_order"].pop()
        s["tentative_placement_order"].insert(0, gid)
        s["by_classification"]["place:place"] -= 1
        s["by_classification"]["uncertain:uncertain"] = 1
        g = group(p, gid)
        g.update(classification="uncertain", reason="uncertain")
        g["placement"].update(status="tentative", main_connection=None, tentative_reason="made up")
        self.assertCaught(p, "alignment hint of {} is not a configured alignment to a placed anchor".format(gid))
        # a configured hint whose layer distance does not match the placement
        p = self.plan()
        p["settings"]["tentative_alignments"][0]["layer_delta"] -= 1
        self.assertCaught(p, "alignment hint of {} is not a configured alignment".format(
            p["settings"]["tentative_alignments"][0]["group"]))

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

    def test_a_dangling_alignment(self):
        p = self.plan()
        p["settings"]["tentative_alignments"].append(
            {"group": "/not/a/group", "anchor": "/also/missing", "layer_delta": 0, "why": "fabricated"})
        self.assertCaught(p, "tentative alignment of /not/a/group to /also/missing is not the hint of a tentative "
                             "placement")
        # an entry between two ordinary proposed groups is not applied anywhere
        p = self.plan()
        a, b = p["summary"]["placement_order"][:2]
        p["settings"]["tentative_alignments"].append({"group": b, "anchor": a, "layer_delta": -1, "why": "stale"})
        self.assertCaught(p, "tentative alignment of {} to {} is not the hint of a tentative placement".format(b, a))

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
        a = p["settings"]["tentative_alignments"][0]
        a["why"] = ["x"]
        group(p, a["group"])["placement"]["tentative_reason"] = ["x"]
        self.assertCaught(p, "tentative_reason of {} is not a text".format(a["group"]),
                          "tentative alignment of {} to {} is not the hint".format(a["group"], a["anchor"]))

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
