#!/usr/bin/env python3
"""
Lightweight runtime API stub for campus SDN demo without Mininet.

Serves port 9091 with the same interface as campus_topology.py's RuntimeAPI.
Simulates all scenarios by:
  - Writing to /tmp/campus_sim_overlay.json  (merged by dashboard /api/metrics)
  - Writing to /tmp/campus_ml_action.json    (read by controller every 0.5s)
  - Writing to /tmp/campus_simulation_context.json (read by controller)
"""

import json
import logging
import os
import threading
import time
import uuid
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("campus.runtime_stub")

OVERLAY_FILE = "/tmp/campus_sim_overlay.json"
ML_ACTION_FILE = os.getenv("CAMPUS_ML_ACTION_FILE", "/tmp/campus_ml_action.json")
SIM_CONTEXT_FILE = os.getenv("CAMPUS_SIM_CONTEXT_FILE", "/tmp/campus_simulation_context.json")
METRICS_FILE = os.getenv("CAMPUS_METRICS_FILE", "/tmp/campus_metrics.json")
HOST_PORT = int(os.getenv("CAMPUS_RUNTIME_API_PORT", "9091"))
HOST_ADDR = os.getenv("CAMPUS_RUNTIME_API_HOST", "127.0.0.1")

# ── State ──────────────────────────────────────────────────────────────────────

_lock = threading.RLock()
_operation_events: list = []
_stress_active = False
_attack_active = False
_attack_type = None
_attack_attacker = None
_attack_target = None
_stress_clients: list = []
_sim_thread: threading.Thread | None = None
_device_registry: dict = {}


def _now() -> float:
    return time.time()


def _write_json(path: str, data: dict):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("write_json %s failed: %s", path, exc)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _record_op(op_type: str, status: str, **fields):
    event = {"ts": _now(), "op": op_type, "status": status}
    event.update(fields)
    with _lock:
        _operation_events.append(event)
        while len(_operation_events) > 200:
            _operation_events.pop(0)
    return event


def _clear_overlay():
    try:
        os.remove(OVERLAY_FILE)
    except FileNotFoundError:
        pass


def _write_overlay(**fields):
    existing = _read_json(OVERLAY_FILE)
    existing.update(fields)
    _write_json(OVERLAY_FILE, existing)


def _write_ml_action(routing_choice: str | None, note: str = "runtime_stub"):
    action = {
        "ts": _now(),
        "routing_choice": routing_choice,
        "note": note,
        "dqn": {"action_name": note, "steps": 0},
    }
    _write_json(ML_ACTION_FILE, action)


def _cancel_sim_after(delay_s: float, on_done):
    def _run():
        time.sleep(delay_s)
        on_done()
    t = threading.Thread(target=_run, daemon=True, name="sim-cancel")
    t.start()
    return t


# ── Simulation helpers ─────────────────────────────────────────────────────────

def _simulate_congestion(duration_s: int, clients: list):
    global _stress_active, _stress_clients
    with _lock:
        _stress_active = True
        _stress_clients = clients
    _record_op("start_stress", "ok", clients=clients, duration_s=duration_s)
    logger.info("Congestion simulation started: %s for %ds", clients, duration_s)

    ramp_steps = min(20, duration_s * 2)
    step_s = duration_s / max(ramp_steps, 1)

    def _run():
        global _stress_active, _stress_clients
        for i in range(ramp_steps):
            if not _stress_active:
                break
            mbps = 50.0 + (i / ramp_steps) * 260.0   # 50→310 Mbps ramp
            _write_overlay(
                core_primary_mbps=mbps,
                core_wifi_mbps=mbps * 0.3,
                student_throttle_active=(mbps > 100),
            )
            # Trigger reroute when congested
            if mbps > 150:
                _write_ml_action("reroute_backup", "congestion_simulation")
            else:
                _write_ml_action("primary", "normal")
            time.sleep(step_s)
        # Cool down
        for i in range(10):
            if not _stress_active:
                break
            mbps = max(0, 310.0 - i * 30.0)
            _write_overlay(core_primary_mbps=mbps, student_throttle_active=(mbps > 100))
            time.sleep(0.5)
        with _lock:
            _stress_active = False
            _stress_clients = []
        _write_overlay(core_primary_mbps=0.0, core_wifi_mbps=0.0, student_throttle_active=False)
        _write_ml_action("primary", "normal")
        _record_op("stop_stress", "ok", reason="simulation_complete")
        logger.info("Congestion simulation ended")

    t = threading.Thread(target=_run, daemon=True, name="sim-congestion")
    t.start()
    return t


def _simulate_ddos(attacker: str, target: str, duration_s: int, attack_type: str = "udp_flood"):
    global _attack_active, _attack_type, _attack_attacker, _attack_target
    with _lock:
        _attack_active = True
        _attack_type = attack_type
        _attack_attacker = attacker
        _attack_target = target
    _record_op("start_attack", "ok", attacker=attacker, target=target, attack_type=attack_type)
    logger.info("DDoS simulation started: %s -> %s (%s)", attacker, target, attack_type)

    def _run():
        global _attack_active, _attack_type, _attack_attacker, _attack_target
        # Phase 1: attack ramps up (first 20% of duration)
        ramp_s = max(2, duration_s * 0.2)
        for i in range(int(ramp_s * 5)):
            if not _attack_active:
                break
            pct = i / (ramp_s * 5)
            _write_overlay(
                ddos_active=False,
                ddos_attack_type=attack_type,
                ddos_attacker_ips=[attacker],
                ddos_blocked_flows=0,
                core_wifi_mbps=pct * 400,
            )
            time.sleep(0.2)

        # Phase 2: detected and blocked (remaining 80%)
        if _attack_active:
            _record_op("ddos_detected", "ok", attack_type=attack_type, attacker=attacker)
            _write_overlay(
                ddos_active=True,
                ddos_attack_type=attack_type,
                ddos_attacker_ips=[attacker],
                ddos_blocked_flows=1,
                ddos_detection_ts=_now(),
                core_wifi_mbps=0.0,   # traffic blocked
            )
            logger.info("DDoS detected and blocked")
            time.sleep(duration_s * 0.8)

        with _lock:
            _attack_active = False
            _attack_type = None
            _attack_attacker = None
            _attack_target = None
        _write_overlay(
            ddos_active=False,
            ddos_attack_type=None,
            ddos_attacker_ips=[],
            ddos_blocked_flows=0,
            core_wifi_mbps=0.0,
        )
        _record_op("stop_attack", "ok", reason="simulation_complete")
        logger.info("DDoS simulation ended")

    t = threading.Thread(target=_run, daemon=True, name="sim-ddos")
    t.start()
    return t


def _simulate_link_failure(switch: str, port: int, duration_s: int):
    _record_op("link_failure", "ok", switch=switch, port=port, duration_s=duration_s)
    logger.info("Link failure simulation: %s port %d for %ds", switch, port, duration_s)
    real_metrics = _read_json(METRICS_FILE)
    connected = real_metrics.get("connected_switches", [])
    if isinstance(connected, list):
        reduced = [s for s in connected if s != str(switch.replace("s", ""))]
    else:
        reduced = connected

    def _run():
        # Bring down: remove one switch from connected list, trigger reroute
        _write_overlay(
            connected_switches=reduced,
            reroute_active=True,
        )
        _write_ml_action("reroute_backup", "link_failure_simulation")
        _record_op("failover_triggered", "ok", switch=switch)
        time.sleep(duration_s)
        # Restore
        _write_overlay(
            connected_switches=connected,
            reroute_active=False,
        )
        _write_ml_action("primary", "normal")
        _record_op("link_restored", "ok", switch=switch)
        logger.info("Link failure simulation ended, switch %s restored", switch)

    t = threading.Thread(target=_run, daemon=True, name="sim-link-failure")
    t.start()
    return t


# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask("runtime_api_stub")
app.config["JSON_SORT_KEYS"] = False


@app.get("/health")
def health():
    return jsonify({"ok": True, "mode": "stub", "switches": 14})


@app.get("/operations")
def operations():
    with _lock:
        evs = list(_operation_events)
    real = _read_json(METRICS_FILE)
    connected = real.get("connected_switches", [])
    sw_count = len(connected) if isinstance(connected, list) else int(connected or 0)
    return jsonify({
        "ok": True,
        "events": evs[-50:],
        "running_stress_clients": _stress_clients if _stress_active else [],
        "last_pingall_result": {
            "tested": sw_count * 3,
            "reachable": sw_count * 3,
            "packet_loss_pct": 0.0,
            "timestamp": _now(),
            "note": "Topology connected — run Ping All for live result",
        },
    })


@app.post("/pingall")
def pingall():
    real = _read_json(METRICS_FILE)
    connected = real.get("connected_switches", [])
    sw_count = len(connected) if isinstance(connected, list) else int(connected or 0)
    result = {
        "ok": True,
        "tested": sw_count * 3,
        "reachable": sw_count * 3,
        "packet_loss_pct": 0.0,
        "timestamp": _now(),
    }
    _record_op("pingall", "ok", **result)
    return jsonify(result)


@app.post("/start_stress")
def start_stress():
    payload = request.get_json(silent=True) or {}
    duration_s = int(payload.get("seconds", 45))
    clients = payload.get("clients") or ["h_lab2_1", "h_lab3_1", "h_lab4_1"]
    if isinstance(clients, list):
        client_names = clients
    else:
        client_names = [clients]
    _simulate_congestion(duration_s, client_names)
    return jsonify({
        "ok": True,
        "message": f"Congestion simulation started on {', '.join(client_names)} for {duration_s}s",
        "clients": client_names,
        "duration_s": duration_s,
    })


@app.post("/stop_stress")
def stop_stress():
    global _stress_active, _stress_clients
    with _lock:
        _stress_active = False
        _stress_clients = []
    _write_overlay(core_primary_mbps=0.0, core_wifi_mbps=0.0, student_throttle_active=False)
    _write_ml_action("primary", "manual_stop")
    _record_op("stop_stress", "ok", reason="manual")
    return jsonify({"ok": True, "message": "Stress test stopped"})


@app.get("/attack_status")
def attack_status():
    with _lock:
        active = _attack_active
        atype = _attack_type
        attacker = _attack_attacker
        target = _attack_target
    return jsonify({
        "ok": True,
        "attack_active": active,
        "attack_type": atype,
        "attacker": attacker,
        "target": target,
    })


@app.post("/start_attack")
def start_attack():
    payload = request.get_json(silent=True) or {}
    attacker = payload.get("attacker", "h_lab7_1")
    target = payload.get("target", "10.0.1.10")
    duration = int(payload.get("duration", 30))
    attack_type = payload.get("attack_type", "udp_flood")
    _simulate_ddos(attacker, target, duration, attack_type)
    return jsonify({
        "ok": True,
        "message": f"DDoS simulation started: {attacker} → {target} ({attack_type})",
        "attacker": attacker,
        "target": target,
        "duration": duration,
        "attack_type": attack_type,
    })


@app.post("/stop_attack")
def stop_attack():
    global _attack_active, _attack_type, _attack_attacker, _attack_target
    with _lock:
        _attack_active = False
        _attack_type = None
        _attack_attacker = None
        _attack_target = None
    _write_overlay(
        ddos_active=False, ddos_attack_type=None,
        ddos_attacker_ips=[], ddos_blocked_flows=0,
    )
    _record_op("stop_attack", "ok", reason="manual")
    return jsonify({"ok": True, "message": "Attack simulation stopped"})


@app.post("/link_failure")
def link_failure():
    payload = request.get_json(silent=True) or {}
    switch = payload.get("switch", "s1")
    port = int(payload.get("port", 1))
    duration = int(payload.get("duration", 20))
    _simulate_link_failure(switch, port, duration)
    return jsonify({
        "ok": True,
        "message": f"Link failure simulated on {switch} port {port} for {duration}s",
        "switch": switch,
        "port": port,
        "duration": duration,
    })


@app.post("/add_host")
def add_host():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name", f"h_stub_{int(_now())}")
    ip = payload.get("ip", "")
    mac = payload.get("mac", "")
    switch = payload.get("attach_switch", "s4")
    _device_registry[name] = {
        "name": name, "ip": ip, "mac": mac, "switch": switch,
        "category": payload.get("category", "lab"),
        "bandwidth_mbps": payload.get("bandwidth_mbps", 100),
        "created_ts": _now(), "stub": True,
    }
    _record_op("add_host", "ok", name=name, ip=ip, switch=switch)
    return jsonify({
        "ok": True, "name": name, "ip": ip, "mac": mac,
        "switch": switch, "message": f"Host {name} registered in stub registry",
    })


@app.get("/device/<name>")
def device_details(name):
    dev = _device_registry.get(name, {})
    return jsonify({
        "ok": True,
        "device": {
            "name": name,
            "ip": dev.get("ip", ""),
            "mac": dev.get("mac", ""),
            "switch": dev.get("switch", ""),
            "bandwidth_mbps": dev.get("bandwidth_mbps", 100),
            "stub": True,
            "ifconfig": f"# Runtime API stub — host {name} registered\n# IP: {dev.get('ip','?')}\n",
            "ping_result": {"ok": True, "rtt_ms": 1.2, "note": "simulated"},
        },
    })


@app.get("/device/<name>/workspace")
def device_workspace(name):
    dev = _device_registry.get(name, {})
    return jsonify({
        "ok": True,
        "name": name,
        "ip": dev.get("ip", ""),
        "sessions": [],
        "available_actions": ["ping", "iperf3_client", "iperf3_server", "traceroute"],
        "stub": True,
    })


@app.post("/device/<name>/action")
def device_action(name):
    payload = request.get_json(silent=True) or {}
    action = payload.get("action", "ping")
    target = payload.get("target", "10.0.1.10")
    _record_op("device_action", "ok", device=name, action=action, target=target)
    simulated = {
        "ping": f"PING {target}: 64 bytes, icmp_seq=1 ttl=64 time=1.2 ms\n"
                f"--- {target} ping statistics ---\n1 packets transmitted, 1 received, 0% loss",
        "traceroute": f"traceroute to {target}:\n 1  10.0.0.1  0.5 ms\n 2  {target}  1.2 ms",
        "iperf3_client": f"Connecting to {target}...\n[SUM] 0.00-10.00 sec  112 MBytes  93.8 Mbits/sec",
    }
    output = simulated.get(action, f"[stub] action '{action}' on {name} completed successfully")
    return jsonify({"ok": True, "output": output, "action": action, "target": target, "stub": True})


@app.post("/api/scenario")
def set_scenario():
    payload = request.get_json(silent=True) or {}
    label = payload.get("label", "baseline")
    _record_op("scenario_change", "ok", label=label)
    return jsonify({"ok": True, "scenario": label})


@app.get("/hosts")
def hosts():
    real = _read_json(METRICS_FILE)
    connected = real.get("connected_switches", [])
    sw_count = len(connected) if isinstance(connected, list) else 14
    return jsonify({"ok": True, "host_count": sw_count * 3, "hosts": list(_device_registry.keys())})


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=HOST_ADDR)
    parser.add_argument("--port", type=int, default=HOST_PORT)
    args = parser.parse_args()

    # Clear any stale overlay on startup
    _clear_overlay()
    _record_op("startup", "ok", host=args.host, port=args.port)

    print(f"Runtime API Stub  : http://{args.host}:{args.port}")
    print(f"Metrics overlay   : {OVERLAY_FILE}")
    print(f"ML action hook    : {ML_ACTION_FILE}")
    print(f"Mode              : simulation stub (no Mininet required)")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)
