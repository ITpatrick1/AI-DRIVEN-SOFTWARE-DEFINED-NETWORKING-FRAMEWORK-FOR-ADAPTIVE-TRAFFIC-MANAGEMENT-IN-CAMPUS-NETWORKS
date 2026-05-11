#!/usr/bin/env python3
"""
Timetable Engine — Tumba College SDN

Synchronizes with Tumba College's academic systems to determine:
  - Current academic period (lecture, lab, exam, admin, off)
  - Whether exam mode should be active (exam_flag)
  - Zone priority adjustments based on schedule

Uses SQLite database with HTTP sync endpoint.
Writes state to /tmp/campus_timetable_state.json for controller consumption.
"""

import argparse
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


DB_PATH = os.environ.get('CAMPUS_TIMETABLE_DB', '/tmp/campus_timetable.db')
STATE_FILE = os.environ.get('CAMPUS_TIMETABLE_STATE', '/tmp/campus_timetable_state.json')
API_PORT = int(os.environ.get('CAMPUS_TIMETABLE_API_PORT', '9093'))


def init_db(path):
    """Initialize the timetable SQLite database with required tables."""
    con = sqlite3.connect(path)

    con.executescript("""
    CREATE TABLE IF NOT EXISTS slots (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        day_of_week  TEXT NOT NULL,        -- MON/TUE/WED/THU/FRI/SAT/SUN
        start_time   TEXT NOT NULL,        -- HH:MM
        end_time     TEXT NOT NULL,        -- HH:MM
        slot_type    TEXT NOT NULL,        -- lecture/lab/exam/admin/off
        department   TEXT DEFAULT '',
        course_code  TEXT DEFAULT '',
        room         TEXT DEFAULT '',
        zone         TEXT DEFAULT '',      -- staff_lan/server_zone/it_lab/student_wifi
        priority_override INTEGER DEFAULT NULL,
        notes        TEXT DEFAULT '',
        active       INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS overrides (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           REAL NOT NULL,
        override_type TEXT NOT NULL,       -- exam_mode/priority_change/emergency
        zone         TEXT DEFAULT '',
        value        TEXT DEFAULT '',
        expires_at   REAL DEFAULT 0,
        active       INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS sync_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           REAL NOT NULL,
        source       TEXT NOT NULL,        -- manual/api/cron/external
        action       TEXT NOT NULL,
        details      TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS security_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           REAL NOT NULL,
        event_type   TEXT NOT NULL,
        zone         TEXT DEFAULT '',
        src_ip       TEXT DEFAULT '',
        src_mac      TEXT DEFAULT '',
        dst_ip       TEXT DEFAULT '',
        attack_type  TEXT DEFAULT '',
        action_taken TEXT DEFAULT '',
        response_ms  REAL DEFAULT 0,
        details      TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS performance_metrics (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ts               REAL NOT NULL,
        scenario_name    TEXT NOT NULL,
        zone             TEXT DEFAULT '',
        throughput_mbps  REAL DEFAULT 0,
        latency_ms       REAL DEFAULT 0,
        packet_loss_pct  REAL DEFAULT 0,
        jitter_ms        REAL DEFAULT 0,
        flow_count       INTEGER DEFAULT 0,
        ai_action        TEXT DEFAULT '',
        recovery_time_ms REAL DEFAULT 0,
        notes            TEXT DEFAULT ''
    );
    """)

    # Seed with Tumba College sample timetable if empty
    if con.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 0:
        sample_slots = [
            # Monday
            ('MON', '08:00', '10:00', 'lecture', 'IT', 'NET301', 'Lab A', 'it_lab'),
            ('MON', '10:00', '12:00', 'lab', 'IT', 'NET302', 'Lab B', 'it_lab'),
            ('MON', '13:00', '15:00', 'lecture', 'Business', 'BUS201', 'Room 101', 'student_wifi'),
            ('MON', '08:00', '17:00', 'admin', 'Admin', '', 'Office', 'staff_lan'),
            # Tuesday
            ('TUE', '08:00', '10:00', 'lecture', 'IT', 'SYS201', 'Lab A', 'it_lab'),
            ('TUE', '10:00', '12:00', 'exam', 'IT', 'NET301', 'Exam Hall', 'student_wifi'),
            ('TUE', '13:00', '16:00', 'lab', 'IT', 'NET303', 'Lab A', 'it_lab'),
            ('TUE', '08:00', '17:00', 'admin', 'Admin', '', 'Office', 'staff_lan'),
            # Wednesday
            ('WED', '08:00', '10:00', 'lecture', 'IT', 'DB301', 'Room 201', 'student_wifi'),
            ('WED', '10:00', '12:00', 'lab', 'IT', 'DB302', 'Lab B', 'it_lab'),
            ('WED', '14:00', '16:00', 'lecture', 'Business', 'ACC101', 'Room 102', 'student_wifi'),
            ('WED', '08:00', '17:00', 'admin', 'Admin', '', 'Office', 'staff_lan'),
            # Thursday
            ('THU', '08:00', '10:00', 'exam', 'IT', 'SYS201', 'Exam Hall', 'student_wifi'),
            ('THU', '10:00', '12:00', 'lecture', 'IT', 'SEC201', 'Lab A', 'it_lab'),
            ('THU', '13:00', '15:00', 'lab', 'IT', 'SEC202', 'Lab B', 'it_lab'),
            ('THU', '08:00', '17:00', 'admin', 'Admin', '', 'Office', 'staff_lan'),
            # Friday
            ('FRI', '08:00', '10:00', 'lecture', 'IT', 'PRJ401', 'Room 201', 'student_wifi'),
            ('FRI', '10:00', '16:00', 'lab', 'IT', 'PRJ401', 'Lab A', 'it_lab'),
            ('FRI', '08:00', '17:00', 'admin', 'Admin', '', 'Office', 'staff_lan'),
        ]
        for slot in sample_slots:
            con.execute(
                "INSERT INTO slots (day_of_week, start_time, end_time, slot_type, "
                "department, course_code, room, zone) VALUES (?,?,?,?,?,?,?,?)",
                slot,
            )
        con.commit()
        print(f"Initialized timetable DB with {len(sample_slots)} sample slots")

    con.close()
    return path


def get_active_slots(db_path):
    """Get currently active timetable slots."""
    now = datetime.now()
    day = now.strftime('%a').upper()[:3]
    current_time = now.strftime('%H:%M')

    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT id, day_of_week, start_time, end_time, slot_type, department, "
        "course_code, room, zone, priority_override, notes "
        "FROM slots WHERE day_of_week=? AND start_time<=? AND end_time>? AND active=1",
        (day, current_time, current_time),
    ).fetchall()
    con.close()

    slots = []
    for row in rows:
        slots.append({
            'id': row[0], 'day': row[1], 'start': row[2], 'end': row[3],
            'type': row[4], 'department': row[5], 'course': row[6],
            'room': row[7], 'zone': row[8], 'priority_override': row[9],
            'notes': row[10],
        })
    return slots


def get_active_overrides(db_path):
    """Get currently active overrides."""
    now = time.time()
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT id, override_type, zone, value, expires_at "
        "FROM overrides WHERE active=1 AND (expires_at=0 OR expires_at>?)",
        (now,),
    ).fetchall()
    con.close()

    overrides = []
    for row in rows:
        overrides.append({
            'id': row[0], 'type': row[1], 'zone': row[2],
            'value': row[3], 'expires': row[4],
        })
    return overrides


def compute_state(db_path):
    """Compute current timetable-aware state."""
    slots = get_active_slots(db_path)
    overrides = get_active_overrides(db_path)

    # Determine if exam mode is active
    exam_active = any(s['type'] == 'exam' for s in slots)
    exam_active = exam_active or any(o['type'] == 'exam_mode' for o in overrides)

    # Determine current period
    now = datetime.now()
    hour = now.hour
    if 8 <= hour < 12:
        period = 'morning_classes'
    elif 12 <= hour < 13:
        period = 'lunch_break'
    elif 13 <= hour < 17:
        period = 'afternoon_classes'
    elif 17 <= hour < 20:
        period = 'evening_study'
    else:
        period = 'off_hours'

    # Determine zone priorities
    zone_priorities = {
        'staff_lan': 1,
        'server_zone': 1,
        'it_lab': 2,
        'student_wifi': 3,
    }
    if exam_active:
        zone_priorities['student_wifi'] = 1  # Elevate during exams

    for slot in slots:
        if slot.get('priority_override') is not None:
            zone = slot.get('zone')
            if zone in zone_priorities:
                zone_priorities[zone] = slot['priority_override']

    state = {
        'ts': time.time(),
        'exam_flag': 1 if exam_active else 0,
        'period': period,
        'day': now.strftime('%a').upper()[:3],
        'time': now.strftime('%H:%M'),
        'active_slots': slots,
        'active_overrides': overrides,
        'zone_priorities': zone_priorities,
        'slot_count': len(slots),
    }
    return state


def write_state(state, path):
    """Write timetable state to JSON file."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


class TimetableHandler(BaseHTTPRequestHandler):
    """HTTP API handler for timetable engine."""
    _db_path = DB_PATH

    def log_message(self, *args): pass

    def _send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get('Content-Length', 0))
        if n > 0: return json.loads(self.rfile.read(n))
        return {}

    def do_GET(self):
        if self.path == '/health':
            self._send_json({'ok': True, 'service': 'timetable_engine'})
        elif self.path == '/state':
            self._send_json(compute_state(self._db_path))
        elif self.path == '/slots':
            self._send_json({'slots': get_active_slots(self._db_path)})
        else:
            self._send_json({'error': 'not found'}, 404)

    def do_POST(self):
        body = self._read_body()
        if self.path == '/override':
            override_type = body.get('type', 'exam_mode')
            zone = body.get('zone', '')
            value = body.get('value', '')
            duration_s = int(body.get('duration_s', 3600))
            expires_at = time.time() + duration_s

            con = sqlite3.connect(self._db_path)
            con.execute(
                "INSERT INTO overrides (ts, override_type, zone, value, expires_at) "
                "VALUES (?,?,?,?,?)",
                (time.time(), override_type, zone, value, expires_at),
            )
            con.execute(
                "INSERT INTO sync_log (ts, source, action, details) VALUES (?,?,?,?)",
                (time.time(), 'api', 'override_added', json.dumps(body)),
            )
            con.commit()
            con.close()
            self._send_json({'ok': True, 'msg': f'Override {override_type} added'})
        elif self.path == '/sync':
            state = compute_state(self._db_path)
            write_state(state, STATE_FILE)
            self._send_json(state)
        else:
            self._send_json({'error': 'not found'}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


def sync_loop(db_path, state_file, interval=10):
    """Background loop that syncs timetable state every interval seconds."""
    while True:
        try:
            state = compute_state(db_path)
            write_state(state, state_file)
        except Exception as e:
            print(f"Timetable sync error: {e}")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description='Tumba College Timetable Engine')
    parser.add_argument('--db', default=DB_PATH)
    parser.add_argument('--state-file', default=STATE_FILE)
    parser.add_argument('--port', type=int, default=API_PORT)
    parser.add_argument('--sync-interval', type=int, default=10)
    args = parser.parse_args()

    db = init_db(args.db)
    TimetableHandler._db_path = args.db

    # Start sync loop in background
    sync_thread = threading.Thread(
        target=sync_loop, args=(args.db, args.state_file, args.sync_interval),
        daemon=True,
    )
    sync_thread.start()

    # Start HTTP server
    server = ThreadingHTTPServer(('0.0.0.0', args.port), TimetableHandler)
    print(f"Timetable engine listening on http://0.0.0.0:{args.port}")
    print(f"  DB: {args.db}")
    print(f"  State file: {args.state_file}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
