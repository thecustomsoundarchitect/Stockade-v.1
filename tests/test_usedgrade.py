"""Used grade tests (Patch 2). Run:  python3 tests/test_usedgrade.py"""
import test_autosync
from test_autosync import check, start, in_step

HARMLESS = ("", "From a sealed set", "No scratches, cracks or bites", "Scratch-free, clean", "Good condition",
            "Yellow version with print", "Not damaged. Complete.", "Without any wear")
DEFECT = ("Deep scratches on top", "Cracked at the corner", "Yellowed", "Bite marks", "Print is worn",
          "No cracks but yellowed", "No box. Stud is damaged", "Missing one clip", "Stress marks on clip")


def grade(e, lotid):
    return e.bo.find(lotid)["full_con"]


def test_new_listings_get_the_grade():
    e, (a, b) = start()
    try:
        e.bo.catalog["75192-1"] = ("Set", 400001)
        good = [e.bl.add_lot("3003", 5, 1.0, cond="U", description=n) for n in HARMLESS]
        bad = [e.bl.add_lot("3004", 5, 1.0, cond="U", description=n) for n in DEFECT]
        new = e.bl.add_lot("3005", 5, 1.0, cond="N", description="Scratched and cracked")
        aset = e.bl.add_lot("75192-1", 1, 500.0, cond="U", color=0, itemtype="SET", description="Box damaged, torn")
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        for lotid, note in zip(good, HARMLESS):
            check(grade(e, lotid) == "usedg", "note %r: expected Good, got %s" % (note, grade(e, lotid)))
        for lotid, note in zip(bad, DEFECT):
            check(grade(e, lotid) == "useda", "note %r: expected Acceptable, got %s" % (note, grade(e, lotid)))
        check(grade(e, new) == "new", "a New part was graded %s" % grade(e, new))
        check(grade(e, aset) == "usedc", "a set was graded by the part rule: %s" % grade(e, aset))
        check(grade(e, b) == "usedg", "the first used lot is %s" % grade(e, b))
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
        c = e.bl.add_lot("3003", 5, 1.0, cond="U", description="Deep scratches")
        aset = e.bl.add_lot("75192-1", 1, 500.0, cond="U", color=0, itemtype="SET", description="Box damaged")
        check(e.cycle()["result"] == "ok", "cycle failed")
        # Brick Owl holds other grades than the rule gives
        e.bo.find(a)["full_con"] = "new"
        e.bo.find(b)["full_con"] = "usedn"
        e.bo.find(c)["full_con"] = "usedg"
        e.bo.find(aset)["full_con"] = "usedi"
        e.bo.server.clear_log()
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        check(grade(e, b) == "usedg", "Like New lot was not set to Good: %s" % grade(e, b))
        check(grade(e, c) == "useda", "defect lot was not set to Acceptable: %s" % grade(e, c))
        check(grade(e, a) == "new", "New lot changed: %s" % grade(e, a))
        check(grade(e, aset) == "usedi", "the set's grade was changed: %s" % grade(e, aset))
        writes = e.bo.server.writes()
        check(len(writes) == 2 and all(w[1] == "/v1/inventory/update" for w in writes), "Brick Owl writes: %r" % writes)
        check(e.bl.server.writes() == [], "wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


def test_corrected_note_goes_back_to_good():
    e, (a, b) = start()
    try:
        e.bl.lots[b]["description"] = "Cracked and yellowed"
        check(e.cycle()["result"] == "ok", "cycle failed")
        check(grade(e, b) == "useda", "defect note: expected Acceptable, got %s" % grade(e, b))
        e.bl.lots[b]["description"] = "Checked again, no cracks"
        check(e.cycle()["result"] == "ok", "cycle failed")
        check(grade(e, b) == "usedg", "corrected note: expected Good, got %s" % grade(e, b))
        check(e.bo.find(b)["public_note"] == "Checked again, no cracks", "note not copied")
        check(e.bo.qty(b) == 5 and e.bl.qty(b) == 5, "quantity changed")
        check(e.bl.server.writes() == [], "wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


if __name__ == "__main__":
    test_autosync.main({k: v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)})
