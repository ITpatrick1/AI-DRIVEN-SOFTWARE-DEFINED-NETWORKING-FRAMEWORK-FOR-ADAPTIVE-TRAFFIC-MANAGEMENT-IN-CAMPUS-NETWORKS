#!/usr/bin/env python3
"""
Timetable Synchronization Engine — Tumba College of Technology
Rwanda Polytechnic Capstone Project

Manages a SQLite timetable database and exports the current activity slot
to /tmp/campus_timetable_state.json every 30 seconds. The controller and
dashboard read that file; the dashboard can also write a simulation override.

Usage:
  python3 examples/timetable_engine.py [--db /path/to/timetable.db]
  python3 examples/timetable_engine.py --init-only   # create DB and exit
"""

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] timetable: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("timetable")

# ── Paths (overridable via env) ────────────────────────────────────────────────
DB_PATH       = os.environ.get("CAMPUS_TIMETABLE_DB",       "/tmp/campus_timetable.db")
STATE_FILE    = os.environ.get("CAMPUS_TIMETABLE_STATE",    "/tmp/campus_timetable_state.json")
OVERRIDE_FILE = os.environ.get("CAMPUS_TIMETABLE_OVERRIDE", "/tmp/campus_timetable_override.json")
API_PORT      = int(os.environ.get("CAMPUS_TIMETABLE_PORT", "9092"))

# ── Full weekly schedule — Tumba College of Technology ────────────────────────
SAMPLE_TIMETABLE = [
    # (day, start_time, end_time, activity, zone, description, priority)
    # Monday
    ("Monday","07:30","08:00","admin","staff","Pre-class system check — IT infrastructure",1),
    ("Monday","08:00","10:00","admin","staff","Morning admin window — MIS/Finance operations",1),
    ("Monday","08:30","10:30","lecture","lab","CS101 Networking Lab practical session",2),
    ("Monday","10:00","12:00","exam","wifi","Online exam — MIS portal (TCP 8443)",1),
    ("Monday","12:00","13:00","break","all","Lunch break — equal bandwidth distribution",3),
    ("Monday","13:00","15:00","lecture","lab","CS201 Moodle lecture — Afternoon session",2),
    ("Monday","15:00","17:00","admin","staff","Admin peak — Finance/HR/Payroll systems",1),
    ("Monday","17:00","22:00","after_hours","all","After-hours reduced operations",3),
    # Tuesday
    ("Tuesday","08:00","10:00","lecture","lab","CS301 Advanced Networking Lab",2),
    ("Tuesday","10:00","12:00","lecture","wifi","E-learning session — Moodle access",2),
    ("Tuesday","12:00","13:00","break","all","Lunch break",3),
    ("Tuesday","13:00","15:00","exam","wifi","Online CAT — MIS portal assessment",1),
    ("Tuesday","15:00","17:00","admin","staff","Staff admin peak — reporting cycle",1),
    ("Tuesday","17:00","22:00","after_hours","all","After-hours operations",3),
    # Wednesday
    ("Wednesday","07:30","08:00","admin","staff","Morning system health check",1),
    ("Wednesday","08:00","10:00","admin","staff","Mid-week admin — inventory management",1),
    ("Wednesday","09:00","11:00","lecture","lab","CS401 Security Lab practical",2),
    ("Wednesday","10:00","12:00","exam","wifi","Mid-semester examination",1),
    ("Wednesday","12:00","13:00","break","all","Lunch break",3),
    ("Wednesday","13:00","15:00","lecture","lab","Afternoon lab session — project work",2),
    ("Wednesday","15:00","17:00","peak_hour","all","Peak usage — research + admin overlap",1),
    ("Wednesday","17:00","22:00","after_hours","all","After-hours",3),
    # Thursday
    ("Thursday","08:00","10:00","lecture","lab","CS201 Morning lab session",2),
    ("Thursday","10:00","12:00","admin","staff","Thursday admin operations — procurement",1),
    ("Thursday","12:00","13:00","break","all","Lunch break",3),
    ("Thursday","13:00","15:00","exam","wifi","Thursday online assessment",1),
    ("Thursday","15:00","17:00","peak_hour","all","Peak hour — all zones maximum load",1),
    ("Thursday","17:00","22:00","after_hours","all","After-hours operations",3),
    # Friday
    ("Friday","08:00","10:00","admin","staff","End-of-week admin — MIS reporting",1),
    ("Friday","09:00","11:00","lecture","lab","Friday morning lab session",2),
    ("Friday","11:00","13:00","exam","wifi","End-of-week online assessment",1),
    ("Friday","12:00","13:00","break","all","Lunch break",3),
    ("Friday","13:00","15:00","lecture","all","Afternoon combined lecture — Moodle",2),
    ("Friday","15:00","17:00","peak_hour","all","TGIF peak — high social + academic mix",1),
    ("Friday","17:00","22:00","after_hours","all","Weekend begins — reduced operations",3),
    # Weekend
    ("Saturday","00:00","23:59","after_hours","all","Weekend — minimal operations",3),
    ("Sunday","00:00","23:59","after_hours","all","Weekend — minimal operations",3),
]

# ── Mode → controller effects table ──────────────────────────────────────────
TIMETABLE_EFFECTS = {
    "exam": {
        "label": "Exam Period",
        "icon": "📝",
        "congest_high_mbps": 20.0,
        "congest_low_mbps": 10.0,
        "wifi_qos": "high",
        "exam_flag": 1.0,
        "color": "danger",
        "description": "Student Wi-Fi elevated to Priority 1 — MIS exam portal protected on TCP 8443.",
        "dqn_hint": "exam_mode",
    },
    "lecture": {
        "label": "Lecture Session",
        "icon": "📚",
        "congest_high_mbps": 35.0,
        "congest_low_mbps": 18.0,
        "wifi_qos": "medium",
        "exam_flag": 0.0,
        "color": "info",
        "description": "Moodle and Lab traffic prioritized. Bulk downloads throttled.",
        "dqn_hint": "boost_lab_zone",
    },
    "lab": {
        "label": "Lab Session",
        "icon": "🔬",
        "congest_high_mbps": 50.0,
        "congest_low_mbps": 25.0,
        "wifi_qos": "medium",
        "exam_flag": 0.0,
        "color": "info",
        "description": "Lab zone bandwidth expanded. Practical session support active.",
        "dqn_hint": "boost_lab_zone",
    },
    "admin": {
        "label": "Admin Operations",
        "icon": "🏛️",
        "congest_high_mbps": 30.0,
        "congest_low_mbps": 15.0,
        "wifi_qos": "low",
        "exam_flag": 0.0,
        "color": "warning",
        "description": "Staff LAN prioritized for MIS/Finance. Student Wi-Fi deprioritized.",
        "dqn_hint": "boost_staff_lan",
    },
    "break": {
        "label": "Break Time",
        "icon": "☕",
        "congest_high_mbps": 40.0,
        "congest_low_mbps": 20.0,
        "wifi_qos": "medium",
        "exam_flag": 0.0,
        "color": "success",
        "description": "Normal traffic distribution. Equal bandwidth allocation across all zones.",
        "dqn_hint": "normal_mode",
    },
    "after_hours": {
        "label": "After Hours",
        "icon": "🌙",
        "congest_high_mbps": 60.0,
        "congest_low_mbps": 30.0,
        "wifi_qos": "low",
        "exam_flag": 0.0,
        "color": "secondary",
        "description": "Relaxed thresholds — minimal expected traffic. Energy-efficient mode.",
        "dqn_hint": "normal_mode",
    },
    "peak_hour": {
        "label": "Peak Hour",
        "icon": "⚡",
        "congest_high_mbps": 25.0,
        "congest_low_mbps": 12.0,
        "wifi_qos": "throttled",
        "exam_flag": 0.0,
        "color": "danger",
        "description": "All zones under maximum load. Strict SLO enforcement. Wi-Fi throttled.",
        "dqn_hint": "peak_hour_mode",
    },
    "normal": {
        "label": "Normal Operations",
        "icon": "✅",
        "congest_high_mbps": 40.0,
        "congest_low_mbps": 20.0,
        "wifi_qos": "medium",
        "exam_flag": 0.0,
        "color": "success",
        "description": "Default policies. Standard traffic management.",
        "dqn_hint": "normal_mode",
    },
    "congestion": {
        "label": "Congestion Simulation",
        "icon": "🌊",
        "congest_high_mbps": 15.0,
        "congest_low_mbps": 8.0,
        "wifi_qos": "throttled",
        "exam_flag": 0.0,
        "color": "danger",
        "description": "Congestion flood simulation active — DQN throttle response triggered.",
        "dqn_hint": "throttle_wifi_70pct",
    },
    "ddos": {
        "label": "DDoS Attack",
        "icon": "🚨",
        "congest_high_mbps": 20.0,
        "congest_low_mbps": 10.0,
        "wifi_qos": "throttled",
        "exam_flag": 0.0,
        "color": "danger",
        "description": "DDoS simulation active — security module drop rules in effect.",
        "dqn_hint": "security_isolation_wifi",
    },
    "combined": {
        "label": "All Scenarios",
        "icon": "🎯",
        "congest_high_mbps": 18.0,
        "congest_low_mbps": 9.0,
        "wifi_qos": "throttled",
        "exam_flag": 1.0,
        "color": "danger",
        "description": "All scenarios running simultaneously — full DQN stress test.",
        "dqn_hint": "emergency_staff_protection",
    },
}

DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]


# ── SQLite helpers ─────────────────────────────────────────────────────────────
def init_db(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS timetable (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            day         TEXT    NOT NULL,
            start_time  TEXT    NOT NULL,
            end_time    TEXT    NOT NULL,
            activity    TEXT    NOT NULL,
            zone        TEXT    NOT NULL DEFAULT 'all',
            description TEXT,
            priority    INTEGER NOT NULL DEFAULT 3
        );
        CREATE TABLE IF NOT EXISTS security_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL    NOT NULL,
            event_type   TEXT    NOT NULL,
            zone         TEXT,
            src_ip       TEXT,
            src_mac      TEXT,
            dst_ip       TEXT,
            attack_type  TEXT,
            action_taken TEXT,
            response_ms  REAL,
            details      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tt_day ON timetable(day);
        CREATE INDEX IF NOT EXISTS idx_se_ts  ON security_events(ts);
    """)

    # Only seed if table is empty
    cur.execute("SELECT COUNT(*) FROM timetable")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO timetable (day,start_time,end_time,activity,zone,description,priority) "
            "VALUES (?,?,?,?,?,?,?)",
            SAMPLE_TIMETABLE,
        )
        log.info("Seeded %d timetable entries", len(SAMPLE_TIMETABLE))

    con.commit()
    con.close()


def get_current_slot(db_path: str) -> dict | None:
    """Return the highest-priority active slot for right now, or None."""
    now = datetime.now()
    day = DAY_NAMES[now.weekday()]
    hhmm = now.strftime("%H:%M")
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            """
            SELECT * FROM timetable
            WHERE day = ? AND start_time <= ? AND end_time > ?
            ORDER BY priority ASC, start_time ASC
            LIMIT 1
            """,
            (day, hhmm, hhmm),
        )
        row = cur.fetchone()
        con.close()
        return dict(row) if row else None
    except Exception as exc:
        log.warning("get_current_slot error: %s", exc)
        return None


def get_all_slots(db_path: str) -> list:
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT * FROM timetable ORDER BY day, start_time")
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return rows
    except Exception as exc:
        log.warning("get_all_slots error: %s", exc)
        return []


def add_slot(db_path: str, day, start, end, activity, zone, description, priority) -> int:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO timetable (day,start_time,end_time,activity,zone,description,priority) "
        "VALUES (?,?,?,?,?,?,?)",
        (day, start, end, activity, zone, description, priority),
    )
    new_id = cur.lastrowid
    con.commit()
    con.close()
    return new_id


def delete_slot(db_path: str, slot_id: int) -> bool:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("DELETE FROM timetable WHERE id=?", (slot_id,))
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed


def log_security_event(db_path: str, event_type, zone="", src_ip="", src_mac="",
                       dst_ip="", attack_type="", action_taken="",
                       response_ms=0.0, details="") -> None:
    try:
        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO security_events "
            "(ts,event_type,zone,src_ip,src_mac,dst_ip,attack_type,action_taken,response_ms,details) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), event_type, zone, src_ip, src_mac,
             dst_ip, attack_type, action_taken, response_ms, details),
        )
        con.commit()
        con.close()
    except Exception as exc:
        log.warning("log_security_event error: %s", exc)


def get_security_events(db_path: str, limit=50) -> list:
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            "SELECT * FROM security_events ORDER BY ts DESC LIMIT ?", (limit,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return rows
    except Exception as exc:
        log.warning("get_security_events error: %s", exc)
        return []


# ── Override file helpers ──────────────────────────────────────────────────────
def read_override(override_path: str) -> dict | None:
    """Return active override dict or None if file missing/expired."""
    if not os.path.exists(override_path):
        return None
    try:
        with open(override_path) as f:
            data = json.load(f)
        expires = float(data.get("expires_at", 0))
        if expires > 0 and time.time() > expires:
            os.remove(override_path)
            return None
        return data
    except Exception:
        return None


def write_override(override_path: str, mode: str, duration_s: int = 300,
                   label: str = "", reason: str = "dashboard_simulation") -> None:
    effects = TIMETABLE_EFFECTS.get(mode, TIMETABLE_EFFECTS["normal"])
    data = {
        "mode": mode,
        "label": label or effects["label"],
        "icon":  effects["icon"],
        "reason": reason,
        "expires_at": time.time() + duration_s,
        "duration_s": duration_s,
        "started_at": time.time(),
    }
    tmp = override_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, override_path)


def clear_override(override_path: str) -> None:
    try:
        os.remove(override_path)
    except FileNotFoundError:
        pass


# ── State export ───────────────────────────────────────────────────────────────
def build_state(db_path: str, override_path: str) -> dict:
    """Compute the current timetable state and return it as a dict."""
    override = read_override(override_path)
    now = datetime.now()
    slot = get_current_slot(db_path)

    if override:
        mode = override.get("mode", "normal")
        active_label = override.get("label", mode)
        active_icon  = override.get("icon", "")
        is_override  = True
        time_remaining = max(0, override.get("expires_at", 0) - time.time())
    elif slot:
        mode = slot.get("activity", "normal")
        active_label = slot.get("description", mode)
        active_icon  = TIMETABLE_EFFECTS.get(mode, {}).get("icon", "")
        is_override  = False
        time_remaining = 0
        # compute minutes remaining in slot
        end_hhmm = slot.get("end_time", "")
        if end_hhmm:
            try:
                eh, em = map(int, end_hhmm.split(":"))
                end_dt = now.replace(hour=eh, minute=em, second=0, microsecond=0)
                time_remaining = max(0, (end_dt - now).total_seconds())
            except Exception:
                pass
    else:
        mode = "normal"
        active_label = "Normal Operations"
        active_icon  = "✅"
        is_override  = False
        time_remaining = 0

    effects = TIMETABLE_EFFECTS.get(mode, TIMETABLE_EFFECTS["normal"])
    return {
        "ts": time.time(),
        "current_time": now.strftime("%H:%M:%S"),
        "current_day":  DAY_NAMES[now.weekday()],
        "mode": mode,
        "label": active_label,
        "icon":  active_icon,
        "effects": effects,
        "is_override": is_override,
        "time_remaining_s": round(time_remaining),
        "slot": slot,
        "exam_flag": effects.get("exam_flag", 0.0),
        "congest_high_mbps": effects["congest_high_mbps"],
        "congest_low_mbps":  effects["congest_low_mbps"],
        "wifi_qos": effects.get("wifi_qos", "medium"),
        "dqn_hint": effects.get("dqn_hint", "normal_mode"),
    }


def write_state(state: dict, state_path: str) -> None:
    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path)


# ── HTTP API ──────────────────────────────────────────────────────────────────
class TimetableHandler(BaseHTTPRequestHandler):
    db_path       = DB_PATH
    override_path = OVERRIDE_FILE
    state_path    = STATE_FILE

    def log_message(self, *_):
        pass  # silence default access log

    def _send(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send({"ok": True, "service": "timetable_engine"})
        elif path == "/state":
            self._send(build_state(self.db_path, self.override_path))
        elif path == "/slots":
            self._send({"ok": True, "slots": get_all_slots(self.db_path)})
        elif path == "/effects":
            self._send({"ok": True, "effects": TIMETABLE_EFFECTS})
        elif path == "/security_events":
            self._send({"ok": True, "events": get_security_events(self.db_path)})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path == "/override":
            mode     = body.get("mode", "normal")
            duration = int(body.get("duration_s", 300))
            label    = body.get("label", "")
            if mode not in TIMETABLE_EFFECTS and mode not in ("normal",):
                self._send({"ok": False, "error": f"unknown mode: {mode}"}, 400)
                return
            write_override(self.override_path, mode, duration, label)
            state = build_state(self.db_path, self.override_path)
            write_state(state, self.state_path)
            log.info("Override set: mode=%s duration=%ds", mode, duration)
            self._send({"ok": True, "state": state})
        elif path == "/override/clear":
            clear_override(self.override_path)
            state = build_state(self.db_path, self.override_path)
            write_state(state, self.state_path)
            log.info("Override cleared")
            self._send({"ok": True, "state": state})
        elif path == "/slots":
            try:
                new_id = add_slot(
                    self.db_path,
                    body["day"], body["start_time"], body["end_time"],
                    body["activity"], body.get("zone", "all"),
                    body.get("description", ""), int(body.get("priority", 3)),
                )
                self._send({"ok": True, "id": new_id})
            except (KeyError, Exception) as e:
                self._send({"ok": False, "error": str(e)}, 400)
        elif path == "/log_security":
            log_security_event(
                self.db_path,
                event_type  = body.get("event_type", "unknown"),
                zone        = body.get("zone", ""),
                src_ip      = body.get("src_ip", ""),
                src_mac     = body.get("src_mac", ""),
                dst_ip      = body.get("dst_ip", ""),
                attack_type = body.get("attack_type", ""),
                action_taken= body.get("action_taken", ""),
                response_ms = float(body.get("response_ms", 0)),
                details     = body.get("details", ""),
            )
            self._send({"ok": True})
        else:
            self._send({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        if parts[0] == "slots" and len(parts) == 2:
            try:
                slot_id = int(parts[1])
                ok = delete_slot(self.db_path, slot_id)
                self._send({"ok": ok})
            except ValueError:
                self._send({"ok": False, "error": "invalid id"}, 400)
        else:
            self._send({"error": "not found"}, 404)


# ── Background refresh loop ────────────────────────────────────────────────────
_stop_event = threading.Event()

def _refresh_loop(db_path: str, override_path: str, state_path: str,
                  interval: float = 30.0) -> None:
    log.info("Timetable refresh loop started (interval=%.0fs)", interval)
    while not _stop_event.wait(interval):
        try:
            state = build_state(db_path, override_path)
            write_state(state, state_path)
            log.debug("State refreshed: mode=%s", state["mode"])
        except Exception as exc:
            log.warning("Refresh loop error: %s", exc)
    log.info("Refresh loop stopped")


# ── Entrypoint ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Timetable Engine — Tumba College SDN")
    parser.add_argument("--db",        default=DB_PATH,       help="SQLite DB path")
    parser.add_argument("--state",     default=STATE_FILE,    help="State JSON output path")
    parser.add_argument("--override",  default=OVERRIDE_FILE, help="Override file path")
    parser.add_argument("--port",      type=int, default=API_PORT, help="HTTP API port")
    parser.add_argument("--interval",  type=float, default=30.0,   help="Refresh interval (s)")
    parser.add_argument("--init-only", action="store_true", help="Init DB and exit")
    args = parser.parse_args()

    log.info("Initialising database: %s", args.db)
    init_db(args.db)

    if args.init_only:
        log.info("Init-only mode — exiting")
        return

    # First state write
    state = build_state(args.db, args.override)
    write_state(state, args.state)
    log.info("Initial state: mode=%s label=%r", state["mode"], state["label"])

    # Set handler paths
    TimetableHandler.db_path       = args.db
    TimetableHandler.override_path = args.override
    TimetableHandler.state_path    = args.state

    # Background refresh thread
    t = threading.Thread(
        target=_refresh_loop,
        args=(args.db, args.override, args.state, args.interval),
        daemon=True,
    )
    t.start()

    # HTTP server
    server = HTTPServer(("127.0.0.1", args.port), TimetableHandler)
    log.info("Timetable API listening on http://127.0.0.1:%d", args.port)
    log.info("State file: %s", args.state)
    log.info("Override file: %s", args.override)

    def _shutdown(sig, frame):
        log.info("Shutting down…")
        _stop_event.set()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
