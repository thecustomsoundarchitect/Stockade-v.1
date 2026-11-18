"""Auto sync tests. Every test runs the real BrickSync code (test build) against the fake stores
and checks the final stock on both stores and the exact write calls that were made.

Run:  python3 tests/test_autosync.py            (all tests)
      python3 tests/test_autosync.py NAME ...   (some tests)
"""
import json, os, re, subprocess, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor
from harness import BINARY, Env


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


def test_autosync_sellout_ignores_retainemptylots():
    e = Env()
    try:
        with open(e.path("data", "bricksync.conf.txt"), "a") as f:
            f.write("retainemptylots = 1;\n")
        a = e.bl.add_lot("3001", 10, 1.25)
        b = e.bl.add_lot("3002", 5, 0.50, cond="U")
        e.start()
        check(e.status()["result"] == "ok", "first cycle failed")
        in_step(e)
        owlids = {lotid: e.bo.find(lotid)["lot_id"] for lotid in (a, b)}
        remaining_bl = dict(e.bl.lots[b])
        remaining_owl = dict(e.bo.find(b))
        e.bl.server.clear_log()
        e.bo.server.clear_log()

        e.bo.sale({a: 2})
        check(e.cycle()["result"] == "ok", "partial Brick Owl sale failed")
        check(e.bl.qty(a) == 8 and e.bo.qty(a) == 8, "partial sale did not reach BrickLink")
        check(not e.bl.lots[a]["is_stock_room"], "partial sale put the lot in stockroom")
        check(bl_quantity_writes(e) == [(a, -2, "ok")], "partial sale was not deducted once: %r" % bl_quantity_writes(e))

        e.bo.sale({a: 8})
        check(e.cycle()["result"] == "ok", "Brick Owl sellout failed")
        check(e.bl.qty(a) is None, "sold out BrickLink lot was retained in stockroom: %r" % e.bl.lots.get(a))
        check(e.bo.find(a) is None and e.bo.lots[owlids[a]]["qty"] == 0, "Brick Owl did not retain its sold out record at zero")
        check(e.bl.lots[b] == remaining_bl and e.bo.find(b) == remaining_owl, "the other lot changed after sellout")
        expected = [(a, -2, "ok"), ("DELETE", "/api/store/v1/inventories/%d" % a, "ok")]
        check(bl_quantity_writes(e) == expected, "sellout did not delete BrickLink exactly once: %r" % bl_quantity_writes(e))
        check(all(not json.loads(w[3]).get("is_stock_room", False) for w in e.bl.server.writes() if w[0] == "PUT"), "auto sync wrote a stockroom mark")
        in_step(e)

        e.bl.sale({b: 5})
        status = e.cycle()
        check(status["result"] == "ok" and status["lots"] == 0, "last BrickLink lot sellout failed: %r" % status)
        check(e.bl.lots == {} and e.bo.active_count() == 0, "sold out lots remain active")
        check(set(e.bo.lots) == set(owlids.values()) and all(lot["qty"] == 0 for lot in e.bo.lots.values()), "Brick Owl did not keep both original zero quantity records")
        writes = e.bo.server.writes()
        check(len(writes) == 1 and writes[0][1] == "/v1/inventory/update" and "absolute_quantity=0" in writes[0][3], "Brick Owl did not receive only the final zero quantity update: %r" % writes)
        for _ in range(2):
            check(e.cycle()["result"] == "ok", "empty cycle after sellout failed")
        e.stop()
        e.start()
        check(e.status()["result"] == "ok", "restart after sellout failed")
        check(e.cycle()["result"] == "ok", "cycle after sellout restart failed")
        check(e.bl.lots == {} and set(e.bo.lots) == set(owlids.values()) and all(lot["qty"] == 0 for lot in e.bo.lots.values()), "restart recreated or removed a sold out lot")
        check(bl_quantity_writes(e) == expected, "restart repeated a BrickLink deduction: %r" % bl_quantity_writes(e))
        check(e.bo.server.writes() == writes, "restart wrote a sold out Brick Owl lot again")
        check(not any(w[1] == "/v1/inventory/delete" for w in e.bo.server.writes()), "auto sync called Brick Owl inventory delete")
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


def test_old_autosync_off_setting_is_ignored():
    e = Env(interval=None)
    a = e.bl.add_lot("3001", 10, 1.25)
    with open(e.path("data", "bricksync.conf.txt"), "a") as f:
        f.write("autosync = 0;\n")
    try:
        e.start()
        check(e.status()["result"] == "ok", "the old off setting stopped automatic syncing")
        check(e.bo.qty(a) == 10, "the first automatic sync did not run")
        plain = re.sub("\x1b\\[[0-9;]*m", "", e.output())
        check("Auto sync is on" in plain, "automatic syncing did not start: " + plain[-2000:])
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
    _uncertain_deduction_holds_lot("drop_after", 10)


def test_bricklink_deduction_lost_not_applied():
    _uncertain_deduction_holds_lot("drop_before", 8)


def test_bricklink_deduction_rejected():
    _uncertain_deduction_holds_lot("500", 10)


def test_bricklink_deduction_http_500_stays_uncertain():
    _uncertain_deduction_holds_lot("500", 8)


def _uncertain_deduction_holds_lot(mode, edited_quantity):
    e, (a, b) = start()
    try:
        e.bo.sale({a: 2})
        e.bl.server.fail("PUT", r"/inventories/\d+$", mode, 1)
        check(e.cycle()["result"] == "failed", "the unconfirmed sale was not reported")
        check(e.status()["pending_bricklink_deductions"] == 1, "the sale was not kept pending")
        check(e.bo.find(a) is None, "the uncertain lot remains on Brick Owl")
        with e.bl.lock:
            e.bl.lots[a]["quantity"] = edited_quantity
            e.bl.lots[a]["unit_price"] = "3.0000"
            e.bl.lots[b]["quantity"] = 11
        check(e.cycle()["result"] == "failed", "uncertainty was incorrectly resolved")
        check(e.bl.qty(a) == edited_quantity, "the uncertain deduction was sent again")
        check(e.bo.find(a) is None, "the uncertain lot returned to Brick Owl")
        check(e.bo.qty(b) == 11, "an unrelated lot did not sync")
        check(bl_quantity_writes(e) == [(a, -2, mode)], "unexpected BrickLink writes: %r" % bl_quantity_writes(e))
        check(e.status()["pending_bricklink_deductions"] == 1, "the uncertain sale disappeared")
        e.stop()
        e.start()
        check(e.status()["result"] == "failed", "restart incorrectly resolved the uncertainty")
        check(e.bl.qty(a) == edited_quantity and e.bo.find(a) is None, "restart changed the uncertain lot")
        check(e.status()["pending_bricklink_deductions"] == 1, "restart lost the pending sale")
        check(bl_quantity_writes(e) == [(a, -2, mode)], "restart repeated the deduction")
    finally:
        e.cleanup()


def test_uncertain_sale_holds_later_sale_on_same_lot():
    e, (a, b) = start()
    try:
        e.bo.sale({a: 2})
        e.bo.sale({a: 1})
        e.bl.server.fail("PUT", r"/inventories/\d+$", "drop_before", 1)
        check(e.cycle()["result"] == "failed", "the first sale was not pending")
        check(e.cycle()["result"] == "failed", "the sales were not kept pending")
        check(e.bl.qty(a) == 10 and e.bo.find(a) is None, "the affected lot was not skipped")
        check(e.status()["pending_bricklink_deductions"] == 2, "both sales were not kept pending")
        check(bl_quantity_writes(e) == [(a, -2, "drop_before")], "a later sale was sent while the lot was uncertain")
    finally:
        e.cleanup()


def test_uncertain_sale_holds_missing_bricklink_lot_on_owl():
    e, (a, b) = start()
    try:
        e.bo.sale({a: 2})
        e.bl.server.fail("PUT", r"/inventories/\d+$", "drop_before", 1)
        check(e.cycle()["result"] == "failed", "the sale was not pending")
        with e.bl.lock:
            del e.bl.lots[a]
            e.bl.lots[b]["quantity"] = 11
        check(e.cycle()["result"] == "failed", "the missing lot was incorrectly confirmed")
        check(e.bo.find(a) is None, "the missing BrickLink lot remains on Brick Owl")
        check(e.bo.qty(b) == 11, "the unrelated lot did not sync")
        check(e.status()["pending_bricklink_deductions"] == 1, "the sale was not kept pending")
    finally:
        e.cleanup()


def test_uncertain_sale_holds_unmapped_missing_bricklink_lot_on_owl():
    e, (a, b) = start()
    try:
        with e.bo.lock:
            owl_lot_id = e.bo.find(a)["lot_id"]
        e.bo.sale({a: 2})
        with e.bo.lock:
            affected_lot = dict(e.bo.lots[owl_lot_id])
            unrelated_lot = dict(e.bo.find(b))
            unrelated_lot_id = unrelated_lot["lot_id"] + 100
        e.bl.server.fail("PUT", r"/inventories/\d+$", "drop_before", 1)
        check(e.cycle()["result"] == "failed", "the sale was not pending")
        with e.bo.lock:
            # Restore the listing without its external ID to model a lost mapping.
            e.bo.lots[owl_lot_id] = affected_lot
            e.bo.lots[owl_lot_id]["external_id"] = ""
            unrelated_lot["lot_id"] = unrelated_lot_id
            unrelated_lot["external_id"] = ""
            e.bo.lots[unrelated_lot_id] = unrelated_lot
        with e.bl.lock:
            del e.bl.lots[a]
            e.bl.lots[b]["quantity"] = 11
        check(e.cycle()["result"] == "failed", "the missing lot was incorrectly confirmed")
        with e.bo.lock:
            owl = e.bo.lots.get(owl_lot_id)
            check(owl is not None and owl["qty"] == 0, "the identifiable affected Owl lot was not skipped")
            check(e.bo.lots[unrelated_lot_id]["qty"] == 5, "an unrelated unmapped Owl lot was changed")
        check(e.bo.qty(b) == 11, "the unrelated mapped lot did not sync")
        check(e.status()["pending_bricklink_deductions"] == 1, "the sale was not kept pending")
        e.stop()
        e.start()
        with e.bo.lock:
            owl = e.bo.lots.get(owl_lot_id)
            check(owl is not None and owl["qty"] == 0, "restart restored the affected Owl lot")
            check(e.bo.lots[unrelated_lot_id]["qty"] == 5, "restart changed the unrelated unmapped Owl lot")
        check(e.status()["pending_bricklink_deductions"] == 1, "restart lost the pending sale")
    finally:
        e.cleanup()


def test_uncertain_lost_reply_does_not_deduct_again():
    e, (a, b) = start()
    try:
        e.bo.sale({a: 2})
        e.bl.server.fail("PUT", r"/inventories/\d+$", "drop_after", 1)
        check(e.cycle()["result"] == "failed", "the lost reply was not kept pending")
        with e.bl.lock:
            e.bl.lots[a]["quantity"] = 20
        check(e.cycle()["result"] == "failed", "the uncertain deduction should remain pending")
        check(e.bl.qty(a) == 20, "stock was deducted again after an uncertain reply")
        check(e.bo.find(a) is None, "the uncertain lot remains on Brick Owl")
        check(e.status()["pending_bricklink_deductions"] == 1, "the uncertain sale record disappeared")
        check(bl_quantity_writes(e) == [(a, -2, "drop_after")], "BrickLink was written again: %r" % bl_quantity_writes(e))
        check(e.cycle()["result"] == "failed", "the uncertain deduction should still remain pending")
        check(e.bl.qty(a) == 20, "stock changed during the next review cycle")
        check(bl_quantity_writes(e) == [(a, -2, "drop_after")], "BrickLink was written again: %r" % bl_quantity_writes(e))
    finally:
        e.cleanup()


def test_short_bricklink_stock_keeps_unresolved_remainder():
    e, (a, b) = start()
    try:
        e.stop()
        with e.bl.lock:
            e.bl.lots[a]["quantity"] = 2
        with open(e.path("data", "bricksync.blpending.txt"), "w") as f:
            f.write("BSLEDGER 1\n8000001 %d 5 0 0\n" % a)
        e.proc, e.out = None, None
        e.start()
        status = e.status()
        check(status["result"] == "failed", "the unresolved remainder was not reported")
        check(status["pending_bricklink_deductions"] == 1, "the remainder was forgotten: %r" % status)
        check(e.bl.qty(a) is None, "the available BrickLink stock was not deducted")
        with open(e.path("data", "bricksync.blpending.txt")) as f:
            ledger = f.read().splitlines()
        check(ledger == ["BSLEDGER 1", "8000001 %d 3 0 0" % a], "unexpected pending sale record: %r" % ledger)
        e.bl.server.clear_log()
        check(e.cycle()["result"] == "failed", "the missing amount should remain unresolved")
        check(e.status()["pending_bricklink_deductions"] == 1, "the missing amount disappeared on the next cycle")
        check(e.bl.server.writes() == [], "BrickLink was written again: %r" % e.bl.server.writes())
    finally:
        e.cleanup()


def test_damaged_pending_file_stops_startup_without_removal_instruction():
    e = Env()
    damaged = "this is not a pending sale file\n"
    with open(e.path("data", "bricksync.blpending.txt"), "w") as f:
        f.write(damaged)
    try:
        env = dict(os.environ, BS_TEST_BL_PORT=str(e.bl.server.port), BS_TEST_BO_PORT=str(e.bo.server.port))
        e.out = open(e.path("stdout.txt"), "ab")
        e.proc = subprocess.Popen([BINARY], cwd=e.dir, env=env, stdin=subprocess.PIPE, stdout=e.out, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while "Do you agree to apply these changes" not in e.output():
            check(time.time() < deadline and e.proc.poll() is None, "first setup did not reach its confirmation:\n" + e.output()[-3000:])
            time.sleep(0.1)
        e.send("yes")
        deadline = time.time() + 20
        while "must be reviewed before BrickSync can start" not in e.output():
            check(time.time() < deadline and e.proc.poll() is None, "startup did not stop on the damaged file:\n" + e.output()[-3000:])
            time.sleep(0.1)
        plain = re.sub("\x1b\\[[0-9;]*m", "", e.output())
        check("Fix or remove" not in plain and "remove it" not in plain, "startup still tells the user to remove the file")
        with open(e.path("data", "bricksync.blpending.txt")) as f:
            check(f.read() == damaged, "the damaged pending sale file changed")
        e.send("")
        e.proc.wait(timeout=20)
        check(e.proc.returncode != 0, "BrickSync did not stop after the damaged file warning")
    finally:
        e.cleanup()


def test_bricklink_deduction_reply_lost_then_bricklink_sale():
    e, (a, b) = start()
    try:
        e.bo.sale({a: 2})
        e.bl.server.fail("PUT", r"/inventories/\d+$", "drop_after", 1)   # applied: BrickLink 8
        check(e.cycle()["result"] == "failed", "cycle should be counted as failed")
        e.bl.sale({a: 3})                                               # BrickLink 5
        check(e.cycle()["result"] == "failed", "the uncertain deduction should remain pending")
        check(e.bl.qty(a) == 5 and e.bo.find(a) is None, "the uncertain lot was not skipped")
        check(e.status()["pending_bricklink_deductions"] == 1, "the uncertain sale disappeared")
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
        check(e.status()["result"] == "failed", "restart incorrectly resolved the uncertain deduction")
        check(e.bl.qty(a) == 8 and e.bo.find(a) is None, "restart did not skip the uncertain lot")
        check(len(e.bl.server.writes()) == 1, "BrickLink was written again: %r" % bl_quantity_writes(e))
        check(e.status()["pending_bricklink_deductions"] == 1, "restart lost the uncertain sale")
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


def test_restart_owl_read_failure_keeps_sale_pending_until_retry():
    e, (a, b) = start()
    try:
        e.stop()
        e.bo.sale({a: 2})
        e.bo.server.fail("GET", r"/v1/inventory/list$", "500", 1000)
        e.start()
        check(e.status()["result"] == "failed", "startup should fail when Brick Owl inventory cannot be read")
        check(e.bl.server.writes() == [] and e.bo.server.writes() == [], "a store was written before the Brick Owl read")
        check(e.status()["pending_bricklink_deductions"] == 1, "the sale was not kept pending")
        check(e.bl.qty(a) == 10, "BrickLink was changed before the Brick Owl read")
        e.bo.server.fails.clear()
        check(e.cycle()["result"] == "ok", "retry failed: " + e.status()["error"])
        check(bl_quantity_writes(e) == [(a, -2, "ok")], "sale was not deducted exactly once: %r" % bl_quantity_writes(e))
        in_step(e)
    finally:
        e.cleanup()


def test_restart_order_during_owl_read_is_processed_before_deduction():
    e, (a, b) = start()
    try:
        e.stop()
        e.bo.sale({a: 2})
        writes_during_read = []
        def sale_during_read():
            writes_during_read.extend(bl_quantity_writes(e))
            e.bo.sale({b: 1})
        e.bo.on_inventory_read = sale_during_read
        e.start()
        check(e.status()["result"] == "ok", "startup failed: " + e.status()["error"])
        check(writes_during_read == [], "BrickLink was written before the startup Brick Owl read: %r" % writes_during_read)
        check(sorted(bl_quantity_writes(e)) == sorted([(a, -2, "ok"), (b, -1, "ok")]), "sales were not deducted exactly once: %r" % bl_quantity_writes(e))
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



def test_daily_backup_archives_orders_and_retains_ten_days():
    import tarfile
    from pathlib import Path

    base = int(time.time()) - 3 * 86400
    e = Env()
    Path(e.path("data", "test.dailybackup-time")).write_text(str(base))
    a = e.bl.add_lot("3001", 10, 1.25)
    b = e.bl.add_lot("3002", 5, 0.50)

    def wait_backup():
        deadline = time.monotonic() + 30
        expected = (e.proc.pid, e.status()["cycle"])
        while time.monotonic() < deadline:
            try:
                if tuple(map(int, Path(e.path("data", "test.dailybackup-cycle")).read_text().split())) == expected:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise AssertionError("daily backup check did not finish: " + e.output()[-2000:])

    def archive(timestamp):
        return Path(e.path("data", "backups", "daily-" + time.strftime("%Y-%m-%d", time.localtime(timestamp)) + ".tar"))

    try:
        e.start()
        wait_backup()
        first = archive(base)
        check(first.is_file(), "the first daily archive was not created")
        first_data = first.read_bytes()
        check(e.cycle()["result"] == "ok", "repeat cycle failed")
        wait_backup()
        check(first.read_bytes() == first_data, "the same day's archive was replaced")
        Path(e.path("data", "stockade-sync-report.txt")).write_text("Existing sync report\n")
        bl_order = e.bl.sale({a: 2})
        bo_order = e.bo.sale({b: 1})
        next_day = base + 86400
        backups = first.parent
        old = backups / "daily-2001-01-01.tar"
        boundary = backups / "daily-2001-01-02.tar"
        newer = backups / "daily-2001-01-03.tar"
        legacy = backups / "2001-01-01" / "00001.bsx"
        legacy.parent.mkdir(exist_ok=True)
        for path, age in ((old, 10 * 86400 + 1), (boundary, 10 * 86400), (newer, 10 * 86400 - 1), (legacy, 20 * 86400)):
            path.write_bytes(b"existing backup")
            os.utime(path, (next_day - age, next_day - age))
        Path(e.path("data", "test.dailybackup-time")).write_text(str(next_day))
        check(e.cycle()["result"] == "ok", "sales or next day backup cycle failed")
        wait_backup()
        second = archive(next_day)
        check(second.is_file(), "the next daily archive was not created")
        expected = {"data/bricksync.conf.txt", "data/bricksync.state", "data/bricksync.inventory.bsx",
                    "data/quarantine.json", "data/stockade-sync-report.txt",
                    "data/orders/bricklink-%d.bsx" % bl_order, "data/orders/brickowl-%d.bsx" % bo_order}
        with tarfile.open(second) as saved:
            members = {item.name.removeprefix("./"): item for item in saved.getmembers() if item.isfile()}
            check(expected <= set(members), "daily archive is missing records: %r" % (expected - set(members)))
            for name in expected:
                check(saved.extractfile(members[name]).read() == Path(e.path(*name.split("/"))).read_bytes(),
                      "daily archive does not match the saved record: " + name)
        check(not old.exists(), "daily archive older than ten days was retained")
        check(boundary.exists() and newer.exists() and legacy.exists(), "retention removed a kept or existing backup")
        check(Path(e.path("data", "orders", "bricklink-%d.bsx" % bl_order)).is_file(), "BrickLink order file was removed")
        check(Path(e.path("data", "orders", "brickowl-%d.bsx" % bo_order)).is_file(), "Brick Owl order file was removed")
        check(e.bl.qty(a) == 8 and e.bl.qty(b) == 4, "backup changed sale syncing")
    finally:
        e.cleanup()



def test_daily_backup_failure_retries_without_stopping_sales():
    from pathlib import Path

    base = int(time.time()) - 3 * 86400
    e = Env()
    Path(e.path("data", "test.dailybackup-time")).write_text(str(base))
    a = e.bl.add_lot("3001", 10, 1.25)
    b = e.bl.add_lot("3002", 5, 0.50)
    day = time.strftime("%Y-%m-%d", time.localtime(base))
    backup = Path(e.path("data", "backups", "daily-" + day + ".tar"))
    blocker = backup.with_name("." + backup.name + ".tmp")
    blocker.mkdir(parents=True)
    try:
        e.start()
        deadline = time.monotonic() + 30
        while "Daily backup: could not replace temporary archive" not in e.output():
            check(time.monotonic() < deadline, "backup failure was not reported")
            time.sleep(0.05)
        e.bl.sale({a: 2})
        e.bo.sale({b: 1})
        check(e.cycle()["result"] == "ok", "backup failure stopped sale syncing")
        check(e.bl.qty(a) == 8 and e.bo.qty(a) == 8 and e.bl.qty(b) == 4 and e.bo.qty(b) == 4,
              "sale quantities were incorrect while backup failed")
        blocker.rmdir()
        check(e.cycle()["result"] == "ok", "cycle after backup failure failed")
        deadline = time.monotonic() + 30
        while not backup.is_file():
            check(time.monotonic() < deadline, "daily backup was not retried")
            time.sleep(0.05)
        check(bl_quantity_writes(e) == [(b, -1, "ok")], "backup retry repeated a sale deduction")
    finally:
        e.cleanup()


def test_sync_report_confirms_changes_and_lists_skipped_lot():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["quantity"] = 14
        added = e.bl.add_lot("3003", 7, 1.00)
        skipped = e.bl.add_lot("3004", 4, 1.00)
        e.bl.lots[skipped]["remarks"] = "~"
        check(e.cycle()["result"] == "ok", "the update failed")
        with open(e.path("data", "stockade-sync-report.txt")) as report:
            text = report.read()
        check("Confirmed: BrickLink lot %d," % a in text, "the updated lot was not confirmed")
        check("Confirmed: BrickLink lot %d," % added in text, "the new lot was not confirmed")
        check("Skipped: BrickLink lot %d," % skipped in text, "the intentionally skipped lot was not listed correctly")
        check("Problem: BrickLink lot %d," % skipped not in text, "the intentionally skipped lot was reported as a problem")
        check("quantity 14" in next(line for line in text.splitlines() if line.startswith("Confirmed: BrickLink lot %d," % a)), "the updated quantity was not reported")
        check("quantity 7" in next(line for line in text.splitlines() if line.startswith("Confirmed: BrickLink lot %d," % added)), "the new quantity was not reported")
        check(e.bo.qty(a) == 14 and e.bo.qty(added) == 7, "confirmed quantities do not match Brick Owl")
        check(e.bo.find(skipped) is None, "the skipped lot appeared on Brick Owl")
        check(e.bl.server.writes() == [], "the report wrote to BrickLink")
        check(e.cycle()["result"] == "ok", "the repeat cycle failed")
        with open(e.path("data", "stockade-sync-report.txt")) as report:
            check(report.read() == text, "an unchanged skipped lot was reported again")
        e.bl.lots[skipped]["quantity"] = 5
        check(e.cycle()["result"] == "ok", "the later update failed")
        with open(e.path("data", "stockade-sync-report.txt")) as report:
            updated = report.read()
        rows = [line for line in updated.splitlines() if line.startswith("Skipped: BrickLink lot %d," % skipped)]
        check(len(rows) == 2 and "quantity 5" in rows[-1], "a later change to the skipped lot was not reported")
    finally:
        e.cleanup()


def test_sync_report_does_not_confirm_an_unapplied_update():
    e, (a, b) = start()
    try:
        handle = e.bo.server.handler
        def acknowledge_without_update(method, path, query, body):
            if path == "/v1/inventory/update":
                return 200, json.dumps({"status": "success"})
            return handle(method, path, query, body)
        e.bo.server.handler = acknowledge_without_update
        e.bl.lots[a]["quantity"] = 14
        check(e.cycle()["result"] == "ok", "the update cycle failed")
        with open(e.path("data", "stockade-sync-report.txt")) as report:
            text = report.read()
        check(e.bo.qty(a) == 10, "the fake store applied the injected update")
        check("Problem: BrickLink lot %d," % a in text, "the unapplied update was not reported")
        check("Confirmed: BrickLink lot %d," % a not in text, "the unapplied update was falsely confirmed")
        e.bo.server.handler = handle
        check(e.cycle()["result"] == "ok", "the retry failed")
        with open(e.path("data", "stockade-sync-report.txt")) as report:
            retried = report.read()
        check(e.bo.qty(a) == 14, "the retry did not update Brick Owl")
        check("Confirmed: BrickLink lot %d," % a in retried, "the successful retry was not confirmed")
        check(e.cycle()["result"] == "ok", "the cycle after the retry failed")
        with open(e.path("data", "stockade-sync-report.txt")) as report:
            check(report.read() == retried, "the resolved lot was reported again")
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
