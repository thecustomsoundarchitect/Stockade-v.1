"""Local fake BrickLink and fake Brick Owl for the BrickSync tests.

They hold stock and orders in memory, answer the API calls BrickSync makes, and record
every call. No test talks to a real store. Run by test_autosync.py.
"""
import json, re, socket, threading, time, urllib.parse

# BrickLink colour id -> Brick Owl colour id, for the colours the tests use
COLORS = {11: 38}
# BrickLink item number -> Brick Owl BOID
BOIDS = {"3001": 100001, "3002": 100002, "3003": 100003, "3004": 100004, "3005": 100005}


class Fail:
    """One injected failure: the next `count` calls matching method + path regex."""
    def __init__(self, method, pattern, mode, count):
        self.method, self.pattern, self.mode, self.count = method, re.compile(pattern), mode, count


class Server:
    def __init__(self, name, handler):
        self.name, self.handler = name, handler
        self.log, self.fails, self.lock = [], [], threading.RLock()
        self.down = False
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def fail(self, method, pattern, mode, count=1):
        with self.lock:
            self.fails.append(Fail(method, pattern, mode, count))

    def writes(self):
        with self.lock:
            return [e for e in self.log if e[0] != "GET"]

    def clear_log(self):
        with self.lock:
            self.log.clear()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        try:
            while True:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                head, buf = buf.split(b"\r\n\r\n", 1)
                lines = head.decode("latin1").split("\r\n")
                method, target, _ = lines[0].split(" ", 2)
                length = 0
                for line in lines[1:]:
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                while len(buf) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                body, buf = buf[:length].decode("utf8"), buf[length:]
                path, _, query = target.partition("?")
                with self.lock:
                    mode = None
                    if self.down:
                        mode = "500"
                    for f in self.fails:
                        if f.count > 0 and f.method == method and f.pattern.search(path):
                            f.count -= 1
                            mode = f.mode
                            break
                    self.log.append((method, path, query, body, mode or "ok"))
                    if mode == "drop_before":
                        return
                    if mode in ("500", "400"):
                        status, payload = int(mode), '{"error":"injected"}'
                    else:
                        status, payload = self.handler(method, path, urllib.parse.parse_qs(query), body)
                    if mode == "drop_after":
                        return
                data = payload.encode("utf8")
                reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
                conn.sendall(("HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: Keep-Alive\r\n\r\n" % (status, reason, len(data))).encode() + data)
        except OSError:
            pass
        finally:
            conn.close()


def bl_ok(data):
    return 200, json.dumps({"meta": {"description": "OK", "message": "OK", "code": 200}, "data": data})


def bl_err(code, message):
    return 200, json.dumps({"meta": {"description": message, "message": message, "code": code}})


def bl_date(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


class FakeBrickLink:
    def __init__(self):
        self.lots, self.orders = {}, {}
        self.nextlot, self.nextorder = 50000001, 7000001
        self.on_inventory_read = None
        self.server = Server("bricklink", self.handle)
        self.lock = self.server.lock

    def add_lot(self, no, qty, price=1.0, cond="N", color=11, description="", remarks="", itemtype="PART"):
        with self.lock:
            lotid = self.nextlot
            self.nextlot += 1
            self.lots[lotid] = dict(inventory_id=lotid, item=dict(no=no, name="Item " + no, type=itemtype, categoryID=5),
                                    color_id=color, color_name="Black", quantity=qty, new_or_used=cond, completeness="C",
                                    unit_price="%.4f" % price, description=description, remarks=remarks, bulk=1,
                                    is_retain=False, is_stock_room=False, my_cost="0.0000", sale_rate=0,
                                    tier_quantity1=0, tier_price1="0.0000", tier_quantity2=0, tier_price2="0.0000",
                                    tier_quantity3=0, tier_price3="0.0000")
            return lotid

    def qty(self, lotid):
        with self.lock:
            return self.lots[lotid]["quantity"] if lotid in self.lots else None

    def sale(self, items):
        """A buyer orders on BrickLink: items = {lotid: quantity}. BrickLink deducts its own stock."""
        with self.lock:
            orderid = self.nextorder
            self.nextorder += 1
            now = int(time.time())
            lines = []
            for lotid, q in items.items():
                lot = self.lots[lotid]
                line = dict(lot)
                line["quantity"] = q
                lines.append(line)
                lot["quantity"] -= q
                if lot["quantity"] <= 0:
                    del self.lots[lotid]
            self.orders[orderid] = dict(order_id=orderid, date_ordered=now, date_status_changed=now, status="PAID", lines=lines)
            return orderid

    def touch_order(self, orderid, status="PACKED"):
        with self.lock:
            self.orders[orderid]["status"] = status
            self.orders[orderid]["date_status_changed"] = int(time.time())

    def handle(self, method, path, query, body):
        m = re.match(r"^/api/store/v1/orders/(\d+)/items$", path)
        if method == "GET" and m:
            order = self.orders.get(int(m.group(1)))
            return bl_ok([order["lines"]]) if order else bl_err(404, "RESOURCE_NOT_FOUND")
        if method == "GET" and path == "/api/store/v1/orders":
            out = []
            for o in self.orders.values():
                total = sum(l["quantity"] for l in o["lines"])
                out.append(dict(order_id=o["order_id"], date_ordered=bl_date(o["date_ordered"]),
                                date_status_changed=bl_date(o["date_status_changed"]), buyer_name="buyer",
                                total_count=total, unique_count=len(o["lines"]), status=o["status"],
                                cost=dict(currency_code="CAD", subtotal="1.0000", grand_total="1.0000", shipping="0.0000", vat_amount="0.0000"),
                                disp_cost=dict(currency_code="CAD", subtotal="1.0000", grand_total="1.0000")))
            return bl_ok(out)
        if method == "GET" and path == "/api/store/v1/inventories":
            data = [dict(l) for l in self.lots.values() if not l["is_stock_room"]]
            hook, self.on_inventory_read = self.on_inventory_read, None
            if hook:
                hook()
            return bl_ok(data)
        m = re.match(r"^/api/store/v1/inventories/(\d+)$", path)
        if m:
            lotid = int(m.group(1))
            if lotid not in self.lots:
                return bl_err(404, "RESOURCE_NOT_FOUND")
            if method == "DELETE":
                del self.lots[lotid]
                return bl_ok({})
            if method == "PUT":
                lot, req = self.lots[lotid], json.loads(body)
                if "quantity" in req:
                    lot["quantity"] += int(req["quantity"])
                for key in ("description", "remarks", "bulk", "is_retain", "is_stock_room", "my_cost"):
                    if key in req:
                        lot[key] = req[key]
                if "unit_price" in req:
                    lot["unit_price"] = "%.4f" % float(req["unit_price"])
                return bl_ok(dict(lot))
        return bl_err(404, "unhandled " + method + " " + path)


class FakeBrickOwl:
    def __init__(self):
        self.lots, self.orders = {}, {}
        self.nextlot, self.nextorder = 9000001, 8000001
        self.on_inventory_read = None
        # Brick Owl catalogue: BrickLink item number -> (Brick Owl type, BOID)
        # True: accept sale_percent but do not apply it (to test the read-back)
        self.ignore_sale = False
        self.catalog = {k: ("Part", v) for k, v in BOIDS.items()}
        self.server = Server("brickowl", self.handle)
        self.lock = self.server.lock

    def find(self, bl_lotid):
        """The Brick Owl lot that carries this BrickLink lot id, or None."""
        with self.lock:
            for lot in self.lots.values():
                if lot["external_id"] == str(bl_lotid) and lot["qty"] > 0:
                    return lot
            return None

    def qty(self, bl_lotid):
        lot = self.find(bl_lotid)
        return lot["qty"] if lot else None

    def active_count(self):
        with self.lock:
            return len([l for l in self.lots.values() if l["qty"] > 0])

    def sale(self, items):
        """A buyer orders on Brick Owl: items = {bl_lotid: quantity}. Brick Owl deducts its own stock."""
        with self.lock:
            orderid = self.nextorder
            self.nextorder += 1
            lines = []
            for bl_lotid, q in items.items():
                lot = self.find(bl_lotid)
                lot["qty"] -= q
                lines.append(dict(condition=("New" if lot["full_con"].startswith("new") else "Used"), name="Item", boid=lot["boid"],
                                  personal_note=lot["personal_note"], bl_lot_id=lot["external_id"],
                                  external_lot_ids={"other": lot["external_id"]}, remaining_quantity=str(lot["qty"]),
                                  public_note=lot["public_note"], ordered_quantity=str(q), base_price=lot["price"],
                                  color_id=lot["boid"].split("-")[1], lot_id=str(lot["lot_id"])))
            self.orders[orderid] = dict(order_id=orderid, time=int(time.time()), lines=lines)
            return orderid

    def view(self, o):
        total = sum(int(l["ordered_quantity"]) for l in o["lines"])
        return dict(order_id=str(o["order_id"]), order_date=str(o["time"]), order_time=str(o["time"]),
                    total_quantity=str(total), total_lots=str(len(o["lines"])), base_order_total="1.00",
                    status="Payment Received", status_id="2", sub_total="1.00", payment_currency="CAD",
                    payment_total="1.00", buyer_name="buyer", ship_total="0.00", tax_amount="0.00")

    def handle(self, method, path, query, body):
        if path == "/v1/token/details":
            return 200, json.dumps(dict(username="tester", store_details=dict(name="Test Store", base_currency="CAD")))
        if path == "/v1/order/list":
            since = int(query.get("order_time", ["0"])[0])
            return 200, json.dumps([self.view(o) for o in self.orders.values() if o["time"] >= since])
        if path == "/v1/order/view":
            return 200, json.dumps(self.view(self.orders[int(query["order_id"][0])]))
        if path == "/v1/order/items":
            return 200, json.dumps(self.orders[int(query["order_id"][0])]["lines"])
        if path == "/v1/catalog/id_lookup":
            kind, boid = self.catalog.get(query["id"][0], (None, None))
            if kind != query.get("type", [kind])[0]:
                boid = None
            return 200, json.dumps({"boids": [str(boid)] if boid else []})
        if path == "/v1/inventory/list":
            out = []
            for l in self.lots.values():
                if l["qty"] <= 0:
                    continue
                out.append(dict(con=("new" if l["full_con"].startswith("new") else "used"), full_con=l["full_con"], qty=str(l["qty"]),
                                lot_id=str(l["lot_id"]), price=l["price"], base_price=l["price"], url="http://x/store/item", public_note=l["public_note"],
                                personal_note=l["personal_note"], sale_percent=str(l["sale_percent"]), bulk_qty="1", for_sale="1",
                                boid=l["boid"], external_lot_ids=({"other": l["external_id"]} if l["external_id"] else {}), type="Part"))
            hook, self.on_inventory_read = self.on_inventory_read, None
            if hook:
                hook()
            return 200, json.dumps(out)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}
        if path == "/v1/inventory/create":
            lotid = self.nextlot
            self.nextlot += 1
            boid = form["boid"] + ("-" + form["color_id"] if "color_id" in form else "")
            self.lots[lotid] = dict(lot_id=lotid, boid=boid, qty=int(form["quantity"]), price="%.3f" % float(form["price"]),
                                    full_con=form.get("condition", "new"), external_id=form.get("external_id", ""),
                                    public_note="", personal_note="", sale_percent=0)
            return 200, json.dumps(dict(status="success", lot_id=str(lotid)))
        if path == "/v1/inventory/update":
            lot = None
            if "lot_id" in form:
                lot = self.lots.get(int(form["lot_id"]))
            elif "external_id" in form:
                lot = next((l for l in self.lots.values() if l["external_id"] == form["external_id"]), None)
            if lot is None:
                return 400, json.dumps(dict(error=dict(status="Invalid lot")))
            if "relative_quantity" in form:
                lot["qty"] += int(form["relative_quantity"])
            if "absolute_quantity" in form:
                lot["qty"] = int(form["absolute_quantity"])
            if "price" in form:
                lot["price"] = "%.3f" % float(form["price"])
            for key in ("public_note", "personal_note"):
                if key in form:
                    lot[key] = form[key]
            if "condition" in form:
                lot["full_con"] = form["condition"]
            if "sale_percent" in form and not (-95 <= int(form["sale_percent"]) <= 95):
                return 400, json.dumps(dict(error=dict(status="Invalid sale percent")))
            if "sale_percent" in form and not self.ignore_sale:
                lot["sale_percent"] = int(form["sale_percent"])
            return 200, json.dumps(dict(status="success"))
        return 404, json.dumps(dict(error="unhandled " + path))
