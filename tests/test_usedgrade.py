"""Used grade tests (Patch 2). Run: python3 tests/test_usedgrade.py."""
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


def test_used_notes_respect_exact_terms_and_negation():
    e, _ = start()
    try:
        cases = (
            ("NO CRACKS", "usedg"),
            ("NO YELLOWING", "usedg"),
            ("ONE ARM IS LOOSE BUT NO CRACKS", "useda"),
            ("No hairline cracks, scratches or yellowing", "usedg"),
            ("No cracks but slight yellowing", "useda"),
            ("No cracks. Scratches on one side", "useda"),
            ("Figure wearing a hat with an unbroken shield", "usedg"),
            ("Some surface  ware", "useda"),
            ("A few scrathches", "useda"),
            ("Sticker coming up at one edge", "useda"),
            ("Corner lifted", "useda"),
        )
        lots = [
            (e.bl.add_lot("3003", 5, 1.0, cond="U", description=note), note, expected)
            for note, expected in cases
        ]
        e.bo.catalog["75192-1"] = ("Set", 400001)
        new = e.bl.add_lot("3005", 5, 1.0, cond="N", description="cracks")
        aset = e.bl.add_lot("75192-1", 1, 500.0, cond="U", color=0, itemtype="SET", description="cracks")
        status = e.cycle()
        check(status["result"] == "ok", "cycle failed: " + status["error"])
        for lot, note, expected in lots:
            actual = grade(e, lot)
            check(actual == expected, "%r: expected %s, got %s" % (note, expected, actual))
        check(grade(e, new) == "new", "a New lot was graded by the used rule")
        check(grade(e, aset) == "usedc", "a set was graded by the used rule")
        check(e.bl.server.writes() == [], "grading wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


def test_existing_used_lot_returns_to_good_after_note_correction():
    e, (_, lot) = start()
    try:
        owl_lot = e.bo.find(lot)["lot_id"]
        check(grade(e, lot) == "usedg", "existing lot did not start as Good")
        e.bl.lots[lot]["description"] = "ONE ARM IS LOOSE BUT NO CRACKS"
        check(e.cycle()["result"] == "ok", "cycle failed with defect note")
        check(grade(e, lot) == "useda", "existing lot did not change to Acceptable")

        corrected_note = "NO CRACKS OR YELLOWING"
        e.bl.lots[lot]["description"] = corrected_note
        check(e.cycle()["result"] == "ok", "cycle failed with corrected note")
        corrected = e.bo.find(lot)
        check(corrected["full_con"] == "usedg", "corrected lot did not return to Good")
        check(corrected["lot_id"] == owl_lot, "grading replaced the existing listing")
        check(corrected["public_note"] == corrected_note, "the corrected public note was not copied")
        check(e.bl.server.writes() == [], "grading wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


if __name__ == "__main__":
    test_autosync.main({k: v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)})
