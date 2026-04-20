#!/usr/bin/env python3
"""Stage 7 traffic monitoring module (separate component).

This module polls the Ryu REST API (ofctl_rest) and provides:
- live utilization per switch/port
- active flow counts
- warnings for congested ports and drop spikes
- simple traffic trend history

Run:
  python3 examples/traffic_monitor.py \
    --host 127.0.0.1 --port 8090 \
    --ryu-base http://127.0.0.1:8081
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

import requests
from flask import Flask, Response, jsonify


PORT_CAPACITY_MBPS = {
    1: {1: 1000.0, 2: 1000.0, 3: 1000.0, 4: 1000.0, 5: 1000.0},
    2: {1: 1000.0, 2: 100.0, 3: 100.0},
    3: {1: 1000.0, 2: 100.0, 3: 100.0, 4: 1000.0},
    4: {1: 1000.0, 2: 100.0, 3: 100.0},
    5: {1: 1000.0, 2: 50.0, 3: 50.0},
}


def _dpid_key(dpid: Any) -> str:
    try:
        return str(int(dpid))
    except Exception:
        return str(dpid)


def _is_local_port(port_no: int) -> bool:
    # OFPP_LOCAL is commonly 0xfffffffe.
    return port_no >= 0xFFFFFF00


@dataclass
class MonitorConfig:
    ryu_base: str
    poll_interval: float
    warn_util_pct: float
    history_points: int
    request_timeout: float
    state_file: str


class TrafficMonitor:
    def __init__(self, cfg: MonitorConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.thread = None

        self.last_bytes = {}
        self.last_drop = {}
        self.last_rate = {}

        self.history = deque(maxlen=max(20, cfg.history_points))
        self.summary = {
            "ok": False,
            "ts": time.time(),
            "ryu_base": cfg.ryu_base,
            "error": "monitor not started",
            "switch_count": 0,
            "active_flows_total": 0,
            "total_mbps": 0.0,
            "warnings": [],
            "warnings_count": 0,
            "switches": [],
            "top_ports": [],
            "trend": {"direction": "stable", "points": []},
        }

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None

    def get_summary(self):
        with self.lock:
            return dict(self.summary)

    def get_history(self):
        with self.lock:
            return list(self.history)

    def _get_json(self, path: str):
        url = self.cfg.ryu_base.rstrip("/") + path
        r = requests.get(url, timeout=self.cfg.request_timeout)
        r.raise_for_status()
        return r.json()

    def _switches(self):
        raw = self._get_json("/stats/switches")
        if isinstance(raw, list):
            return [int(x) for x in raw]
        return []

    def _ports(self, dpid: int):
        raw = self._get_json(f"/stats/port/{dpid}")
        if not isinstance(raw, dict):
            return []
        rows = raw.get(str(dpid))
        if rows is None:
            rows = raw.get(dpid)
        return rows if isinstance(rows, list) else []

    def _flows(self, dpid: int):
        raw = self._get_json(f"/stats/flow/{dpid}")
        if not isinstance(raw, dict):
            return []
        rows = raw.get(str(dpid))
        if rows is None:
            rows = raw.get(dpid)
        return rows if isinstance(rows, list) else []

    def _capacity(self, dpid: int, port_no: int) -> float:
        return float(PORT_CAPACITY_MBPS.get(int(dpid), {}).get(int(port_no), 0.0))

    def _save_state(self, payload: dict):
        tmp = self.cfg.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp, self.cfg.state_file)
        except Exception:
            pass

    def _trend_direction(self, points):
        if len(points) < 3:
            return "stable"
        x0 = points[0]["total_mbps"]
        x1 = points[-1]["total_mbps"]
        delta = x1 - x0
        if delta > 5.0:
            return "up"
        if delta < -5.0:
            return "down"
        return "stable"

    def _loop(self):
        while not self.stop_ev.is_set():
            self._poll_once()
            self.stop_ev.wait(self.cfg.poll_interval)

    def _poll_once(self):
        now = time.time()
        try:
            switches = self._switches()
        except Exception as exc:
            payload = {
                "ok": False,
                "ts": now,
                "ryu_base": self.cfg.ryu_base,
                "error": str(exc),
                "switch_count": 0,
                "active_flows_total": 0,
                "total_mbps": 0.0,
                "warnings": [{"type": "ryu_rest_unreachable", "detail": str(exc)}],
                "warnings_count": 1,
                "switches": [],
                "top_ports": [],
                "trend": {
                    "direction": self._trend_direction(list(self.history)),
                    "points": list(self.history),
                },
            }
            with self.lock:
                self.summary = payload
            self._save_state(payload)
            return

        warnings = []
        switch_rows = []
        top_ports = []
        total_mbps = 0.0
        total_flows = 0

        for dpid in sorted(switches):
            try:
                ports = self._ports(dpid)
            except Exception:
                ports = []
            try:
                flows = self._flows(dpid)
            except Exception:
                flows = []

            active_flows = [
                f
                for f in flows
                if int(f.get("priority", 0)) > 0
                and int(f.get("packet_count", 0)) >= 0
            ]
            total_flows += len(active_flows)

            util_vals = []
            port_rows = []
            for p in ports:
                try:
                    port_no = int(p.get("port_no", -1))
                except Exception:
                    # Ryu may report OFPP_LOCAL as "LOCAL" string.
                    continue
                if port_no <= 0 or _is_local_port(port_no):
                    continue

                rx = int(p.get("rx_bytes", 0))
                tx = int(p.get("tx_bytes", 0))
                total_bytes = rx + tx
                key = (dpid, port_no)

                prev = self.last_bytes.get(key)
                self.last_bytes[key] = (total_bytes, now)
                mbps = 0.0
                if prev is not None:
                    prev_bytes, prev_ts = prev
                    elapsed = max(now - prev_ts, 1e-6)
                    delta = max(total_bytes - prev_bytes, 0)
                    mbps = (delta * 8.0) / elapsed / 1_000_000.0

                cap = self._capacity(dpid, port_no)
                util = (mbps / cap * 100.0) if cap > 0 else 0.0
                util = max(0.0, min(100.0, util))
                util_vals.append(util)
                total_mbps += mbps

                prev_rate = float(self.last_rate.get(key, mbps))
                self.last_rate[key] = mbps
                if mbps > prev_rate + 1.0:
                    rate_trend = "up"
                elif mbps < prev_rate - 1.0:
                    rate_trend = "down"
                else:
                    rate_trend = "stable"

                drops = int(p.get("rx_dropped", 0)) + int(p.get("tx_dropped", 0))
                prev_drop = int(self.last_drop.get(key, drops))
                self.last_drop[key] = drops
                drop_delta = max(0, drops - prev_drop)

                row = {
                    "dpid": int(dpid),
                    "port": int(port_no),
                    "mbps": round(mbps, 3),
                    "util_pct": round(util, 3),
                    "capacity_mbps": round(cap, 3),
                    "trend": rate_trend,
                    "drop_delta": int(drop_delta),
                }
                port_rows.append(row)
                top_ports.append(row)

                if util >= self.cfg.warn_util_pct:
                    warnings.append(
                        {
                            "type": "high_utilization",
                            "dpid": int(dpid),
                            "port": int(port_no),
                            "util_pct": round(util, 2),
                            "mbps": round(mbps, 2),
                            "threshold_pct": self.cfg.warn_util_pct,
                        }
                    )
                if drop_delta > 0:
                    warnings.append(
                        {
                            "type": "drop_spike",
                            "dpid": int(dpid),
                            "port": int(port_no),
                            "drop_delta": int(drop_delta),
                        }
                    )

            switch_rows.append(
                {
                    "dpid": int(dpid),
                    "active_flows": len(active_flows),
                    "avg_util_pct": round(sum(util_vals) / len(util_vals), 3)
                    if util_vals
                    else 0.0,
                    "ports": port_rows,
                }
            )

        top_ports.sort(key=lambda x: x.get("util_pct", 0.0), reverse=True)
        top_ports = top_ports[:16]

        hist_item = {
            "ts": now,
            "total_mbps": round(total_mbps, 3),
            "active_flows_total": int(total_flows),
            "warnings_count": len(warnings),
        }
        self.history.append(hist_item)
        hist_points = list(self.history)

        payload = {
            "ok": True,
            "ts": now,
            "ryu_base": self.cfg.ryu_base,
            "switch_count": len(switches),
            "active_flows_total": int(total_flows),
            "total_mbps": round(total_mbps, 3),
            "warnings": warnings,
            "warnings_count": len(warnings),
            "switches": switch_rows,
            "top_ports": top_ports,
            "trend": {
                "direction": self._trend_direction(hist_points),
                "points": hist_points,
            },
        }
        with self.lock:
            self.summary = payload
        self._save_state(payload)


def app_html():
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Campus Traffic Monitor</title>
  <style>
    :root {
      --bg: #0b1220;
      --panel: #111a2e;
      --muted: #99a8c2;
      --text: #eaf0ff;
      --ok: #37c887;
      --warn: #f0b04d;
      --bad: #ff6f61;
      --line: #1f2d4f;
      --cyan: #5ed6ff;
    }
    body { margin:0; background:linear-gradient(135deg,#0b1220,#0e1b33 55%,#132747); color:var(--text); font-family: "Segoe UI", Tahoma, sans-serif; }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 18px; }
    .head { display:flex; justify-content:space-between; align-items:center; gap:10px; }
    .h1 { font-size: 22px; font-weight: 700; letter-spacing: 0.2px; }
    .sub { color: var(--muted); font-size: 13px; }
    .grid { display:grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap:12px; margin-top:14px; }
    .card { background: rgba(17,26,46,0.95); border:1px solid var(--line); border-radius: 12px; padding: 12px; }
    .k { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
    .v { font-size: 24px; font-weight: 800; margin-top: 6px; }
    .v.ok { color: var(--ok); } .v.warn { color: var(--warn); } .v.bad { color: var(--bad); }
    .cols { display:grid; grid-template-columns: 1.2fr 1fr; gap:12px; margin-top:12px; }
    .tbl { width:100%; border-collapse: collapse; font-size: 13px; }
    .tbl th,.tbl td { padding: 8px; border-bottom: 1px solid var(--line); text-align:left; }
    .tbl th { color: var(--muted); font-weight:600; }
    .warnbox { max-height: 240px; overflow:auto; }
    .warn { border-left: 3px solid var(--warn); background: rgba(240,176,77,0.12); padding:8px 10px; margin-bottom:8px; border-radius:8px; font-size:13px; }
    .trend { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: var(--cyan); font-size: 12px; white-space: pre-wrap; }
    @media (max-width: 980px){ .grid{grid-template-columns:repeat(2,minmax(0,1fr));} .cols{grid-template-columns:1fr;} }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <div>
        <div class="h1">Campus Traffic Monitoring Module</div>
        <div id="sub" class="sub">Polling Ryu REST API...</div>
      </div>
    </div>

    <div class="grid">
      <div class="card"><div class="k">Switches</div><div id="sw" class="v">-</div></div>
      <div class="card"><div class="k">Active Flows</div><div id="flows" class="v">-</div></div>
      <div class="card"><div class="k">Total Throughput</div><div id="mbps" class="v">-</div></div>
      <div class="card"><div class="k">Warnings</div><div id="warns" class="v">-</div></div>
    </div>

    <div class="cols">
      <div class="card">
        <div class="k">Top Port Utilization</div>
        <table class="tbl">
          <thead><tr><th>Switch</th><th>Port</th><th>Rate</th><th>Util</th><th>Trend</th></tr></thead>
          <tbody id="ports"></tbody>
        </table>
      </div>
      <div class="card">
        <div class="k">Warnings</div>
        <div id="warnbox" class="warnbox"></div>
      </div>
    </div>

    <div class="card" style="margin-top:12px;">
      <div class="k">Traffic Trend (last samples)</div>
      <div id="trend" class="trend">-</div>
    </div>
  </div>

  <script>
    function cls(v){
      if (v >= 80) return 'bad';
      if (v >= 50) return 'warn';
      return 'ok';
    }
    async function refresh(){
      const r = await fetch('/api/summary');
      const d = await r.json();
      document.getElementById('sub').textContent =
        `Ryu: ${d.ryu_base || '-'} | Last update: ${new Date((d.ts || 0) * 1000).toLocaleTimeString()} | Trend: ${(d.trend||{}).direction || '-'}`;
      document.getElementById('sw').textContent = d.switch_count ?? '-';
      document.getElementById('flows').textContent = d.active_flows_total ?? '-';
      document.getElementById('mbps').textContent = `${Number(d.total_mbps || 0).toFixed(1)} Mb/s`;
      document.getElementById('warns').textContent = d.warnings_count ?? 0;
      document.getElementById('warns').className = 'v ' + cls(Number(d.warnings_count || 0) * 20);

      const ports = d.top_ports || [];
      document.getElementById('ports').innerHTML = ports.map(p =>
        `<tr><td>s${p.dpid}</td><td>${p.port}</td><td>${Number(p.mbps).toFixed(2)} Mb/s</td><td>${Number(p.util_pct).toFixed(1)}%</td><td>${p.trend}</td></tr>`
      ).join('') || '<tr><td colspan="5">No data</td></tr>';

      const warnings = d.warnings || [];
      document.getElementById('warnbox').innerHTML = warnings.map(w => {
        if (w.type === 'high_utilization') return `<div class="warn">High utilization: s${w.dpid}-p${w.port} at ${w.util_pct}% (${w.mbps} Mb/s)</div>`;
        if (w.type === 'drop_spike') return `<div class="warn">Drop spike: s${w.dpid}-p${w.port}, +${w.drop_delta} drops</div>`;
        return `<div class="warn">${JSON.stringify(w)}</div>`;
      }).join('') || '<div class="sub">No active warnings</div>';

      const pts = ((d.trend || {}).points || []).slice(-12);
      document.getElementById('trend').textContent = pts.map(p =>
        `${new Date((p.ts||0)*1000).toLocaleTimeString()}  total=${Number(p.total_mbps||0).toFixed(1)}Mb/s  flows=${p.active_flows_total||0}  warns=${p.warnings_count||0}`
      ).join('\\n') || '-';
    }
    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--ryu-base", default="http://127.0.0.1:8081")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--warn-util-pct", type=float, default=80.0)
    parser.add_argument("--history-points", type=int, default=180)
    parser.add_argument("--request-timeout", type=float, default=2.0)
    parser.add_argument(
        "--state-file",
        default="/tmp/campus_traffic_monitor.json",
        help="JSON output file for latest monitor state",
    )
    args = parser.parse_args()

    cfg = MonitorConfig(
        ryu_base=args.ryu_base.rstrip("/"),
        poll_interval=max(0.5, float(args.poll_interval)),
        warn_util_pct=max(1.0, float(args.warn_util_pct)),
        history_points=max(20, int(args.history_points)),
        request_timeout=max(0.5, float(args.request_timeout)),
        state_file=args.state_file,
    )
    monitor = TrafficMonitor(cfg)
    monitor.start()

    app = Flask(__name__)

    @app.get("/")
    def index():
        return Response(app_html(), mimetype="text/html")

    @app.get("/health")
    def health():
        s = monitor.get_summary()
        return jsonify(
            {
                "ok": bool(s.get("ok")),
                "ts": s.get("ts"),
                "ryu_base": s.get("ryu_base"),
                "error": s.get("error"),
            }
        )

    @app.get("/api/summary")
    def api_summary():
        return jsonify(monitor.get_summary())

    @app.get("/api/history")
    def api_history():
        return jsonify({"history": monitor.get_history()})

    print(
        "Traffic monitor listening on http://%s:%s (Ryu REST: %s)"
        % (args.host, args.port, cfg.ryu_base)
    )
    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
