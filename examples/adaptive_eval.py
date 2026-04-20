#!/usr/bin/env python3

"""Automated baseline vs congestion evaluation for campus SDN demo."""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mininet.log import setLogLevel
from campus_topology import _configure_priority_qos, create_campus_net

PRIMARY_SERVER_IP = "10.0.0.100"
PRIMARY_SERVER_IFACE = "h_server-eth0"
BACKUP_SERVER_IFACE = "h_server_b-eth0"
METRICS_PATH = os.getenv("CAMPUS_METRICS_FILE", "/tmp/campus_metrics.json")
EVENTS_PATH = os.getenv("CAMPUS_EVENTS_FILE", "/tmp/campus_policy_events.jsonl")
PRIMARY_BOTTLENECK_MBPS = max(
    10, int(float(os.getenv("CAMPUS_EVAL_PRIMARY_BOTTLENECK_MBPS", "35")))
)
STRESS_DURATION_S = max(12, int(float(os.getenv("CAMPUS_EVAL_STRESS_SECONDS", "20"))))
PROBE_PORT = int(os.getenv("CAMPUS_EVAL_PROBE_PORT", "5204"))
NOISE_PORTS = (
    int(os.getenv("CAMPUS_EVAL_NOISE_PORT_1", "5201")),
    int(os.getenv("CAMPUS_EVAL_NOISE_PORT_2", "5202")),
)


def parse_ping_stats(text):
    out = {
        "tx": 0,
        "rx": 0,
        "loss_pct": 100.0,
        "rtt_min_ms": None,
        "rtt_avg_ms": None,
        "rtt_max_ms": None,
        "rtt_mdev_ms": None,
    }
    m = re.search(r"(\d+)\s+packets transmitted,\s+(\d+)\s+received,\s+([\d.]+)% packet loss", text)
    if m:
        out["tx"] = int(m.group(1))
        out["rx"] = int(m.group(2))
        out["loss_pct"] = float(m.group(3))
    r = re.search(
        r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", text
    )
    if r:
        out["rtt_min_ms"] = float(r.group(1))
        out["rtt_avg_ms"] = float(r.group(2))
        out["rtt_max_ms"] = float(r.group(3))
        out["rtt_mdev_ms"] = float(r.group(4))
    return out


def parse_iperf3_rate_mbps(text):
    try:
        data = json.loads(text)
        end = data.get("end", {})
        rate = (
            end.get("sum_received", {}).get("bits_per_second")
            or end.get("sum_sent", {}).get("bits_per_second")
            or 0
        )
        return float(rate) / 1_000_000.0
    except Exception:
        return None


def parse_iperf3_result(text):
    result = {
        "throughput_mbps": None,
        "error": None,
    }
    try:
        data = json.loads(text)
    except Exception:
        stripped = text.strip()
        if stripped:
            result["error"] = stripped.splitlines()[-1][:300]
        return result

    result["throughput_mbps"] = parse_iperf3_rate_mbps(text)
    err = data.get("error")
    if err:
        result["error"] = str(err)
    return result


def read_json_file(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def read_event_log(path):
    events = []
    if not os.path.exists(path):
        return events
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return events


def remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def count_backup_icmp_lines(text):
    n = 0
    for line in text.splitlines():
        if "ICMP echo request" in line or "ICMP echo reply" in line:
            n += 1
    return n


def delivery_pct(stats):
    tx = int(stats.get("tx", 0) or 0)
    rx = int(stats.get("rx", 0) or 0)
    if tx <= 0:
        return 0.0
    return round((float(rx) / float(tx)) * 100.0, 3)


def wait_for_reroute_active(metrics_path, timeout_s=15.0, poll_s=0.5):
    start = time.time()
    while (time.time() - start) < timeout_s:
        m = read_json_file(metrics_path)
        if isinstance(m, dict) and bool(m.get("reroute_active", False)):
            return True, round(time.time() - start, 3)
        time.sleep(poll_s)
    return False, round(time.time() - start, 3)


def wait_for_reroute_state(metrics_path, desired_state, timeout_s=15.0, poll_s=0.5):
    start = time.time()
    while (time.time() - start) < timeout_s:
        m = read_json_file(metrics_path)
        if isinstance(m, dict) and bool(m.get("reroute_active", False)) == bool(desired_state):
            return True, round(time.time() - start, 3)
        time.sleep(poll_s)
    return False, round(time.time() - start, 3)


def wait_for_core_primary_load(metrics_path, min_mbps, timeout_s=12.0, poll_s=0.5):
    start = time.time()
    while (time.time() - start) < timeout_s:
        m = read_json_file(metrics_path)
        if isinstance(m, dict):
            try:
                if float(m.get("core_primary_mbps", 0.0) or 0.0) >= float(min_mbps):
                    return True, round(time.time() - start, 3)
            except Exception:
                pass
        time.sleep(poll_s)
    return False, round(time.time() - start, 3)


def first_event_ts(events, event_name, min_ts=0.0):
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("event") != event_name:
            continue
        try:
            ts = float(e.get("ts", 0.0))
        except Exception:
            return None
        if ts >= float(min_ts):
            return ts
    return None


def apply_primary_server_bottleneck(server_host, rate_mbps):
    cmd = (
        "tc qdisc replace dev {iface} root "
        "tbf rate {rate}mbit burst 64kbit latency 250ms"
    ).format(iface=PRIMARY_SERVER_IFACE, rate=int(rate_mbps))
    out = server_host.cmd(cmd)
    if "RTNETLINK answers" in out or "Error" in out:
        raise RuntimeError("Failed to apply evaluation bottleneck: %s" % out.strip())
    return server_host.cmd("tc qdisc show dev %s" % PRIMARY_SERVER_IFACE).strip()


def start_iperf3_servers(server_host, ports):
    server_host.cmd("pkill -9 -f 'iperf3 -s -p ' >/dev/null 2>&1 || true")
    for port in ports:
        server_host.cmd(
            "iperf3 -s -p {port} >/tmp/iperf3_server_{port}.log 2>&1 &".format(port=port)
        )
    time.sleep(1.0)


def stop_eval_processes(hosts):
    for host in hosts:
        host.cmd("pkill -9 -f 'iperf3 -s -p ' >/dev/null 2>&1 || true")
        host.cmd("pkill -9 -f 'iperf3 -c 10.0.0.100' >/dev/null 2>&1 || true")
        host.cmd(
            "pkill -9 -f 'tcpdump -U -n -l -i h_server_b-eth0 icmp' >/dev/null 2>&1 || true"
        )


def run_eval(results_dir, tag):
    os.makedirs(results_dir, exist_ok=True)
    net, hosts = create_campus_net()
    try:
        net.start()
        _configure_priority_qos()
        disconnected = [sw.name for sw in net.switches if not sw.connected()]
        if disconnected:
            raise RuntimeError(
                "Controller disconnected: " + ", ".join(disconnected) +
                " (start controller first)"
            )
        time.sleep(2.0)

        h_it1 = hosts["h_it1"]
        h_it2 = hosts["h_it2"]
        h_wifi1 = hosts["h_wifi1"]
        h_wifi2 = hosts["h_wifi2"]
        h_server = hosts["h_server"]
        h_server_b = hosts["h_server_b"]

        pingall_loss_pct = float(net.pingAll(timeout="1"))
        bottleneck_qdisc = apply_primary_server_bottleneck(
            h_server, PRIMARY_BOTTLENECK_MBPS
        )
        start_iperf3_servers(h_server, NOISE_PORTS + (PROBE_PORT,))
        initial_metrics = read_json_file(METRICS_PATH) or {}

        # Baseline measurements.
        baseline_ping_raw = h_it2.cmd(
            "ping -c 10 -i 0.2 {ip}".format(ip=PRIMARY_SERVER_IP)
        )
        baseline_iperf = parse_iperf3_result(
            h_it2.cmd(
                "iperf3 -J -c {ip} -p {port} -t 5 -R".format(
                    ip=PRIMARY_SERVER_IP, port=PROBE_PORT
                )
            )
        )
        baseline = {
            "scenario": "baseline",
            "throughput_mbps": baseline_iperf["throughput_mbps"],
            "iperf_error": baseline_iperf["error"],
        }
        baseline.update(parse_ping_stats(baseline_ping_raw))
        baseline["packet_delivery_pct"] = delivery_pct(baseline)

        reroute_cleared, reroute_clear_wait_s = wait_for_reroute_state(
            METRICS_PATH, False, timeout_s=12.0, poll_s=0.5
        )
        if not reroute_cleared:
            raise RuntimeError(
                "Adaptive policy did not clear before the stress test window started."
            )

        # Congestion phase: throttle-sensitive Wi-Fi downloads compete with the
        # IT download probe on a shared bottlenecked primary server link.
        remove_file(EVENTS_PATH)
        h_server_b.cmd("rm -f /tmp/backup_icmp.log")
        h_server_b.cmd(
            "timeout {timeout_s} tcpdump -U -n -l -i {iface} icmp "
            ">/tmp/backup_icmp.log 2>&1 &".format(
                timeout_s=STRESS_DURATION_S + 10,
                iface=BACKUP_SERVER_IFACE,
            )
        )
        stress_start_ts = time.time()
        h_wifi1.cmd(
            "iperf3 -c {ip} -p {port} -t {seconds} -R >/tmp/stage11_noise_wifi1.log 2>&1 &".format(
                ip=PRIMARY_SERVER_IP, port=NOISE_PORTS[0], seconds=STRESS_DURATION_S
            )
        )
        h_wifi2.cmd(
            "iperf3 -c {ip} -p {port} -t {seconds} -R >/tmp/stage11_noise_wifi2.log 2>&1 &".format(
                ip=PRIMARY_SERVER_IP, port=NOISE_PORTS[1], seconds=STRESS_DURATION_S
            )
        )
        h_it1.cmd(
            "ping -c 16 -i 0.25 {ip} >/tmp/stage11_it1_ping.log 2>&1 &".format(
                ip=PRIMARY_SERVER_IP
            )
        )
        load_seen, load_wait_s = wait_for_core_primary_load(
            METRICS_PATH,
            min_mbps=max(10.0, round(PRIMARY_BOTTLENECK_MBPS * 0.55, 3)),
            timeout_s=10.0,
            poll_s=0.5,
        )
        reroute_seen, reroute_wait_s = wait_for_reroute_active(
            METRICS_PATH, timeout_s=8.0, poll_s=0.5
        )
        if reroute_seen:
            h_it1.cmd(
                "ping -c 8 -i 0.2 {ip} >/tmp/stage11_reroute_confirm_ping.log 2>&1".format(
                    ip=PRIMARY_SERVER_IP
                )
            )

        congest_ping_raw = h_it2.cmd(
            "ping -c 18 -i 0.2 {ip}".format(ip=PRIMARY_SERVER_IP)
        )
        congest_iperf = parse_iperf3_result(
            h_it2.cmd(
                "iperf3 -J -c {ip} -p {port} -t 5 -R".format(
                    ip=PRIMARY_SERVER_IP, port=PROBE_PORT
                )
            )
        )
        h_server_b.cmd(
            "pkill -INT -f 'tcpdump -U -n -l -i h_server_b-eth0 icmp' >/dev/null 2>&1 || true"
        )
        time.sleep(1.5)

        backup_log = h_server_b.cmd("cat /tmp/backup_icmp.log")
        congest = {
            "scenario": "congestion",
            "throughput_mbps": congest_iperf["throughput_mbps"],
            "iperf_error": congest_iperf["error"],
            "backup_icmp_packets": count_backup_icmp_lines(backup_log),
            "load_seen_during_probe": bool(load_seen),
            "load_wait_s": load_wait_s,
            "reroute_seen_during_probe": bool(reroute_seen),
            "reroute_wait_s": reroute_wait_s,
        }
        congest.update(parse_ping_stats(congest_ping_raw))
        congest["packet_delivery_pct"] = delivery_pct(congest)

        # Controller-side snapshot evidence.
        metrics = read_json_file(METRICS_PATH)
        events = read_event_log(EVENTS_PATH)
        stress_events = []
        for event in events:
            try:
                if float(event.get("ts", 0.0) or 0.0) >= stress_start_ts:
                    stress_events.append(event)
            except Exception:
                continue

        activated = [e for e in stress_events if e.get("event") == "policy_activated"]
        deactivated = [e for e in stress_events if e.get("event") == "policy_deactivated"]

        first_congest_ts = first_event_ts(
            stress_events, "throughput_congestion_on", stress_start_ts
        )
        if first_congest_ts is None:
            first_congest_ts = first_event_ts(
                stress_events, "port_congestion_on", stress_start_ts
            )
        if first_congest_ts is None and load_seen:
            first_congest_ts = round(stress_start_ts + float(load_wait_s), 6)
        first_activated_ts = first_event_ts(stress_events, "policy_activated", stress_start_ts)
        first_deactivated_ts = first_event_ts(
            stress_events, "policy_deactivated", stress_start_ts
        )
        congestion_response_s = None
        if (
            isinstance(first_congest_ts, float)
            and isinstance(first_activated_ts, float)
            and first_activated_ts >= first_congest_ts
        ):
            congestion_response_s = round(first_activated_ts - first_congest_ts, 3)

        baseline_thr = float(baseline.get("throughput_mbps") or 0.0)
        congest_thr = float(congest.get("throughput_mbps") or 0.0)
        throughput_delta_mbps = round(congest_thr - baseline_thr, 3)
        throughput_delta_pct = None
        if baseline_thr > 0:
            throughput_delta_pct = round((throughput_delta_mbps / baseline_thr) * 100.0, 3)

        latency_delta_ms = None
        if baseline.get("rtt_avg_ms") is not None and congest.get("rtt_avg_ms") is not None:
            latency_delta_ms = round(
                float(congest.get("rtt_avg_ms") or 0.0)
                - float(baseline.get("rtt_avg_ms") or 0.0),
                3,
            )

        summary = {
            "tag": tag,
            "ts": datetime.now(timezone.utc).isoformat(),
            "connectivity": {
                "pingall_loss_pct": round(pingall_loss_pct, 3),
                "pingall_passed": bool(pingall_loss_pct == 0.0),
            },
            "controller_config": {
                "congest_high_mbps": initial_metrics.get("congest_high_mbps"),
                "congest_low_mbps": initial_metrics.get("congest_low_mbps"),
                "port_congest_high_pct": initial_metrics.get("port_congest_high_pct"),
                "port_congest_low_pct": initial_metrics.get("port_congest_low_pct"),
            },
            "stress_profile": {
                "primary_server_bottleneck_mbps": PRIMARY_BOTTLENECK_MBPS,
                "shared_probe_port": PROBE_PORT,
                "wifi_congestion_ports": list(NOISE_PORTS),
                "stress_duration_s": STRESS_DURATION_S,
                "bottleneck_qdisc": bottleneck_qdisc,
                "reroute_cleared_before_stress": bool(reroute_cleared),
                "reroute_clear_wait_s": reroute_clear_wait_s,
            },
            "baseline": baseline,
            "congestion": congest,
            "controller_metrics": metrics,
            "policy_events": {
                "activated_count": len(activated),
                "deactivated_count": len(deactivated),
                "first_congestion_ts": first_congest_ts,
                "first_activated_ts": first_activated_ts,
                "first_deactivated_ts": first_deactivated_ts,
                "congestion_response_s": congestion_response_s,
            },
            "derived_metrics": {
                "throughput_delta_mbps": throughput_delta_mbps,
                "throughput_delta_pct": throughput_delta_pct,
                "baseline_packet_delivery_pct": baseline["packet_delivery_pct"],
                "congestion_packet_delivery_pct": congest["packet_delivery_pct"],
                "congestion_loss_pct": float(congest.get("loss_pct", 100.0)),
                "congestion_rtt_avg_ms": float(congest.get("rtt_avg_ms") or 0.0),
                "latency_delta_ms": latency_delta_ms,
                "reroute_evidence_packets": int(congest.get("backup_icmp_packets", 0)),
                "reroute_observed": bool(
                    congest.get("backup_icmp_packets", 0) > 0
                    or congest.get("reroute_seen_during_probe", False)
                ),
            },
            "notes": [
                "pingall_loss_pct records the explicit topology connectivity check for this run.",
                "The primary server link is deliberately rate-limited so congestion is repeatable.",
                "Reverse-mode iperf3 downloads model the Wi-Fi film-download workload.",
                "backup_icmp_packets > 0 is evidence that reroute path carried ICMP.",
                "policy_activated_count > 0 indicates adaptive policy engaged.",
                "congestion_response_s is measured from first port_congestion_on to policy_activated.",
            ],
        }

        json_path = os.path.join(results_dir, f"adaptive_eval_{tag}.json")
        csv_path = os.path.join(results_dir, f"adaptive_eval_{tag}.csv")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, sort_keys=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "scenario",
                    "throughput_mbps",
                    "packet_delivery_pct",
                    "loss_pct",
                    "rtt_avg_ms",
                    "tx",
                    "rx",
                    "backup_icmp_packets",
                    "reroute_seen_during_probe",
                    "iperf_error",
                ],
            )
            w.writeheader()
            w.writerow(
                {
                    "scenario": baseline["scenario"],
                    "throughput_mbps": baseline["throughput_mbps"],
                    "packet_delivery_pct": baseline["packet_delivery_pct"],
                    "loss_pct": baseline["loss_pct"],
                    "rtt_avg_ms": baseline["rtt_avg_ms"],
                    "tx": baseline["tx"],
                    "rx": baseline["rx"],
                    "backup_icmp_packets": 0,
                    "reroute_seen_during_probe": False,
                    "iperf_error": baseline["iperf_error"],
                }
            )
            w.writerow(
                {
                    "scenario": congest["scenario"],
                    "throughput_mbps": congest["throughput_mbps"],
                    "packet_delivery_pct": congest["packet_delivery_pct"],
                    "loss_pct": congest["loss_pct"],
                    "rtt_avg_ms": congest["rtt_avg_ms"],
                    "tx": congest["tx"],
                    "rx": congest["rx"],
                    "backup_icmp_packets": congest["backup_icmp_packets"],
                    "reroute_seen_during_probe": congest["reroute_seen_during_probe"],
                    "iperf_error": congest["iperf_error"],
                }
            )

        print(f"Evaluation JSON: {json_path}")
        print(f"Evaluation CSV : {csv_path}")
        print(
            "Policy activations:",
            len(activated),
            "| Backup ICMP packets:",
            congest["backup_icmp_packets"],
            "| Throughput baseline->congestion (Mbps):",
            baseline["throughput_mbps"],
            "->",
            congest["throughput_mbps"],
        )
    finally:
        stop_eval_processes(list(hosts.values()))
        net.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", default="/home/patrick/mininet/results")
    parser.add_argument(
        "--tag",
        default=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
    )
    args = parser.parse_args()

    setLogLevel("info")
    run_eval(args.results_dir, args.tag)


if __name__ == "__main__":
    main()
