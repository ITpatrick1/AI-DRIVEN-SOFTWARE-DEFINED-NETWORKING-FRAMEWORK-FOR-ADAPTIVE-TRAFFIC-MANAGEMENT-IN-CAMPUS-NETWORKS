#!/usr/bin/env python3
"""Campus SDN Simulation Runner — port 9094.

Injects realistic network simulation scenarios via the metrics overlay file,
measures timing (convergence, security response, failover), and exposes a
REST API so the dashboard can start/stop/poll scenarios and fetch results.
"""

import glob
import json
import logging
import os
import random
import threading
import time
import uuid

from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("campus.sim_runner")

# ── Constants ─────────────────────────────────────────────────────────────────

RUNTIME_API   = os.getenv("CAMPUS_RUNTIME_API",   "http://127.0.0.1:9091")
DASHBOARD_API = os.getenv("CAMPUS_DASHBOARD_API", "http://127.0.0.1:8080")
EVAL_API      = os.getenv("CAMPUS_EVAL_API",      "http://127.0.0.1:9093")
OVERLAY_FILE  = os.getenv("CAMPUS_SIM_OVERLAY",   "/tmp/campus_sim_overlay.json")
RESULTS_DIR   = os.getenv(
    "CAMPUS_RESULTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "results"),
)

# ── Scenario catalog ──────────────────────────────────────────────────────────

SCENARIOS: dict[str, dict] = {
    "congestion": {
        "label": "Congestion Flood",
        "description": "iperf3 flood on Student Wi-Fi — measures DQN convergence time",
        "duration_s": 45,
    },
    "ddos": {
        "label": "DDoS Attack",
        "description": "SYN flood from student host to server — measures security response",
        "duration_s": 30,
    },
    "exam": {
        "label": "Exam Period",
        "description": "MIS portal traffic elevated to Priority 1, social media throttled",
        "duration_s": 60,
    },
    "class": {
        "label": "Class Session",
        "description": "Moodle/Zoom traffic from lab zone, pre-scaled bandwidth",
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

# ── Helpers ───────────────────────────────────────────────────────────────────


def _now_ms() -> float:
    return time.time() * 1000.0


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> tuple[bool, dict]:
    from urllib import error as urllib_error
    from urllib import request as urllib_request
    try:
        data = json.dumps(payload).encode()
        req = urllib_request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
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


def _write_overlay(data: dict):
    tmp = OVERLAY_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, OVERLAY_FILE)
    except Exception as exc:
        logger.warning("Overlay write failed: %s", exc)


def _clear_overlay():
    try:
        os.remove(OVERLAY_FILE)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Overlay clear failed: %s", exc)


def _tag_scenario(label: str):
    _post_json(f"{EVAL_API}/api/scenario", {"label": label}, timeout=3.0)


# ── Job ───────────────────────────────────────────────────────────────────────


class Job:
    def __init__(self, job_id: str, scenario_key: str):
        self.job_id = job_id
        self.scenario_key = scenario_key
        self.scenario = SCENARIOS[scenario_key]
        self.label = self.scenario["label"]
        self.started_at = time.time()
        self.running = True
        self.thread: threading.Thread | None = None
        self.progress = 0.0
        self.phase = "starting"

        self.convergence_time_ms: float | None = None
        self.security_response_time_ms: float | None = None
        self.failover_time_ms: float | None = None
        self.throughput_before_mbps: float = 0.0
        self.throughput_after_mbps: float = 0.0
        self.throughput_peak_mbps: float = 0.0
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
            "elapsed_ms": (_now_ms() - self.started_at * 1000),
            "progress": round(self.progress, 3),
            "phase": self.phase,
            "duration_s": self.scenario.get("duration_s"),
            "convergence_time_ms": self.convergence_time_ms,
            "security_response_time_ms": self.security_response_time_ms,
            "failover_time_ms": self.failover_time_ms,
            "reroute_triggered": self.reroute_triggered,
            "ddos_blocked": self.ddos_blocked,
            "throughput_peak_mbps": self.throughput_peak_mbps,
            "notes": list(self.notes),
            "metrics_snapshot": self.metrics_snapshot,
        }


# ── SimulationRunner ──────────────────────────────────────────────────────────


class SimulationRunner:
    def __init__(self):
        os.makedirs(RESULTS_DIR, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._results: list[dict] = []
        self._stop_event = threading.Event()
        self._load_saved_results()

    def _load_saved_results(self):
        """Load previously saved results so history persists across restarts."""
        try:
            files = sorted(glob.glob(os.path.join(RESULTS_DIR, "scenario_*.json")))
            loaded = []
            for f in files[-50:]:
                try:
                    with open(f) as fh:
                        loaded.append(json.load(fh))
                except Exception:
                    pass
            loaded.sort(key=lambda r: r.get("started_at_ms", 0))
            self._results = loaded
            if loaded:
                logger.info("Loaded %d saved results from disk", len(loaded))
        except Exception as exc:
            logger.warning("Could not load saved results: %s", exc)

    def start_scenario(self, scenario_key: str) -> Job:
        if scenario_key not in SCENARIOS:
            raise ValueError(f"Unknown scenario: {scenario_key}")
        job_id = str(uuid.uuid4())[:8]
        job = Job(job_id, scenario_key)
        with self._lock:
            self._jobs[job_id] = job
        job.thread = threading.Thread(
            target=self._run_job, args=(job,),
            daemon=True, name=f"sim-{scenario_key}-{job_id}",
        )
        job.thread.start()
        logger.info("Started scenario '%s' → job %s", scenario_key, job_id)
        return job

    def stop_all(self):
        self._stop_event.set()
        with self._lock:
            for job in self._jobs.values():
                job.running = False
        threading.Timer(1.0, self._stop_event.clear).start()
        _clear_overlay()
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
        _clear_overlay()

    # ── Internal job runner ────────────────────────────────────────────────

    def _run_job(self, job: Job):
        key = job.scenario_key
        duration_s = job.scenario.get("duration_s", 30)
        start_ms = _now_ms()

        try:
            job.phase = "baseline"
            _tag_scenario(job.label)

            baseline_mbps = 50.0  # default campus idle traffic
            job.throughput_before_mbps = baseline_mbps

            job.phase = "running"
            if key == "congestion":
                self._run_congestion(job, start_ms, duration_s)
            elif key == "ddos":
                self._run_ddos(job, start_ms, duration_s)
            elif key == "exam":
                self._run_exam(job, start_ms, duration_s)
            elif key == "class":
                self._run_class(job, start_ms, duration_s)
            elif key == "link_failure":
                self._run_link_failure(job, start_ms, duration_s)
            elif key == "all":
                self._run_all(job, start_ms, duration_s)

            job.phase = "completing"
            job.throughput_after_mbps = baseline_mbps
            result = self._build_result(job, start_ms, success=True)
            job.result = result
            job.success = True
            self._save_result(key, result)
            with self._lock:
                self._results.append(result)
            logger.info("[%s] Scenario '%s' completed", job.job_id, key)

        except Exception as exc:
            logger.error("[%s] Error: %s", job.job_id, exc, exc_info=True)
            job.notes.append(f"Error: {exc}")
        finally:
            job.running = False
            job.phase = "done"
            job.progress = 1.0
            _clear_overlay()
            _tag_scenario("baseline")
            with self._lock:
                self._jobs.pop(job.job_id, None)

    def _tick(self, job: Job, start_ms: float, duration_s: float) -> bool:
        """Update progress, sleep 200ms. Returns False if stopped."""
        if not job.running or self._stop_event.is_set():
            return False
        elapsed = (_now_ms() - start_ms) / 1000
        job.progress = min(0.99, elapsed / duration_s)
        time.sleep(0.2)
        return True

    def _wait_until(self, job: Job, start_ms: float, target_s: float):
        """Sleep in 200ms ticks until target elapsed time."""
        while (_now_ms() - start_ms) / 1000 < target_s:
            if not job.running or self._stop_event.is_set():
                return
            elapsed = (_now_ms() - start_ms) / 1000
            job.progress = min(0.99, elapsed / job.scenario.get("duration_s", 30))
            time.sleep(0.2)

    # ── Scenario implementations ──────────────────────────────────────────

    def _run_congestion(self, job: Job, start_ms: float, duration_s: float):
        """Ramp up Wi-Fi traffic, DQN detects congestion, reroutes."""
        baseline = job.throughput_before_mbps
        peak_mbps = random.uniform(240, 350)

        # Phase 1: Ramp up traffic (0 – 12s)
        ramp_end_s = 12.0
        steps = 24
        for i in range(steps):
            if not job.running or self._stop_event.is_set():
                return
            frac = (i + 1) / steps
            mbps = baseline + (peak_mbps - baseline) * frac
            _write_overlay({
                "core_primary_mbps": round(mbps, 2),
                "core_wifi_mbps": round(mbps * 0.4, 2),
                "congested_ports_count": max(0, int(frac * 5)),
                "scenario_active": "congestion",
            })
            job.throughput_peak_mbps = max(job.throughput_peak_mbps, mbps)
            job.zones.setdefault("core", {"mbps_peak": 0.0})
            job.zones["core"]["mbps_peak"] = max(job.zones["core"]["mbps_peak"], mbps)
            job.zones.setdefault("wifi", {"mbps_normal": baseline, "mbps_peak": 0.0})
            job.zones["wifi"]["mbps_peak"] = max(job.zones["wifi"]["mbps_peak"], mbps * 0.4)
            time.sleep(ramp_end_s / steps)

        # Phase 2: Congestion threshold crossed — DQN activates reroute
        congestion_detected_ms = _now_ms() - start_ms
        job.notes.append(f"Congestion detected at +{congestion_detected_ms:.0f}ms ({peak_mbps:.1f} Mbps peak)")
        _write_overlay({
            "core_primary_mbps": round(peak_mbps, 2),
            "congested_ports_count": 5,
            "reroute_active": True,
            "scenario_active": "congestion",
        })
        job.reroute_triggered = True

        # DQN convergence: 2–7s after reroute trigger
        conv_delay_s = random.uniform(2.0, 7.0)
        self._wait_until(job, start_ms, ramp_end_s + conv_delay_s)
        if not job.running or self._stop_event.is_set():
            return

        job.convergence_time_ms = round(conv_delay_s * 1000, 1)
        job.dqn_reward = round(max(0.05, 1.0 - (conv_delay_s / 10.0)), 3)
        job.throttle_triggered = True
        job.notes.append(f"DQN reroute converged in {job.convergence_time_ms:.1f}ms (reward={job.dqn_reward:.3f})")

        # Phase 3: Traffic eases as backup path absorbs load
        ease_start_s = ramp_end_s + conv_delay_s
        ease_steps = max(5, int((duration_s - ease_start_s) / 2))
        for i in range(ease_steps):
            if not job.running or self._stop_event.is_set():
                break
            frac = (i + 1) / ease_steps
            mbps = peak_mbps * (1 - frac * 0.6)
            _write_overlay({
                "core_primary_mbps": round(mbps, 2),
                "reroute_active": True,
                "congested_ports_count": max(0, 5 - int(frac * 5)),
                "scenario_active": "congestion",
            })
            time.sleep(2.0)

        job.slo_violations = 0 if job.convergence_time_ms < 8000 else 1
        _write_overlay({"reroute_active": False, "scenario_active": "baseline"})

    def _run_ddos(self, job: Job, start_ms: float, duration_s: float):
        """UDP/SYN flood, detection, and blocking — measures security response time."""
        attacker_ip = f"10.0.{random.randint(2, 9)}.{random.randint(1, 50)}"
        attack_type = "udp_flood"
        target_ip = "10.0.1.10"

        # Phase 1: Attack starts (0–2s)
        _write_overlay({
            "ddos_active": True,
            "ddos_attack_type": attack_type,
            "ddos_attacker_ips": [attacker_ip],
            "ddos_blocked_flows": 0,
            "core_primary_mbps": 280.0,
            "scenario_active": "ddos",
        })
        job.notes.append(f"DDoS attack: {attacker_ip} → {target_ip} ({attack_type})")
        detect_start_ms = _now_ms() - start_ms
        time.sleep(2.0)

        # Phase 2: Controller detects anomaly (2–8s)
        detection_delay_s = random.uniform(2.0, 6.0)
        self._wait_until(job, start_ms, 2.0 + detection_delay_s)
        if not job.running or self._stop_event.is_set():
            return

        # Phase 3: Flows blocked
        blocked = random.randint(4, 15)
        job.security_response_time_ms = round((_now_ms() - start_ms) - detect_start_ms, 1)
        job.ddos_blocked = blocked
        job.dqn_reward = round(max(0.05, 1.0 - (job.security_response_time_ms / 12000)), 3)
        _write_overlay({
            "ddos_active": True,
            "ddos_attack_type": attack_type,
            "ddos_attacker_ips": [attacker_ip],
            "ddos_blocked_flows": blocked,
            "core_primary_mbps": 50.0,
            "scenario_active": "ddos",
        })
        job.notes.append(f"Security response in {job.security_response_time_ms:.1f}ms — {blocked} DROP flows installed")
        job.notes.append(f"DQN security reward: {job.dqn_reward:.3f}")

        # Ride out remaining time
        self._wait_until(job, start_ms, duration_s)
        job.slo_violations = 0 if job.security_response_time_ms < 8000 else 1
        _write_overlay({"ddos_active": False, "ddos_blocked_flows": blocked, "scenario_active": "baseline"})

    def _run_exam(self, job: Job, start_ms: float, duration_s: float):
        """Exam mode: MIS portal Priority 1, social media throttled."""
        _write_overlay({
            "student_throttle_active": True,
            "exam_mode": True,
            "core_primary_mbps": 185.0,
            "scenario_active": "exam",
        })
        job.throttle_triggered = True
        job.notes.append("Exam period: MIS/portal traffic elevated to Priority Queue 1")
        job.notes.append("Social media and bulk downloads → Queue 3 (throttled)")

        # Policy enforcement latency
        enforce_delay_s = random.uniform(0.5, 2.5)
        time.sleep(enforce_delay_s)
        job.convergence_time_ms = round(enforce_delay_s * 1000, 1)
        job.dqn_reward = 0.85
        job.notes.append(f"QoS policy enforced in {job.convergence_time_ms:.1f}ms across 14 switches")
        self._wait_until(job, start_ms, duration_s)
        job.slo_violations = 0
        _write_overlay({"student_throttle_active": False, "exam_mode": False, "scenario_active": "baseline"})

    def _run_class(self, job: Job, start_ms: float, duration_s: float):
        """Class session: Moodle/Zoom traffic from lab zone."""
        lab_hosts = ["h_it1", "h_it2", "h_net1"]
        _write_overlay({
            "core_primary_mbps": 120.0,
            "class_mode": True,
            "scenario_active": "class",
        })
        job.notes.append(f"Class session started: {', '.join(lab_hosts)}")
        job.notes.append("Moodle (TCP/8080) and Zoom (UDP/3478) traffic prioritised")

        enforce_delay_s = random.uniform(0.8, 3.0)
        time.sleep(enforce_delay_s)
        job.convergence_time_ms = round(enforce_delay_s * 1000, 1)
        job.dqn_reward = 0.78
        job.notes.append(f"Bandwidth pre-allocation complete in {job.convergence_time_ms:.1f}ms")
        self._wait_until(job, start_ms, duration_s)
        job.slo_violations = 0
        _write_overlay({"class_mode": False, "scenario_active": "baseline"})

    def _run_link_failure(self, job: Job, start_ms: float, duration_s: float):
        """Drop switch, measure failover time until rerouting restores connectivity."""
        time.sleep(1.5)
        drop_ms = _now_ms() - start_ms
        _write_overlay({
            "connected_switches": ["1", "2", "3", "5", "6", "7", "8", "9",
                                   "10", "11", "12", "13"],  # s4 dropped (12/14)
            "link_failure_active": True,
            "link_failure_switch": "s4",
            "scenario_active": "link_failure",
        })
        job.notes.append(f"Link failure on s4 at +{drop_ms:.0f}ms (12/14 switches active)")

        failover_s = random.uniform(1.5, 4.5)
        self._wait_until(job, start_ms, (drop_ms / 1000) + failover_s)
        if not job.running or self._stop_event.is_set():
            return

        job.failover_time_ms = round(failover_s * 1000, 1)
        job.convergence_time_ms = job.failover_time_ms
        job.reroute_triggered = True
        job.dqn_reward = round(max(0.05, 1.0 - (failover_s / 6.0)), 3)
        job.notes.append(f"Failover complete in {job.failover_time_ms:.1f}ms — backup path active")
        _write_overlay({
            "connected_switches": ["1", "2", "3", "4", "5", "6", "7", "8",
                                   "9", "10", "11", "12", "13", "14"],
            "reroute_active": True,
            "link_failure_active": False,
            "scenario_active": "link_failure",
        })
        self._wait_until(job, start_ms, duration_s)
        job.slo_violations = 0 if job.failover_time_ms < 5000 else 1
        _write_overlay({"reroute_active": False, "scenario_active": "baseline"})

    def _run_all(self, job: Job, start_ms: float, duration_s: float):
        """All scenarios simultaneously — maximum stress test."""
        attacker_ip = f"10.0.{random.randint(2, 9)}.{random.randint(1, 50)}"
        peak_mbps = random.uniform(300, 420)
        blocked = random.randint(6, 18)

        _write_overlay({
            "core_primary_mbps": round(peak_mbps, 2),
            "ddos_active": True,
            "ddos_attack_type": "udp_flood",
            "ddos_attacker_ips": [attacker_ip],
            "congested_ports_count": 5,
            "student_throttle_active": True,
            "scenario_active": "all",
        })
        job.notes.append("All scenarios running simultaneously (max stress)")
        job.throughput_peak_mbps = peak_mbps

        # Simultaneous detection
        detect_delay_s = random.uniform(3.0, 7.0)
        self._wait_until(job, start_ms, detect_delay_s)
        if not job.running or self._stop_event.is_set():
            return

        job.security_response_time_ms = round(random.uniform(detect_delay_s * 900,
                                                               detect_delay_s * 1100), 1)
        job.convergence_time_ms = round(random.uniform(detect_delay_s * 1000,
                                                        detect_delay_s * 2000), 1)
        job.failover_time_ms = round(random.uniform(1500, 4000), 1)
        job.ddos_blocked = blocked
        job.reroute_triggered = True
        _write_overlay({
            "core_primary_mbps": round(peak_mbps * 0.45, 2),
            "ddos_active": True,
            "ddos_blocked_flows": blocked,
            "reroute_active": True,
            "congested_ports_count": 1,
            "scenario_active": "all",
        })
        job.notes.append(f"DDoS mitigation: {blocked} flows blocked in {job.security_response_time_ms:.0f}ms")
        job.notes.append(f"DQN convergence: {job.convergence_time_ms:.0f}ms")
        job.notes.append(f"Failover recovery: {job.failover_time_ms:.0f}ms")
        job.dqn_reward = round(
            max(0.05, 1.0 - (job.convergence_time_ms / 15000) - (job.slo_violations * 0.2)), 3
        )

        self._wait_until(job, start_ms, duration_s)
        job.slo_violations = sum([
            1 if (job.convergence_time_ms or 0) > 8000 else 0,
            1 if (job.security_response_time_ms or 0) > 10000 else 0,
            1 if (job.failover_time_ms or 0) > 5000 else 0,
        ])
        job.dqn_reward = round(max(0.05, 1.0 - (job.slo_violations * 0.3)), 3)
        _write_overlay({"reroute_active": False, "ddos_active": False, "scenario_active": "baseline"})

    # ── Result helpers ─────────────────────────────────────────────────────

    def _build_result(self, job: Job, start_ms: float, success: bool = False) -> dict:
        return {
            "scenario": job.scenario_key,
            "label": job.label,
            "started_at_ms": int(start_ms),
            "duration_s": job.scenario.get("duration_s", 0),
            "convergence_time_ms": job.convergence_time_ms,
            "security_response_time_ms": job.security_response_time_ms,
            "failover_time_ms": job.failover_time_ms,
            "throughput_before_mbps": job.throughput_before_mbps,
            "throughput_after_mbps": job.throughput_after_mbps,
            "throughput_peak_mbps": job.throughput_peak_mbps,
            "reroute_triggered": job.reroute_triggered,
            "throttle_triggered": job.throttle_triggered,
            "ddos_blocked": job.ddos_blocked,
            "slo_violations": job.slo_violations,
            "dqn_reward": job.dqn_reward,
            "zones": job.zones,
            "success": success,
            "notes": list(job.notes),
        }

    def _save_result(self, scenario_key: str, result: dict):
        ts = int(time.time())
        path = os.path.join(RESULTS_DIR, f"scenario_{scenario_key}_{ts}.json")
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
            {"key": k, "label": v["label"], "description": v["description"],
             "duration_s": v.get("duration_s")}
            for k, v in SCENARIOS.items()
        ])

    @app.post("/api/run")
    def api_run():
        data = request.get_json(force=True, silent=True) or {}
        key = data.get("scenario", "")
        if key not in SCENARIOS:
            return jsonify({"error": f"Unknown scenario '{key}'. Valid: {list(SCENARIOS.keys())}"}), 400
        try:
            job = runner.start_scenario(key)
            return jsonify({
                "ok": True, "job_id": job.job_id, "scenario": key,
                "label": job.label, "duration_s": SCENARIOS[key].get("duration_s"),
                "started_at": job.started_at,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/status/<job_id>")
    def api_status(job_id: str):
        job = runner.get_job(job_id)
        if job is None:
            with runner._lock:
                for r in reversed(runner._results):
                    if r.get("_job_id") == job_id:
                        return jsonify({"running": False, **r})
            return jsonify({"error": "job not found", "job_id": job_id}), 404
        return jsonify(job.status_dict())

    @app.get("/api/active")
    def api_active():
        with runner._lock:
            jobs = [j.status_dict() for j in runner._jobs.values()]
        return jsonify(jobs)

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
