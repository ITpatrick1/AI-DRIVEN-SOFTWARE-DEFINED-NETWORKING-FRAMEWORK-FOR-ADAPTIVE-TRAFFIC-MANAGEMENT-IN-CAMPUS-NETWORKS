#!/usr/bin/env python3
"""Campus SDN Simulation Runner — port 9094.

Triggers simulation scenarios via the Mininet runtime API and the campus
dashboard API, collects per-scenario metrics, and exposes a REST API so the
dashboard can start/stop scenarios and retrieve results.
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("campus.sim_runner")

# ── Constants ─────────────────────────────────────────────────────────────────

RUNTIME_API = os.getenv("CAMPUS_RUNTIME_API", "http://127.0.0.1:9091")
DASHBOARD_API = os.getenv("CAMPUS_DASHBOARD_API", "http://127.0.0.1:8080")
EVAL_API = os.getenv("CAMPUS_EVAL_API", "http://127.0.0.1:9093")
RESULTS_DIR = os.getenv(
    "CAMPUS_RESULTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "results"),
)

SCENARIO_POLL_INTERVAL_S = 0.1  # 100 ms

# ── Scenario definitions ──────────────────────────────────────────────────────

SCENARIOS: dict[str, dict] = {
    "congestion": {
        "label": "Congestion Flood",
        "description": "iperf3 flood on Student Wi-Fi — measures DQN convergence time",
        "clients": ["h_wifi1", "h_wifi2"],
        "duration_s": 45,
    },
    "ddos": {
        "label": "DDoS Attack",
        "description": "SYN flood from student host to server — measures security response",
        "attacker": "h_wifi1",
        "target": "10.0.0.100",
        "duration_s": 30,
    },
    "exam": {
        "label": "Exam Period",
        "description": "MIS portal traffic elevated to Priority 1, social media throttled",
        "exam_hosts": ["h_wifi1", "h_wifi2"],
        "social_hosts": ["h_net1"],
        "duration_s": 60,
    },
    "class": {
        "label": "Class Session",
        "description": "Moodle/Zoom traffic from lab zone, pre-scaled bandwidth",
        "lab_hosts": ["h_it1", "h_it2", "h_net1"],
        "duration_s": 60,
    },
    "link_failure": {
        "label": "Link Failure",
        "description": "Simulated link failure — measures failover/rerouting time",
        "duration_s": 20,
    },
    "all": {
        "label": "All Scenarios",
        "description": "Congestion + DDoS + Exam + Class simultaneously",
        "duration_s": 90,
    },
}

# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> tuple[bool, dict]:
    """POST JSON to *url*. Returns (ok, response_dict)."""
    try:
        data = json.dumps(payload).encode()
        req = urllib_request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read())
    except urllib_error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {"error": str(exc)}
        return False, body
    except Exception as exc:
        return False, {"error": str(exc)}


def _get_json(url: str, timeout: float = 5.0) -> dict | None:
    """GET JSON from *url*, return None on any error."""
    try:
        with urllib_request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _tag_scenario(label: str):
    """Tell the performance evaluator which scenario is running."""
    _post_json(f"{EVAL_API}/api/scenario", {"label": label}, timeout=3.0)


def _snapshot_metrics() -> dict:
    """Return latest metrics from dashboard API."""
    return _get_json(f"{DASHBOARD_API}/api/metrics") or {}


def _now_ms() -> float:
    return time.time() * 1000.0


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Job / result state ────────────────────────────────────────────────────────


class Job:
    def __init__(self, job_id: str, scenario_key: str):
        self.job_id = job_id
        self.scenario_key = scenario_key
        self.scenario = SCENARIOS[scenario_key]
        self.label = self.scenario["label"]
        self.started_at = time.time()
        self.running = True
        self.thread: threading.Thread | None = None

        # Measured values
        self.convergence_time_ms: float | None = None
        self.security_response_time_ms: float | None = None
        self.throughput_before_mbps: float = 0.0
        self.throughput_after_mbps: float = 0.0
        self.reroute_triggered = False
        self.throttle_triggered = False
        self.ddos_blocked = 0
        self.slo_violations = 0
        self.dqn_reward: float | None = None
        self.zones: dict = {}
        self.notes: list[str] = []
        self.success = False
        self.metrics_snapshot: dict = {}
        self.result: dict | None = None

    def status_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "scenario": self.scenario_key,
            "label": self.label,
            "running": self.running,
            "started_at": self.started_at,
            "duration_s": self.scenario.get("duration_s"),
            "convergence_time_ms": self.convergence_time_ms,
            "security_response_time_ms": self.security_response_time_ms,
            "reroute_triggered": self.reroute_triggered,
            "ddos_blocked": self.ddos_blocked,
            "metrics_snapshot": self.metrics_snapshot,
        }


# ── SimulationRunner ──────────────────────────────────────────────────────────


class SimulationRunner:
    def __init__(self):
        os.makedirs(RESULTS_DIR, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}      # job_id → Job (active)
        self._results: list[dict] = []       # completed result summaries
        self._stop_event = threading.Event()

    # ── Scenario execution ─────────────────────────────────────────────────

    def start_scenario(self, scenario_key: str) -> Job:
        if scenario_key not in SCENARIOS:
            raise ValueError(f"Unknown scenario: {scenario_key}")

        job_id = str(uuid.uuid4())[:8]
        job = Job(job_id, scenario_key)

        with self._lock:
            self._jobs[job_id] = job

        job.thread = threading.Thread(
            target=self._run_job,
            args=(job,),
            daemon=True,
            name=f"sim-{scenario_key}-{job_id}",
        )
        job.thread.start()
        logger.info("Started scenario '%s' → job %s", scenario_key, job_id)
        return job

    def stop_all(self):
        """Signal all running jobs to stop."""
        self._stop_event.set()
        with self._lock:
            for job in self._jobs.values():
                job.running = False
        # Reset stop event so future runs work
        threading.Timer(1.0, self._stop_event.clear).start()
        logger.info("Stop-all issued")

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def results(self) -> list:
        with self._lock:
            return list(self._results)

    def reset(self):
        with self._lock:
            self._jobs.clear()
            self._results.clear()

    # ── Internal job runner ────────────────────────────────────────────────

    def _run_job(self, job: Job):
        key = job.scenario_key
        scenario = job.scenario
        duration_s = scenario.get("duration_s", 30)

        try:
            # 1. Tag the performance evaluator
            _tag_scenario(scenario["label"])

            # 2. Capture baseline metrics
            baseline = _snapshot_metrics()
            job.throughput_before_mbps = float(baseline.get("core_primary_mbps", 0.0))

            # 3. Start the appropriate stress/action via dashboard API
            self._launch_scenario(job, scenario, baseline)

            start_ms = _now_ms()

            # 4. Poll for state changes
            self._monitor_scenario(job, start_ms, duration_s)

            # 5. Stop stress
            self._stop_scenario(job, scenario)

            # 6. Wait for network to stabilize
            logger.info("[%s] Waiting 5s for network stabilization", job.job_id)
            for _ in range(50):
                if not job.running:
                    break
                time.sleep(0.1)

            # 7. Final metrics snapshot
            final = _snapshot_metrics()
            job.throughput_after_mbps = float(final.get("core_primary_mbps", 0.0))
            job.metrics_snapshot = final

            # 8. Build result summary
            result = self._build_result(job, start_ms)
            job.result = result

            # 9. Persist result file
            self._save_result(key, result)

            with self._lock:
                self._results.append(result)

            job.success = True
            logger.info("[%s] Scenario '%s' completed successfully", job.job_id, key)

        except Exception as exc:
            logger.error("[%s] Scenario '%s' error: %s", job.job_id, key, exc, exc_info=True)
            job.notes.append(f"Error: {exc}")
        finally:
            job.running = False
            # 10. Reset evaluator label to baseline
            _tag_scenario("baseline")
            # Remove from active jobs dict
            with self._lock:
                self._jobs.pop(job.job_id, None)

    def _launch_scenario(self, job: Job, scenario: dict, baseline: dict):
        key = job.scenario_key

        if key == "congestion":
            clients = scenario.get("clients", ["h_wifi1", "h_wifi2"])
            ok, resp = _post_json(
                f"{DASHBOARD_API}/api/actions/start-stress",
                {"seconds": scenario["duration_s"] + 10, "clients": clients, "reverse_download": True},
            )
            if not ok:
                logger.warning("[%s] start-stress failed: %s", job.job_id, resp)
            else:
                job.notes.append(f"iperf3 flood started on: {', '.join(clients)}")

        elif key == "ddos":
            attacker = scenario.get("attacker", "h_wifi1")
            target = scenario.get("target", "10.0.0.100")
            ok, resp = _post_json(
                f"{DASHBOARD_API}/api/actions/start-attack",
                {"attacker": attacker, "target": target,
                 "duration": scenario["duration_s"], "attack_type": "udp_flood"},
            )
            if not ok:
                logger.warning("[%s] start-attack failed: %s", job.job_id, resp)
            else:
                job.notes.append(f"DDoS attack launched: {attacker} → {target}")

        elif key == "exam":
            exam_hosts = scenario.get("exam_hosts", ["h_wifi1", "h_wifi2"])
            # Elevate MIS/exam traffic priority via Ryu action
            ok, resp = _post_json(
                f"{DASHBOARD_API}/api/actions/start-stress",
                {"seconds": scenario["duration_s"], "clients": exam_hosts,
                 "reverse_download": False, "profile": "exam"},
            )
            if not ok:
                logger.warning("[%s] exam start-stress failed: %s", job.job_id, resp)
            job.notes.append("Exam period: MIS portal priority elevated")

        elif key == "class":
            lab_hosts = scenario.get("lab_hosts", ["h_it1", "h_it2", "h_net1"])
            ok, resp = _post_json(
                f"{DASHBOARD_API}/api/actions/start-stress",
                {"seconds": scenario["duration_s"], "clients": lab_hosts,
                 "reverse_download": True, "profile": "class"},
            )
            if not ok:
                logger.warning("[%s] class start-stress failed: %s", job.job_id, resp)
            job.notes.append(f"Class session: Moodle/Zoom from {', '.join(lab_hosts)}")

        elif key == "link_failure":
            # Use runtime API to trigger link failure
            ok, resp = _post_json(
                f"{RUNTIME_API}/link_failure",
                {"switch": "s1", "port": 1, "duration": scenario["duration_s"]},
                timeout=10.0,
            )
            if not ok:
                # Fallback: toggle stress as a proxy signal
                _post_json(
                    f"{DASHBOARD_API}/api/actions/start-stress",
                    {"seconds": scenario["duration_s"], "clients": ["h_wifi1"]},
                )
                job.notes.append("Link failure simulated via stress (runtime API unavailable)")
            else:
                job.notes.append("Link failure injected on s1 port 1")

        elif key == "all":
            # Run all sub-scenarios
            clients = ["h_wifi1", "h_wifi2", "h_it1", "h_it2", "h_net1"]
            ok, resp = _post_json(
                f"{DASHBOARD_API}/api/actions/start-stress",
                {"seconds": scenario["duration_s"], "clients": clients, "reverse_download": True},
            )
            if not ok:
                logger.warning("[%s] all-scenario start-stress failed: %s", job.job_id, resp)
            ok2, resp2 = _post_json(
                f"{DASHBOARD_API}/api/actions/start-attack",
                {"attacker": "h_wifi1", "target": "10.0.0.100",
                 "duration": scenario["duration_s"], "attack_type": "udp_flood"},
            )
            if not ok2:
                logger.warning("[%s] all-scenario start-attack failed: %s", job.job_id, resp2)
            job.notes.append("All scenarios running simultaneously")

    def _monitor_scenario(self, job: Job, start_ms: float, duration_s: float):
        deadline = start_ms + duration_s * 1000

        prev_reroute = False
        prev_ddos_blocked = 0
        prev_switches = None
        reroute_first_ms: float | None = None
        ddos_detect_ms: float | None = None
        switch_drop_ms: float | None = None

        while _now_ms() < deadline and job.running and not self._stop_event.is_set():
            m = _snapshot_metrics()
            now_ms = _now_ms()
            elapsed_ms = now_ms - start_ms

            reroute = bool(m.get("reroute_active", False))
            throttle = bool(m.get("student_throttle_active", False))
            ddos_active = bool(m.get("ddos_active", False))
            ddos_blocked = int(m.get("ddos_blocked_flows", 0))
            _sw = m.get("connected_switches", 0)
            switches = len(_sw) if isinstance(_sw, list) else int(_sw or 0)
            flow_mods = int(m.get("controller_flow_mods", 0))

            # Track reroute first activation
            if reroute and not job.reroute_triggered:
                job.reroute_triggered = True
                reroute_first_ms = elapsed_ms
                job.notes.append(f"Reroute triggered at +{elapsed_ms:.0f}ms")
                logger.info("[%s] Reroute at +%.0fms", job.job_id, elapsed_ms)

            if throttle and not job.throttle_triggered:
                job.throttle_triggered = True
                job.notes.append("Student throttle activated")

            # Convergence: time from reroute activation to new flow mods
            if job.reroute_triggered and job.convergence_time_ms is None:
                if flow_mods > int(m.get("controller_flow_mods", 0)):
                    if reroute_first_ms is not None:
                        job.convergence_time_ms = elapsed_ms - reroute_first_ms
                        job.notes.append(f"Convergence: {job.convergence_time_ms:.1f}ms")

            # DDoS security response
            if ddos_active and ddos_detect_ms is None:
                ddos_detect_ms = elapsed_ms
            if ddos_detect_ms is not None and ddos_blocked > 0 and job.security_response_time_ms is None:
                job.security_response_time_ms = elapsed_ms - ddos_detect_ms
                job.notes.append(f"Security response: {job.security_response_time_ms:.1f}ms")

            job.ddos_blocked = max(job.ddos_blocked, ddos_blocked)

            # Failover timing (link failure scenario)
            if prev_switches is not None:
                if switches < prev_switches and switch_drop_ms is None:
                    switch_drop_ms = elapsed_ms
                elif switch_drop_ms is not None and switches >= prev_switches:
                    failover_ms = elapsed_ms - switch_drop_ms
                    job.notes.append(f"Failover: {failover_ms:.1f}ms")
                    switch_drop_ms = None

            # Throughput tracking for zones
            wifi_mbps = float(m.get("core_wifi_mbps", 0.0))
            core_mbps = float(m.get("core_primary_mbps", 0.0))
            if "wifi" not in job.zones:
                job.zones["wifi"] = {"mbps_peak": 0.0, "mbps_normal": job.throughput_before_mbps}
            job.zones["wifi"]["mbps_peak"] = max(job.zones["wifi"]["mbps_peak"], wifi_mbps)
            if "core" not in job.zones:
                job.zones["core"] = {"mbps_peak": 0.0}
            job.zones["core"]["mbps_peak"] = max(job.zones["core"]["mbps_peak"], core_mbps)

            prev_reroute = reroute
            prev_ddos_blocked = ddos_blocked
            prev_switches = switches

            job.metrics_snapshot = m
            time.sleep(SCENARIO_POLL_INTERVAL_S)

    def _stop_scenario(self, job: Job, scenario: dict):
        key = job.scenario_key
        if key in ("congestion", "exam", "class", "all"):
            ok, resp = _post_json(f"{DASHBOARD_API}/api/actions/stop-stress", {})
            if not ok:
                logger.warning("[%s] stop-stress failed: %s", job.job_id, resp)
        if key in ("ddos", "all"):
            ok, resp = _post_json(f"{DASHBOARD_API}/api/actions/stop-attack", {})
            if not ok:
                logger.warning("[%s] stop-attack failed: %s", job.job_id, resp)
        if key == "link_failure":
            # Runtime will auto-restore; send stop just in case
            _post_json(f"{DASHBOARD_API}/api/actions/stop-stress", {}, timeout=5.0)

    def _build_result(self, job: Job, start_ms: float) -> dict:
        scenario = job.scenario
        return {
            "scenario": job.scenario_key,
            "label": job.label,
            "started_at_ms": int(start_ms),
            "duration_s": scenario.get("duration_s", 0),
            "convergence_time_ms": job.convergence_time_ms,
            "security_response_time_ms": job.security_response_time_ms,
            "throughput_before_mbps": job.throughput_before_mbps,
            "throughput_after_mbps": job.throughput_after_mbps,
            "reroute_triggered": job.reroute_triggered,
            "throttle_triggered": job.throttle_triggered,
            "ddos_blocked": job.ddos_blocked,
            "slo_violations": job.slo_violations,
            "dqn_reward": job.dqn_reward,
            "zones": job.zones,
            "success": job.success,
            "notes": job.notes,
        }

    def _save_result(self, scenario_key: str, result: dict):
        ts = int(time.time())
        filename = f"scenario_{scenario_key}_{ts}.json"
        path = os.path.join(RESULTS_DIR, filename)
        try:
            with open(path, "w") as fh:
                json.dump(result, fh, indent=2)
            logger.info("Result saved: %s", path)
        except Exception as exc:
            logger.warning("Failed to save result: %s", exc)


# ── Flask app ─────────────────────────────────────────────────────────────────

runner = SimulationRunner()


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        with runner._lock:
            active = len(runner._jobs)
        return jsonify({"ok": True, "active_jobs": active, "scenarios": list(SCENARIOS.keys())})

    @app.get("/api/scenarios")
    def api_scenarios():
        return jsonify([
            {
                "key": k,
                "label": v["label"],
                "description": v["description"],
                "duration_s": v.get("duration_s"),
            }
            for k, v in SCENARIOS.items()
        ])

    @app.post("/api/run")
    def api_run():
        data = request.get_json(force=True, silent=True) or {}
        scenario_key = data.get("scenario", "")
        if scenario_key not in SCENARIOS:
            return jsonify({"error": f"Unknown scenario '{scenario_key}'. Valid: {list(SCENARIOS.keys())}"}), 400
        try:
            job = runner.start_scenario(scenario_key)
            return jsonify({
                "ok": True,
                "job_id": job.job_id,
                "scenario": scenario_key,
                "label": job.label,
                "duration_s": SCENARIOS[scenario_key].get("duration_s"),
                "started_at": job.started_at,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/status/<job_id>")
    def api_status(job_id: str):
        job = runner.get_job(job_id)
        if job is None:
            # Check completed results
            with runner._lock:
                for r in reversed(runner._results):
                    if r.get("_job_id") == job_id:
                        return jsonify({"running": False, **r})
            return jsonify({"error": "job not found", "job_id": job_id}), 404
        return jsonify(job.status_dict())

    @app.post("/api/stop")
    def api_stop():
        runner.stop_all()
        return jsonify({"ok": True})

    @app.get("/api/results")
    def api_results():
        return jsonify(runner.results())

    @app.post("/api/reset")
    def api_reset():
        runner.reset()
        return jsonify({"ok": True})

    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Campus SDN Simulation Runner")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9094)
    args = parser.parse_args()

    app = create_app()
    print(f"Simulation Runner  : http://{args.host}:{args.port}")
    print(f"Runtime API        : {RUNTIME_API}")
    print(f"Dashboard API      : {DASHBOARD_API}")
    print(f"Evaluator API      : {EVAL_API}")
    print(f"Results dir        : {RESULTS_DIR}")
    print("Scenarios available:", ", ".join(SCENARIOS.keys()))
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
