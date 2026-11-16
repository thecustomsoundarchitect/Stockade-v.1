"""Auto sync tests. Every test runs the real BrickSync code (test build) against the fake stores
and checks the final stock on both stores and the exact write calls that were made.

Run:  python3 tests/test_autosync.py            (all tests)
      python3 tests/test_autosync.py NAME ...   (some tests)
"""
import json, os, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor
from harness import Env


def check(cond, message):
    if not cond:
        raise AssertionError(message)


def in_step(e):
    """Brick Owl holds exactly the BrickLink lots, with the same quantity and price."""
    with e.bl.lock:
        lots = {k: dict(v) for k, v in e.bl.lots.items()}
    for lotid, lot in lots.items():
        owl = e.bo.find(lotid)
        check(owl is not None, "BrickLink lot %d is missing on Brick Owl" % lotid)
        check(owl["qty"] == lot["quantity"], "lot %d: BrickLink %d, Brick Owl %d" % (lotid, lot["quantity"], owl["qty"]))
        check(abs(float(owl["price"]) - float(lot["unit_price"])) < 0.002, "lot %d: price differs" % lotid)
    check(e.bo.active_count() == len(lots), "Brick Owl has %d lots, BrickLink has %d" % (e.bo.active_count(), len(lots)))


def bl_quantity_writes(e):
    out = []
    for method, path, query, body, mode in e.bl.server.writes():
        if method == "PUT":
            out.append((int(path.rsplit("/", 1)[1]), json.loads(body).get("quantity"), mode))
        else:
            out.append((method, path, mode))
    return out


def start(lots=((("3001", 10, 1.25), {}), (("3002", 5, 0.50), {"cond": "U"})), interval=3600):
    e = Env(interval)
    ids = [e.bl.add_lot(*args, **kw) for args, kw in lots]
    e.start()
    check(e.status()["result"] == "ok", "first cycle failed: " + e.status().get("error", ""))
    in_step(e)
    check(e.bl.server.writes() == [], "the first sync wrote to BrickLink: %r" % e.bl.server.writes())
    e.bl.server.clear_log()
    e.bo.server.clear_log()
    return e, ids


def test_idle_cycle_writes_nothing():
    e, (a, b) = start()
    try:
        for _ in range(2):
            check(e.cycle()["result"] == "ok", "cycle failed")
        check(e.bl.server.writes() == [], "idle cycle wrote to BrickLink: %r" % e.bl.server.writes())
        check(e.bo.server.writes() == [], "idle cycle wrote to Brick Owl: %r" % e.bo.server.writes())
        in_step(e)
    finally:
        e.cleanup()


def test_manual_quantity_edit_reaches_owl():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["quantity"] = 25
        check(e.cycle()["result"] == "ok", "cycle failed")
        check(e.bo.qty(a) == 25, "Brick Owl holds %r, expected 25" % e.bo.qty(a))
        check(e.bl.qty(a) == 25, "BrickLink edit was changed to %r" % e.bl.qty(a))
        check(e.bl.server.writes() == [], "wrote to BrickLink: %r" % e.bl.server.writes())
        in_step(e)
    finally:
        e.cleanup()


def test_manual_price_note_new_lot_deleted_lot():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["unit_price"] = "2.7500"
        e.bl.lots[a]["description"] = "fresh from a sealed set"
        c = e.bl.add_lot("3003", 40, 0.10)
        del e.bl.lots[b]
        check(e.cycle()["result"] == "ok", "cycle failed")
        check(e.bo.find(a)["price"] == "2.750", "price not copied: %r" % e.bo.find(a)["price"])
        check(e.bo.find(a)["public_note"] == "fresh from a sealed set", "note not copied: %r" % e.bo.find(a)["public_note"])
        check(e.bo.qty(c) == 40, "new lot not created on Brick Owl")
        check(e.bo.find(b) is None, "deleted lot is still for sale on Brick Owl")
        check(e.bl.server.writes() == [], "wrote to BrickLink: %r" % e.bl.server.writes())
        check(e.bl.lots[a]["unit_price"] == "2.7500" and e.bl.lots[a]["description"] == "fresh from a sealed set", "BrickLink edit was undone")
        in_step(e)
    finally:
        e.cleanup()


def test_bricklink_sale_deducts_owl_once():
    e, (a, b) = start()
    try:
        order = e.bl.sale({a: 3})
        check(e.cycle()["result"] == "ok", "cycle failed")
        check(e.bl.qty(a) == 7 and e.bo.qty(a) == 7, "after sale: BrickLink %r, Brick Owl %r, expected 7 and 7" % (e.bl.qty(a), e.bo.qty(a)))
        check(os.path.exists(e.path("data", "orders", "bricklink-%d.bsx" % order)), "order file was not saved")
        check(e.bl.server.writes() == [], "wrote to BrickLink: %r" % e.bl.server.writes())
        # The same order shows up again (status change), three more cycles
        for status in ("PACKED", "SHIPPED", "RECEIVED"):
            e.bl.touch_order(order, status)
            check(e.cycle()["result"] == "ok", "cycle failed")
            check(e.bl.qty(a) == 7 and e.bo.qty(a) == 7, "order seen again changed stock: BrickLink %r, Brick Owl %r" % (e.bl.qty(a), e.bo.qty(a)))
        check(e.bl.server.writes() == [], "wrote to BrickLink: %r" % e.bl.server.writes())
        in_step(e)
    finally:
        e.cleanup()


def test_owl_sale_deducts_bricklink_once():
    e, (a, b) = start()
    try:
        order = e.bo.sale({a: 2})
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        check(e.bl.qty(a) == 8 and e.bo.qty(a) == 8, "after sale: BrickLink %r, Brick Owl %r, expected 8 and 8" % (e.bl.qty(a), e.bo.qty(a)))
        check(bl_quantity_writes(e) == [(a, -2, "ok")], "BrickLink writes: %r" % bl_quantity_writes(e))
        check(os.path.exists(e.path("data", "orders", "brickowl-%d.bsx" % order)), "order file was not saved")
        for _ in range(3):
            check(e.cycle()["result"] == "ok", "cycle failed")
        check(e.bl.qty(a) == 8 and e.bo.qty(a) == 8, "order seen again changed stock: BrickLink %r, Brick Owl %r" % (e.bl.qty(a), e.bo.qty(a)))
        check(bl_quantity_writes(e) == [(a, -2, "ok")], "BrickLink writes: %r" % bl_quantity_writes(e))
        in_step(e)
    finally:
        e.cleanup()


def test_owl_sale_sells_out_lot():
    e, (a, b) = start()
    try:
        e.bo.sale({b: 5})
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        check(e.bl.qty(b) is None, "sold out lot still for sale on BrickLink with %r" % e.bl.qty(b))
        check(e.bo.find(b) is None, "sold out lot still for sale on Brick Owl")
        check(e.bl.qty(a) == 10, "other lot changed")
        in_step(e)
    finally:
        e.cleanup()


def test_bricklink_sales_sell_out_every_lot():
    e, (a, b) = start()
    try:
        remaining_bl = dict(e.bl.lots[a])
        remaining_owl = dict(e.bo.find(a))
        e.bl.sale({b: 5})
        check(e.cycle()["result"] == "ok", "first lot sellout failed: " + e.status()["error"])
        check(e.bl.qty(b) is None and e.bo.find(b) is None, "the sold out lot remains for sale")
        check(e.bl.lots[a] == remaining_bl and e.bo.find(a) == remaining_owl, "selling out one lot changed the other lot")
        in_step(e)

        e.bl.sale({a: 10})
        status = e.cycle()
        check(status["result"] == "ok" and status["lots"] == 0, "last lot sellout failed: %r" % status)
        check(e.bl.lots == {} and e.bo.active_count() == 0, "the stores are not both sold out")
        check(e.bl.server.writes() == [], "BrickLink orders caused BrickLink writes")
        writes = e.bo.server.writes()
        check(len(writes) == 2 and all(w[1] == "/v1/inventory/update" and "absolute_quantity=0" in w[3] for w in writes), "expected one Brick Owl removal per lot: %r" % writes)
        for _ in range(2):
            check(e.cycle()["result"] == "ok", "empty cycle after BrickLink sellout failed")
            in_step(e)
        e.stop()
        e.start()
        check(e.status()["result"] == "ok" and e.status()["lots"] == 0, "restart after BrickLink sellout failed")
        check(e.cycle()["result"] == "ok", "cycle after sold out restart failed")
        check(e.bl.lots == {} and e.bo.active_count() == 0, "sold out stock returned after restart")
        check(e.bl.server.writes() == [], "BrickLink was written after sellout")
        check(e.bo.server.writes() == writes, "sold out lots were written again")
    finally:
        e.cleanup()


def test_owl_sales_sell_out_every_lot_once():
    e, (a, b) = start()
    try:
        remaining_bl = dict(e.bl.lots[a])
        remaining_owl = dict(e.bo.find(a))
        e.bo.sale({b: 5})
        check(e.cycle()["result"] == "ok", "first lot sellout failed: " + e.status()["error"])
        check(e.bl.qty(b) is None and e.bo.find(b) is None, "the sold out lot remains for sale")
        check(e.bl.lots[a] == remaining_bl and e.bo.find(a) == remaining_owl, "selling out one lot changed the other lot")
        expected = [("DELETE", "/api/store/v1/inventories/%d" % b, "ok")]
        check(bl_quantity_writes(e) == expected, "the first sold out lot was not deducted once: %r" % bl_quantity_writes(e))
        in_step(e)

        e.bo.sale({a: 10})
        status = e.cycle()
        check(status["result"] == "ok" and status["lots"] == 0 and status["pending_bricklink_deductions"] == 0, "last lot sellout failed: %r" % status)
        check(e.bl.lots == {} and e.bo.active_count() == 0, "the stores are not both sold out")
        expected.append(("DELETE", "/api/store/v1/inventories/%d" % a, "ok"))
        check(bl_quantity_writes(e) == expected, "the sold out lots were not deducted once: %r" % bl_quantity_writes(e))
        for _ in range(2):
            check(e.cycle()["result"] == "ok", "empty cycle after Brick Owl sellout failed")
            in_step(e)
        e.stop()
        e.start()
        check(e.status()["result"] == "ok" and e.status()["lots"] == 0, "restart after Brick Owl sellout failed")
        check(e.cycle()["result"] == "ok", "cycle after sold out restart failed")
        check(e.bl.lots == {} and e.bo.active_count() == 0, "sold out stock returned after restart")
        check(bl_quantity_writes(e) == expected, "a sold out lot was deducted again: %r" % bl_quantity_writes(e))
        check(e.bo.server.writes() == [], "Brick Owl was written after its own sellout")
    finally:
        e.cleanup()


def test_sales_on_both_stores_same_cycle():
    e, (a, b) = start()
    try:
        e.bl.sale({a: 3})
        e.bo.sale({a: 2, b: 1})
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        check(e.bl.qty(a) == 5 and e.bo.qty(a) == 5, "lot a: BrickLink %r, Brick Owl %r, expected 5" % (e.bl.qty(a), e.bo.qty(a)))
        check(e.bl.qty(b) == 4 and e.bo.qty(b) == 4, "lot b: BrickLink %r, Brick Owl %r, expected 4" % (e.bl.qty(b), e.bo.qty(b)))
        check(sorted(bl_quantity_writes(e)) == sorted([(a, -2, "ok"), (b, -1, "ok")]), "BrickLink writes: %r" % bl_quantity_writes(e))
        in_step(e)
    finally:
        e.cleanup()


def test_manual_edit_and_owl_sale_same_lot_same_cycle():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["quantity"] = 30          # manual upload: 10 -> 30
        e.bl.lots[a]["description"] = "edited by hand"
        e.bo.sale({a: 2})                      # Brick Owl still had 10, sells 2
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        check(e.bl.qty(a) == 28 and e.bo.qty(a) == 28, "BrickLink %r, Brick Owl %r, expected 28 and 28" % (e.bl.qty(a), e.bo.qty(a)))
        check(e.bl.lots[a]["description"] == "edited by hand", "the manual note was overwritten: %r" % e.bl.lots[a]["description"])
        check(bl_quantity_writes(e) == [(a, -2, "ok")], "BrickLink writes: %r" % bl_quantity_writes(e))
        in_step(e)
    finally:
        e.cleanup()


def test_order_arrives_during_bricklink_read():
    e, (a, b) = start()
    try:
        e.bl.on_inventory_read = lambda: e.bl.sale({a: 1})
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        check(e.bl.qty(a) == 9 and e.bo.qty(a) == 9, "BrickLink %r, Brick Owl %r, expected 9 and 9" % (e.bl.qty(a), e.bo.qty(a)))
        check(e.bl.server.writes() == [], "wrote to BrickLink: %r" % e.bl.server.writes())
        in_step(e)
    finally:
        e.cleanup()


def test_order_arrives_during_owl_read():
    e, (a, b) = start()
    try:
        e.bl.lots[b]["quantity"] = 6
        e.bo.on_inventory_read = lambda: e.bo.sale({a: 4})
        e.cycle()
        if e.status()["result"] != "ok":
            check(e.cycle()["result"] == "ok", "second cycle failed: " + e.status()["error"])
        check(e.bl.qty(a) == 6 and e.bo.qty(a) == 6, "lot a: BrickLink %r, Brick Owl %r, expected 6 and 6" % (e.bl.qty(a), e.bo.qty(a)))
        check(bl_quantity_writes(e) == [(a, -4, "ok")], "BrickLink writes: %r" % bl_quantity_writes(e))
        in_step(e)
    finally:
        e.cleanup()


def test_bricklink_read_fails_nothing_written():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["quantity"] = 99
        e.bl.server.fail("GET", r"/inventories$", "500", 1000)
        status = e.cycle()
        check(status["result"] == "failed", "cycle should have failed")
        check(e.bl.server.writes() == [] and e.bo.server.writes() == [], "writes after a failed BrickLink read: %r %r" % (e.bl.server.writes(), e.bo.server.writes()))
        check(e.bo.qty(a) == 10, "Brick Owl changed to %r" % e.bo.qty(a))
        e.bl.server.fails.clear()
        check(e.cycle()["result"] == "ok", "recovery cycle failed")
        check(e.bo.qty(a) == 99, "Brick Owl holds %r after recovery, expected 99" % e.bo.qty(a))
        in_step(e)
    finally:
        e.cleanup()


def test_bricklink_empty_inventory_removes_owl_lots_and_restores():
    e, (a, b) = start()
    try:
        with e.bl.lock:
            saved = dict(e.bl.lots)
            e.bl.lots.clear()
        status = e.cycle()
        check(status["result"] == "ok" and status["lots"] == 0, "empty BrickLink inventory did not sync: %r" % status)
        check(e.bl.lots == {} and e.bo.active_count() == 0, "Brick Owl still holds lots after BrickLink became empty")
        check(e.bl.server.writes() == [], "empty inventory sync wrote to BrickLink")
        in_step(e)
        with e.bl.lock:
            e.bl.lots.update(saved)
        check(e.cycle()["result"] == "ok", "recovery cycle failed")
        check(e.bl.lots == saved, "restoring the inventory changed BrickLink")
        check(e.bl.server.writes() == [], "inventory restoration wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


def test_large_batch_of_new_lots_uploaded_at_once():
    e, (a, b) = start()
    try:
        new = []
        with e.bl.lock:
            for i in range(400):
                no = "b%04d" % i
                e.bo.catalog[no] = ("Part", 200000 + i)
                new.append(e.bl.add_lot(no, 1 + i % 50, 0.05 + i * 0.01, cond=("U" if i % 3 == 0 else "N")))
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        in_step(e)
        creates = [w for w in e.bo.server.writes() if w[1] == "/v1/inventory/create"]
        check(len(creates) == 400, "expected 400 Brick Owl creates, found %d" % len(creates))
        check(len(e.bo.server.writes()) == 400, "extra Brick Owl writes: %d" % len(e.bo.server.writes()))
        check(e.bl.server.writes() == [], "wrote to BrickLink: %r" % e.bl.server.writes()[:3])
        for i, lotid in enumerate(new):
            owl = e.bo.find(lotid)
            check((owl["full_con"] == "new") == (i % 3 != 0), "lot %d: wrong condition %r" % (lotid, owl["full_con"]))
        # Nothing is created twice afterwards
        e.bo.server.clear_log()
        check(e.cycle()["result"] == "ok", "second cycle failed")
        check(e.bo.server.writes() == [], "second cycle wrote to Brick Owl: %r" % e.bo.server.writes()[:3])
        in_step(e)
    finally:
        e.cleanup()


def test_new_lots_of_each_item_type():
    e, (a, b) = start()
    try:
        kinds = (("PART", "Part", 11), ("MINIFIG", "Minifigure", 0), ("SET", "Set", 0), ("GEAR", "Gear", 0),
                 ("INSTRUCTION", "Instructions", 0), ("ORIGINAL_BOX", "Packaging", 0))
        new = {}
        with e.bl.lock:
            for i, (bltype, owltype, color) in enumerate(kinds):
                no = "t%d-1" % i
                e.bo.catalog[no] = (owltype, 300000 + i)
                new[bltype] = e.bl.add_lot(no, 3 + i, 2.0 + i, color=color, itemtype=bltype)
        check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
        for bltype, lotid in new.items():
            check(e.bo.qty(lotid) == e.bl.qty(lotid), "%s lot did not arrive on Brick Owl: %r" % (bltype, e.bo.qty(lotid)))
        in_step(e)
        check(e.bl.server.writes() == [], "wrote to BrickLink: %r" % e.bl.server.writes())
        e.bo.server.clear_log()
        check(e.cycle()["result"] == "ok", "second cycle failed")
        check(e.bo.server.writes() == [], "second cycle wrote to Brick Owl: %r" % e.bo.server.writes())
    finally:
        e.cleanup()


def test_default_interval_is_five_minutes():
    e, (a, b) = start(interval=None)
    try:
        check("a cycle runs every 300 seconds" in __import__("re").sub("\x1b\\[[0-9;]*m", "", e.output()), "default interval is not 300 seconds:\n" + e.output()[:2000])
        status = e.status()
        check(295 <= status["next_cycle_time"] - status["time"] <= 305, "next cycle is not 5 minutes away: %r" % status)
    finally:
        e.cleanup()


def test_owl_write_fails_then_recovers():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["quantity"] = 17
        e.bl.sale({b: 2})
        e.bo.server.fail("POST", r"/inventory/update$", "500", 1000)
        check(e.cycle()["result"] == "failed", "cycle should have failed")
        e.bo.server.fails.clear()
        check(e.cycle()["result"] == "ok", "recovery cycle failed: " + e.status()["error"])
        check(e.bo.qty(a) == 17, "lot a: Brick Owl %r, expected 17" % e.bo.qty(a))
        check(e.bl.qty(b) == 3 and e.bo.qty(b) == 3, "lot b: BrickLink %r, Brick Owl %r, expected 3" % (e.bl.qty(b), e.bo.qty(b)))
        check(e.bl.server.writes() == [], "wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


def test_owl_write_reply_lost_no_double():
    e, (a, b) = start()
    try:
        e.bl.sale({a: 3})
        e.bo.server.fail("POST", r"/inventory/update$", "drop_after", 1)   # applied, reply never arrives
        e.cycle()
        if e.status()["result"] != "ok":
            check(e.cycle()["result"] == "ok", "recovery cycle failed")
        check(e.bl.qty(a) == 7 and e.bo.qty(a) == 7, "BrickLink %r, Brick Owl %r, expected 7 and 7" % (e.bl.qty(a), e.bo.qty(a)))
        check(e.cycle()["result"] == "ok", "cycle failed")
        in_step(e)
    finally:
        e.cleanup()


def _owl_sale_with_bricklink_failure(mode, expect_puts):
    e, (a, b) = start()
    try:
        e.bo.sale({a: 2})
        e.bl.server.fail("PUT", r"/inventories/\d+$", mode, 1)
        check(e.cycle()["result"] == "failed", "cycle should be counted as failed while the deduction is unconfirmed")
        check(e.status()["pending_bricklink_deductions"] == 1, "the deduction should still be pending")
        check(e.bo.qty(a) == 8, "Brick Owl holds %r, expected 8" % e.bo.qty(a))
        check(e.cycle()["result"] == "ok", "recovery cycle failed: " + e.status()["error"])
        check(e.bl.qty(a) == 8 and e.bo.qty(a) == 8, "BrickLink %r, Brick Owl %r, expected 8 and 8" % (e.bl.qty(a), e.bo.qty(a)))
        check(e.status()["pending_bricklink_deductions"] == 0, "a deduction is still pending")
        check([w[2] for w in bl_quantity_writes(e)] == expect_puts, "BrickLink writes: %r" % bl_quantity_writes(e))
        check(e.cycle()["result"] == "ok", "cycle failed")
        check(e.bl.qty(a) == 8, "BrickLink changed again to %r" % e.bl.qty(a))
        in_step(e)
    finally:
        e.cleanup()


def test_bricklink_deduction_reply_lost_applied():
    # BrickLink applied the deduction but the reply never arrived: it must not be sent again
    _owl_sale_with_bricklink_failure("drop_after", ["drop_after"])


def test_bricklink_deduction_lost_not_applied():
    # The request never reached BrickLink: it must be sent again, once
    _owl_sale_with_bricklink_failure("drop_before", ["drop_before", "ok"])


def test_bricklink_deduction_rejected():
    _owl_sale_with_bricklink_failure("500", ["500", "ok"])


def test_bricklink_deduction_reply_lost_then_bricklink_sale():
    e, (a, b) = start()
    try:
        e.bo.sale({a: 2})
        e.bl.server.fail("PUT", r"/inventories/\d+$", "drop_after", 1)   # applied: BrickLink 8
        check(e.cycle()["result"] == "failed", "cycle should be counted as failed")
        e.bl.sale({a: 3})                                               # BrickLink 5
        check(e.cycle()["result"] == "ok", "recovery cycle failed: " + e.status()["error"])
        check(e.bl.qty(a) == 5 and e.bo.qty(a) == 5, "BrickLink %r, Brick Owl %r, expected 5 and 5" % (e.bl.qty(a), e.bo.qty(a)))
        in_step(e)
    finally:
        e.cleanup()


def test_killed_after_deduction_sent_then_restart():
    e, (a, b) = start()
    try:
        e.bo.sale({a: 2})
        e.bl.server.fail("PUT", r"/inventories/\d+$", "drop_after", 1)
        e.send("check")
        deadline = time.time() + 60
        while not e.bl.server.writes():
            check(time.time() < deadline, "the deduction was never sent")
            time.sleep(0.05)
        e.kill()                                   # dies right after BrickLink applied it
        check(e.bl.qty(a) == 8, "BrickLink holds %r" % e.bl.qty(a))
        e.start()
        check(e.status()["result"] == "ok", "cycle after restart failed: " + e.status()["error"])
        check(e.bl.qty(a) == 8 and e.bo.qty(a) == 8, "after restart: BrickLink %r, Brick Owl %r, expected 8 and 8" % (e.bl.qty(a), e.bo.qty(a)))
        check(len(e.bl.server.writes()) == 1, "BrickLink was written again: %r" % bl_quantity_writes(e))
        in_step(e)
    finally:
        e.cleanup()


def test_killed_mid_cycle_at_several_points():
    for delay in (0.3, 1.0, 2.5, 4.5):
        e, (a, b) = start()
        try:
            e.bo.sale({a: 2})
            e.bl.sale({b: 1})
            e.bl.lots[a]["quantity"] = 20          # manual: 10 -> 20
            e.send("check")
            time.sleep(delay)
            e.kill()
            e.start()
            if e.status()["result"] != "ok":
                check(e.cycle()["result"] == "ok", "cycle failed: " + e.status()["error"])
            check(e.bl.qty(a) == 18 and e.bo.qty(a) == 18, "kill after %.1fs, lot a: BrickLink %r, Brick Owl %r, expected 18" % (delay, e.bl.qty(a), e.bo.qty(a)))
            check(e.bl.qty(b) == 4 and e.bo.qty(b) == 4, "kill after %.1fs, lot b: BrickLink %r, Brick Owl %r, expected 4" % (delay, e.bl.qty(b), e.bo.qty(b)))
            in_step(e)
        finally:
            e.cleanup()


def test_restart_reads_live_and_keeps_manual_edits():
    e, (a, b) = start()
    try:
        e.stop()
        # While BrickSync is not running: manual BrickLink edits and sales on both stores
        e.bl.lots[a]["quantity"] = 50
        e.bl.lots[a]["unit_price"] = "3.0000"
        c = e.bl.add_lot("3004", 12, 0.30)
        e.bl.sale({b: 1})
        e.bo.sale({b: 2})
        e.start()
        check(e.status()["result"] == "ok", "cycle after restart failed: " + e.status()["error"])
        check(e.bl.qty(a) == 50 and e.bl.lots[a]["unit_price"] == "3.0000", "the manual BrickLink edit was undone")
        check(e.bl.qty(c) == 12, "the manually added lot was changed or removed")
        check(e.bl.qty(b) == 2 and e.bo.qty(b) == 2, "lot b: BrickLink %r, Brick Owl %r, expected 2 and 2" % (e.bl.qty(b), e.bo.qty(b)))
        check(bl_quantity_writes(e) == [(b, -2, "ok")], "BrickLink writes after restart: %r" % bl_quantity_writes(e))
        in_step(e)
    finally:
        e.cleanup()


def test_four_failed_cycles_alert_then_reset():
    e, (a, b) = start()
    try:
        e.bl.server.down = True
        for n in range(1, 5):
            status = e.cycle()
            check(status["result"] == "failed" and status["consecutive_failures"] == n, "cycle %d: %r" % (n, status))
            check(status["alert"] == (n >= 4), "alert flag wrong at failure %d" % n)
        check(e.cycle()["consecutive_failures"] == 5, "it should keep retrying and counting")
        check(e.output().count("ALERT") == 1, "the alert should be raised once, found %d" % e.output().count("ALERT"))
        e.bl.server.down = False
        status = e.cycle()
        check(status["result"] == "ok" and status["consecutive_failures"] == 0 and status["alert"] is False, "not reset: %r" % status)
        in_step(e)
    finally:
        e.cleanup()


def test_timer_runs_without_any_command():
    e, (a, b) = start(interval=6)
    try:
        e.bl.lots[a]["quantity"] = 33
        c = e.bl.add_lot("3005", 7, 0.80)
        base = e.status()["cycle"]
        e.wait_cycle(base + 2, timeout=90)
        check(e.bo.qty(a) == 33 and e.bo.qty(c) == 7, "the timer did not sync: Brick Owl %r and %r" % (e.bo.qty(a), e.bo.qty(c)))
        check(e.bl.server.writes() == [], "wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


def main(tests):
    names = sys.argv[1:] or list(tests)

    def run(name):
        begin = time.time()
        try:
            tests[name]()
            return name, None, time.time() - begin
        except Exception:
            return name, traceback.format_exc(), time.time() - begin

    failed = 0
    with ThreadPoolExecutor(max_workers=int(os.environ.get("BS_TEST_JOBS", "6"))) as pool:
        for name, error, seconds in pool.map(run, names):
            print("%s  %s  (%.0fs)" % ("PASS" if error is None else "FAIL", name, seconds), flush=True)
            if error:
                failed += 1
                print(error, flush=True)
    print("\n%d passed, %d failed" % (len(names) - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main({k: v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)})
