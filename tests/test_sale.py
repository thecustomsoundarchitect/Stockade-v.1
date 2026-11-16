"""Sale percentage tests (Patch 4). Run:  python3 tests/test_sale.py"""
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
            check(owl_reads(e) == 2, "Brick Owl should be read back once after the sale was sent, reads: %d" % owl_reads(e))
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


def test_sale_not_shown_by_owl_is_not_a_failed_sync():
    e, (a, b) = start()
    try:
        e.bl.lots[a]["sale_rate"] = 25
        e.bl.lots[b]["quantity"] = 9
        e.bo.ignore_sale = True
        status = e.cycle()
        check(status["result"] == "ok" and status["consecutive_failures"] == 0, "a sale Brick Owl does not show must not fail the sync: %r" % status)
        check(e.bo.qty(b) == 9, "the other lot did not sync")
        e.bo.ignore_sale = False
        check(e.cycle()["result"] == "ok", "next cycle failed")
        check(e.bo.find(a)["sale_percent"] == 25 and e.bo.find(a)["price"] == "1.250", "sale not set on the next cycle")
        in_step(e)
    finally:
        e.cleanup()


if __name__ == "__main__":
    test_autosync.main({k: v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)})
