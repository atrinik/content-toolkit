#!/usr/bin/env python3
"""Negative tests for the placement-plan verifier and generator (standard library only).

    python3 -I scripts/test_placement_plan.py

Each verifier test tampers with a copy of the committed placement-plan.json
and checks that verify_plan.verify() rejects it with the expected error. The
generator tests drive build_plan.place_pool() with stand-in callbacks, so they
need no content checkout.
"""

from __future__ import annotations

import json
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
        self.assertCaught(p, "placement of {} has both a main connection and an alignment hint".format(deck))
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

    def test_the_overworld_map_count(self):
        p = self.plan()
        next(g for g in p["groups"] if g["classification"] == "overworld")["map_count"] += 1
        p["summary"]["maps_total"] += 1
        self.assertCaught(p, "summary group and map counts differ from the groups")

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


if __name__ == "__main__":
    unittest.main()
