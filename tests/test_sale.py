"""Sale percentage tests (Patch 4). Run:  python3 tests/test_sale.py"""
import json
from pathlib import Path

import test_autosync
from test_autosync import check, start, in_step


def owl_updates(e):
    return [w[3] for w in e.bo.server.writes() if w[1] == "/v1/inventory/update"]


def owl_reads(e):
    with e.bo.server.lock:
        return len([w for w in e.bo.server.log if w[1] == "/v1/inventory/list"])


def test_sale_is_copied_changed_and_cleared():
    e, (a, b) = start()
    try:
        for percent in (20, 35, 0):
            e.bl.lots[a]["sale_rate"] = percent
            e.bo.server.clear_log()
            check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
            owl = e.bo.find(a)
            check(owl["sale_percent"] == percent, "Brick Owl sale is %r, expected %d" % (owl["sale_percent"], percent))
            check(owl["price"] == "1.250", "base price changed to %s" % owl["price"])
            check(e.bl.lots[a]["unit_price"] == "1.2500" and e.bl.lots[a]["sale_rate"] == percent, "BrickLink was changed")
            updates = owl_updates(e)
            check(len(updates) == 1 and ("sale_percent=%d" % percent) in updates[0] and "price=" not in updates[0].replace("sale_percent", ""),
                  "Brick Owl updates: %r" % updates)
            check(owl_reads(e) == 3, "Brick Owl should be read back for the sale and sync report, reads: %d" % owl_reads(e))
            with e.bo.server.lock:
                sequence = [(entry[0], entry[1]) for entry in e.bo.server.log
                            if entry[1] in ("/v1/inventory/list", "/v1/inventory/update")]
            check(sequence == [("GET", "/v1/inventory/list"), ("POST", "/v1/inventory/update"),
                               ("GET", "/v1/inventory/list"), ("GET", "/v1/inventory/list")],
                  "expected sale readback followed by report confirmation: %r" % (sequence,))
            with open(e.path("data", "stockade-sync-report.txt")) as f:
                latest_report = f.read().strip().split("\n\n")[-1]
            check("Confirmed: BrickLink lot %d," % a in latest_report, "sale update was not confirmed in the report")
            check(e.bo.find(b)["sale_percent"] == 0, "another lot got a sale")
        check(e.bl.server.writes() == [], "wrote to BrickLink")
        e.bo.server.clear_log()
        check(e.cycle()["result"] == "ok", "idle cycle failed")
        check(e.bo.server.writes() == [] and owl_reads(e) == 1, "idle cycle wrote or read back")
        in_step(e)
    finally:
        e.cleanup()


def test_new_lot_with_sale_and_existing_owl_sale():
    e, (a, b) = start()
    try:
        c = e.bl.add_lot("3003", 8, 2.0)
        e.bl.lots[c]["sale_rate"] = 15
        # Brick Owl carries a sale BrickLink does not have
        e.bo.find(b)["sale_percent"] = 40
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        check(e.bo.find(c)["sale_percent"] == 15, "new lot sale is %r" % e.bo.find(c)["sale_percent"])
        check(e.bo.find(c)["price"] == "2.000" and e.bo.qty(c) == 8, "new lot price or quantity wrong")
        check(e.bo.find(b)["sale_percent"] == 0, "sale that BrickLink does not have was not cleared")
        check(e.bo.find(b)["price"] == "0.500", "base price changed")
        check(e.bl.server.writes() == [], "wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


def test_quarantine_lists_catalogue_and_sale_problems():
    e, (a, b) = start()
    try:
        unknown = e.bl.add_lot("unknown123", 4, 1.0)
        e.bl.lots[a]["sale_rate"] = 25
        e.bl.lots[b]["quantity"] = 9
        e.bo.ignore_sale = True
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        with open(e.path("data", "quarantine.json")) as f:
            snapshot = json.load(f)
        items = {item["lot_id"]: item["reasons"] for item in snapshot["items"]}
        check(items == {a: ["sale_percentage"], unknown: ["unknown_brickowl_catalogue"]}, "wrong problem items: %r" % items)
        with open(e.path("data", "stockade-sync-report.txt")) as f:
            report = f.read()
        check("Problem: BrickLink lot %d," % a in report, "the sale percentage failure was not reported as a problem")
        check(e.bo.find(a) is None and e.bo.find(unknown) is None and e.bo.qty(b) == 9, "problem or unrelated Owl lot was wrong")
        check(e.bl.qty(a) == 10 and e.bl.qty(unknown) == 4 and e.bl.qty(b) == 9, "BrickLink quantities changed")

        e.bo.ignore_sale = False
        e.bo.catalog["unknown123"] = ("Part", 500001)
        check(e.cycle()["result"] == "ok", "cycle after catalogue update failed")
        with open(e.path("data", "quarantine.json")) as f:
            snapshot = json.load(f)
        check(snapshot["items"] == [{"lot_id": a, "reasons": ["sale_percentage"]}], "resolved or repeated problem items: %r" % snapshot)
        check(e.bo.qty(unknown) == 4 and e.bo.find(a) is None, "catalogue or sale skip behavior changed")
    finally:
        e.cleanup()


def test_unreadable_public_notes_skip_only_their_lots_until_corrected():
    e, (healthy, held) = start()
    try:
        prior_owl_lot = e.bo.find(held)["lot_id"]
        e.bl.lots[held]["description"] = "\ud800"
        new_held = e.bl.add_lot("3003", 4, 1.0, cond="U", description="Broken encoding\x00")
        valid_note = "From a sealed set \U0001f600"
        e.bl.lots[healthy].update(quantity=11, description=valid_note)

        check(e.cycle()["result"] == "ok", "a bad note failed the cycle")
        check(e.bl.qty(held) == 5 and e.bl.qty(new_held) == 4, "held BrickLink stock changed")
        check(e.bo.lots[prior_owl_lot]["qty"] == 0, "existing Owl listing was not set to zero")
        check(e.bo.find(new_held) is None, "new held lot was listed on Owl")
        check(e.bo.qty(healthy) == 11, "healthy lot did not sync")
        check(e.bo.find(healthy)["public_note"] == valid_note, "valid Unicode note did not sync")
        quarantine = Path(e.path("data", "quarantine.json"))
        reasons = {item["lot_id"]: item["reasons"] for item in json.loads(quarantine.read_text())["items"]}
        check(reasons == {held: ["unreadable_public_note"], new_held: ["unreadable_public_note"]},
              "incorrect quarantine reasons: %r" % reasons)

        order = e.bl.sale({held: 2})
        check(e.cycle()["result"] == "ok", "sale on held BrickLink lot failed")
        check(e.bl.qty(held) == 3 and e.bo.find(held) is None, "sale reopened the held lot")
        check(Path(e.path("data", "orders", "bricklink-%d.bsx" % order)).is_file(), "held lot order was not saved")

        e.bl.lots[held]["description"] = "From a sealed set"
        e.bl.lots[new_held]["description"] = ""
        check(e.cycle()["result"] == "ok", "corrected notes did not sync")
        check(e.bo.qty(held) == 3 and e.bo.qty(new_held) == 4, "corrected lots did not return at current quantities")
        check(e.bo.find(held)["public_note"] == "From a sealed set", "corrected note did not sync")
        check(json.loads(quarantine.read_text())["items"] == [], "corrected lots stayed in quarantine")
        check(e.bl.server.writes() == [], "quarantine wrote to BrickLink")
        check(not Path(e.path("data", "bricksync.owlignore.txt")).exists(), "quarantine wrote a permanent hold")
        in_step(e)
    finally:
        e.cleanup()


def test_fourteen_character_item_mapping_is_saved():
    e, (a, b) = start()
    try:
        item_no = "12345678901234"
        e.bo.catalog[item_no] = ("Part", 500003)
        first = e.bl.add_lot(item_no, 2, 1.0)
        check(e.cycle()["result"] == "ok" and e.bo.qty(first) == 2, "first lot did not sync")
        e.stop()
        e.start()
        e.bo.server.clear_log()
        second = e.bl.add_lot(item_no, 3, 1.0, color=12)
        check(e.cycle()["result"] == "ok" and e.bo.qty(second) == 3, "second lot did not sync")
        with e.bo.server.lock:
            lookups = [entry for entry in e.bo.server.log if entry[1] == "/v1/catalog/id_lookup" and item_no in entry[2]]
        check(not lookups, "saved mapping was not reused: %r" % (lookups,))
        check(e.bl.server.writes() == [], "wrote to BrickLink")
    finally:
        e.cleanup()


def test_sale_not_shown_by_owl_is_ignored_forever():
    for new_lot, percent in ((False, 25), (True, 25), (False, 0)):
        e, (a, b) = start()
        try:
            if new_lot:
                a = e.bl.add_lot("3003", 8, 2.0)
            if percent == 0:
                e.bo.find(a)["sale_percent"] = 40
            e.bl.lots[a]["sale_rate"] = percent
            e.bl.lots[a]["remarks"] = "Bin A"
            original = dict(e.bl.lots[a])
            marked = dict(original, remarks=original["remarks"] + "~")
            e.bl.lots[b]["quantity"] = 9
            e.bo.ignore_sale = True
            status = e.cycle()
            check(status["result"] == "ok" and status["consecutive_failures"] == 0, "a sale Brick Owl does not show must not fail the sync: %r" % status)
            check(e.bo.find(a) is None, "the ignored lot is still on Brick Owl")
            check(e.bl.lots[a] == marked, "the ignored BrickLink lot was not marked or its other fields changed")
            check(e.bo.qty(b) == 9, "the other lot did not sync")
            check(e.bo.active_count() == len(e.bl.lots) - 1, "the wrong lots were removed from Brick Owl")
            bl_writes = e.bl.server.writes()
            check(len(bl_writes) == 1 and bl_writes[0][0] == "PUT" and bl_writes[0][1] == "/api/store/v1/inventories/%d" % a,
                  "expected one BrickLink remarks update: %r" % (bl_writes,))
            check(json.loads(bl_writes[0][3]).get("remarks") == marked["remarks"], "wrong BrickLink remarks marker")
            sale_updates = [body for body in owl_updates(e) if "sale_percent=" in body]
            check(len(sale_updates) == 1 and "sale_percent=%d" % percent in sale_updates[0], "sale was sent more than once before removal: %r" % sale_updates)
            warning = "sale %d percent did not appear on BrickOwl; removing it from BrickOwl and ignoring it permanently." % percent
            check(e.output().count(warning) == 1, "expected one warning for the ignored lot")
            with open(e.path("data", "bricksync.owlignore.txt")) as f:
                ignored = f.read()
            lines = [line.split() for line in ignored.splitlines() if line.strip()]
            check(len(lines) == 1 and len(lines[0]) == 3, "expected one ignored lot with its sale and time: %r" % ignored)
            check(int(lines[0][0]) == a and int(lines[0][1]) == percent and int(lines[0][2]) > 0, "wrong ignored lot, sale or time: %r" % ignored)

            e.bo.ignore_sale = False
            e.bo.server.clear_log()
            for _ in range(2):
                check(e.cycle()["result"] == "ok", "cycle after Brick Owl recovered failed")
                check(e.bo.find(a) is None, "the ignored lot returned after Brick Owl recovered")
            check(e.bl.lots[a] == marked, "later cycles changed the ignored BrickLink lot")
            e.bl.sale({a: 2})
            check(e.cycle()["result"] == "ok", "BrickLink sale of the ignored lot failed")
            check(e.bl.qty(a) == original["quantity"] - 2 and e.bl.lots[a]["sale_rate"] == percent, "the ignored BrickLink sale was changed")
            check(e.bo.find(a) is None, "the ignored lot returned after its BrickLink sale")
            e.stop()
            e.start()
            check(e.status()["result"] == "ok", "restart cycle failed")
            check(e.cycle()["result"] == "ok", "cycle after restart failed")
            check(e.bo.find(a) is None, "the ignored lot returned after restart")
            check(e.bo.qty(b) == 9, "restart changed the other lot")
            check(e.bo.server.writes() == [], "Brick Owl was written after the lot was ignored: %r" % e.bo.server.writes())
            check(e.bl.server.writes() == bl_writes, "later cycles wrote to BrickLink")
            check(e.output().count(warning) == 1, "the ignored lot printed another warning")
            with open(e.path("data", "bricksync.owlignore.txt")) as f:
                check(f.read() == ignored, "later cycles rewrote or duplicated the ignored lot")
        finally:
            e.cleanup()


def test_full_bricklink_remarks_are_not_truncated_for_sale_marker():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["remarks"] = "A" * 255
        e.bl.lots[a]["sale_rate"] = 25
        original = dict(e.bl.lots[a])
        e.bl.lots[b]["quantity"] = 9
        e.bo.ignore_sale = True
        status = e.cycle()
        check(status["result"] == "ok", "a full remark prevented skipping the item")
        check(e.bl.lots[a] == original, "a full BrickLink remark or another field was changed")
        check(e.bl.server.writes() == [], "the full remark was written to BrickLink")
        check(e.bo.find(a) is None and e.bo.qty(b) == 9, "the affected or unrelated Owl lot was wrong")
        check("could not be marked with ~" in e.output(), "the missing marker was not reported")
        with open(e.path("data", "bricksync.owlignore.txt")) as f:
            check(any(line.split()[0] == str(a) for line in f if line.strip()), "the item was not recorded for skipping")
    finally:
        e.cleanup()


def test_sale_marker_preserves_later_bricklink_remarks_and_description():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["remarks"] = "Old bin"
        e.bl.lots[a]["description"] = "Old note"
        e.bl.lots[a]["sale_rate"] = 25
        e.bl.lots[b]["quantity"] = 9
        e.bo.ignore_sale = True

        def edit_after_initial_read():
            e.bl.lots[a]["remarks"] = "New bin"
            e.bl.lots[a]["description"] = "New note"

        e.bl.on_inventory_read = edit_after_initial_read
        check(e.cycle()["result"] == "ok", "cycle after the remark edit failed")
        check(e.bl.lots[a]["remarks"] == "New bin~", "the newer remark was overwritten")
        check(e.bl.lots[a]["description"] == "New note", "the newer public note was overwritten")
        check(e.bo.find(a) is None and e.bo.qty(b) == 9, "the affected or unrelated Owl lot was wrong")
        e.stop()
        e.start()
        check(e.status()["result"] == "ok" and e.bo.find(a) is None, "restart lost the sale marker")
        check(e.bl.lots[a]["remarks"] == "New bin~" and e.bl.lots[a]["description"] == "New note", "restart changed the newer notes")
    finally:
        e.cleanup()


def test_lost_bricklink_sale_marker_reply_is_checked_before_retry():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["remarks"] = "Bin A"
        e.bl.lots[a]["sale_rate"] = 25
        e.bl.lots[b]["quantity"] = 9
        e.bo.ignore_sale = True
        e.bl.server.fail("PUT", r"/inventories/\d+$", "drop_after", 1)
        check(e.cycle()["result"] == "ok", "a recorded marker with a lost reply was not confirmed")
        check(e.bl.lots[a]["remarks"] == "Bin A~", "the sale marker was lost or duplicated")
        check(e.bo.find(a) is None and e.bo.qty(b) == 9, "the affected or unrelated Owl lot was wrong")
        writes = e.bl.server.writes()
        check(len(writes) == 1 and writes[0][4] == "drop_after", "the marker was written more than once: %r" % writes)
        e.stop()
        e.start()
        check(e.status()["result"] == "ok" and e.bo.find(a) is None, "restart lost the marked item")
        check(e.bl.lots[a]["remarks"] == "Bin A~" and len(e.bl.server.writes()) == 1, "restart repeated the marker")
    finally:
        e.cleanup()


def test_failed_bricklink_sale_marker_still_skips_owl_lot():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["remarks"] = "Bin A"
        e.bl.lots[a]["sale_rate"] = 25
        e.bl.lots[b]["quantity"] = 9
        original = dict(e.bl.lots[a])
        e.bo.ignore_sale = True
        e.bl.server.fail("PUT", r"/inventories/\d+$", "drop_before", 1)
        check(e.cycle()["result"] == "ok", "a failed marker prevented skipping the item")
        check(e.bl.lots[a] == original, "a failed marker changed the BrickLink lot")
        check(e.bo.find(a) is None and e.bo.qty(b) == 9, "the affected or unrelated Owl lot was wrong")
        check("could not be marked with ~" in e.output(), "the missing marker was not reported")
        check(len(e.bl.server.writes()) == 1, "the marker was retried")
        e.stop()
        e.start()
        check(e.status()["result"] == "ok" and e.bo.find(a) is None, "restart lost the skip")
        check(e.bl.lots[a] == original and len(e.bl.server.writes()) == 1, "restart changed or retried the marker")
    finally:
        e.cleanup()


def test_lost_sale_reply_does_not_ignore_working_lot():
    for mode in ("drop_before", "drop_after"):
        e, (a, b) = start()
        try:
            e.bl.lots[a]["sale_rate"] = 25
            original = dict(e.bl.lots[a])
            e.bo.server.fail("POST", r"/inventory/update$", mode, 1)
            status = e.cycle()
            check(status["result"] == "ok", "cycle after %s failed: %r" % (mode, status))
            owl = e.bo.find(a)
            check(owl is not None and owl["sale_percent"] == 25, "a lost reply caused a working lot to be ignored")
            check(owl["price"] == "1.250", "a lost sale reply changed the base price")
            check(e.bl.lots[a] == original, "a lost sale reply changed the BrickLink lot")
            try:
                with open(e.path("data", "bricksync.owlignore.txt")) as f:
                    ignored = f.read().splitlines()
            except FileNotFoundError:
                ignored = []
            check(all(not line.strip() or int(line.split()[0]) != a for line in ignored), "a lost reply permanently ignored a working lot")
            check(e.bl.server.writes() == [], "wrote to BrickLink")
            in_step(e)
        finally:
            e.cleanup()



if __name__ == "__main__":
    test_autosync.main({k: v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)})
