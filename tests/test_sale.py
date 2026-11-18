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


def test_sale_not_shown_by_owl_is_ignored_forever():
    for new_lot, percent in ((False, 25), (True, 25), (False, 0)):
        e, (a, b) = start()
        try:
            if new_lot:
                a = e.bl.add_lot("3003", 8, 2.0)
            if percent == 0:
                e.bo.find(a)["sale_percent"] = 40
            e.bl.lots[a]["sale_rate"] = percent
            original = dict(e.bl.lots[a])
            e.bl.lots[b]["quantity"] = 9
            e.bo.ignore_sale = True
            status = e.cycle()
            check(status["result"] == "ok" and status["consecutive_failures"] == 0, "a sale Brick Owl does not show must not fail the sync: %r" % status)
            check(e.bo.find(a) is None, "the ignored lot is still on Brick Owl")
            check(e.bl.lots[a] == original, "the ignored BrickLink lot was changed")
            check(e.bo.qty(b) == 9, "the other lot did not sync")
            check(e.bo.active_count() == len(e.bl.lots) - 1, "the wrong lots were removed from Brick Owl")
            check(e.bl.server.writes() == [], "wrote to BrickLink")
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
            check(e.bl.lots[a] == original, "later cycles changed the ignored BrickLink lot")
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
            check(e.bl.server.writes() == [], "wrote to BrickLink")
            check(e.output().count(warning) == 1, "the ignored lot printed another warning")
            with open(e.path("data", "bricksync.owlignore.txt")) as f:
                check(f.read() == ignored, "later cycles rewrote or duplicated the ignored lot")
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
