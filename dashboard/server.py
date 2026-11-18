#!/usr/bin/env python3
"""Local Stockade status dashboard. Reads the existing BrickSync records."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
LOT_REQUEST_FILE = "stockade-lot-request.json"
LOT_RESULT_FILE = "stockade-lot-result.json"


def running_state(data_dir):
    """Inspect the existing lock holder without taking or changing the lock."""
    if not data_dir.is_dir():
        return "unavailable"
    lock = data_dir / "bricksync.lock"
    if not lock.exists():
        return "stopped"
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-Fpc", "--", str(lock)],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return "stopped"
    if result.returncode != 0:
        return "unavailable"
    for line in result.stdout.splitlines():
        if line.startswith("c") and line[1:] in ("bricksync", "bricksync-test"):
            return "running"
    return "unavailable"


def read_status(data_dir):
    try:
        raw = json.loads((data_dir / "autosync.status.json").read_text(encoding="utf-8"))
        if raw.get("result") not in ("ok", "failed") or not isinstance(raw.get("time"), (int, float)):
            return None
        return {"result": raw["result"], "time": raw["time"],
                "consecutive_failures": raw.get("consecutive_failures"),
                "error": raw.get("error", "")}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def read_sale_skips(data_dir):
    path = data_dir / "bricksync.owlignore.txt"
    if not data_dir.is_dir():
        return None
    try:
        if not path.exists():
            return [] if read_status(data_dir) is not None else None
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) != 3:
                return None
            lot, percent, _timestamp = map(int, fields)
            entries.append({"lot": lot, "percent": percent})
        return entries
    except (OSError, ValueError, UnicodeError):
        return None


def read_quarantine(data_dir):
    try:
        data = json.loads((data_dir / "quarantine.json").read_text(encoding="utf-8"))
        if not isinstance(data.get("time"), (int, float)) or not isinstance(data.get("items"), list):
            return None
        if not all(isinstance(item.get("lot_id"), int) and isinstance(item.get("reasons"), list)
                   for item in data["items"]):
            return None
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def read_sync_reports(data_dir):
    try:
        data = (data_dir / "stockade-sync-report.txt").read_bytes()
        sections = data.split(b"\n\n")
        digest = hashlib.sha256()
        reports = []
        for section in sections[:-1]:
            digest.update(section + b"\n\n")
            text = section.decode("utf-8")
            if not text.startswith(("Observed BrickLink lots in this sync:\n", "Observed BrickLink changes in this sync:\n")):
                continue
            text = "\n".join(line for line in text.splitlines() if not line.startswith(("Observation: ", "Resolved: ")))
            reports.append({"id": digest.hexdigest(), "text": text})
        return reports
    except (OSError, UnicodeError):
        return []


def snapshot(data_dir):
    reports = read_sync_reports(data_dir)
    return {"service": running_state(data_dir), "last_cycle": read_status(data_dir),
            "sale_skips": read_sale_skips(data_dir), "quarantine": read_quarantine(data_dir),
            "sync_report": reports[-1] if reports else None, "sync_reports": reports,
            "quarantine_email": None, "sync_email": None, "failure_email": None, "downtime_email": None}


def lot_action(payload):
    if not isinstance(payload, dict):
        raise ValueError("Enter a BrickLink lot ID and choose an action.")
    lot_id = payload.get("lot_id")
    if (not isinstance(lot_id, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", lot_id)
            or int(lot_id) > 9223372036854775807):
        raise ValueError("Enter a valid BrickLink lot ID.")
    action = payload.get("action")
    if action not in ("lookup", "edit_note", "skip_owl"):
        raise ValueError("Choose a supported lot action.")
    request = {"request_id": uuid4().hex, "lot_id": lot_id, "action": action}
    if action == "edit_note":
        note = payload.get("note")
        if not isinstance(note, str) or "\0" in note:
            raise ValueError("Enter a valid public Note.")
        note.encode("utf-8")
        request["note"] = note
    return request


def queue_lot_request(data_dir, request):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=data_dir,
                                         prefix=".stockade-lot-request.", delete=False) as stream:
            temporary = stream.name
            json.dump(request, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish the private file atomically without overwriting a pending request.
        os.link(temporary, data_dir / LOT_REQUEST_FILE)
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def handler_for(data_dir):
    class Handler(BaseHTTPRequestHandler):
        def lot_controls_allowed(self, post=False):
            port = self.server.server_address[1]
            host = self.headers.get("Host")
            local_hosts = {"127.0.0.1", "localhost"}
            origins = {f"{name}:{port}" for name in local_hosts}
            if port == 80:
                origins.update(local_hosts)
            origin = self.headers.get("Origin")
            return (self.client_address[0] == "127.0.0.1"
                    and self.server.server_address[0] == "127.0.0.1"
                    and host in origins
                    and self.headers.get("Sec-Fetch-Site") in (None, "same-origin")
                    and (origin == f"http://{host}" or (not post and origin is None)))

        def lot_response(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/api/lot":
                self.send_error(404)
                return
            if not self.lot_controls_allowed(post=True):
                self.lot_response(403, {"message": "Open the local Stockade dashboard to use lot controls."})
                return
            try:
                length = self.headers.get("Content-Length", "")
                if (not re.fullmatch(r"[1-9][0-9]{0,3}", length) or int(length) > 8192
                        or self.headers.get_content_type() != "application/json"):
                    raise ValueError("Use a valid lot request.")
                request = lot_action(json.loads(self.rfile.read(int(length))))
            except (UnicodeError, json.JSONDecodeError):
                self.lot_response(400, {"message": "Use a valid lot request."})
                return
            except ValueError as error:
                self.lot_response(400, {"message": str(error)})
                return
            if running_state(data_dir) != "running":
                self.lot_response(503, {"message": "BrickSync is unavailable to process this request."})
                return
            try:
                queue_lot_request(data_dir, request)
            except FileExistsError:
                self.lot_response(409, {"message": "BrickSync still has a lot request to process."})
                return
            except OSError:
                self.lot_response(503, {"message": "Stockade could not queue the lot request."})
                return
            self.lot_response(202, {"request_id": request["request_id"]})

        def get_lot_result(self, query):
            if not self.lot_controls_allowed():
                self.lot_response(403, {"message": "Open the local Stockade dashboard to use lot controls."})
                return
            ids = parse_qs(query).get("request_id", [])
            if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{32}", ids[0]):
                self.lot_response(400, {"message": "Use a valid lot request ID."})
                return
            try:
                result = json.loads((data_dir / LOT_RESULT_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = None
            if isinstance(result, dict) and result.get("request_id") == ids[0]:
                self.lot_response(200, result)
                return
            if running_state(data_dir) != "running":
                self.lot_response(503, {"message": "BrickSync is unavailable to return the lot result."})
                return
            self.lot_response(202, {"status": "pending"})

        def do_GET(self):
            if self.path.partition("?")[0] == "/api/lot-result":
                self.get_lot_result(urlsplit(self.path).query)
                return
            if self.path == "/api/status":
                state = snapshot(data_dir)
                state.pop("sync_reports", None)
                body = json.dumps(state).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif self.path in ("/", "/style.css", "/app.js"):
                name = {"/": "index.html", "/style.css": "style.css", "/app.js": "app.js"}[self.path]
                body = (ROOT / "static" / name).read_bytes()
                content_type = {"index.html": "text/html", "style.css": "text/css", "app.js": "text/javascript"}[name] + "; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Show the local Stockade status")
    parser.add_argument("--data", type=Path, default=ROOT.parent / "data", help="BrickSync data folder")
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    args = parser.parse_args()
    data_dir = args.data.expanduser().resolve()
    server = ThreadingHTTPServer((args.host, args.port), handler_for(data_dir))
    print("Stockade dashboard listening on port %d, reading %s" % (args.port, data_dir), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
