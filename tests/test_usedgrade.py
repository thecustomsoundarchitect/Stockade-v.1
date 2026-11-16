"""Used grade tests (Patch 2). Run:  python3 tests/test_usedgrade.py

The list of words that make a used item Acceptable is empty until Tammy picks the words
(bsevalgrade.c), so these tests cover Good, New and sets. Acceptable is not tested yet.
"""
import test_autosync
from test_autosync import check, start, in_step

NOTES = ("", "From a sealed set", "Good condition", "Yellow version with print")


def grade(e, lotid):
    return e.bo.find(lotid)["full_con"]


def test_new_listings_get_the_grade():
    e, (a, b) = start()
    try:
        kinds = (("MINIFIG", "Minifigure"), ("GEAR", "Gear"), ("INSTRUCTION", "Instructions"))
        e.bo.catalog["75192-1"] = ("Set", 400001)
        good = [e.bl.add_lot("3003", 5, 1.0, cond="U", description=n) for n in NOTES]
        others = []
        for i, (bltype, owltype) in enumerate(kinds):
            e.bo.catalog["u%d" % i] = (owltype, 410000 + i)
            others.append(e.bl.add_lot("u%d" % i, 2, 3.0, cond="U", color=0, itemtype=bltype))
        new = e.bl.add_lot("3005", 5, 1.0, cond="N", description="Some note")
        aset = e.bl.add_lot("75192-1", 1, 500.0, cond="U", color=0, itemtype="SET", description="Some note")
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        for lotid in good + others + [b]:
            check(grade(e, lotid) == "usedg", "lot %d: expected Good, got %s" % (lotid, grade(e, lotid)))
        check(grade(e, new) == "new", "a New part was graded %s" % grade(e, new))
        check(grade(e, aset) == "usedc", "a set was graded by the rule: %s" % grade(e, aset))
        check(e.bl.server.writes() == [], "wrote to BrickLink")
        e.bo.server.clear_log()
        check(e.cycle()["result"] == "ok", "second cycle failed")
        check(e.bo.server.writes() == [], "idle cycle wrote to Brick Owl: %r" % e.bo.server.writes()[:3])
        in_step(e)
    finally:
        e.cleanup()


def test_existing_owl_lots_are_corrected():
    e, (a, b) = start()
    try:
        e.bo.catalog["75192-1"] = ("Set", 400001)
        aset = e.bl.add_lot("75192-1", 1, 500.0, cond="U", color=0, itemtype="SET")
        check(e.cycle()["result"] == "ok", "cycle failed")
        # Brick Owl holds another grade than the rule gives
        e.bo.find(b)["full_con"] = "useda"
        e.bo.find(aset)["full_con"] = "usedi"
        e.bo.server.clear_log()
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        check(grade(e, b) == "usedg", "used lot was not set to Good: %s" % grade(e, b))
        check(grade(e, a) == "new", "New lot changed: %s" % grade(e, a))
        check(grade(e, aset) == "usedi", "the set's grade was changed: %s" % grade(e, aset))
        writes = e.bo.server.writes()
        check(len(writes) == 1 and writes[0][1] == "/v1/inventory/update", "Brick Owl writes: %r" % writes)
        check(e.bl.server.writes() == [], "wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


if __name__ == "__main__":
    test_autosync.main({k: v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)})
