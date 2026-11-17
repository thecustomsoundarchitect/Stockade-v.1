"""Daily local backup tests using the real test build and local fake stores."""
import datetime
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import time
import xml.etree.ElementTree as ET

import test_autosync
from harness import Env
from test_autosync import bl_quantity_writes, check, in_step


DAY = 86400
RETENTION = 10 * DAY
RECORDS = ("bricksync.inventory.bsx", "bricksync.state", "bricksync.conf.txt")
OPTIONAL = ("bricksync.trs", "bricksync.blpending.txt", "bricksync.owlignore.txt", "autosync.status.json")
ORDER_NAME = re.compile(r"(?:bricklink|brickowl)-[0-9]+\.bsx\Z")


def backup_time(days_ago=3):
    date = datetime.date.today() - datetime.timedelta(days=days_ago)
    return int(time.mktime((date.year, date.month, date.day, 12, 0, 0, 0, 0, -1)))


def set_backup_time(e, timestamp):
    if e.proc and e.proc.poll() is None:
        wait_backup_check(e)
    Path(e.path("data", "test.dailybackup-time")).write_text("%d\n" % timestamp)


def archive_path(e, timestamp):
    day = time.strftime("%Y-%m-%d", time.localtime(timestamp))
    return Path(e.path("data", "backups", "daily-%s.tar" % day))


def wait_for(e, condition, message, timeout=30):
    deadline = time.monotonic() + timeout
    while not condition():
        check(e.proc is None or e.proc.poll() is None, "BrickSync exited while waiting for backup: " + e.output()[-2000:])
        check(time.monotonic() < deadline, message + "\n" + e.output()[-2000:])
        time.sleep(0.05)


def archive_files(path):
    with tarfile.open(path, "r:") as archive:
        files = {}
        for member in archive.getmembers():
            name = member.name.removeprefix("./").rstrip("/")
            check(not member.issym() and not member.islnk(), "archive contains a link: " + member.name)
            if member.isdir():
                check(name in ("data", "data/orders"), "archive contains an unrelated directory: " + name)
                continue
            check(member.isfile(), "archive contains a special file: " + member.name)
            check(name not in files, "archive repeats a saved record: " + name)
            files[name] = archive.extractfile(member).read()
        return files


def wait_backup_check(e):
    expected = (e.proc.pid, e.status()["cycle"])

    def completed():
        try:
            return tuple(map(int, Path(e.path("data", "test.dailybackup-cycle")).read_text().split())) == expected
        except (OSError, ValueError):
            return False

    wait_for(e, completed, "daily backup check did not finish for the current cycle")


def wait_archive(e, timestamp):
    wait_backup_check(e)
    path = archive_path(e, timestamp)
    wait_for(e, path.is_file, "daily archive was not published: " + str(path))
    check(stat.S_IMODE(path.stat().st_mode) == 0o600, "daily archive permissions are not 0600")
    files = archive_files(path)
    check(not path.with_name("." + path.name + ".tmp").exists(), "published daily archive left its temporary file")
    return path, files


def inventory_quantities(data):
    root = ET.fromstring(data)
    check(root.tag == "BrickStoreXML", "backup inventory is not a BrickStore XML record")
    return {int(item.findtext("LotID")): int(item.findtext("Qty")) for item in root.findall("./Inventory/Item")}


def check_saved_records(e, files, quantities):
    expected = {"data/" + name for name in RECORDS}
    expected.update("data/" + name for name in OPTIONAL if Path(e.path("data", name)).is_file())
    orders = Path(e.path("data", "orders"))
    expected.update("data/orders/" + path.name for path in orders.iterdir()
                    if ORDER_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink())
    check(set(files) == expected, "archive record list differs: missing %r, extra %r" % (expected - set(files), set(files) - expected))
    for name in expected:
        check(files[name] == Path(e.path(*name.split("/"))).read_bytes(), "archive does not contain the settled saved record: " + name)
    check(inventory_quantities(files["data/bricksync.inventory.bsx"]) == quantities, "archived inventory has the wrong quantities")
    items = {int(item.findtext("LotID")): item for item in ET.fromstring(files["data/bricksync.inventory.bsx"]).findall("./Inventory/Item")}
    for lot_id, item in items.items():
        check(item.findtext("Comments", "") == e.bl.lots[lot_id]["description"], "archived public note differs from BrickLink")
        check(item.findtext("Remarks", "") == e.bl.lots[lot_id]["remarks"], "archived location differs from BrickLink")
        owl = e.bo.find(lot_id)
        check(owl is not None and int(item.findtext("OwlLotID", "-1")) == owl["lot_id"], "archive missed the freshly assigned Brick Owl lot ID")
    check(len(files["data/bricksync.state"]) > 0, "archived state is empty")
    check(b"autosync = 1;" in files["data/bricksync.conf.txt"], "archived configuration is incomplete")
    if "data/autosync.status.json" in files:
        check(json.loads(files["data/autosync.status.json"])["result"] == "ok", "archived status is not the completed successful cycle")


def make_env(timestamp):
    e = Env()
    set_backup_time(e, timestamp)
    a = e.bl.add_lot("3001", 10, 1.25, description="From a sealed set", remarks="A01")
    b = e.bl.add_lot("3002", 5, 0.50, cond="U", description="Good condition", remarks="B02")
    return e, (a, b)


def bsx_backup_paths(e):
    return set(Path(e.path("data", "backups")).rglob("*.bsx"))


def test_daily_archive_contains_records_orders_and_stays_once_per_day():
    first_time = backup_time()
    next_time = backup_time(2)
    e, (a, b) = make_env(first_time)
    try:
        Path(e.path("data", "bricksync.owlignore.txt")).write_text("99999999 25 %d\n" % first_time)
        e.start()
        first, files = wait_archive(e, first_time)
        check_saved_records(e, files, {a: 10, b: 5})
        first_bytes, first_stat = first.read_bytes(), first.stat()
        initial_snapshots = bsx_backup_paths(e)
        for path in initial_snapshots:
            check(path.exists(), "initial inventory snapshot disappeared")

        e.send("backup manual-backup.bsx")
        manual = Path(e.path("manual-backup.bsx"))
        wait_for(e, lambda: manual.is_file() and "We saved a backup of the tracked inventory" in e.output(), "explicit manual backup was not completed")
        check(inventory_quantities(manual.read_bytes()) == {a: 10, b: 5}, "manual backup did not save the current inventory")
        Path(e.path("data", "temp.unrelated.bsx")).write_text("temporary data")
        Path(e.path("data", "logs", "excluded.txt")).write_text("log data")
        Path(e.path("data", "orders", ".temp.bricklink-12345.bsx")).write_text("temporary order")
        Path(e.path("data", "orders", "notes.txt")).write_text("unrelated order note")
        Path(e.path("data", "backups", "unrelated.txt")).write_text("unrelated backup note")

        bl_order = e.bl.sale({a: 3})
        owl_order = e.bo.sale({b: 2})
        check(e.cycle()["result"] == "ok", "sales cycle failed")
        check(e.cycle()["result"] == "ok", "repeat cycle failed")
        check(bsx_backup_paths(e) == initial_snapshots, "automatic order processing created per-order BSX backups")
        check(first.read_bytes() == first_bytes and first.stat().st_mtime_ns == first_stat.st_mtime_ns,
              "another cycle rewrote the same day's archive")
        e.stop()
        e.start()
        check(e.status()["result"] == "ok", "restart cycle failed")
        check(e.cycle()["result"] == "ok", "cycle after restart failed")
        check(first.read_bytes() == first_bytes and first.stat().st_mtime_ns == first_stat.st_mtime_ns,
              "restart rewrote the same day's archive")
        check(list(first.parent.glob("daily-*.tar")) == [first], "same day produced another archive")

        e.bl.lots[a]["description"] = "Updated harmless note"
        e.bl.lots[a]["remarks"] = "A09"
        set_backup_time(e, next_time)
        check(e.cycle()["result"] == "ok", "next day cycle failed")
        second, files = wait_archive(e, next_time)
        check(second != first and first.read_bytes() == first_bytes, "new day replaced the earlier archive")
        check_saved_records(e, files, {a: 7, b: 3})
        updated = {int(item.findtext("LotID")): item for item in ET.fromstring(files["data/bricksync.inventory.bsx"]).findall("./Inventory/Item")}[a]
        check(updated.findtext("Comments") == "Updated harmless note" and updated.findtext("Remarks") == "A09", "next day archive missed the changed note or location")
        for service, order_id, lot_id, sold in (("bricklink", bl_order, a, 3), ("brickowl", owl_order, b, 2)):
            name = "data/orders/%s-%d.bsx" % (service, order_id)
            order = ET.fromstring(files[name])
            check(int(order.findtext("./Order/OrderID")) == order_id, "archived order has the wrong identity")
            check(inventory_quantities(files[name]) == {lot_id: sold}, "archived order lost its sold quantity")
        check(bl_quantity_writes(e) == [(b, -2, "ok")], "backup or restart repeated a sale deduction")
        check(bsx_backup_paths(e) == initial_snapshots, "later order processing created a BSX backup")
        check(manual.exists(), "daily backup removed the explicit manual backup")
        in_step(e)
    finally:
        e.cleanup()


def write_fixture(path, data, timestamp):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (timestamp, timestamp))
    return path


def test_retention_uses_exact_mtime_and_preserves_other_records_and_links():
    timestamp = backup_time()
    cutoff = timestamp - RETENTION
    e, ids = make_env(timestamp)
    try:
        backups = Path(e.path("data", "backups"))
        backup_storage = Path(e.path("daily-backup-storage"))
        backup_storage.mkdir()
        backups.symlink_to(backup_storage, target_is_directory=True)
        old_archive = write_fixture(backups / "daily-2001-01-01.tar", b"old archive", cutoff - 1)
        boundary_archive = write_fixture(backups / "daily-2001-01-02.tar", b"boundary archive", cutoff)
        newer_archive = write_fixture(backups / "daily-2001-01-03.tar", b"newer archive", cutoff + 1)
        xml = b'<?xml version="1.0"?><BrickStoreXML><Inventory/></BrickStoreXML>'
        old_legacy = write_fixture(backups / "2001-02-03" / "00001.bsx", xml, cutoff - 1)
        boundary_legacy = write_fixture(backups / "2001-02-03" / "00002.bsx", xml, cutoff)
        newer_legacy = write_fixture(backups / "2001-02-03" / "00003.bsx", xml, cutoff + 1)
        preserved = [boundary_archive, newer_archive, boundary_legacy, newer_legacy,
                     write_fixture(backups / "notes.txt", b"unrelated", cutoff - 1),
                     write_fixture(backups / "2001-02-03" / "notes.txt", b"unrelated", cutoff - 1),
                     write_fixture(backups / "my-notes" / "00001.bsx", xml, cutoff - 1),
                     write_fixture(Path(e.path("data", "orders", "bricklink-12345.bsx")), xml, cutoff - 1),
                     write_fixture(Path(e.path("data", "orders", "brickowl-12345.bsx")), xml, cutoff - 1)]
        link_target = write_fixture(Path(e.path("keep-linked-record.tar")), b"linked target", cutoff - 1)
        link = backups / "daily-2001-01-04.tar"
        link.symlink_to(link_target)
        os.utime(link, (cutoff - 1, cutoff - 1), follow_symlinks=False)
        legacy_link = backups / "2001-02-03" / "00004.bsx"
        legacy_link.symlink_to(link_target)
        os.utime(legacy_link, (cutoff - 1, cutoff - 1), follow_symlinks=False)
        directory_target = Path(e.path("keep-linked-directory"))
        target_snapshot = write_fixture(directory_target / "00001.bsx", xml, cutoff - 1)
        directory_link = backups / "2001-02-04"
        directory_link.symlink_to(directory_target, target_is_directory=True)
        before = {path: path.read_bytes() for path in preserved + [link_target, target_snapshot]}
        e.start()
        wait_archive(e, timestamp)
        wait_for(e, lambda: not old_archive.exists() and not old_legacy.exists(), "old daily or legacy backups were not removed")
        for path, data in before.items():
            check(path.is_file() and path.read_bytes() == data, "retention changed a retained or unrelated record: " + str(path))
        check(link.is_symlink() and legacy_link.is_symlink() and directory_link.is_symlink(), "retention removed a symlink")
        check(e.bl.server.writes() == [], "retention wrote to BrickLink")
        in_step(e)
    finally:
        e.cleanup()


def test_failed_daily_archive_keeps_sales_running_and_retries_next_cycle():
    timestamp = backup_time()
    e, (a, b) = make_env(timestamp)
    try:
        target = archive_path(e, timestamp)
        blocker = target.with_name("." + target.name + ".tmp")
        blocker.mkdir(parents=True)
        e.start()
        check(e.status()["result"] == "ok", "backup failure failed the initial sync")
        failure = 'Daily backup: could not replace temporary archive "data/backups/%s":' % blocker.name
        wait_for(e, lambda: failure in e.output(), "the blocked temporary archive did not report its actual backup error")
        bl_order = e.bl.sale({a: 3})
        owl_order = e.bo.sale({b: 2})
        status = e.cycle()
        check(status["result"] == "ok" and status["consecutive_failures"] == 0, "backup failure stopped a sales cycle: %r" % status)
        check(e.bl.qty(a) == 7 and e.bo.qty(a) == 7 and e.bl.qty(b) == 3 and e.bo.qty(b) == 3, "sales did not sync while backup creation failed")
        check(e.cycle()["result"] == "ok", "repeat cycle failed while the temporary path was blocked")
        wait_backup_check(e)
        check(blocker.is_dir() and not target.exists(), "blocked backup was incorrectly published or removed the blocker")
        blocker.rmdir()
        check(e.cycle()["result"] == "ok", "retry cycle failed")
        path, files = wait_archive(e, timestamp)
        check_saved_records(e, files, {a: 7, b: 3})
        check("data/orders/bricklink-%d.bsx" % bl_order in files and "data/orders/brickowl-%d.bsx" % owl_order in files,
              "retry archive lost the orders saved during backup failure")
        check(bl_quantity_writes(e) == [(b, -2, "ok")], "backup retries repeated a BrickLink sale deduction")
        check(e.cycle()["result"] == "ok", "cycle after successful retry failed")
        check(bl_quantity_writes(e) == [(b, -2, "ok")], "a later backup cycle repeated the sale deduction")
        in_step(e)
    finally:
        e.cleanup()


if __name__ == "__main__":
    test_autosync.main({k: v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)})
