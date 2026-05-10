#!/usr/bin/env python3
"""Campus SDN Performance Evaluator — port 9093.

Polls campus metrics every 200 ms, detects state transitions,
computes per-scenario statistics, and exposes a REST API for the
dashboard and external analysis scripts.
"""

import csv
import io
import json
import logging
import os
import statistics
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib import error as urllib_error
from urllib import request as urllib_request

from flask import Flask, Response, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("campus.perf_eval")

# ── Constants ─────────────────────────────────────────────────────────────────

METRICS_FILE = os.getenv("CAMPUS_METRICS_FILE", "/tmp/sdn_patrick/campus_metrics.json")
DASHBOARD_API = os.getenv("CAMPUS_DASHBOARD_API", "http://127.0.0.1:8080")
RESULTS_DIR = os.getenv("CAMPUS_RESULTS_DIR", os.path.join(os.path.dirname(__file__), "..", "results"))
DATASET_FILE = os.path.join(RESULTS_DIR, "campus_dataset.jsonl")

POLL_INTERVAL_S = 0.2          # 200 ms
MAX_HISTORY = 10_000           # deque capacity
SLO_STAFF_LATENCY_MS = 20.0   # violation threshold
SLO_THROTTLE_IDLE_S = 30.0    # student throttle without reroute
SLO_SWITCH_MIN = 5             # minimum connected switches
SLO_SWITCH_GRACE_S = 15.0     # grace period before switch-loss SLO fires (15s for controller start)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_ms() -> float:
    return time.time() * 1000.0


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_json(url: str, timeout: float = 2.0):
    """Return parsed JSON from *url*, or None on any error."""
    try:
        with urllib_request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _read_json_file(path: str):
    """Read a JSON file, returning None on any error."""
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except Exception:
        return None


def _percentile(data: list, p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


# ── Core evaluator state ──────────────────────────────────────────────────────

class PerformanceEvaluator:
    def __init__(self):
        os.makedirs(RESULTS_DIR, exist_ok=True)

        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=MAX_HISTORY)
        self._events: list = []          # detected transition events
        self._scenario = "baseline"      # current label
        self._running = False
        self._thread: threading.Thread | None = None

        # Per-scenario rolling stats: {scenario: {metric: [values]}}
        self._scenario_values: dict = {}

        # State-transition tracking
        self._prev_reroute: bool | None = None
        self._prev_ddos: bool | None = None
        self._prev_switches: int | None = None
        self._reroute_start_ms: float | None = None
        self._reroute_start_flow_mods: int | None = None
        self._ddos_start_ms: float | None = None
        self._switch_drop_ms: float | None = None

        # SLO timers
        self._throttle_no_reroute_start: float | None = None
        self._switches_low_start: float | None = None

        # Throttle for dataset writes
        self._dataset_fh = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        try:
            self._dataset_fh = open(DATASET_FILE, "a")  # explicit flush per write
            logger.info("Dataset file opened: %s", os.path.abspath(DATASET_FILE))
        except Exception as exc:
            logger.warning("Cannot open dataset file %s: %s", DATASET_FILE, exc)
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="perf-eval")
        self._thread.start()
        logger.info("Performance evaluator started (poll interval %.0f ms)", POLL_INTERVAL_S * 1000)

    def stop(self):
        self._running = False
        if self._dataset_fh:
            self._dataset_fh.close()
            self._dataset_fh = None

    # ── Polling loop ──────────────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            t0 = time.monotonic()
            try:
                self._collect_sample()
            except Exception as exc:
                logger.warning("Sample collection error: %s", exc, exc_info=True)
            elapsed = time.monotonic() - t0
            sleep_s = max(0.0, POLL_INTERVAL_S - elapsed)
            time.sleep(sleep_s)

    def _collect_sample(self):
        # Pull from file and HTTP in parallel using threads for speed
        results = {}

        def _fetch_file():
            results["file"] = _read_json_file(METRICS_FILE) or {}

        def _fetch_metrics():
            results["api_metrics"] = _fetch_json(f"{DASHBOARD_API}/api/metrics") or {}

        def _fetch_dashboard():
            results["api_dashboard"] = _fetch_json(f"{DASHBOARD_API}/api/dashboard") or {}

        threads = [
            threading.Thread(target=_fetch_file, daemon=True),
            threading.Thread(target=_fetch_metrics, daemon=True),
            threading.Thread(target=_fetch_dashboard, daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1.5)

        file_m = results.get("file", {})
        api_m = results.get("api_metrics", {})
        api_d = results.get("api_dashboard", {})

        # Merge: file is ground truth, API supplements
        merged = {**file_m, **api_m}

        ts_ms = _now_ms()
        ts_iso = _iso()

        # Extract key fields
        core_primary = float(merged.get("core_primary_mbps", 0.0))
        core_wifi = float(merged.get("core_wifi_mbps", 0.0))
        _sw_raw = merged.get("connected_switches", 0)
        connected_sw = len(_sw_raw) if isinstance(_sw_raw, list) else int(_sw_raw or 0)
        flow_mods = int(merged.get("controller_flow_mods", 0))
        reroute = bool(merged.get("reroute_active", False))
        ddos = bool(merged.get("ddos_active", False))
        ddos_blocked = int(merged.get("ddos_blocked_flows", 0))
        throttle = bool(merged.get("student_throttle_active", False))

        # Congested ports
        port_utils = merged.get("port_utilization", {}) or {}
        congested = sum(
            1 for v in port_utils.values()
            if isinstance(v, (int, float)) and v > 80
        )

        # --- SLO checks ---
        slo_violations = 0

        # SLO 1: Staff LAN latency > 20ms
        staff_latency = float(merged.get("staff_latency_ms", 0.0))
        if staff_latency > SLO_STAFF_LATENCY_MS:
            slo_violations += 1

        # SLO 2: Student throttle active > 30s without reroute
        if throttle and not reroute:
            if self._throttle_no_reroute_start is None:
                self._throttle_no_reroute_start = ts_ms
            elif (ts_ms - self._throttle_no_reroute_start) > SLO_THROTTLE_IDLE_S * 1000:
                slo_violations += 1
        else:
            self._throttle_no_reroute_start = None

        # SLO 3: connected_switches < 5 for > grace period
        # Skip entirely when there is no controller data at all (metrics file absent / fresh start)
        has_controller_data = bool(merged.get("controller_online") or merged.get("controller_flow_mods"))
        if has_controller_data and connected_sw < SLO_SWITCH_MIN:
            if self._switches_low_start is None:
                self._switches_low_start = ts_ms
            elif (ts_ms - self._switches_low_start) > SLO_SWITCH_GRACE_S * 1000:
                slo_violations += 1
        else:
            self._switches_low_start = None

        # --- State transition detection ---
        convergence_ms = None
        security_ms = None
        failover_ms = None

        # Reroute False→True: start timer, wait for new flow mods
        if self._prev_reroute is not None and not self._prev_reroute and reroute:
            self._reroute_start_ms = ts_ms
            self._reroute_start_flow_mods = flow_mods
            logger.info("Reroute transition detected — measuring convergence time")
            self._emit_event("convergence_start", {"flow_mods_at_start": flow_mods})

        # Once reroute started, check for new flow mods appearing
        if self._reroute_start_ms is not None and reroute:
            if flow_mods > (self._reroute_start_flow_mods or 0):
                convergence_ms = ts_ms - self._reroute_start_ms
                logger.info("Convergence measured: %.1f ms", convergence_ms)
                self._emit_event("convergence", {"duration_ms": convergence_ms})
                self._reroute_start_ms = None
                self._reroute_start_flow_mods = None

        # DDoS False→True: start timer, wait for ddos_blocked_flows > 0
        if self._prev_ddos is not None and not self._prev_ddos and ddos:
            self._ddos_start_ms = ts_ms
            logger.info("DDoS transition detected — measuring security response time")
            self._emit_event("security_start", {})

        if self._ddos_start_ms is not None and ddos:
            if ddos_blocked > 0:
                security_ms = ts_ms - self._ddos_start_ms
                logger.info("Security response measured: %.1f ms", security_ms)
                self._emit_event("security", {"duration_ms": security_ms})
                self._ddos_start_ms = None

        # Switch count drop then recovery
        if self._prev_switches is not None:
            if connected_sw < self._prev_switches:
                self._switch_drop_ms = ts_ms
                logger.info("Switch count dropped to %d — measuring failover", connected_sw)
                self._emit_event("failover_start", {"switches": connected_sw})
            elif self._switch_drop_ms is not None and connected_sw >= (self._prev_switches or 0):
                failover_ms = ts_ms - self._switch_drop_ms
                logger.info("Failover measured: %.1f ms", failover_ms)
                self._emit_event("failover", {"duration_ms": failover_ms})
                self._switch_drop_ms = None

        if slo_violations > 0:
            self._emit_event("slo_violation", {"count": slo_violations, "scenario": self._scenario})

        # Update prev state
        self._prev_reroute = reroute
        self._prev_ddos = ddos
        self._prev_switches = connected_sw

        sample = {
            "ts": ts_ms,
            "ts_iso": ts_iso,
            "scenario": self._scenario,
            "core_primary_mbps": core_primary,
            "core_wifi_mbps": core_wifi,
            "connected_switches": connected_sw,
            "controller_flow_mods": flow_mods,
            "reroute_active": reroute,
            "ddos_active": ddos,
            "ddos_blocked_flows": ddos_blocked,
            "student_throttle_active": throttle,
            "congested_ports_count": congested,
            "convergence_time_ms": convergence_ms,
            "security_response_time_ms": security_ms,
            "failover_time_ms": failover_ms,
            "slo_violations": slo_violations,
        }

        with self._lock:
            self._history.append(sample)
            self._update_scenario_stats(sample)

        # Persist to JSONL
        if self._dataset_fh:
            try:
                line = json.dumps(sample, default=str) + "\n"
                self._dataset_fh.write(line)
                self._dataset_fh.flush()
            except Exception as exc:
                logger.warning("Dataset write error: %s", exc)
                self._dataset_fh = None  # stop retrying on broken handle

    # ── Statistics ────────────────────────────────────────────────────────────

    _NUMERIC_KEYS = [
        "core_primary_mbps", "core_wifi_mbps", "connected_switches",
        "controller_flow_mods", "congested_ports_count", "slo_violations",
    ]

    def _update_scenario_stats(self, sample: dict):
        scenario = sample["scenario"]
        if scenario not in self._scenario_values:
            self._scenario_values[scenario] = {
                "count": 0,
                "reroute_count": 0,
                "ddos_count": 0,
                "slo_violation_total": 0,
            }
            for k in self._NUMERIC_KEYS:
                self._scenario_values[scenario][k] = []

        sv = self._scenario_values[scenario]
        sv["count"] += 1
        if sample["reroute_active"]:
            sv["reroute_count"] += 1
        if sample["ddos_active"]:
            sv["ddos_count"] += 1
        sv["slo_violation_total"] += sample["slo_violations"]
        for k in self._NUMERIC_KEYS:
            sv[k].append(sample[k])

    def _compute_stats(self) -> dict:
        result = {}
        with self._lock:
            for scenario, sv in self._scenario_values.items():
                entry = {
                    "count": sv["count"],
                    "reroute_count": sv["reroute_count"],
                    "ddos_count": sv["ddos_count"],
                    "slo_violations": sv["slo_violation_total"],
                }
                for k in self._NUMERIC_KEYS:
                    vals = sv[k]
                    if vals:
                        entry[k] = {
                            "mean": statistics.mean(vals),
                            "min": min(vals),
                            "max": max(vals),
                            "p95": _percentile(vals, 95),
                        }
                    else:
                        entry[k] = {"mean": 0, "min": 0, "max": 0, "p95": 0}
                result[scenario] = entry
        return result

    # ── Event emission ────────────────────────────────────────────────────────

    def _emit_event(self, event_type: str, data: dict):
        event = {
            "type": event_type,
            "ts": _now_ms(),
            "ts_iso": _iso(),
            "scenario": self._scenario,
            **data,
        }
        with self._lock:
            self._events.append(event)
            # Keep last 1000 events
            if len(self._events) > 1000:
                self._events = self._events[-1000:]

    # ── Public API helpers ────────────────────────────────────────────────────

    def set_scenario(self, label: str):
        with self._lock:
            self._scenario = label
        logger.info("Scenario label set to: %s", label)

    def reset(self):
        with self._lock:
            self._history.clear()
            self._events.clear()
            self._scenario_values.clear()
            self._scenario = "baseline"
        self._prev_reroute = None
        self._prev_ddos = None
        self._prev_switches = None
        self._reroute_start_ms = None
        self._ddos_start_ms = None
        self._switch_drop_ms = None
        self._throttle_no_reroute_start = None
        self._switches_low_start = None
        logger.info("Evaluator state reset")

    @property
    def sample_count(self) -> int:
        return len(self._history)

    def latest_sample(self) -> dict | None:
        with self._lock:
            return dict(self._history[-1]) if self._history else None

    def history(self, limit: int = 100) -> list:
        with self._lock:
            items = list(self._history)
        return items[-limit:]

    def events(self) -> list:
        with self._lock:
            return list(self._events)

    def stats(self) -> dict:
        return self._compute_stats()

    def report_json(self) -> dict:
        with self._lock:
            hist = list(self._history)
            evts = list(self._events)
        return {
            "generated_at": _iso(),
            "total_samples": len(hist),
            "stats": self._compute_stats(),
            "events": evts,
            "history": hist[-500:],  # cap at 500 for JSON response size
        }

    def report_csv(self) -> str:
        with self._lock:
            hist = list(self._history)
        if not hist:
            return ""
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(hist[0].keys()))
        writer.writeheader()
        writer.writerows(hist)
        return out.getvalue()

    def report_md(self) -> str:
        stats = self._compute_stats()
        evts = self.events()
        lines = [
            "# Campus SDN Performance Report",
            f"",
            f"Generated: {_iso()}",
            f"Total samples: {self.sample_count}",
            f"",
            "## Per-Scenario Statistics",
            "",
        ]
        for scenario, s in stats.items():
            lines.append(f"### {scenario}")
            lines.append(f"- Samples: {s['count']}")
            lines.append(f"- Reroute activations: {s['reroute_count']}")
            lines.append(f"- DDoS events: {s['ddos_count']}")
            lines.append(f"- SLO violations: {s['slo_violations']}")
            mbps = s.get("core_primary_mbps", {})
            lines.append(f"- Core Mbps mean/max: {mbps.get('mean', 0):.2f} / {mbps.get('max', 0):.2f}")
            lines.append("")
        lines.append("## Detected Events")
        lines.append("")
        conv_evts = [e for e in evts if e["type"] == "convergence"]
        sec_evts = [e for e in evts if e["type"] == "security"]
        fail_evts = [e for e in evts if e["type"] == "failover"]
        slo_evts = [e for e in evts if e["type"] == "slo_violation"]
        if conv_evts:
            avg_c = statistics.mean(e["duration_ms"] for e in conv_evts)
            lines.append(f"- Convergence events: {len(conv_evts)}, avg {avg_c:.1f} ms")
        if sec_evts:
            avg_s = statistics.mean(e["duration_ms"] for e in sec_evts)
            lines.append(f"- Security response events: {len(sec_evts)}, avg {avg_s:.1f} ms")
        if fail_evts:
            avg_f = statistics.mean(e["duration_ms"] for e in fail_evts)
            lines.append(f"- Failover events: {len(fail_evts)}, avg {avg_f:.1f} ms")
        if slo_evts:
            lines.append(f"- SLO violation events: {len(slo_evts)}")
        return "\n".join(lines)


# ── Flask app ─────────────────────────────────────────────────────────────────

evaluator = PerformanceEvaluator()


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "samples": evaluator.sample_count, "scenario": evaluator._scenario})

    @app.get("/api/current")
    def api_current():
        sample = evaluator.latest_sample()
        if sample is None:
            return jsonify({"error": "no data yet"}), 503
        return jsonify(sample)

    @app.get("/api/history")
    def api_history():
        limit = int(request.args.get("limit", 100))
        limit = max(1, min(limit, MAX_HISTORY))
        return jsonify(evaluator.history(limit))

    @app.get("/api/stats")
    def api_stats():
        return jsonify(evaluator.stats())

    @app.get("/api/events")
    def api_events():
        return jsonify(evaluator.events())

    @app.post("/api/scenario")
    def api_scenario():
        data = request.get_json(force=True, silent=True) or {}
        label = data.get("label", "baseline")
        evaluator.set_scenario(str(label))
        return jsonify({"ok": True, "scenario": label})

    @app.post("/api/reset")
    def api_reset():
        evaluator.reset()
        return jsonify({"ok": True})

    @app.get("/api/report/json")
    def api_report_json():
        return jsonify(evaluator.report_json())

    @app.get("/api/report/csv")
    def api_report_csv():
        csv_data = evaluator.report_csv()
        return Response(csv_data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=campus_dataset.csv"})

    @app.get("/api/report/md")
    def api_report_md():
        return Response(evaluator.report_md(), mimetype="text/plain")

    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Campus SDN Performance Evaluator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9093)
    args = parser.parse_args()

    evaluator.start()

    app = create_app()
    print(f"Performance Evaluator : http://{args.host}:{args.port}")
    print(f"Metrics file          : {METRICS_FILE}")
    print(f"Dashboard API         : {DASHBOARD_API}")
    print(f"Dataset output        : {DATASET_FILE}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
