#!/usr/bin/env python3

"""External ML-hook stub for campus controller.

This is a lightweight policy agent that reads controller metrics and writes
actions to CAMPUS_ML_ACTION_FILE (default /tmp/campus_ml_action.json).
"""

import argparse
import json
import os
import time


def read_metrics(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_action(path, action):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(action, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def run_loop(metrics_file, action_file, interval, max_high, min_high, emergency_mbps):
    print("ML stub started")
    print("  metrics:", metrics_file)
    print("  action :", action_file)
    high = 40.0
    low = 20.0
    while True:
        m = read_metrics(metrics_file)
        if not m:
            time.sleep(interval)
            continue
        mbps = float(m.get("core_primary_mbps", 0.0))
        active = bool(m.get("reroute_active", False))

        # Very simple adaptation heuristic (placeholder for DQN/MARL agent):
        # - tighten thresholds if link is busy
        # - relax thresholds if link is idle
        if mbps > high * 0.8:
            high = max(min_high, high - 3.0)
        elif mbps < low * 0.6:
            high = min(max_high, high + 2.0)
        low = max(5.0, high * 0.5)

        force = mbps >= emergency_mbps
        action = {
            "congest_high_mbps": round(high, 2),
            "congest_low_mbps": round(low, 2),
            "force_reroute": force,
            "note": "ml_policy_stub",
            "seen_reroute_active": active,
            "seen_core_primary_mbps": round(mbps, 2),
            "ts": time.time(),
        }
        write_action(action_file, action)
        print(
            f"wrote action high={action['congest_high_mbps']} "
            f"low={action['congest_low_mbps']} force={force} mbps={mbps:.2f}"
        )
        time.sleep(interval)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--metrics-file", default="/tmp/campus_metrics.json")
    p.add_argument("--action-file", default="/tmp/campus_ml_action.json")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--max-high", type=float, default=120.0)
    p.add_argument("--min-high", type=float, default=20.0)
    p.add_argument("--emergency-mbps", type=float, default=180.0)
    args = p.parse_args()
    run_loop(
        metrics_file=args.metrics_file,
        action_file=args.action_file,
        interval=args.interval,
        max_high=args.max_high,
        min_high=args.min_high,
        emergency_mbps=args.emergency_mbps,
    )


if __name__ == "__main__":
    main()
