#!/usr/bin/env python3
"""
PC Activity Manager — Tumba College SDN

End-device-centric simulation engine. Each user PC can independently run
one of six activity types that drive realistic, differentiated traffic:

    exam           → high priority, latency-sensitive, TCP to MIS
    video_conf     → real-time, UDP to MIS
    elearning      → medium-high, TCP to Moodle
    video_streaming→ moderate, TCP to Moodle
    file_download  → bandwidth-intensive, TCP to MIS
    social_media   → low priority, TCP to Moodle

All traffic originates from actual Mininet host namespaces (ip netns exec),
not from centralized triggers. Security scenario (ddos_attack) is also
available for student WiFi hosts to demonstrate anomaly detection.

HTTP API (port 9095):
  GET  /state              → full PC activity state + baseline
  POST /set_activity       → {"host": "h_wifi1", "activity": "exam"}
  POST /capture_baseline   → snapshot current network metrics as baseline
  POST /reset_all          → set all PCs to idle
  GET  /health             → {"ok": true}
  GET  /profiles           → activity profile definitions
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─── File paths ───────────────────────────────────────────────────────────────
PC_ACTIVITIES_FILE = os.environ.get('CAMPUS_PC_ACTIVITIES_FILE',
                                    '/tmp/campus_pc_activities.json')
BASELINE_FILE      = os.environ.get('CAMPUS_BASELINE_FILE',
                                    '/tmp/campus_baseline.json')
METRICS_FILE       = os.environ.get('CAMPUS_METRICS_FILE',
                                    '/tmp/campus_metrics.json')

# ─── Activity profiles ────────────────────────────────────────────────────────
# Each profile defines traffic characteristics and QoS intent.
ACTIVITY_PROFILES = {
    'idle': {
        'label':        'Idle',
        'priority':     5,
        'dscp':         0,
        'bandwidth_mbps': 0.0,
        'color':        '#64748b',
        'icon':         '●',
        'description':  'No active traffic — baseline state',
    },
    'exam': {
        'label':        'Online Examination',
        'priority':     1,
        'dscp':         46,         # EF — expedited forwarding
        'bandwidth_mbps': 5.0,
        'dst_ip':       '10.20.0.1',  # h_mis (MIS Server)
        'dst_port':     5201,
        'proto':        'tcp',
        'pattern':      'burst',
        'burst_dur':    4,
        'burst_gap':    1,
        'color':        '#ef4444',
        'icon':         'E',
        'description':  'High priority, latency-sensitive exam traffic',
    },
    'video_conf': {
        'label':        'Video Conferencing',
        'priority':     1,
        'dscp':         40,         # CS5
        'bandwidth_mbps': 4.0,
        'dst_ip':       '10.20.0.1',  # h_mis (MIS Server)
        'dst_port':     5201,
        'proto':        'udp',
        'pattern':      'stream',
        'burst_dur':    8,
        'burst_gap':    1,
        'color':        '#3b82f6',
        'icon':         'V',
        'description':  'Real-time, delay-sensitive video call',
    },
    'elearning': {
        'label':        'E-learning Platform',
        'priority':     2,
        'dscp':         26,         # AF31
        'bandwidth_mbps': 3.0,
        'dst_ip':       '10.20.0.4',  # h_moodle (Moodle Server)
        'dst_port':     5204,
        'proto':        'tcp',
        'pattern':      'moderate',
        'burst_dur':    5,
        'burst_gap':    2,
        'color':        '#10b981',
        'icon':         'L',
        'description':  'Medium-high priority Moodle / LMS access',
    },
    'video_streaming': {
        'label':        'Academic Streaming',
        'priority':     3,
        'dscp':         18,         # AF21
        'bandwidth_mbps': 5.0,
        'dst_ip':       '10.20.0.4',  # h_moodle (Moodle Server)
        'dst_port':     5204,
        'proto':        'tcp',
        'pattern':      'stream',
        'burst_dur':    10,
        'burst_gap':    1,
        'color':        '#a855f7',
        'icon':         'S',
        'description':  'Moderate priority academic video stream',
    },
    'file_download': {
        'label':        'File Downloading',
        'priority':     4,
        'dscp':         10,         # AF11
        'bandwidth_mbps': 10.0,
        'dst_ip':       '10.20.0.1',  # h_mis (MIS Server)
        'dst_port':     5201,
        'proto':        'tcp',
        'pattern':      'bulk',
        'burst_dur':    12,
        'burst_gap':    1,
        'color':        '#f59e0b',
        'icon':         'D',
        'description':  'Bandwidth-intensive bulk file transfer',
    },
    'social_media': {
        'label':        'Social Media',
        'priority':     5,
        'dscp':         0,
        'bandwidth_mbps': 1.0,
        'dst_ip':       '10.20.0.4',  # h_moodle (Moodle Server)
        'dst_port':     5204,
        'proto':        'tcp',
        'pattern':      'bursty',
        'burst_dur':    2,
        'burst_gap':    4,
        'color':        '#06b6d4',
        'icon':         'M',
        'description':  'Low priority best-effort browsing',
    },
    'ddos_attack': {
        'label':        'DDoS Attack (Security)',
        'priority':     0,
        'dscp':         0,
        'bandwidth_mbps': 50.0,
        'dst_ip':       '10.20.0.1',  # h_mis (MIS Server — target)
        'dst_port':     80,
        'proto':        'tcp',
        'pattern':      'flood',
        'burst_dur':    30,
        'burst_gap':    0,
        'color':        '#dc2626',
        'icon':         '!',
        'description':  'Security scenario: simulated DDoS from this PC',
    },
}

# Priority label for display
PRIORITY_LABELS = {1: 'CRITICAL', 2: 'HIGH', 3: 'MEDIUM', 4: 'LOW', 5: 'BEST-EFFORT', 0: 'ATTACK'}

# ─── User PC definitions — Tumba College diagram topology ────────────────────
# Matches tumba_topo.py exactly (tumba_sdn/topology/tumba_topo.py)
# Servers (targets, not PCs): h_mis/h_dhcp/h_auth/h_moodle @ 10.20.0.x
USER_PCS: dict[str, dict] = {
    # Staff LAN — VLAN 10 — as1 (DPID 4) — 10.10.0.x
    'h_staff1': {'zone': 'staff_lan',    'ip': '10.10.0.1', 'label': 'Staff PC 1',   'role': 'staff'},
    'h_staff2': {'zone': 'staff_lan',    'ip': '10.10.0.2', 'label': 'Staff PC 2',   'role': 'staff'},
    'h_staff3': {'zone': 'staff_lan',    'ip': '10.10.0.3', 'label': 'Staff PC 3',   'role': 'staff'},
    'h_staff4': {'zone': 'staff_lan',    'ip': '10.10.0.4', 'label': 'Staff PC 4',   'role': 'staff'},
    'h_staff5': {'zone': 'staff_lan',    'ip': '10.10.0.5', 'label': 'Staff PC 5',   'role': 'staff'},
    'h_staff6': {'zone': 'staff_lan',    'ip': '10.10.0.6', 'label': 'Staff PC 6',   'role': 'staff'},
    # IT / Networking Lab — VLAN 30 — as3 (DPID 6) — 10.30.0.x
    'h_lab1':   {'zone': 'it_lab',       'ip': '10.30.0.1', 'label': 'Lab PC 1',     'role': 'student'},
    'h_lab2':   {'zone': 'it_lab',       'ip': '10.30.0.2', 'label': 'Lab PC 2',     'role': 'student'},
    'h_lab3':   {'zone': 'it_lab',       'ip': '10.30.0.3', 'label': 'Lab PC 3',     'role': 'student'},
    'h_lab4':   {'zone': 'it_lab',       'ip': '10.30.0.4', 'label': 'Lab PC 4',     'role': 'student'},
    # Student Wi-Fi — VLAN 40 — as4 (DPID 7) — 10.40.0.x
    'h_wifi1':  {'zone': 'student_wifi', 'ip': '10.40.0.1',  'label': 'WiFi Device 1',  'role': 'student'},
    'h_wifi2':  {'zone': 'student_wifi', 'ip': '10.40.0.2',  'label': 'WiFi Device 2',  'role': 'student'},
    'h_wifi3':  {'zone': 'student_wifi', 'ip': '10.40.0.3',  'label': 'WiFi Device 3',  'role': 'student'},
    'h_wifi4':  {'zone': 'student_wifi', 'ip': '10.40.0.4',  'label': 'WiFi Device 4',  'role': 'student'},
    'h_wifi5':  {'zone': 'student_wifi', 'ip': '10.40.0.5',  'label': 'WiFi Device 5',  'role': 'student'},
    'h_wifi6':  {'zone': 'student_wifi', 'ip': '10.40.0.6',  'label': 'WiFi Device 6',  'role': 'student'},
    'h_wifi7':  {'zone': 'student_wifi', 'ip': '10.40.0.7',  'label': 'WiFi Device 7',  'role': 'student'},
    'h_wifi8':  {'zone': 'student_wifi', 'ip': '10.40.0.8',  'label': 'WiFi Device 8',  'role': 'student'},
    'h_wifi9':  {'zone': 'student_wifi', 'ip': '10.40.0.9',  'label': 'WiFi Device 9',  'role': 'student'},
    'h_wifi10': {'zone': 'student_wifi', 'ip': '10.40.0.10', 'label': 'WiFi Device 10', 'role': 'student'},
}

# ─── PC Activity Manager ──────────────────────────────────────────────────────

class PCActivityManager:
    """Manages per-PC activity state and drives corresponding traffic."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = True

        # Per-PC state
        self._activity: dict[str, str] = {pc: 'idle' for pc in USER_PCS}
        self._since_ts: dict[str, float] = {pc: time.time() for pc in USER_PCS}
        self._traffic_mbps: dict[str, float] = {pc: 0.0 for pc in USER_PCS}
        self._pkts_sent: dict[str, int] = {pc: 0 for pc in USER_PCS}

        # Per-PC traffic thread control
        self._stop_events: dict[str, threading.Event] = {
            pc: threading.Event() for pc in USER_PCS
        }
        self._threads: dict[str, threading.Thread | None] = {pc: None for pc in USER_PCS}

        # Child processes for cleanup
        self._child_procs: list[subprocess.Popen] = []
        self._server_started = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_activity(self, host: str, activity: str) -> dict:
        """Set activity for a single PC. Returns status dict."""
        if host not in USER_PCS:
            return {'ok': False, 'error': f'Unknown host: {host}'}
        if activity not in ACTIVITY_PROFILES:
            return {'ok': False, 'error': f'Unknown activity: {activity}'}

        with self._lock:
            old = self._activity[host]
            if old == activity:
                return {'ok': True, 'host': host, 'activity': activity, 'changed': False}

            # Stop current traffic thread for this PC
            self._stop_events[host].set()
            t = self._threads[host]
            if t and t.is_alive():
                t.join(timeout=3)

            # Update state
            self._activity[host] = activity
            self._since_ts[host] = time.time()
            self._traffic_mbps[host] = 0.0

            # Reset stop event and start new thread (unless idle)
            self._stop_events[host] = threading.Event()
            if activity != 'idle':
                t = threading.Thread(
                    target=self._traffic_loop,
                    args=(host, activity, self._stop_events[host]),
                    daemon=True,
                    name=f'traffic_{host}',
                )
                self._threads[host] = t
                t.start()
            else:
                self._threads[host] = None

        print(f'[pcam] {host}: {old} → {activity}')
        self._write_state()
        return {'ok': True, 'host': host, 'activity': activity, 'changed': True}

    def reset_all(self) -> dict:
        """Set all PCs to idle."""
        for host in list(USER_PCS):
            self.set_activity(host, 'idle')
        return {'ok': True, 'reset': len(USER_PCS)}

    def get_state(self) -> dict:
        """Return full state snapshot."""
        with self._lock:
            pcs = {}
            for host, meta in USER_PCS.items():
                act = self._activity[host]
                profile = ACTIVITY_PROFILES[act]
                pcs[host] = {
                    **meta,
                    'activity':      act,
                    'activity_label': profile['label'],
                    'priority':      profile['priority'],
                    'priority_label': PRIORITY_LABELS[profile['priority']],
                    'dscp':          profile['dscp'],
                    'traffic_mbps':  round(self._traffic_mbps[host], 2),
                    'since_ts':      self._since_ts[host],
                    'color':         profile['color'],
                    'icon':          profile['icon'],
                    'bw_target':     profile['bandwidth_mbps'],
                }

        baseline = {}
        try:
            with open(BASELINE_FILE) as f:
                baseline = json.load(f)
        except Exception:
            pass

        return {
            'ts':       time.time(),
            'pcs':      pcs,
            'baseline': baseline,
            'profiles': ACTIVITY_PROFILES,
        }

    def capture_baseline(self) -> dict:
        """Snapshot current metrics as the baseline for comparison."""
        metrics = {}
        try:
            with open(METRICS_FILE) as f:
                metrics = json.load(f)
        except Exception:
            pass

        # Summarise active activities at capture time
        with self._lock:
            active = {h: a for h, a in self._activity.items() if a != 'idle'}

        baseline = {
            'captured_ts': time.time(),
            'active_pcs':  len(active),
            'metrics':     metrics,
            'activities':  active,
        }
        try:
            tmp = BASELINE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(baseline, f, indent=2)
            os.replace(tmp, BASELINE_FILE)
        except Exception as e:
            return {'ok': False, 'error': str(e)}

        return {'ok': True, 'captured_ts': baseline['captured_ts']}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        """Start background writer and iperf servers."""
        self._ensure_iperf_servers()
        writer = threading.Thread(target=self._state_writer, daemon=True)
        writer.start()
        print(f'[pcam] Activity manager started — {len(USER_PCS)} PCs registered')

    def stop(self):
        """Stop all traffic threads and cleanup."""
        self.running = False
        for host in USER_PCS:
            self._stop_events[host].set()
        for p in self._child_procs:
            try:
                p.terminate()
            except Exception:
                pass
        print('[pcam] Stopped.')

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _ensure_iperf_servers(self):
        """Start iperf3 servers on tumba server-zone hosts (idempotent)."""
        if self._server_started:
            return
        # Tumba topology servers (as2, VLAN 20, DPID 5)
        servers = {
            'h_mis':    5201,   # MIS Server     — exam, video_conf, file_download
            'h_dhcp':   5202,   # DHCP Server    — spare
            'h_auth':   5203,   # Auth Server    — spare
            'h_moodle': 5204,   # Moodle Server  — elearning, video_streaming, social_media
        }
        for host, port in servers.items():
            try:
                p = subprocess.Popen(
                    ['sudo', 'ip', 'netns', 'exec', host,
                     'iperf3', '-s', '-p', str(port), '-D'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._child_procs.append(p)
                print(f'[pcam] iperf3 server on {host}:{port}')
            except Exception as e:
                print(f'[pcam] WARNING: could not start iperf3 server on {host}: {e}')
        time.sleep(0.5)
        self._server_started = True

    def _traffic_loop(self, host: str, activity: str, stop_event: threading.Event):
        """Traffic generation loop for one PC — runs until stop_event set."""
        profile = ACTIVITY_PROFILES.get(activity, {})
        pattern   = profile.get('pattern', 'burst')
        dst_ip    = profile.get('dst_ip', '10.20.0.1')
        dst_port  = profile.get('dst_port', 5201)
        proto     = profile.get('proto', 'tcp')
        bw_mbps   = profile.get('bandwidth_mbps', 1.0)
        burst_dur = profile.get('burst_dur', 5)
        burst_gap = profile.get('burst_gap', 1)

        while not stop_event.is_set():
            # Verify activity hasn't changed (re-check state)
            with self._lock:
                if self._activity.get(host) != activity:
                    break

            measured = self._run_iperf(host, dst_ip, dst_port, proto,
                                       bw_mbps, burst_dur)
            with self._lock:
                if self._activity.get(host) == activity:
                    self._traffic_mbps[host] = measured
                    self._pkts_sent[host] += 1

            # Gap between bursts (skip for stream/flood patterns)
            if burst_gap > 0 and pattern not in ('stream', 'flood'):
                stop_event.wait(timeout=burst_gap)

        # Zero out traffic on exit
        with self._lock:
            if self._activity.get(host) == 'idle':
                self._traffic_mbps[host] = 0.0

    def _run_iperf(self, host: str, dst_ip: str, port: int, proto: str,
                   bw_mbps: float, duration: int) -> float:
        """
        Run a single iperf3 session from the host's network namespace.
        Returns measured throughput in Mbps.
        """
        cmd = [
            'sudo', 'ip', 'netns', 'exec', host,
            'iperf3', '-c', dst_ip, '-p', str(port),
            '-t', str(duration),
            '-b', f'{bw_mbps}M',
            '-J',                        # JSON output for parsing
            '--connect-timeout', '2000',
        ]
        if proto == 'udp':
            cmd.append('-u')

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=duration + 10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                end = data.get('end', {})
                # TCP: sum sent bits_per_second; UDP: sum bits_per_second
                if proto == 'tcp':
                    bps = end.get('sum_sent', {}).get('bits_per_second', 0)
                else:
                    bps = end.get('sum', {}).get('bits_per_second', 0)
                return round(bps / 1_000_000, 2)
        except subprocess.TimeoutExpired:
            pass
        except json.JSONDecodeError:
            pass
        except Exception:
            pass
        # Return target if we couldn't measure (namespace not yet ready, etc.)
        return bw_mbps * 0.8

    def _state_writer(self):
        """Background thread: writes state file every 2 seconds."""
        while self.running:
            self._write_state()
            time.sleep(2)

    def _write_state(self):
        state = self.get_state()
        try:
            tmp = PC_ACTIVITIES_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, PC_ACTIVITIES_FILE)
        except Exception as e:
            print(f'[pcam] state write error: {e}')


# ─── HTTP API handler ─────────────────────────────────────────────────────────

def make_handler(manager: PCActivityManager):
    class Handler(BaseHTTPRequestHandler):
        _mgr = manager

        def log_message(self, *args):
            pass  # suppress request logs

        def _json(self, data, code=200):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get('Content-Length', 0))
            return json.loads(self.rfile.read(n)) if n > 0 else {}

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

        def do_GET(self):
            if self.path == '/health':
                self._json({'ok': True, 'service': 'pc_activity_manager',
                            'pcs': len(USER_PCS)})
            elif self.path == '/state':
                self._json(self._mgr.get_state())
            elif self.path == '/profiles':
                self._json(ACTIVITY_PROFILES)
            else:
                self._json({'error': 'not found'}, 404)

        def do_POST(self):
            if self.path == '/set_activity':
                body = self._body()
                host = body.get('host', '')
                activity = body.get('activity', 'idle')
                result = self._mgr.set_activity(host, activity)
                self._json(result)
            elif self.path == '/capture_baseline':
                self._json(self._mgr.capture_baseline())
            elif self.path == '/reset_all':
                self._json(self._mgr.reset_all())
            elif self.path == '/set_scenario':
                # Accept either {"scenario": "canonical"} or {"assignments": [...]}
                body = self._body()
                scenario_name = body.get('scenario')
                if scenario_name and scenario_name in DEMO_SCENARIOS:
                    assignments = DEMO_SCENARIOS[scenario_name]
                else:
                    assignments = body.get('assignments', [])
                results = []
                for a in assignments:
                    r = self._mgr.set_activity(a.get('host', ''), a.get('activity', 'idle'))
                    results.append(r)
                ok = all(r.get('ok') for r in results)
                self._json({'ok': ok, 'scenario': scenario_name, 'results': results})
            else:
                self._json({'error': 'not found'}, 404)

    return Handler


# ─── Pre-defined demonstration scenarios ─────────────────────────────────────

DEMO_SCENARIOS = {
    'canonical': [
        # Design spec: PC1→Exam, PC2→Social, PC3→VideoConf,
        #              PC4→FileDownload, PC5→E-learning, PC6→Streaming
        {'host': 'h_wifi1', 'activity': 'exam'},
        {'host': 'h_wifi2', 'activity': 'social_media'},
        {'host': 'h_wifi3', 'activity': 'video_conf'},
        {'host': 'h_wifi4', 'activity': 'file_download'},
        {'host': 'h_wifi5', 'activity': 'elearning'},
        {'host': 'h_wifi6', 'activity': 'video_streaming'},
    ],
    'staff_heavy': [
        {'host': 'h_staff1', 'activity': 'exam'},
        {'host': 'h_staff2', 'activity': 'video_conf'},
        {'host': 'h_staff3', 'activity': 'elearning'},
        {'host': 'h_staff4', 'activity': 'file_download'},
        {'host': 'h_staff5', 'activity': 'video_streaming'},
        {'host': 'h_staff6', 'activity': 'social_media'},
    ],
    'security_test': [
        # Exam running while DDoS attack is launched from student WiFi
        {'host': 'h_staff1',  'activity': 'exam'},
        {'host': 'h_wifi7',   'activity': 'ddos_attack'},
        {'host': 'h_wifi8',   'activity': 'ddos_attack'},
        {'host': 'h_wifi9',   'activity': 'ddos_attack'},
        {'host': 'h_staff2',  'activity': 'video_conf'},
        {'host': 'h_lab1',    'activity': 'elearning'},
    ],
    'congestion': [
        # Multiple high-bandwidth activities saturating uplinks
        {'host': 'h_wifi1',  'activity': 'file_download'},
        {'host': 'h_wifi2',  'activity': 'file_download'},
        {'host': 'h_wifi3',  'activity': 'video_streaming'},
        {'host': 'h_wifi4',  'activity': 'video_streaming'},
        {'host': 'h_wifi5',  'activity': 'video_conf'},
        {'host': 'h_wifi6',  'activity': 'exam'},
    ],
}


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='PC Activity Manager')
    parser.add_argument('--port', type=int, default=9095)
    parser.add_argument('--host', default='0.0.0.0')
    args = parser.parse_args()

    manager = PCActivityManager()
    manager.start()

    # Graceful shutdown
    def _shutdown(sig, frame):
        print('\n[pcam] Shutting down...')
        manager.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(manager))
    print(f'[pcam] HTTP API: http://{args.host}:{args.port}')
    print(f'[pcam] State file: {PC_ACTIVITIES_FILE}')
    print(f'[pcam] Endpoints: GET /state  POST /set_activity  POST /capture_baseline')
    server.serve_forever()


if __name__ == '__main__':
    main()
