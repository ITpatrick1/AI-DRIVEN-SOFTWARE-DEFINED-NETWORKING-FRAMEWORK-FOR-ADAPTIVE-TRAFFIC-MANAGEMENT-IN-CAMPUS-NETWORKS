#!/usr/bin/env python3
"""
Continuous Traffic Generator — Tumba College SDN
campus_traffic_gen

Generates realistic background traffic across ALL 24 end devices so the
dashboard topology view shows animated traffic flows.

Traffic patterns:
  - Staff PCs → Server Zone (MIS, Moodle): HTTP-like iperf bursts
  - IT Lab PCs → Server Zone: SSH/DB iperf sessions
  - Student WiFi → Server Zone (Moodle): browsing bursts
  - Intra-zone ping sweeps (all hosts ping each other)
  - Cross-zone periodic pings (every zone pings every other)

All traffic runs via `sudo ip netns exec <host>` which works with Mininet.
"""

import os
import random
import subprocess
import sys
import time
import threading
import signal
import json

# All hosts in the topology
STAFF_HOSTS = [f"h_staff{i}" for i in range(1, 7)]       # 6 hosts
SERVER_HOSTS = ["h_mis", "h_dhcp", "h_auth", "h_moodle"]  # 4 hosts
LAB_HOSTS = [f"h_lab{i}" for i in range(1, 5)]            # 4 hosts
WIFI_HOSTS = [f"h_wifi{i}" for i in range(1, 11)]         # 10 hosts

ALL_HOSTS = STAFF_HOSTS + SERVER_HOSTS + LAB_HOSTS + WIFI_HOSTS

# IP mapping
HOST_IPS = {}
for i in range(1, 7):
    HOST_IPS[f"h_staff{i}"] = f"10.10.0.{i}"
HOST_IPS["h_mis"] = "10.20.0.1"
HOST_IPS["h_dhcp"] = "10.20.0.2"
HOST_IPS["h_auth"] = "10.20.0.3"
HOST_IPS["h_moodle"] = "10.20.0.4"
for i in range(1, 5):
    HOST_IPS[f"h_lab{i}"] = f"10.30.0.{i}"
for i in range(1, 11):
    HOST_IPS[f"h_wifi{i}"] = f"10.40.0.{i}"

running = True
child_procs = []


def signal_handler(sig, frame):
    global running
    running = False
    print("[traffic_gen] Shutting down...")
    for p in child_procs:
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def mn_exec(host, cmd, timeout=10):
    """Execute a command inside a Mininet host namespace."""
    try:
        result = subprocess.run(
            ["sudo", "ip", "netns", "exec", host] + cmd.split(),
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except Exception:
        return False


def mn_exec_bg(host, cmd, timeout=None):
    """Start a background process in a Mininet host namespace."""
    try:
        p = subprocess.Popen(
            ["sudo", "ip", "netns", "exec", host] + cmd.split(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        child_procs.append(p)
        return p
    except Exception:
        return None


def ping_sweep(src_host, dst_hosts, count=1):
    """Ping from src to multiple destinations."""
    for dst in dst_hosts:
        if not running:
            return
        dst_ip = HOST_IPS.get(dst, "")
        if not dst_ip or src_host == dst:
            continue
        mn_exec(src_host, f"ping -c {count} -W 1 {dst_ip}", timeout=5)


def iperf_burst(src_host, dst_ip, duration=3, bandwidth="5M", port=5201):
    """Run a short iperf3 burst from src to dst."""
    if not running:
        return
    # Start iperf3 server on dst (if not already running)
    # We start a quick client connection
    mn_exec(
        src_host,
        f"iperf3 -c {dst_ip} -p {port} -t {duration} -b {bandwidth} --connect-timeout 2000",
        timeout=duration + 5,
    )


def start_iperf_servers():
    """Start iperf3 servers on server zone hosts."""
    ports = {"h_mis": 5201, "h_dhcp": 5202, "h_auth": 5203, "h_moodle": 5204}
    for host, port in ports.items():
        print(f"[traffic_gen] Starting iperf3 server on {host}:{port}")
        mn_exec_bg(host, f"iperf3 -s -p {port} -D")
    time.sleep(1)


def traffic_loop_staff():
    """Staff PCs continuously access MIS and Moodle."""
    print("[traffic_gen] Staff traffic thread started")
    cycle = 0
    while running:
        cycle += 1
        # Staff PCs ping servers
        for host in STAFF_HOSTS:
            if not running:
                return
            # Ping MIS and Moodle
            mn_exec(host, f"ping -c 2 -W 1 {HOST_IPS['h_mis']}", timeout=6)
            mn_exec(host, f"ping -c 2 -W 1 {HOST_IPS['h_moodle']}", timeout=6)
        # Every 3rd cycle: iperf burst from 2 random staff PCs
        if cycle % 3 == 0:
            src = random.choice(STAFF_HOSTS)
            iperf_burst(src, HOST_IPS["h_mis"], duration=3, bandwidth="8M", port=5201)
        # Intra-zone ping
        if cycle % 5 == 0:
            s1, s2 = random.sample(STAFF_HOSTS, 2)
            mn_exec(s1, f"ping -c 3 -W 1 {HOST_IPS[s2]}", timeout=8)
        time.sleep(2)


def traffic_loop_lab():
    """IT Lab PCs access servers and run lab exercises."""
    print("[traffic_gen] Lab traffic thread started")
    cycle = 0
    while running:
        cycle += 1
        # Lab PCs ping auth and DHCP servers
        for host in LAB_HOSTS:
            if not running:
                return
            mn_exec(host, f"ping -c 2 -W 1 {HOST_IPS['h_auth']}", timeout=6)
            mn_exec(host, f"ping -c 1 -W 1 {HOST_IPS['h_dhcp']}", timeout=4)
        # iperf burst from lab to server zone (simulating DB queries)
        if cycle % 2 == 0:
            src = random.choice(LAB_HOSTS)
            iperf_burst(src, HOST_IPS["h_auth"], duration=2, bandwidth="10M", port=5203)
        # Intra-zone lab ping
        if cycle % 4 == 0:
            l1, l2 = random.sample(LAB_HOSTS, 2)
            mn_exec(l1, f"ping -c 2 -W 1 {HOST_IPS[l2]}", timeout=6)
        time.sleep(2)


def traffic_loop_wifi():
    """Student WiFi devices browse Moodle and general traffic."""
    print("[traffic_gen] WiFi traffic thread started")
    cycle = 0
    while running:
        cycle += 1
        # Rotate through WiFi hosts (3 at a time to avoid overload)
        batch_start = ((cycle - 1) * 3) % len(WIFI_HOSTS)
        batch = WIFI_HOSTS[batch_start:batch_start + 3]
        if len(batch) < 3:
            batch += WIFI_HOSTS[:3 - len(batch)]
        for host in batch:
            if not running:
                return
            # Ping Moodle (browsing simulation)
            mn_exec(host, f"ping -c 2 -W 1 {HOST_IPS['h_moodle']}", timeout=6)
        # Every 4th cycle: iperf burst from random wifi to moodle
        if cycle % 4 == 0:
            src = random.choice(WIFI_HOSTS)
            iperf_burst(src, HOST_IPS["h_moodle"], duration=2, bandwidth="3M", port=5204)
        # Intra-zone wifi ping
        if cycle % 6 == 0:
            w1, w2 = random.sample(WIFI_HOSTS, 2)
            mn_exec(w1, f"ping -c 2 -W 1 {HOST_IPS[w2]}", timeout=6)
        time.sleep(3)


def traffic_loop_cross_zone():
    """Cross-zone traffic: staff→lab, lab→wifi, etc."""
    print("[traffic_gen] Cross-zone traffic thread started")
    cross_pairs = [
        (STAFF_HOSTS, LAB_HOSTS),
        (STAFF_HOSTS, WIFI_HOSTS),
        (LAB_HOSTS, WIFI_HOSTS),
    ]
    while running:
        for src_group, dst_group in cross_pairs:
            if not running:
                return
            src = random.choice(src_group)
            dst = random.choice(dst_group)
            mn_exec(src, f"ping -c 3 -W 1 {HOST_IPS[dst]}", timeout=8)
            time.sleep(2)
        # All zones → server zone ping
        for zone_hosts in [STAFF_HOSTS, LAB_HOSTS, WIFI_HOSTS]:
            if not running:
                return
            src = random.choice(zone_hosts)
            srv = random.choice(SERVER_HOSTS)
            mn_exec(src, f"ping -c 2 -W 1 {HOST_IPS[srv]}", timeout=6)
            time.sleep(1)
        time.sleep(5)


def traffic_loop_server_inter():
    """Server-to-server internal traffic (MIS↔Auth, DHCP↔Moodle)."""
    print("[traffic_gen] Server inter-traffic thread started")
    while running:
        # MIS ↔ Auth
        mn_exec("h_mis", f"ping -c 2 -W 1 {HOST_IPS['h_auth']}", timeout=6)
        # DHCP ↔ Moodle
        mn_exec("h_dhcp", f"ping -c 2 -W 1 {HOST_IPS['h_moodle']}", timeout=6)
        # MIS ↔ Moodle
        mn_exec("h_mis", f"ping -c 2 -W 1 {HOST_IPS['h_moodle']}", timeout=6)
        time.sleep(4)


def main():
    print("=" * 60)
    print("  Tumba College SDN — Continuous Traffic Generator")
    print(f"  Generating traffic across {len(ALL_HOSTS)} end devices")
    print("=" * 60)

    # Wait for Mininet to be fully ready
    print("[traffic_gen] Waiting for Mininet network namespaces...")
    ready = False
    for attempt in range(30):
        try:
            result = subprocess.run(
                ["sudo", "ip", "netns", "list"],
                capture_output=True, text=True, timeout=5,
            )
            ns_list = result.stdout
            # Check if at least some host namespaces exist
            if "h_staff1" in ns_list or "h_mis" in ns_list or "h_wifi1" in ns_list:
                ready = True
                break
        except Exception:
            pass
        time.sleep(2)

    if not ready:
        print("[traffic_gen] WARNING: Could not find Mininet namespaces, "
              "trying direct execution anyway...")

    # Start iperf servers on server zone
    start_iperf_servers()

    # Launch traffic threads
    threads = [
        threading.Thread(target=traffic_loop_staff, daemon=True, name="staff_traffic"),
        threading.Thread(target=traffic_loop_lab, daemon=True, name="lab_traffic"),
        threading.Thread(target=traffic_loop_wifi, daemon=True, name="wifi_traffic"),
        threading.Thread(target=traffic_loop_cross_zone, daemon=True, name="cross_zone"),
        threading.Thread(target=traffic_loop_server_inter, daemon=True, name="server_inter"),
    ]

    for t in threads:
        t.start()
        time.sleep(0.5)

    print(f"[traffic_gen] All {len(threads)} traffic threads active")
    print("[traffic_gen] Traffic is now flowing — check the dashboard topology!")

    # Main loop: keep alive and report stats
    try:
        while running:
            time.sleep(10)
            alive = sum(1 for t in threads if t.is_alive())
            print(f"[traffic_gen] {alive}/{len(threads)} threads active")
    except KeyboardInterrupt:
        pass

    print("[traffic_gen] Stopped.")


if __name__ == "__main__":
    main()
