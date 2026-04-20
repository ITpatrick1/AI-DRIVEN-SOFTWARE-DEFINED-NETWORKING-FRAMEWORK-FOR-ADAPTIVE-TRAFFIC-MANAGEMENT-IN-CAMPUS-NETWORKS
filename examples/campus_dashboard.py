#!/usr/bin/env python3

"""Campus SDN Network Manager UI + API server.

Serves:
- /                 : full frontend UI
- /api/metrics      : controller metrics JSON
- /api/events       : policy event stream
- /api/topology     : live topology + derived utilization view model
- /api/flows        : flow dump for selected switch (best effort)
- /api/devices      : runtime host inventory/create (GET/POST)
- /api/devices/<id> : device detail / edit / remove proxy (GET/PUT/DELETE)
- /api/network/settings : live congestion threshold settings (GET/PUT)
- /api/actions/pingall : live pingall trigger endpoint
- /api/operations   : runtime operation log and latest pingall details
- /api/actions/start-stress : launch Wi-Fi film download stress
- /api/actions/stop-stress  : stop stress workload
"""

import argparse
from collections import deque
import glob
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from flask import Flask, Response, jsonify, request, send_file


DEVICE_CATEGORY_LABELS = {
    "user_device": "User device",
    "iot": "IoT device",
    "service_node": "Service node",
    "lab_device": "Lab device",
}

DEFAULT_HOST_CATEGORIES = {
    "h_it1": "lab_device",
    "h_it2": "lab_device",
    "h_net1": "lab_device",
    "h_net2": "lab_device",
    "h_staff1": "user_device",
    "h_staff2": "user_device",
    "h_wifi1": "user_device",
    "h_wifi2": "user_device",
    "h_server": "service_node",
    "h_server_b": "service_node",
}

SEGMENT_TRAFFIC_PROFILES = [
    {
        "key": "laboratory_block",
        "label": "Laboratory block",
        "description": "Combined traffic from the IT and Networking laboratory access ports.",
        "color": "#58d6ff",
        "ports": ((2, 2), (2, 3), (3, 2), (3, 3)),
    },
    {
        "key": "staff_lan",
        "label": "Staff LAN",
        "description": "Staff access-segment traffic across both office-facing edge ports.",
        "color": "#6ee7b7",
        "ports": ((4, 2), (4, 3)),
    },
    {
        "key": "student_wifi",
        "label": "Student Wi-Fi",
        "description": "Student Wi-Fi traffic on the throttled access ports used during congestion tests.",
        "color": "#f0a73b",
        "ports": ((5, 2), (5, 3)),
    },
    {
        "key": "primary_service",
        "label": "Primary service",
        "description": "Protected traffic on the direct primary-server service path.",
        "color": "#8aa1bf",
        "ports": ((1, 5),),
    },
    {
        "key": "backup_service",
        "label": "Backup service",
        "description": "Protected traffic on the standby backup-server service path.",
        "color": "#ffb347",
        "ports": ((3, 4),),
    },
]


def _normalize_device_category(category_text):
    key = re.sub(r"[^a-z0-9]+", "_", str(category_text or "").strip().lower()).strip("_")
    return key if key in DEVICE_CATEGORY_LABELS else "user_device"


def _device_category_label(category_key):
    return DEVICE_CATEGORY_LABELS.get(
        _normalize_device_category(category_key), DEVICE_CATEGORY_LABELS["user_device"]
    )


def _default_host_category(node_id):
    node_id = str(node_id or "").strip()
    if node_id in DEFAULT_HOST_CATEGORIES:
        return DEFAULT_HOST_CATEGORIES[node_id]
    if node_id.startswith("h_server"):
        return "service_node"
    if node_id.startswith("h_it") or node_id.startswith("h_net"):
        return "lab_device"
    return "user_device"


def _write_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Campus SDN — Traffic Operations Dashboard</title>
  <style>
    :root {
      --bg0: #07111d;
      --bg1: #0d1827;
      --bg2: #122237;
      --bg3: #19314b;
      --card: rgba(10, 19, 31, 0.84);
      --card-strong: rgba(13, 24, 38, 0.94);
      --card-soft: rgba(255, 255, 255, 0.045);
      --line: rgba(151, 181, 213, 0.14);
      --line-strong: rgba(151, 181, 213, 0.28);
      --txt: #edf5ff;
      --muted: #9db2cb;
      --good: #29c983;
      --warn: #ffb347;
      --bad: #ff6b6b;
      --accent: #63d6ff;
      --accent-soft: rgba(99, 214, 255, 0.14);
      --accent-green: #6ee7b7;
      --shadow: 0 28px 70px rgba(2, 9, 18, 0.46);
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      position: relative;
      margin: 0;
      color: var(--txt);
      background:
        radial-gradient(980px 540px at 8% -6%, rgba(99,214,255,0.18) 0%, rgba(99,214,255,0.05) 38%, transparent 62%),
        radial-gradient(820px 460px at 100% 0%, rgba(110,231,183,0.12) 0%, transparent 58%),
        linear-gradient(180deg, #091420 0%, #07111a 100%);
      font-family: "Avenir Next", "IBM Plex Sans", "Nunito Sans", "Segoe UI", sans-serif;
      min-height: 100vh;
      overflow-x: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.07;
      background-image:
        linear-gradient(rgba(255,255,255,0.65) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.65) 1px, transparent 1px);
      background-size: 72px 72px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.5), transparent 78%);
    }
    body::after {
      content: "";
      position: fixed;
      right: -120px;
      bottom: -120px;
      width: 360px;
      height: 360px;
      pointer-events: none;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(99,214,255,0.18) 0%, rgba(99,214,255,0) 70%);
      filter: blur(8px);
    }
    .page {
      position: relative;
      max-width: 1480px;
      margin: 22px auto 28px;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 28px;
      background: linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.018));
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .page::before {
      content: "";
      position: absolute;
      inset: 0 0 auto 0;
      height: 170px;
      pointer-events: none;
      background: linear-gradient(135deg, rgba(99,214,255,0.12), rgba(110,231,183,0.03) 52%, transparent 76%);
    }
    .header {
      position: relative;
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.95fr);
      gap: 18px;
      align-items: stretch;
      margin-bottom: 18px;
    }
    .heroIntro,
    .commandWell {
      position: relative;
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 18px 20px;
      background: linear-gradient(180deg, rgba(17, 28, 44, 0.90), rgba(11, 20, 31, 0.86));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 18px 42px rgba(3, 10, 19, 0.35);
    }
    .heroIntro {
      min-height: 184px;
    }
    .eyebrow {
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }
    .title {
      max-width: 14ch;
      margin-top: 6px;
      font-size: clamp(30px, 3vw, 42px);
      font-weight: 800;
      line-height: 1.02;
      letter-spacing: -0.04em;
    }
    .subtitle {
      max-width: 62ch;
      margin-top: 10px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
    }
    .heroMarks {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }
    .heroMark {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 11px;
      border: 1px solid rgba(151,181,213,0.16);
      border-radius: 999px;
      background: rgba(255,255,255,0.045);
      color: var(--txt);
      font-size: 12px;
      font-weight: 700;
    }
    .heroMark::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--accent-green);
      box-shadow: 0 0 0 4px rgba(110,231,183,0.12);
    }
    .badges,
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .badges {
      margin-top: 16px;
    }
    .badge {
      border: 1px solid var(--line-strong);
      border-radius: 999px;
      padding: 8px 14px;
      background: linear-gradient(180deg, rgba(255,255,255,0.07), rgba(255,255,255,0.03));
      color: var(--txt);
      font-size: 12px;
      font-weight: 700;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.05);
    }
    .commandWell {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 14px;
    }
    .commandWellLabel {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .btn {
      border: 1px solid var(--line-strong);
      border-radius: 12px;
      padding: 10px 14px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      color: var(--txt);
      background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
      transition: transform .14s ease, border-color .2s ease, box-shadow .2s ease, background .2s ease;
    }
    .btn:hover {
      transform: translateY(-1px);
      border-color: rgba(99,214,255,0.55);
      box-shadow: 0 10px 18px rgba(0,0,0,0.18);
    }
    .btn.danger {
      border-color: rgba(255,107,107,0.36);
      background: linear-gradient(180deg, rgba(255,107,107,0.22), rgba(255,107,107,0.08));
      color: #ffe4e4;
    }
    .btn[disabled] {
      opacity: 0.45;
      cursor: not-allowed;
      transform: none;
    }
    #btnStartStress {
      background: linear-gradient(180deg, rgba(99,214,255,0.24), rgba(99,214,255,0.10));
      border-color: rgba(99,214,255,0.42);
    }
    #btnPingall {
      background: linear-gradient(180deg, rgba(110,231,183,0.20), rgba(110,231,183,0.08));
      border-color: rgba(110,231,183,0.34);
    }
    #btnStopStress {
      border-color: rgba(255,107,107,0.36);
    }
    .actions select {
      min-height: 44px;
      border: 1px solid var(--line-strong);
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 13px;
      color: var(--txt);
      background: rgba(255,255,255,0.05);
      min-width: 190px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
    }
    .scenarioPanel {
      position: relative;
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 18px;
      margin-bottom: 16px;
      background: linear-gradient(180deg, rgba(15, 26, 41, 0.90), rgba(11, 20, 31, 0.86));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.05);
      overflow: hidden;
    }
    .scenarioPanel::before {
      content: "";
      position: absolute;
      inset: 0 auto auto 0;
      width: 230px;
      height: 150px;
      background: radial-gradient(circle at top left, rgba(99,214,255,0.18), rgba(99,214,255,0) 72%);
      pointer-events: none;
    }
    .scenarioPanelHead {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }
    .scenarioPanelTitle {
      font-size: 22px;
      font-weight: 800;
      color: var(--txt);
      margin-top: 4px;
      letter-spacing: -0.02em;
    }
    .scenarioGrid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }
    .scenarioCard {
      border: 1px solid rgba(151,181,213,0.14);
      border-radius: 18px;
      padding: 14px;
      background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.025));
      cursor: pointer;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
      transition: border-color .2s ease, transform .14s ease, box-shadow .2s ease, background .2s ease;
    }
    .scenarioCard:hover {
      transform: translateY(-2px);
      border-color: rgba(99,214,255,0.36);
      box-shadow: 0 16px 28px rgba(5, 12, 21, 0.24);
    }
    .scenarioCard.active {
      border-color: rgba(99,214,255,0.56);
      box-shadow: inset 0 0 0 1px rgba(99,214,255,0.20), 0 16px 28px rgba(5, 12, 21, 0.24);
      background: linear-gradient(180deg, rgba(99,214,255,0.14), rgba(255,255,255,0.03));
    }
    .scenarioCardTitle {
      font-size: 15px;
      font-weight: 800;
      color: var(--txt);
      margin-bottom: 6px;
    }
    .scenarioCardDesc {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.55;
      min-height: 52px;
    }
    .scenarioCardMeta {
      display: inline-flex;
      align-items: center;
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 999px;
      padding: 4px 9px;
      margin-top: 10px;
      font-size: 11px;
      font-weight: 800;
      color: var(--accent);
      background: rgba(255,255,255,0.05);
    }
    .scenarioCardActions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }
    .scenarioCard [data-scenario-select] {
      background: rgba(255,255,255,0.05);
    }
    .scenarioCard [data-scenario-run] {
      background: linear-gradient(180deg, rgba(99,214,255,0.24), rgba(99,214,255,0.10));
      border-color: rgba(99,214,255,0.42);
    }
    .summaryStrip {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .summaryCard {
      position: relative;
      border: 1px solid rgba(151,181,213,0.14);
      border-radius: 20px;
      padding: 14px 16px;
      background: linear-gradient(180deg, rgba(14,25,39,0.92), rgba(10,18,29,0.84));
      overflow: hidden;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
    }
    .summaryCard::after {
      content: "";
      position: absolute;
      right: -18px;
      top: -18px;
      width: 96px;
      height: 96px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(99,214,255,0.14), rgba(99,214,255,0) 72%);
      pointer-events: none;
    }
    .summaryLabel {
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.11em;
      text-transform: uppercase;
    }
    .summaryValue {
      margin-top: 10px;
      font-size: 26px;
      font-weight: 800;
      line-height: 1;
      letter-spacing: -0.03em;
    }
    .summarySub {
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .layout {
      display: grid;
      grid-template-columns: 290px minmax(0, 1fr) 360px;
      gap: 14px;
      min-height: 720px;
    }
    .panel {
      border: 1px solid var(--line);
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(13,24,38,0.95), rgba(10,19,31,0.88));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
      overflow: hidden;
    }
    .panel .head {
      border-bottom: 1px solid rgba(151,181,213,0.12);
      padding: 14px 16px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.015));
    }
    .panel .body { padding: 14px; }

    .list { display: grid; gap: 10px; }
    .subsectionLabel {
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .itemMain {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      min-width: 0;
    }
    .itemText {
      min-width: 0;
    }
    .itemTitle {
      font-weight: 700;
      color: var(--txt);
    }
    .item {
      border: 1px solid rgba(151,181,213,0.12);
      border-radius: 16px;
      padding: 12px 14px;
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      font-size: 13px;
      background: linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.02));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
      transition: border-color .2s ease, transform .14s ease, box-shadow .2s ease;
    }
    .item:hover {
      transform: translateY(-1px);
      border-color: rgba(99,214,255,0.28);
      box-shadow: 0 12px 22px rgba(5, 12, 21, 0.18);
    }
    .item small {
      display: block;
      margin-top: 4px;
      color: var(--muted);
    }
    .nodeGlyph {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 34px;
      height: 34px;
      padding: 0 8px;
      border-radius: 12px;
      border: 1px solid rgba(151,181,213,0.18);
      background: rgba(255,255,255,0.05);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.05em;
      color: var(--txt);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.05);
    }
    .nodeGlyph.switch {
      background: linear-gradient(180deg, rgba(99,214,255,0.18), rgba(99,214,255,0.07));
      border-color: rgba(99,214,255,0.36);
      color: #dff8ff;
    }
    .nodeGlyph.user_device {
      background: linear-gradient(180deg, rgba(255,179,71,0.16), rgba(255,179,71,0.06));
      border-color: rgba(255,179,71,0.30);
    }
    .nodeGlyph.lab_device {
      background: linear-gradient(180deg, rgba(110,231,183,0.18), rgba(110,231,183,0.06));
      border-color: rgba(110,231,183,0.30);
    }
    .nodeGlyph.service_node {
      background: linear-gradient(180deg, rgba(120,189,255,0.18), rgba(120,189,255,0.06));
      border-color: rgba(120,189,255,0.30);
    }
    .nodeGlyph.iot {
      background: linear-gradient(180deg, rgba(255,107,107,0.16), rgba(255,107,107,0.06));
      border-color: rgba(255,107,107,0.30);
    }
    .itemMeta {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 6px;
    }
    .itemActions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 11px;
      font-weight: 700;
      color: var(--txt);
      background: rgba(255,255,255,0.05);
    }
    .chip.accent {
      border-color: rgba(88,214,255,0.35);
      color: var(--accent);
    }
    .miniBtn {
      border: 1px solid rgba(151,181,213,0.20);
      border-radius: 10px;
      padding: 6px 10px;
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
      color: var(--txt);
      background: rgba(255,255,255,0.05);
    }
    .miniBtn:hover:not([disabled]) { border-color: #5bd2ff88; }
    .miniBtn.danger {
      border-color: rgba(242,89,89,0.35);
      background: rgba(242,89,89,0.12);
      color: #ffdede;
    }
    .miniBtn[disabled] {
      opacity: 0.45;
      cursor: not-allowed;
    }

    .tabs {
      display: flex;
      gap: 10px;
      padding: 14px 14px 0;
      border-bottom: 1px solid rgba(151,181,213,0.10);
      background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.015));
    }
    .tab {
      border: 1px solid rgba(151,181,213,0.14);
      border-radius: 12px;
      padding: 9px 14px;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.04em;
      cursor: pointer;
      background: rgba(255,255,255,0.04);
      color: var(--txt);
    }
    .tab.active {
      border-color: rgba(99,214,255,0.52);
      box-shadow: inset 0 0 0 1px rgba(99,214,255,0.18);
      background: linear-gradient(180deg, rgba(99,214,255,0.18), rgba(99,214,255,0.06));
    }
    .view { display: none; padding: 14px; }
    .view.active { display: block; }

    #topologyWrap {
      position: relative;
      border: 1px solid rgba(151,181,213,0.14);
      border-radius: 18px;
      background:
        radial-gradient(circle at 60% 40%, rgba(18,35,55,0.98) 0%, rgba(9,17,27,0.98) 72%),
        repeating-linear-gradient(0deg, rgba(255,255,255,0.03) 0, rgba(255,255,255,0.03) 1px, transparent 1px, transparent 42px),
        repeating-linear-gradient(90deg, rgba(255,255,255,0.03) 0, rgba(255,255,255,0.03) 1px, transparent 1px, transparent 42px);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.05);
      overflow: hidden;
    }
    .topologyTools {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 0 4px 12px;
      color: var(--muted);
      font-size: 12px;
    }
    .topologyHint {
      line-height: 1.5;
      max-width: 56ch;
    }
    .btn.secondary {
      padding: 8px 12px;
      font-size: 12px;
      white-space: nowrap;
    }
    #topologySvg {
      width: 100%;
      height: 540px;
      display: block;
      touch-action: none;
    }
    #topologySvg.dragging {
      cursor: grabbing;
    }
    .legend {
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
      padding: 10px 12px;
      border-top: 1px solid rgba(151,181,213,0.12);
      background: rgba(3, 8, 14, 0.34);
    }
    #selectedNode { margin-left: auto; }
    .swatch { width: 18px; height: 4px; display: inline-block; border-radius: 999px; margin-right: 4px; }

    .metric {
      border: 1px solid rgba(151,181,213,0.12);
      border-radius: 18px;
      padding: 13px;
      margin-bottom: 10px;
      background: linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.02));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
    }
    .metric .k {
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }
    .metric .v { font-size: 28px; font-weight: 800; line-height: 1.08; margin-top: 8px; }
    .metric .mini { font-size: 14px; font-weight: 700; margin-top: 8px; color: var(--txt); }
    .spark {
      width: 100%;
      height: 42px;
      display: block;
      margin-top: 10px;
      border-radius: 12px;
      background: rgba(2,8,14,0.32);
      border: 1px solid rgba(255,255,255,0.05);
    }
    .trendGrid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
      margin-top: 10px;
    }
    .trendCard {
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 14px;
      padding: 10px;
      background: rgba(0,0,0,0.16);
    }
    .trendHead {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: baseline;
      font-size: 12px;
      color: var(--muted);
    }
    .trendValue {
      color: var(--txt);
      font-weight: 700;
    }
    .chartLegend {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 6px;
      color: var(--muted);
      font-size: 11px;
    }
    .chartLegend span {
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .chartLegend i {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      display: inline-block;
    }
    .footerBar {
      margin-top: 16px;
      border: 1px solid rgba(151,181,213,0.14);
      border-radius: 20px;
      padding: 14px 16px;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
      background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02));
      color: var(--muted);
      font-size: 12px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.05);
    }
    .footerLinks {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .linkBtn {
      display: inline-flex;
      align-items: center;
      border: 1px solid rgba(151,181,213,0.18);
      border-radius: 999px;
      padding: 7px 12px;
      color: var(--txt);
      text-decoration: none;
      background: rgba(255,255,255,0.05);
      font-size: 12px;
      font-weight: 700;
    }
    .linkBtn:hover { border-color: rgba(99,214,255,0.42); }
    .linkBtn[aria-disabled="true"] {
      opacity: 0.45;
      pointer-events: none;
    }

    .bar {
      height: 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.10);
      overflow: hidden;
      margin-top: 8px;
    }
    .bar > span {
      display: block;
      height: 100%;
      border-radius: 999px;
      transition: width .35s ease;
    }

    .good { color: var(--good); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }

    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 12px;
      color: #d9e7ff;
      line-height: 1.45;
      padding: 10px 12px;
      border: 1px solid rgba(255,255,255,0.07);
      border-radius: 14px;
      background: rgba(5, 10, 16, 0.28);
    }

    .form {
      display: grid;
      gap: 10px;
      margin-top: 14px;
      padding: 14px;
      border: 1px solid rgba(151,181,213,0.12);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.015));
    }
    .form input, .form select, .form textarea {
      width: 100%;
      border: 1px solid rgba(151,181,213,0.18);
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 13px;
      color: var(--txt);
      background: rgba(0,0,0,0.22);
      outline: none;
      transition: border-color .2s ease, box-shadow .2s ease, background .2s ease;
    }
    .form textarea {
      min-height: 82px;
      resize: vertical;
      font-family: inherit;
      line-height: 1.5;
    }
    .form input[readonly] {
      opacity: 0.82;
      background: rgba(255,255,255,0.06);
      cursor: not-allowed;
    }
    .inlineFields {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    [hidden] { display: none !important; }
    .form input:focus,
    .form select:focus,
    .actions select:focus,
    .tab:focus-visible,
    .btn:focus-visible,
    .miniBtn:focus-visible,
    .modalClose:focus-visible,
    .linkBtn:focus-visible {
      border-color: rgba(99,214,255,0.58);
      box-shadow: 0 0 0 3px rgba(99,214,255,0.14);
      outline: none;
    }

    .foot { color: var(--muted); font-size: 12px; margin-top: 8px; }
    #scenarioHint,
    #leftStatus {
      padding: 8px 12px;
      border: 1px solid rgba(151,181,213,0.12);
      border-radius: 999px;
      background: rgba(255,255,255,0.04);
    }
    .inspectorActions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }
    .statusPill {
      padding: 8px 12px;
      border: 1px solid rgba(151,181,213,0.12);
      border-radius: 14px;
      background: rgba(255,255,255,0.04);
      color: var(--muted);
      font-size: 12px;
    }
    .configStack {
      display: grid;
      gap: 8px;
    }
    .configSection {
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid rgba(151,181,213,0.12);
    }
    .configRow {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .configTitle {
      font-size: 13px;
      font-weight: 800;
      color: var(--txt);
    }
    .configMeta {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .emptyState {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
    }
    .modalBackdrop {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
      background: rgba(4, 8, 13, 0.74);
      backdrop-filter: blur(8px);
      z-index: 50;
    }
    .modalBackdrop.open { display: flex; }
    .modalCard {
      width: min(760px, 100%);
      max-height: min(82vh, 860px);
      overflow: auto;
      border: 1px solid rgba(151,181,213,0.16);
      border-radius: 24px;
      padding: 18px;
      background: linear-gradient(180deg, #182436 0%, #0f1623 100%);
      box-shadow: 0 28px 72px rgba(0,0,0,0.46);
    }
    .modalHeader {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 12px;
    }
    .modalLabel {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }
    .modalTitle {
      margin-top: 4px;
      font-size: 18px;
      font-weight: 700;
      color: var(--txt);
    }
    .modalClose {
      border: 1px solid rgba(151,181,213,0.18);
      border-radius: 12px;
      padding: 8px 12px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      color: var(--txt);
      background: rgba(255,255,255,0.05);
    }
    .modalClose:hover { border-color: #5bd2ff88; }

    @media (min-width: 1220px) {
      .layout > .panel:first-child,
      .layout > .panel:last-child {
        position: sticky;
        top: 18px;
        align-self: start;
      }
      .layout > .panel:last-child .view {
        max-height: calc(100vh - 180px);
        overflow: auto;
      }
    }
    @media (max-width: 1180px) {
      .header,
      .layout {
        grid-template-columns: 1fr;
      }
      .summaryStrip {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .title {
        max-width: none;
      }
    }
    @media (max-width: 720px) {
      .page {
        margin: 12px;
        padding: 12px;
        border-radius: 22px;
      }
      .heroIntro,
      .commandWell,
      .scenarioPanel,
      .panel,
      .footerBar {
        border-radius: 18px;
      }
      .actions {
        flex-direction: column;
        align-items: stretch;
      }
      .actions select,
      .actions .btn {
        width: 100%;
      }
      .scenarioGrid {
        grid-template-columns: 1fr;
      }
      .summaryStrip {
        grid-template-columns: 1fr;
      }
      .inlineFields {
        grid-template-columns: 1fr;
      }
      .tabs,
      .legend,
      .topologyTools {
        flex-direction: column;
        align-items: stretch;
      }
      #selectedNode {
        margin-left: 0;
      }
      #topologySvg {
        height: 420px;
      }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="header">
      <div class="heroIntro">
        <div class="eyebrow">Campus Network Operations Center</div>
        <div class="title">Campus SDN — Traffic Operations Dashboard</div>
        <div class="subtitle">Monitor the live topology, trigger traffic scenarios, inspect network endpoints, and follow adaptive controller decisions across the campus in one place.</div>
        <div class="heroMarks">
          <div class="heroMark">AI-Driven SDN Control</div>
          <div class="heroMark">Adaptive Traffic Management</div>
          <div class="heroMark">Live Campus Emulation</div>
        </div>
        <div class="badges">
          <div class="badge" id="bController">SDN controller: -</div>
          <div class="badge" id="bSwitches">OpenFlow switches: -</div>
          <div class="badge" id="bCore">Protected path load: -</div>
          <div class="badge" id="bPolicy">Traffic policy: -</div>
          <div class="badge" id="bHealth">Service health: -</div>
        </div>
      </div>
      <div class="commandWell">
        <div class="commandWellLabel">Quick Controls</div>
        <div class="actions">
        <select id="scenarioSelect">
          <option value="campus">Campus-wide traffic demo</option>
          <option value="bulk">Bulk traffic load test</option>
          <option value="congestion">Congestion stress test</option>
          <option value="protected">Protected service validation</option>
          <option value="light">Light Wi-Fi throughput test</option>
        </select>
          <button class="btn" id="btnStartStress">Start traffic test</button>
          <button class="btn" id="btnStopStress">Stop traffic test</button>
          <button class="btn" id="btnPingall">Run reachability test</button>
          <button class="btn" id="btnRefresh">Refresh dashboard</button>
        </div>
      </div>
    </div>
    <section class="scenarioPanel">
      <div class="scenarioPanelHead">
        <div>
          <div class="modalLabel">Traffic Demo Controls</div>
          <div class="scenarioPanelTitle">Graphical traffic triggers</div>
        </div>
        <div class="foot" id="scenarioHint">Selected traffic demo: -</div>
      </div>
      <div class="scenarioGrid">
        <div class="scenarioCard" data-scenario-card="campus">
          <div class="scenarioCardTitle">Campus-wide traffic demo</div>
          <div class="scenarioCardDesc">Starts traffic from IT Lab, Networking Lab, Staff LAN, and Student Wi-Fi so all major campus links show live utilization together.</div>
          <div class="scenarioCardMeta">Best for full topology activity</div>
          <div class="scenarioCardActions">
            <button class="miniBtn" type="button" data-scenario-select="campus">Select</button>
            <button class="miniBtn" type="button" data-scenario-run="campus">Run now</button>
          </div>
        </div>
        <div class="scenarioCard" data-scenario-card="light">
          <div class="scenarioCardTitle">Light Wi-Fi throughput test</div>
          <div class="scenarioCardDesc">Runs a smaller Wi-Fi load from one client so you can verify the dashboard reacts without stressing the network too much.</div>
          <div class="scenarioCardMeta">Low traffic</div>
          <div class="scenarioCardActions">
            <button class="miniBtn" type="button" data-scenario-select="light">Select</button>
            <button class="miniBtn" type="button" data-scenario-run="light">Run now</button>
          </div>
        </div>
        <div class="scenarioCard" data-scenario-card="bulk">
          <div class="scenarioCardTitle">Bulk traffic load test</div>
          <div class="scenarioCardDesc">Starts two Wi-Fi download clients so you can see live throughput, switch load, and utilization changes across the topology.</div>
          <div class="scenarioCardMeta">Normal demo load</div>
          <div class="scenarioCardActions">
            <button class="miniBtn" type="button" data-scenario-select="bulk">Select</button>
            <button class="miniBtn" type="button" data-scenario-run="bulk">Run now</button>
          </div>
        </div>
        <div class="scenarioCard" data-scenario-card="congestion">
          <div class="scenarioCardTitle">Congestion stress test</div>
          <div class="scenarioCardDesc">Applies the strongest Wi-Fi load and is the best option when you want utilization, policy actions, and congestion handling to become visible.</div>
          <div class="scenarioCardMeta">Best for visible traffic</div>
          <div class="scenarioCardActions">
            <button class="miniBtn" type="button" data-scenario-select="congestion">Select</button>
            <button class="miniBtn" type="button" data-scenario-run="congestion">Run now</button>
          </div>
        </div>
        <div class="scenarioCard" data-scenario-card="protected">
          <div class="scenarioCardTitle">Protected service validation</div>
          <div class="scenarioCardDesc">Runs the protected-service demonstration so you can observe policy protection and compare protected-path behavior during load.</div>
          <div class="scenarioCardMeta">Protected route demo</div>
          <div class="scenarioCardActions">
            <button class="miniBtn" type="button" data-scenario-select="protected">Select</button>
            <button class="miniBtn" type="button" data-scenario-run="protected">Run now</button>
          </div>
        </div>
      </div>
    </section>
    <section class="summaryStrip">
      <div class="summaryCard">
        <div class="summaryLabel">Controller State</div>
        <div class="summaryValue" id="sumController">-</div>
        <div class="summarySub" id="sumControllerSub">Checking controller status...</div>
      </div>
      <div class="summaryCard">
        <div class="summaryLabel">Managed Endpoints</div>
        <div class="summaryValue" id="sumEndpoints">-</div>
        <div class="summarySub" id="sumEndpointsSub">Checking endpoint inventory...</div>
      </div>
      <div class="summaryCard">
        <div class="summaryLabel">Busiest Link</div>
        <div class="summaryValue" id="sumHotLink">-</div>
        <div class="summarySub" id="sumHotLinkSub">Waiting for live utilization samples...</div>
      </div>
      <div class="summaryCard">
        <div class="summaryLabel">Policy State</div>
        <div class="summaryValue" id="sumPolicy">-</div>
        <div class="summarySub" id="sumPolicySub">Checking adaptive policy state...</div>
      </div>
    </section>

    <div class="layout">
      <section class="panel">
        <div class="head">Network Inventory</div>
        <div class="body">
          <div class="subsectionLabel">Switches</div>
          <div class="list" id="switchList"></div>
          <div style="height:10px;"></div>
          <div class="subsectionLabel">Endpoints</div>
          <div class="list" id="hostList"></div>

          <form class="form" id="deviceForm">
            <div class="subsectionLabel">Add Network Endpoint</div>
            <input id="devName" required placeholder="Endpoint name (e.g. Library Camera A)" />
            <input id="devIp" required placeholder="IP (e.g. 10.0.0.150)" />
            <select id="devSwitch">
              <option value="s1">Connect to s1</option>
              <option value="s2">Connect to s2</option>
              <option value="s3">Connect to s3</option>
              <option value="s4">Connect to s4</option>
              <option value="s5">Connect to s5</option>
            </select>
            <select id="devCategory">
              <option value="user_device">User device</option>
              <option value="iot">IoT device</option>
              <option value="service_node">Service node</option>
              <option value="lab_device">Lab device</option>
            </select>
            <input id="devBw" type="number" min="1" max="1000" value="50" />
            <button class="btn" type="submit">+ Add endpoint</button>
            <div class="foot">Campus subnet only: <code>10.0.0.0/24</code>. Duplicate IPs are blocked, and endpoint categories remain attached to the live topology view.</div>
          </form>
          <form class="form" id="settingsForm">
            <div class="subsectionLabel">Network Policy Settings</div>
            <div class="inlineFields">
              <input id="cfgHighMbps" type="number" min="1" step="1" placeholder="High threshold (Mbps)" />
              <input id="cfgLowMbps" type="number" min="1" step="1" placeholder="Low threshold (Mbps)" />
            </div>
            <div class="inlineFields">
              <input id="cfgPortHigh" type="number" min="1" max="100" step="1" placeholder="Port high (%)" />
              <input id="cfgPortLow" type="number" min="1" max="100" step="1" placeholder="Port low (%)" />
            </div>
            <div class="inspectorActions">
              <button class="btn" type="submit">Save network settings</button>
              <button class="btn secondary" id="btnResetSettings" type="button">Restore live values</button>
            </div>
            <div class="foot">These values control when congestion handling becomes active. Changes are applied live through the controller policy hook.</div>
            <div class="statusPill" id="settingsStatus">Live settings will appear here after the first dashboard refresh.</div>
          </form>
          <form class="form" id="automationCommandForm">
            <div class="subsectionLabel">Automation Command</div>
            <div class="foot">Tell the controller what network policy you want in plain language.</div>
            <textarea id="automationCommand" placeholder="Example: configure s3 with vlan 10,20,30 and allow 10-20"></textarea>
            <div class="inspectorActions">
              <button class="btn" type="submit">Run command</button>
            </div>
            <div class="foot">Supported examples: <code>configure s3 with vlan 10,20,30 and allow 10-20</code>, <code>configure the whole network with vlan 10,20,30</code>, <code>make the whole network devices talk to each other</code>, <code>clear automation on s3</code>, <code>block 10-20 on s3</code>, <code>remove vlan 30 from s3</code>.</div>
            <div class="statusPill" id="automationCommandStatus">Command results will appear here after the first refresh.</div>
          </form>
          <form class="form" id="autoVlanForm">
            <div class="subsectionLabel">Intent-Based Network Automation</div>
            <div class="foot">Describe the switch policy you want, and the controller will build the VLAN assignments automatically by access-port order.</div>
            <select id="autoVlanSwitch">
              <option value="s1">Auto-configure s1</option>
              <option value="s2">Auto-configure s2</option>
              <option value="s3" selected>Auto-configure s3</option>
              <option value="s4">Auto-configure s4</option>
              <option value="s5">Auto-configure s5</option>
            </select>
            <input id="autoVlanList" value="10,20,30" placeholder="VLAN plan (e.g. 10,20,30)" />
            <input id="autoVlanLinks" placeholder="Optional communication pairs (e.g. 10-20,20-30)" />
            <div class="inspectorActions">
              <button class="btn" type="submit">Auto-configure switch</button>
              <button class="btn secondary" id="btnClearAutoVlan" type="button">Clear switch automation</button>
            </div>
            <div class="foot">Example: on <code>s3</code>, the controller can map the three connected endpoints to VLAN <code>10</code>, <code>20</code>, and <code>30</code> automatically, then allow selected VLAN-to-VLAN communication.</div>
            <div class="statusPill" id="autoVlanStatus">Intent-based switch automation will appear here after the first refresh.</div>
          </form>
          <form class="form" id="vlanAssignForm">
            <div class="subsectionLabel">Manual VLAN Override</div>
            <div class="foot">Use this only when you want to override the automatic policy for a specific endpoint.</div>
            <div class="inlineFields">
              <select id="vlanSwitch">
                <option value="s1">Manage s1</option>
                <option value="s2">Manage s2</option>
                <option value="s3" selected>Manage s3</option>
                <option value="s4">Manage s4</option>
                <option value="s5">Manage s5</option>
              </select>
              <input id="vlanId" type="number" min="1" max="4094" placeholder="VLAN ID (e.g. 10)" />
            </div>
            <select id="vlanDevice"></select>
            <div class="inspectorActions">
              <button class="btn" type="submit">Assign endpoint to VLAN</button>
            </div>
            <div class="foot">Example: choose <code>s3</code>, select an endpoint, then assign VLAN <code>10</code>, <code>20</code>, or <code>30</code>. The controller will reprogram the switch automatically.</div>
            <div class="statusPill" id="vlanStatus">Live VLAN automation details will appear here after the first refresh.</div>
          </form>
          <form class="form" id="vlanInterconnectForm">
            <div class="subsectionLabel">Cross-VLAN Communication</div>
            <select id="interconnectSwitch">
              <option value="s1">Policy on s1</option>
              <option value="s2">Policy on s2</option>
              <option value="s3" selected>Policy on s3</option>
              <option value="s4">Policy on s4</option>
              <option value="s5">Policy on s5</option>
            </select>
            <div class="inlineFields">
              <select id="interconnectVlanA"></select>
              <select id="interconnectVlanB"></select>
            </div>
            <div class="inspectorActions">
              <button class="btn secondary" type="submit">Allow traffic between VLANs</button>
            </div>
            <div class="foot">Use this when one network should exchange traffic with another. Remove the policy below to isolate the VLANs again.</div>
          </form>
          <div class="subsectionLabel" style="margin-top:14px;">Active Network Automation</div>
          <div class="statusPill" id="vlanSummary">No live VLAN automation has been published yet.</div>
          <div class="list" id="automationList"></div>
          <div class="foot">Use <code>View</code> or <code>Config</code> to open the device details window.</div>
          <div class="foot" id="leftStatus">Ready</div>
        </div>
      </section>

      <section class="panel">
        <div class="tabs">
          <button class="tab active" data-tab="topology">Topology map</button>
          <button class="tab" data-tab="traffic">Traffic paths</button>
          <button class="tab" data-tab="heat">Utilization heat map</button>
        </div>
        <div class="view active" id="view-topology">
          <div class="topologyTools">
            <div class="topologyHint">Drag devices to arrange the map. Your layout is saved automatically in this browser.</div>
            <button class="btn secondary" id="btnResetLayout" type="button">Reset layout</button>
          </div>
          <div id="topologyWrap">
            <svg id="topologySvg" viewBox="0 0 760 520"></svg>
            <div class="legend">
              <span><i class="swatch" style="background:#2bc17f"></i>Normal link</span>
              <span><i class="swatch" style="background:#f0a73b"></i>High utilization link</span>
              <span><i class="swatch" style="background:#f25959"></i>Congested link</span>
              <span><i class="swatch" style="background:#58d6ff"></i>Active protected path</span>
              <span><i class="swatch" style="background:#8aa1bf"></i>Standby protected path</span>
              <span id="selectedNode">Selected node: none</span>
            </div>
          </div>
        </div>
        <div class="view" id="view-traffic">
          <pre id="trafficText">Traffic animation follows the live forwarding path in the topology map.
Use "Start traffic test" to generate Wi-Fi load and observe congestion conditions.
When thresholds are crossed, the controller should reroute protected traffic and apply bulk-traffic QoS controls.</pre>
        </div>
        <div class="view" id="view-heat">
          <pre id="heatText">Utilization heat map summary loading...</pre>
        </div>
      </section>

      <section class="panel">
        <div class="tabs">
          <button class="tab active" data-right="metrics">Network Status</button>
          <button class="tab" data-right="events">Event log</button>
          <button class="tab" data-right="flows">Flow tables</button>
          <button class="tab" data-right="ops">Action log</button>
        </div>
        <div class="view active" id="right-metrics">
          <div class="metric">
            <div class="k">Network service health</div>
            <div class="v" id="mHealth">-</div>
            <div class="foot" id="mHealthProof">Health summary: -</div>
            <div class="foot" id="mMetricsFresh">Network status: -</div>
          </div>
          <div class="metric">
            <div class="k">Operational state</div>
            <pre id="mSystemMode">Loading operational state...</pre>
          </div>
          <div class="metric">
            <div class="k">Core path throughput</div>
            <div class="v" id="mCoreLoad">-</div>
            <div class="foot" id="mThreshold">Threshold: -</div>
          </div>
          <div class="metric">
            <div class="k">Active end hosts</div>
            <div class="v" id="mHosts">-</div>
            <div class="foot" id="mBackup">Standby path: -</div>
          </div>
          <div class="metric">
            <div class="k">Protected service path</div>
            <div class="v" id="mRoute">-</div>
            <div class="foot" id="mRouteDetail">Active path: -</div>
            <div class="foot" id="mRouteDecision">Decision source: -</div>
          </div>
          <div class="metric">
            <div class="k">Routing decision rationale</div>
            <pre id="mWhyPane">Loading routing rationale...</pre>
          </div>
          <div class="metric">
            <div class="k">Link utilization</div>
            <div id="linkBars"></div>
          </div>
          <div class="metric">
            <div class="k">Protected traffic throughput</div>
            <div class="v" id="mThroughput">-</div>
            <div class="foot" id="mLoss">Traffic state: -</div>
          </div>
          <div class="metric">
            <div class="k">Latest reachability test</div>
            <div class="v" id="mPingLoss">-</div>
            <div class="foot" id="mPingRtt">Avg RTT: -</div>
            <div class="foot" id="mPingPairs">Pairs: -</div>
          </div>
          <div class="metric">
            <div class="k">Queue pressure estimate</div>
            <div class="v" id="mQueueDepth">-</div>
            <div class="foot" id="mQueueHint">Queue status: -</div>
          </div>
          <div class="metric">
            <div class="k">Latency trend</div>
            <div class="v" id="mLatencyTrend">-</div>
            <div class="foot" id="mLatencyAvg">Average RTT: -</div>
            <svg class="spark" id="latencySpark" viewBox="0 0 180 42" preserveAspectRatio="none"></svg>
          </div>
          <div class="metric">
            <div class="k">Traffic and congestion trends</div>
            <div class="trendGrid">
              <div class="trendCard">
                <div class="trendHead">
                  <span>Traffic trend</span>
                  <span class="trendValue" id="mTrafficTrend">-</span>
                </div>
                <svg class="spark" id="trafficSpark" viewBox="0 0 180 42" preserveAspectRatio="none"></svg>
                <div class="chartLegend" id="trafficLegend"></div>
              </div>
              <div class="trendCard">
                <div class="trendHead">
                  <span>Pressure timeline</span>
                  <span class="trendValue" id="mPressureTrend">-</span>
                </div>
                <svg class="spark" id="pressureSpark" viewBox="0 0 180 42" preserveAspectRatio="none"></svg>
                <div class="chartLegend" id="pressureLegend"></div>
              </div>
            </div>
            <div class="foot" id="mTrendWindow">History window: -</div>
          </div>
          <div class="metric">
            <div class="k">Installed OpenFlow rules</div>
            <div class="v" id="mActiveFlows">-</div>
            <div class="foot" id="mFlowBySwitch">Per-switch: -</div>
          </div>
          <div class="metric">
            <div class="k">Latest performance comparison</div>
            <pre id="mEvalPane">Looking for Stage 11 comparison artifacts...</pre>
          </div>
          <div class="metric">
            <div class="k">QoS policy classes</div>
            <pre id="mPolicyClasses">Loading QoS policy classes...</pre>
          </div>
          <div class="metric">
            <div class="k">AI routing decision summary</div>
            <pre id="mAiPane">Loading AI routing state...</pre>
          </div>
          <div class="metric">
            <div class="k">Network alerts</div>
            <pre id="alertsPane">No active network alerts.</pre>
          </div>
          <div class="metric">
            <div class="k">Controller decision timeline</div>
            <pre id="mControllerActions">Loading...</pre>
          </div>
          <div class="metric">
            <div class="k">OpenFlow programming summary</div>
            <pre id="mFlowExplain">Loading flow-programming summary...</pre>
          </div>
        </div>
        <div class="view" id="right-events">
          <pre id="eventsPane">Loading controller events...</pre>
        </div>
        <div class="view" id="right-flows">
          <div style="display:flex; gap:8px; margin-bottom:8px;">
            <select id="flowSwitch">
              <option>s1</option><option>s2</option><option>s3</option><option>s4</option><option>s5</option>
            </select>
            <button class="btn" id="btnLoadFlows">Load flow table</button>
          </div>
          <pre id="flowsPane">Select a switch and load its OpenFlow table.</pre>
        </div>
        <div class="view" id="right-ops">
          <pre id="opsPane">Loading operational log...</pre>
        </div>
      </section>
    </div>
    <div class="footerBar">
      <div id="footerStatus">Last refresh: -</div>
      <div class="footerLinks">
        <a class="linkBtn" id="reportJsonLink" href="/api/report/latest/json">Latest JSON</a>
        <a class="linkBtn" id="reportCsvLink" href="/api/report/latest/csv">Latest CSV</a>
        <a class="linkBtn" id="reportMdLink" href="/api/report/latest/md">Latest Markdown</a>
      </div>
    </div>
  </div>
  <div class="modalBackdrop" id="deviceModal" aria-hidden="true">
    <div class="modalCard" role="dialog" aria-modal="true" aria-labelledby="deviceInspectTitle">
      <div class="modalHeader">
        <div>
          <div class="modalLabel">Selected node configuration</div>
          <div class="modalTitle" id="deviceInspectTitle">No node selected</div>
        </div>
        <button class="modalClose" id="btnCloseDeviceModal" type="button">Close</button>
      </div>
      <pre id="deviceConfigPane">Select a switch or endpoint from the inventory or topology map to review its live configuration.</pre>
      <div class="inspectorActions">
        <button class="btn secondary" id="btnRefreshDevice" type="button">Refresh device view</button>
        <button class="btn secondary" id="btnEditDevice" type="button" disabled>Edit endpoint</button>
        <button class="btn secondary danger" id="btnRemoveDevice" type="button" disabled>Remove endpoint</button>
      </div>
      <form class="form" id="deviceEditForm" hidden>
        <div class="subsectionLabel">Edit Live Endpoint</div>
        <input id="editDeviceId" readonly />
        <input id="editDisplayName" required placeholder="Display name" />
        <div class="inlineFields">
          <input id="editIp" required placeholder="IP (e.g. 10.0.0.150)" />
          <input id="editBw" type="number" min="1" max="1000" step="1" placeholder="Bandwidth (Mbps)" />
        </div>
        <div class="inlineFields">
          <select id="editSwitch">
            <option value="s1">Connect to s1</option>
            <option value="s2">Connect to s2</option>
            <option value="s3">Connect to s3</option>
            <option value="s4">Connect to s4</option>
            <option value="s5">Connect to s5</option>
          </select>
          <select id="editCategory">
            <option value="user_device">User device</option>
            <option value="iot">IoT device</option>
            <option value="service_node">Service node</option>
            <option value="lab_device">Lab device</option>
          </select>
        </div>
        <div class="inspectorActions">
          <button class="btn" type="submit">Save endpoint changes</button>
          <button class="btn secondary" id="btnCancelEditDevice" type="button">Cancel edit</button>
        </div>
        <div class="statusPill" id="deviceEditStatus">Only dashboard-added endpoints can be edited live.</div>
      </form>
    </div>
  </div>

<script>
const state = {
  metrics: null,
  topology: null,
  events: [],
  operations: {},
  dashboard: {},
  selectedNode: null,
  deviceDetailsCache: {},
  loadingDeviceId: '',
  deviceModalOpen: false,
  flowAnim: [],
  pageStartTs: Date.now() / 1000,
  lastRefreshTs: 0,
  customPositions: {},
  drag: null,
  suppressNodeClickUntil: 0,
  deviceEditMode: false,
  deviceEditTarget: '',
  networkSettingsDirty: false,
  automation: {}
};
const TOPOLOGY_LAYOUT_KEY = 'campusTopologyLayout.v1';
const TOPOLOGY_VIEWBOX = { width: 760, height: 520 };
const SCENARIO_CATALOG = {
  campus: {
    key: 'campus',
    label: 'Campus-wide traffic demo',
    description: 'Traffic from all main campus zones so every major access link becomes active.',
    payload: { seconds: 45, reverse_download: true, clients: ['h_it1', 'h_net1', 'h_staff1', 'h_wifi1', 'h_wifi2'] }
  },
  light: {
    key: 'light',
    label: 'Light Wi-Fi throughput test',
    description: 'Single Wi-Fi client load for a gentle live check.',
    payload: { seconds: 20, reverse_download: true, clients: ['h_wifi1'] }
  },
  bulk: {
    key: 'bulk',
    label: 'Bulk traffic load test',
    description: 'Two Wi-Fi download clients for normal dashboard traffic activity.',
    payload: { seconds: 45, reverse_download: true, clients: ['h_wifi1', 'h_wifi2'] }
  },
  congestion: {
    key: 'congestion',
    label: 'Congestion stress test',
    description: 'Higher load to make congestion handling easier to observe.',
    payload: { seconds: 60, reverse_download: true, clients: ['h_wifi1', 'h_wifi2'] }
  },
  protected: {
    key: 'protected',
    label: 'Protected service validation',
    description: 'Protected-path validation under live Wi-Fi activity.',
    payload: { seconds: 35, reverse_download: true, clients: ['h_wifi1'] }
  }
};

function q(id) { return document.getElementById(id); }
function pct(v) { return Math.max(0, Math.min(100, Number(v || 0))); }
function utilColor(v) {
  const x = pct(v);
  if (x >= 80) return '#f25959';
  if (x >= 55) return '#f0a73b';
  return '#2bc17f';
}
function utilClass(v) {
  const x = pct(v);
  if (x >= 80) return 'bad';
  if (x >= 55) return 'warn';
  return 'good';
}
function fmt(v, digits = 2) {
  const num = Number(v);
  return Number.isFinite(num) ? num.toFixed(digits) : '-';
}
function formatAge(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return '-';
  if (value < 1) return `${(value * 1000).toFixed(0)} ms`;
  if (value < 60) return `${value.toFixed(1)} s`;
  return `${(value / 60).toFixed(1)} min`;
}
function titleize(text) {
  return String(text || '-')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}
function formatPairs(obj, digits = 3) {
  const pairs = Object.entries(obj || {});
  if (!pairs.length) return 'n/a';
  return pairs
    .map(([k, v]) => `${k}=${Number.isFinite(Number(v)) ? Number(v).toFixed(digits) : String(v)}`)
    .join(', ');
}
function formatClock(ts) {
  const value = Number(ts);
  return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleTimeString() : 'not yet';
}
function categoryLabel(category) {
  const labels = {
    user_device: 'User device',
    iot: 'IoT device',
    service_node: 'Service node',
    lab_device: 'Lab device'
  };
  return labels[category] || titleize(category || 'user device');
}
function sortNumericStrings(values) {
  return (values || []).slice().sort((a, b) => Number(a) - Number(b));
}
function switchAutomationProfile(switchId) {
  const switches = (state.automation && state.automation.switches) || {};
  const cfg = switches[String(switchId || '').trim().toLowerCase()];
  return cfg && typeof cfg === 'object' ? cfg : null;
}
function deviceAutomationProfile(deviceId) {
  const target = String(deviceId || '').trim();
  const switches = (state.automation && state.automation.switches) || {};
  for (const [switchName, cfg] of Object.entries(switches)) {
    const vlans = (cfg && cfg.vlans) || {};
    for (const [vlanId, vlanCfg] of Object.entries(vlans)) {
      const members = Array.isArray(vlanCfg && vlanCfg.members) ? vlanCfg.members : [];
      if (!members.some(member => String(member.device || '').trim() === target)) continue;
      const linked = (Array.isArray(cfg.allow_between) ? cfg.allow_between : [])
        .filter(pair => Number(pair.src_vlan) === Number(vlanId) || Number(pair.dst_vlan) === Number(vlanId))
        .map(pair => Number(pair.src_vlan) === Number(vlanId) ? Number(pair.dst_vlan) : Number(pair.src_vlan))
        .filter(vlan => Number.isFinite(vlan));
      return {
        switchName,
        vlanId: Number(vlanId),
        linkedVlans: linked.sort((a, b) => a - b)
      };
    }
  }
  return null;
}
function nodeGlyphInfo(kind, category) {
  if (kind === 'switch') return { cls: 'switch', text: 'SW' };
  switch (category) {
    case 'service_node':
      return { cls: 'service_node', text: 'SV' };
    case 'lab_device':
      return { cls: 'lab_device', text: 'LB' };
    case 'iot':
      return { cls: 'iot', text: 'IoT' };
    default:
      return { cls: 'user_device', text: 'EP' };
  }
}
function nodeGlyphMarkup(kind, category) {
  const info = nodeGlyphInfo(kind, category);
  return `<span class="nodeGlyph ${info.cls}">${info.text}</span>`;
}
function formatRateState(value, idleLabel = 'idle') {
  const num = Number(value || 0);
  return `${num.toFixed(1)} Mb/s${num < 0.1 ? ` (${idleLabel})` : ''}`;
}
function findTopologyNode(nodeId) {
  return ((state.topology && state.topology.nodes) || []).find(n => n.id === nodeId) || null;
}
function isEndpointNode(node) {
  return Boolean(node && node.kind !== 'switch');
}
function setDeviceModalOpen(open) {
  const modal = q('deviceModal');
  if (!modal) return;
  state.deviceModalOpen = Boolean(open);
  modal.classList.toggle('open', state.deviceModalOpen);
  modal.setAttribute('aria-hidden', state.deviceModalOpen ? 'false' : 'true');
}
function openDeviceModal() {
  setDeviceModalOpen(true);
}
function closeDeviceModal() {
  setDeviceModalOpen(false);
  hideDeviceEditForm();
}
function setRemoveButtonState(deviceId, removable) {
  const btn = q('btnRemoveDevice');
  if (!btn) return;
  btn.dataset.deviceId = removable ? String(deviceId || '') : '';
  btn.disabled = !removable;
}
function setEditButtonState(deviceId, editable) {
  const btn = q('btnEditDevice');
  if (!btn) return;
  btn.dataset.deviceId = editable ? String(deviceId || '') : '';
  btn.disabled = !editable;
}
function hideDeviceEditForm() {
  const form = q('deviceEditForm');
  if (form) form.hidden = true;
  state.deviceEditMode = false;
  state.deviceEditTarget = '';
}
function populateDeviceEditForm(device) {
  const deviceId = String(device.name || device.id || '');
  q('editDeviceId').value = deviceId;
  q('editDisplayName').value = device.display_name || device.label || deviceId;
  q('editIp').value = device.ip || '';
  q('editSwitch').value = device.attach_switch || 's1';
  q('editCategory').value = device.category || 'user_device';
  q('editBw').value = Number(device.bandwidth_mbps || 50);
  q('deviceEditStatus').textContent = `Editing ${device.display_name || device.label || deviceId}. Save to apply the new live endpoint settings.`;
}
function fillNetworkSettingsForm(metrics, force = false) {
  if (state.networkSettingsDirty && !force) return;
  const m = metrics || {};
  q('cfgHighMbps').value = Number(m.congest_high_mbps || 120);
  q('cfgLowMbps').value = Number(m.congest_low_mbps || 80);
  q('cfgPortHigh').value = Number(m.port_congest_high_pct || 80);
  q('cfgPortLow').value = Number(m.port_congest_low_pct || 65);
  state.networkSettingsDirty = false;
  q('settingsStatus').textContent =
    `Live thresholds: ${fmt(m.congest_low_mbps || 80, 1)}/${fmt(m.congest_high_mbps || 120, 1)} Mbps and ${fmt(m.port_congest_low_pct || 65, 0)}/${fmt(m.port_congest_high_pct || 80, 0)}% port utilization.`;
}
function currentAutomationSwitch() {
  return q('vlanSwitch') ? q('vlanSwitch').value : 's3';
}
function currentInterconnectSwitch() {
  return q('interconnectSwitch') ? q('interconnectSwitch').value : 's3';
}
function refreshVlanDeviceOptions() {
  const select = q('vlanDevice');
  if (!select) return;
  const switchName = currentAutomationSwitch();
  const available = (((state.automation || {}).available_devices || {})[switchName]) || [];
  const current = select.value;
  if (!available.length) {
    select.innerHTML = '<option value="">No eligible endpoints are attached to this switch</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = available.map(device =>
    `<option value="${device.name}">${device.display_name || device.name} · port ${device.switch_port}</option>`
  ).join('');
  if (current && available.some(device => device.name === current)) {
    select.value = current;
  }
}
function refreshInterconnectOptions() {
  const switchName = currentInterconnectSwitch();
  const cfg = switchAutomationProfile(switchName) || {};
  const vlanIds = sortNumericStrings(Object.keys((cfg && cfg.vlans) || {}));
  ['interconnectVlanA', 'interconnectVlanB'].forEach((id, idx) => {
    const select = q(id);
    if (!select) return;
    const current = select.value;
    if (!vlanIds.length) {
      select.innerHTML = '<option value="">Create VLANs on this switch first</option>';
      select.disabled = true;
      return;
    }
    select.disabled = false;
    select.innerHTML = vlanIds.map(vlan => `<option value="${vlan}">VLAN ${vlan}</option>`).join('');
    if (current && vlanIds.includes(current)) {
      select.value = current;
    } else if (vlanIds[idx]) {
      select.value = vlanIds[idx];
    }
  });
  if (q('interconnectVlanA') && q('interconnectVlanB') && q('interconnectVlanA').value === q('interconnectVlanB').value && vlanIds.length > 1) {
    q('interconnectVlanB').value = vlanIds[1];
  }
}
function renderNetworkAutomationPanel() {
  const automation = state.automation || {};
  const summary = automation.summary || {};
  const summaryText = summary.managed_switches
    ? `Managed switches ${Number(summary.managed_switches || 0)} | VLANs ${Number(summary.vlans || 0)} | Members ${Number(summary.members || 0)} | Cross-VLAN policies ${Number(summary.interconnects || 0)}`
    : 'No controller-driven VLAN policy is active yet. Assign an endpoint to a VLAN to publish the first automation rule.';
  q('vlanSummary').textContent = summaryText;
  if (q('autoVlanStatus')) {
    q('autoVlanStatus').textContent = summary.managed_switches
      ? `Intent automation active on ${Number(summary.managed_switches || 0)} switch(es). The controller is enforcing ${Number(summary.vlans || 0)} VLANs and ${Number(summary.interconnects || 0)} cross-VLAN policy links.`
      : 'No intent-based switch automation is active yet.';
  }
  refreshVlanDeviceOptions();
  refreshInterconnectOptions();

  const switches = automation.switches || {};
  const entries = Object.entries(switches).sort((a, b) => a[0].localeCompare(b[0]));
  if (!entries.length) {
    q('automationList').innerHTML = `<div class="item"><div class="emptyState">No VLAN automation has been configured yet. Start by assigning an endpoint to a VLAN on a switch such as <code>s3</code>.</div></div>`;
    return;
  }

  q('automationList').innerHTML = entries.map(([switchName, cfg]) => {
    const vlanMarkup = sortNumericStrings(Object.keys((cfg && cfg.vlans) || {})).map(vlanId => {
      const vlanCfg = (cfg.vlans || {})[vlanId] || {};
      const members = Array.isArray(vlanCfg.members) ? vlanCfg.members : [];
      const memberChips = members.length
        ? members.map(member => `<span class="chip">p${member.port} · ${member.display_name || member.device}</span>`).join('')
        : '<span class="chip">No endpoints</span>';
      const memberActions = members.map(member =>
        `<button class="miniBtn danger" type="button" data-vlan-member-remove="${switchName}:${vlanId}:${member.device}">Remove ${member.display_name || member.device}</button>`
      ).join('');
      return `
        <div class="configSection">
          <div class="configTitle">VLAN ${vlanId}</div>
          <div class="configMeta">${members.length} endpoint(s) controlled by the SDN controller on ${switchName}.</div>
          <div class="configRow">${memberChips}</div>
          <div class="configRow">
            ${memberActions}
            <button class="miniBtn danger" type="button" data-vlan-remove="${switchName}:${vlanId}">Remove VLAN ${vlanId}</button>
          </div>
        </div>
      `;
    }).join('');
    const interconnects = Array.isArray(cfg.allow_between) ? cfg.allow_between : [];
    const interconnectMarkup = interconnects.length
      ? `
        <div class="configSection">
          <div class="configTitle">Allowed cross-network communication</div>
          <div class="configMeta">Traffic is permitted only between the VLAN pairs listed below.</div>
          <div class="configRow">
            ${interconnects.map(pair => `<span class="chip accent">VLAN ${pair.src_vlan} <-> VLAN ${pair.dst_vlan}</span>`).join('')}
          </div>
          <div class="configRow">
            ${interconnects.map(pair => `<button class="miniBtn danger" type="button" data-vlan-link-remove="${switchName}:${pair.src_vlan}:${pair.dst_vlan}">Block VLAN ${pair.src_vlan} and VLAN ${pair.dst_vlan}</button>`).join('')}
          </div>
        </div>
      `
      : `
        <div class="configSection">
          <div class="configTitle">Allowed cross-network communication</div>
          <div class="configMeta">This switch is currently isolating each VLAN from the others.</div>
        </div>
      `;
    return `
      <div class="item">
        <div class="itemMain">
          ${nodeGlyphMarkup('switch', 'switch')}
          <div class="itemText">
            <div class="itemTitle">${switchName.toUpperCase()} automation policy</div>
            <small>Controller-managed segmentation and communication rules</small>
          </div>
        </div>
        <div class="itemMeta">
          <div class="chip accent">${sortNumericStrings(Object.keys((cfg && cfg.vlans) || {})).length} VLAN(s)</div>
        </div>
        <div class="configStack" style="grid-column: 1 / -1;">
          ${vlanMarkup || '<div class="configSection"><div class="configMeta">No VLANs configured on this switch.</div></div>'}
          ${interconnectMarkup}
        </div>
      </div>
    `;
  }).join('');

  document.querySelectorAll('[data-vlan-remove]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const [switchName, vlanId] = String(btn.getAttribute('data-vlan-remove') || '').split(':');
      await removeVlanConfig(switchName, vlanId, '');
    });
  });
  document.querySelectorAll('[data-vlan-member-remove]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const [switchName, vlanId, deviceId] = String(btn.getAttribute('data-vlan-member-remove') || '').split(':');
      await removeVlanConfig(switchName, vlanId, deviceId);
    });
  });
  document.querySelectorAll('[data-vlan-link-remove]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const [switchName, vlanA, vlanB] = String(btn.getAttribute('data-vlan-link-remove') || '').split(':');
      await updateVlanInterconnect(null, {
        switchName,
        vlanA,
        vlanB,
        enabled: false,
        statusMessage: `Traffic isolation restored between VLAN ${vlanA} and VLAN ${vlanB} on ${switchName}.`
      });
    });
  });
}
function parseVlanList(text) {
  const raw = String(text || '').split(/[^0-9]+/).filter(Boolean);
  const out = [];
  const seen = new Set();
  for (const token of raw) {
    const value = Number(token);
    if (!Number.isInteger(value) || value <= 0 || value > 4094 || seen.has(value)) continue;
    out.push(value);
    seen.add(value);
  }
  return out;
}
function parseInterconnectPairs(text) {
  const pairs = [];
  const seen = new Set();
  for (const chunk of String(text || '').split(',')) {
    const values = String(chunk || '').split(/[^0-9]+/).filter(Boolean).map(Number);
    if (values.length < 2) continue;
    const a = values[0];
    const b = values[1];
    if (!Number.isInteger(a) || !Number.isInteger(b) || a <= 0 || b <= 0 || a > 4094 || b > 4094 || a === b) continue;
    const key = `${Math.min(a, b)}:${Math.max(a, b)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    pairs.push([Math.min(a, b), Math.max(a, b)]);
  }
  return pairs;
}
async function autoConfigureSwitch(ev) {
  ev.preventDefault();
  const switchName = q('autoVlanSwitch').value;
  const vlanIds = parseVlanList(q('autoVlanList').value);
  const allowBetween = parseInterconnectPairs(q('autoVlanLinks').value);
  if (!vlanIds.length) {
    q('autoVlanStatus').textContent = 'Auto-configuration failed: provide one or more VLAN IDs, for example 10,20,30.';
    return;
  }
  const data = await api('/api/network/automation/auto', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      switch: switchName,
      vlan_ids: vlanIds,
      allow_between: allowBetween
    })
  });
  if (!data || data.error || data.ok === false) {
    const msg = (data && (data.error || data.message)) || 'unknown error';
    q('autoVlanStatus').textContent = 'Auto-configuration failed: ' + msg;
    q('leftStatus').textContent = 'Intent automation failed: ' + msg;
    return;
  }
  state.automation = data.automation || {};
  renderNetworkAutomationPanel();
  q('autoVlanStatus').textContent = data.message || 'Switch automation applied.';
  q('leftStatus').textContent = data.message || `Intent-based automation applied on ${switchName}.`;
  if (q('vlanSwitch')) q('vlanSwitch').value = switchName;
  if (q('interconnectSwitch')) q('interconnectSwitch').value = switchName;
  await refresh();
}
async function clearSwitchAutomation() {
  const switchName = q('autoVlanSwitch').value;
  const data = await api('/api/network/automation/switch/' + encodeURIComponent(switchName), {
    method: 'DELETE'
  });
  if (!data || data.error || data.ok === false) {
    const msg = (data && (data.error || data.message)) || 'unknown error';
    q('autoVlanStatus').textContent = 'Clear failed: ' + msg;
    q('leftStatus').textContent = 'Intent automation clear failed: ' + msg;
    return;
  }
  state.automation = data.automation || {};
  renderNetworkAutomationPanel();
  q('autoVlanStatus').textContent = data.message || `Automation removed from ${switchName}.`;
  q('leftStatus').textContent = data.message || `Intent-based automation removed from ${switchName}.`;
  await refresh();
}
async function runAutomationCommand(ev) {
  ev.preventDefault();
  const command = String(q('automationCommand').value || '').trim();
  if (!command) {
    q('automationCommandStatus').textContent = 'Command failed: enter an automation instruction first.';
    return;
  }
  const data = await api('/api/network/automation/intent', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command })
  });
  if (!data || data.error || data.ok === false) {
    const msg = (data && (data.error || data.message)) || 'unknown error';
    q('automationCommandStatus').textContent = 'Command failed: ' + msg;
    q('leftStatus').textContent = 'Automation command failed: ' + msg;
    return;
  }
  state.automation = data.automation || {};
  renderNetworkAutomationPanel();
  q('automationCommandStatus').textContent = data.message || 'Automation command applied.';
  q('leftStatus').textContent = data.message || 'Automation command applied.';
  if (data.intent && data.intent.switch && data.intent.switch !== 'whole_network') {
    if (q('autoVlanSwitch')) q('autoVlanSwitch').value = data.intent.switch;
    if (q('vlanSwitch')) q('vlanSwitch').value = data.intent.switch;
    if (q('interconnectSwitch')) q('interconnectSwitch').value = data.intent.switch;
  }
  if (data.intent && Array.isArray(data.intent.vlan_ids) && data.intent.vlan_ids.length && q('autoVlanList')) {
    q('autoVlanList').value = data.intent.vlan_ids.join(',');
  }
  if (data.intent && Array.isArray(data.intent.allow_between) && q('autoVlanLinks')) {
    q('autoVlanLinks').value = data.intent.allow_between
      .map(pair => `${pair[0]}-${pair[1]}`)
      .join(',');
  }
  await refresh();
}
function renderDeviceInspector(title, lines, options = {}) {
  const deviceId = String(options.deviceId || '');
  q('deviceInspectTitle').textContent = title || 'No node selected';
  q('deviceConfigPane').textContent = Array.isArray(lines)
    ? lines.join('\n')
    : (lines || 'No configuration data available.');
  setRemoveButtonState(deviceId, Boolean(options.removable));
  setEditButtonState(deviceId, Boolean(options.editable));
  if (!options.editable || state.deviceEditTarget !== deviceId) {
    hideDeviceEditForm();
  }
}
function renderEmptyInspector(message) {
  renderDeviceInspector(
    'No node selected',
    message || 'Select a switch or endpoint from the inventory or topology map to review its live configuration.',
    { removable: false, editable: false }
  );
}
function buildSwitchInspectorLines(node) {
  const lines = [
    'Node type: OpenFlow switch',
    `Switch ID: ${node.id}`,
    `Display label: ${node.label || node.id}`,
    `Average link utilization: ${fmt(node.util, 1)}%`,
    `Routing role: ${titleize(node.route_role || 'none')}`,
    'Lifecycle: Core topology infrastructure (protected from dashboard removal).'
  ];
  const automation = switchAutomationProfile(node.id);
  if (automation) {
    const vlanIds = sortNumericStrings(Object.keys(automation.vlans || {}));
    const interconnects = Array.isArray(automation.allow_between) ? automation.allow_between : [];
    lines.push(`Automation VLANs: ${vlanIds.length ? vlanIds.map(vlan => 'VLAN ' + vlan).join(', ') : 'none'}`);
    lines.push(
      `Cross-VLAN policy: ${
        interconnects.length
          ? interconnects.map(pair => `VLAN ${pair.src_vlan} <-> VLAN ${pair.dst_vlan}`).join(', ')
          : 'none'
      }`
    );
  } else {
    lines.push('Automation VLANs: none');
  }
  return lines;
}
function buildEndpointInspectorLines(device) {
  const running = ((state.operations && state.operations.running_stress_clients) || []);
  const category = device.category_label || categoryLabel(device.category);
  const management = device.management_origin === 'dashboard_added' || device.removable
    ? 'Dashboard-added endpoint (removable)'
    : 'Baseline campus endpoint (protected)';
  const routeRole = device.route_role && device.route_role !== 'none'
    ? titleize(device.route_role)
    : 'Not part of the protected route';
  const accessProfile = Number.isFinite(Number(device.bandwidth_mbps))
    ? `${fmt(device.bandwidth_mbps, 1)} Mbps${device.delay ? `, ${device.delay} delay` : ''}`
    : (device.delay || '-');
  const lines = [
    `Node type: ${device.kind === 'dynamic' || device.removable ? 'Dynamic endpoint' : 'Campus endpoint'}`,
    `Display name: ${device.display_name || device.label || device.name || '-'}`,
    `Endpoint ID: ${device.name || device.id || '-'}`,
    `Category: ${category}`,
    `IP address: ${device.ip || '-'}`,
    `MAC address: ${device.mac || '-'}`,
    `Attached switch: ${device.attach_switch || '-'}`,
    `Access profile: ${accessProfile}`,
    `Host interface: ${device.host_interface || device.default_intf || '-'}`,
    `Switch interface: ${device.switch_interface || '-'}`,
    `Switch port: ${device.switch_port != null ? device.switch_port : '-'}`,
    `Routing role: ${routeRole}`,
    `Traffic-test state: ${device.stress_active || running.includes(device.name || device.id) ? 'Active client workload' : 'Idle'}`,
    `Provisioning: ${management}`,
    `Default route: ${device.default_route || 'not published'}`
  ];
  const automation = deviceAutomationProfile(device.name || device.id);
  if (automation) {
    lines.push(`Automation VLAN: VLAN ${automation.vlanId} on ${automation.switchName}`);
    lines.push(
      `Cross-network communication: ${
        automation.linkedVlans.length
          ? 'Allowed with ' + automation.linkedVlans.map(vlan => `VLAN ${vlan}`).join(', ')
          : 'Isolated to its own VLAN'
      }`
    );
  } else {
    lines.push('Automation VLAN: none');
  }
  const interfaces = Array.isArray(device.interfaces) ? device.interfaces.filter(i => i && i.name) : [];
  if (interfaces.length) {
    lines.push('Interfaces:');
    interfaces.forEach(intf => {
      lines.push(`  ${intf.name} | IP ${intf.ip || '-'} | MAC ${intf.mac || '-'}`);
    });
  }
  if (device.warning) {
    lines.push(`Note: ${device.warning}`);
  }
  return lines;
}
function syncSelectedInspector() {
  if (!state.selectedNode) {
    renderEmptyInspector();
    return;
  }
  const node = findTopologyNode(state.selectedNode);
  if (!node) {
    state.selectedNode = null;
    q('selectedNode').textContent = 'Selected node: none';
    renderEmptyInspector('The selected node is no longer present in the live topology.');
    return;
  }
  if (!isEndpointNode(node)) {
    renderDeviceInspector(node.label || node.id, buildSwitchInspectorLines(node), { removable: false, editable: false });
    return;
  }
  if (state.loadingDeviceId === node.id && !state.deviceDetailsCache[node.id]) {
    renderDeviceInspector(node.label || node.id, 'Loading live endpoint configuration...', { removable: false, editable: false });
    return;
  }
  const cached = state.deviceDetailsCache[node.id];
  const device = cached ? { ...node, ...cached } : node;
  renderDeviceInspector(
    device.display_name || device.label || device.name || node.id,
    buildEndpointInspectorLines(device),
    {
      removable: Boolean(device.removable),
      editable: Boolean(device.removable),
      deviceId: device.name || node.id,
    }
  );
}
async function inspectSelectedNode(nodeId, force = false) {
  const node = findTopologyNode(nodeId);
  if (!node) {
    renderEmptyInspector('The selected node is not present in the topology snapshot.');
    return;
  }
  state.selectedNode = node.id;
  if (!isEndpointNode(node)) {
    syncSelectedInspector();
    return;
  }
  if (!force && state.deviceDetailsCache[node.id]) {
    syncSelectedInspector();
    return;
  }
  state.loadingDeviceId = node.id;
  syncSelectedInspector();
  const data = await api('/api/devices/' + encodeURIComponent(node.id));
  if (state.loadingDeviceId === node.id) {
    state.loadingDeviceId = '';
  }
  if (state.selectedNode !== node.id) return;
  if (data && data.device) {
    state.deviceDetailsCache[node.id] = data.warning
      ? { ...data.device, warning: data.warning }
      : data.device;
  } else {
    state.deviceDetailsCache[node.id] = {
      ...node,
      warning: (data && (data.error || data.message)) || 'Live device details are unavailable right now.'
    };
  }
  syncSelectedInspector();
}
async function removeSelectedDevice() {
  const btn = q('btnRemoveDevice');
  const deviceId = btn && btn.dataset ? btn.dataset.deviceId : '';
  if (!deviceId) {
    q('leftStatus').textContent = 'Remove endpoint skipped: select a dashboard-added endpoint first.';
    return;
  }
  const device = state.deviceDetailsCache[deviceId] || findTopologyNode(deviceId) || { name: deviceId };
  const label = device.display_name || device.label || device.name || deviceId;
  if (!window.confirm(`Remove ${label} [${deviceId}] from the live topology?`)) {
    return;
  }
  const data = await api('/api/devices/' + encodeURIComponent(deviceId), { method: 'DELETE' });
  if (!data || data.error || data.ok === false) {
    q('leftStatus').textContent = 'Remove endpoint failed: ' + ((data && (data.error || data.message)) || 'unknown error');
    return;
  }
  delete state.deviceDetailsCache[deviceId];
  if (state.selectedNode === deviceId) {
    state.selectedNode = null;
    q('selectedNode').textContent = 'Selected node: none';
    renderEmptyInspector('Endpoint removed from the live topology.');
  }
  q('leftStatus').textContent = data.message || `Endpoint removed: ${label}`;
  await refresh();
  closeDeviceModal();
}
async function refreshSelectedDevice() {
  if (!state.selectedNode) {
    q('leftStatus').textContent = 'Select a node first, then refresh its configuration view.';
    return;
  }
  delete state.deviceDetailsCache[state.selectedNode];
  await inspectSelectedNode(state.selectedNode, true);
  q('leftStatus').textContent = 'Selected node configuration refreshed.';
}
async function openSelectedDeviceEditor() {
  if (!state.selectedNode) {
    q('leftStatus').textContent = 'Select an endpoint first, then open its configuration editor.';
    return;
  }
  const node = findTopologyNode(state.selectedNode);
  if (!node || !isEndpointNode(node)) {
    q('leftStatus').textContent = 'Switch infrastructure can be inspected, but only dashboard-added endpoints can be edited live.';
    return;
  }
  await inspectSelectedNode(node.id, true);
  const device = { ...node, ...(state.deviceDetailsCache[node.id] || {}) };
  if (!device.removable) {
    q('deviceEditStatus').textContent = 'Built-in campus endpoints are protected. Add a dashboard endpoint if you want live editing controls.';
    q('leftStatus').textContent = 'This endpoint is protected from live editing.';
    return;
  }
  state.deviceEditMode = true;
  state.deviceEditTarget = String(device.name || node.id);
  q('deviceEditForm').hidden = false;
  populateDeviceEditForm(device);
  openDeviceModal();
}
function cancelDeviceEdit() {
  hideDeviceEditForm();
  q('deviceEditStatus').textContent = 'Endpoint editing cancelled.';
}
async function saveDeviceConfig(ev) {
  ev.preventDefault();
  const deviceId = String(q('editDeviceId').value || '').trim();
  const payload = {
    display_name: q('editDisplayName').value.trim(),
    ip: q('editIp').value.trim(),
    attach_switch: q('editSwitch').value,
    category: q('editCategory').value,
    bandwidth_mbps: Number(q('editBw').value || 0),
  };
  if (!deviceId) {
    q('deviceEditStatus').textContent = 'Edit failed: no endpoint is selected.';
    return;
  }
  const campusMatch = /^10\.0\.0\.(\d{1,3})$/.exec(payload.ip);
  if (!campusMatch) {
    q('deviceEditStatus').textContent = 'Edit failed: use a campus IP inside 10.0.0.0/24.';
    return;
  }
  const hostOctet = Number(campusMatch[1]);
  if (!Number.isInteger(hostOctet) || hostOctet <= 0 || hostOctet >= 255) {
    q('deviceEditStatus').textContent = 'Edit failed: choose a valid host IP inside 10.0.0.1-10.0.0.254.';
    return;
  }
  if (!payload.display_name) {
    q('deviceEditStatus').textContent = 'Edit failed: display name is required.';
    return;
  }
  if (!Number.isFinite(payload.bandwidth_mbps) || payload.bandwidth_mbps <= 0) {
    q('deviceEditStatus').textContent = 'Edit failed: bandwidth must be greater than 0 Mbps.';
    return;
  }
  const existingNodes = (state.topology && Array.isArray(state.topology.nodes)) ? state.topology.nodes : [];
  const duplicateIp = existingNodes.find(node => String(node.id || '') !== deviceId && String(node.ip || '').trim() === payload.ip);
  if (duplicateIp) {
    q('deviceEditStatus').textContent = `Edit failed: ${payload.ip} is already assigned to ${duplicateIp.label || duplicateIp.id}.`;
    return;
  }
  const data = await api('/api/devices/' + encodeURIComponent(deviceId), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (!data || data.error || data.ok === false) {
    const msg = (data && (data.error || data.message)) || 'unknown error';
    q('deviceEditStatus').textContent = 'Edit failed: ' + msg;
    q('leftStatus').textContent = 'Edit endpoint failed: ' + msg;
    return;
  }
  state.deviceDetailsCache[deviceId] = { ...(state.deviceDetailsCache[deviceId] || {}), ...(data.device || {}) };
  q('deviceEditStatus').textContent = data.message || 'Endpoint configuration updated.';
  q('leftStatus').textContent = data.message || `Endpoint updated: ${payload.display_name}`;
  hideDeviceEditForm();
  await refresh();
  selectNode(deviceId, true);
}
async function saveNetworkSettings(ev) {
  ev.preventDefault();
  const payload = {
    congest_high_mbps: Number(q('cfgHighMbps').value || 0),
    congest_low_mbps: Number(q('cfgLowMbps').value || 0),
    port_congest_high_pct: Number(q('cfgPortHigh').value || 0),
    port_congest_low_pct: Number(q('cfgPortLow').value || 0),
  };
  if (
    !Number.isFinite(payload.congest_high_mbps) ||
    !Number.isFinite(payload.congest_low_mbps) ||
    !Number.isFinite(payload.port_congest_high_pct) ||
    !Number.isFinite(payload.port_congest_low_pct)
  ) {
    q('settingsStatus').textContent = 'Save failed: all network settings must be numeric.';
    return;
  }
  if (payload.congest_low_mbps >= payload.congest_high_mbps) {
    q('settingsStatus').textContent = 'Save failed: low throughput threshold must stay below the high threshold.';
    return;
  }
  if (payload.port_congest_low_pct >= payload.port_congest_high_pct) {
    q('settingsStatus').textContent = 'Save failed: low port-utilization threshold must stay below the high threshold.';
    return;
  }
  const data = await api('/api/network/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (!data || data.error || data.ok === false) {
    const msg = (data && (data.error || data.message)) || 'unknown error';
    q('settingsStatus').textContent = 'Save failed: ' + msg;
    q('leftStatus').textContent = 'Network settings update failed: ' + msg;
    return;
  }
  state.networkSettingsDirty = false;
  q('settingsStatus').textContent = data.message || 'Network settings published to the controller.';
  q('leftStatus').textContent = data.message || 'Network policy settings saved.';
  await refresh();
}
function markNetworkSettingsDirty() {
  state.networkSettingsDirty = true;
  q('settingsStatus').textContent = 'Network settings changed locally. Save to apply them to the live controller.';
}
function resetNetworkSettingsForm() {
  fillNetworkSettingsForm(state.metrics || {}, true);
  q('leftStatus').textContent = 'Network settings form restored to the current live values.';
}
async function assignDeviceToVlan(ev) {
  ev.preventDefault();
  const switchName = currentAutomationSwitch();
  const deviceName = q('vlanDevice').value;
  const vlanId = Number(q('vlanId').value || 0);
  if (!deviceName) {
    q('vlanStatus').textContent = 'Assignment failed: choose an endpoint attached to the selected switch.';
    return;
  }
  if (!Number.isInteger(vlanId) || vlanId <= 0 || vlanId > 4094) {
    q('vlanStatus').textContent = 'Assignment failed: choose a VLAN ID between 1 and 4094.';
    return;
  }
  const data = await api('/api/network/automation/vlans', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ switch: switchName, device: deviceName, vlan_id: vlanId })
  });
  if (!data || data.error || data.ok === false) {
    const msg = (data && (data.error || data.message)) || 'unknown error';
    q('vlanStatus').textContent = 'Assignment failed: ' + msg;
    q('leftStatus').textContent = 'VLAN assignment failed: ' + msg;
    return;
  }
  state.automation = data.automation || {};
  renderNetworkAutomationPanel();
  q('vlanStatus').textContent = data.message || `Assigned ${deviceName} to VLAN ${vlanId} on ${switchName}.`;
  q('leftStatus').textContent = data.message || `Controller VLAN automation updated for ${deviceName}.`;
  await refresh();
}
async function removeVlanConfig(switchName, vlanId, deviceId = '') {
  const target = deviceId
    ? `/api/network/automation/vlans/${encodeURIComponent(switchName)}/${encodeURIComponent(vlanId)}/${encodeURIComponent(deviceId)}`
    : `/api/network/automation/vlans/${encodeURIComponent(switchName)}/${encodeURIComponent(vlanId)}`;
  const data = await api(target, { method: 'DELETE' });
  if (!data || data.error || data.ok === false) {
    const msg = (data && (data.error || data.message)) || 'unknown error';
    q('vlanStatus').textContent = 'Remove failed: ' + msg;
    q('leftStatus').textContent = 'VLAN automation update failed: ' + msg;
    return;
  }
  state.automation = data.automation || {};
  renderNetworkAutomationPanel();
  q('vlanStatus').textContent = data.message || 'VLAN automation removed.';
  q('leftStatus').textContent = data.message || 'Controller VLAN automation updated.';
  await refresh();
}
async function updateVlanInterconnect(ev, options = null) {
  if (ev) ev.preventDefault();
  const switchName = options && options.switchName ? options.switchName : currentInterconnectSwitch();
  const vlanA = Number(options && options.vlanA != null ? options.vlanA : (q('interconnectVlanA').value || 0));
  const vlanB = Number(options && options.vlanB != null ? options.vlanB : (q('interconnectVlanB').value || 0));
  const enabled = options && Object.prototype.hasOwnProperty.call(options, 'enabled') ? Boolean(options.enabled) : true;
  if (!Number.isInteger(vlanA) || !Number.isInteger(vlanB) || vlanA <= 0 || vlanB <= 0) {
    q('vlanStatus').textContent = 'Cross-VLAN policy failed: choose two existing VLANs on the selected switch.';
    return;
  }
  if (vlanA === vlanB) {
    q('vlanStatus').textContent = 'Cross-VLAN policy failed: choose two different VLANs.';
    return;
  }
  const data = await api('/api/network/automation/interconnect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      switch: switchName,
      vlan_a: vlanA,
      vlan_b: vlanB,
      enabled
    })
  });
  if (!data || data.error || data.ok === false) {
    const msg = (data && (data.error || data.message)) || 'unknown error';
    q('vlanStatus').textContent = 'Cross-VLAN policy failed: ' + msg;
    q('leftStatus').textContent = 'Cross-VLAN policy update failed: ' + msg;
    return;
  }
  state.automation = data.automation || {};
  renderNetworkAutomationPanel();
  q('vlanStatus').textContent = data.message || options && options.statusMessage || 'Cross-VLAN communication policy updated.';
  q('leftStatus').textContent = data.message || options && options.statusMessage || 'Controller VLAN communication policy updated.';
  await refresh();
}
function selectNode(nodeId, forceRefresh = false) {
  const node = findTopologyNode(nodeId);
  if (!node) return;
  openDeviceModal();
  showNodeDetails(node, { inspect: false });
  inspectSelectedNode(node.id, forceRefresh);
}
function nodeSize(node) {
  return node && node.kind === 'switch'
    ? { width: 104, height: 32 }
    : { width: 88, height: 30 };
}
function clampNodePosition(node, x, y) {
  const size = nodeSize(node);
  const marginX = size.width / 2 + 12;
  const marginY = size.height / 2 + 12;
  return {
    x: Math.max(marginX, Math.min(TOPOLOGY_VIEWBOX.width - marginX, Number(x || 0))),
    y: Math.max(marginY, Math.min(TOPOLOGY_VIEWBOX.height - marginY, Number(y || 0)))
  };
}
function loadTopologyLayout() {
  try {
    const raw = window.localStorage.getItem(TOPOLOGY_LAYOUT_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    state.customPositions = parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    state.customPositions = {};
  }
}
function saveTopologyLayout() {
  try {
    window.localStorage.setItem(TOPOLOGY_LAYOUT_KEY, JSON.stringify(state.customPositions || {}));
  } catch {}
}
function pruneTopologyLayout(nodes) {
  const active = new Set((nodes || []).map(n => n.id));
  let changed = false;
  Object.keys(state.customPositions || {}).forEach(id => {
    if (!active.has(id)) {
      delete state.customPositions[id];
      changed = true;
    }
  });
  if (changed) saveTopologyLayout();
}
function applyCustomTopologyLayout(topo) {
  if (!topo || !Array.isArray(topo.nodes)) return;
  topo.nodes.forEach(node => {
    const saved = state.customPositions && state.customPositions[node.id];
    if (!saved) return;
    const x = Number(saved.x);
    const y = Number(saved.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    const pos = clampNodePosition(node, x, y);
    node.x = pos.x;
    node.y = pos.y;
  });
  pruneTopologyLayout(topo.nodes);
}
function topologyPointFromEvent(ev) {
  const svg = q('topologySvg');
  if (!svg) return { x: 0, y: 0 };
  const rect = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  return {
    x: vb.x + (ev.clientX - rect.left) * (vb.width / Math.max(rect.width, 1)),
    y: vb.y + (ev.clientY - rect.top) * (vb.height / Math.max(rect.height, 1))
  };
}
function beginNodeDrag(ev, nodeId) {
  if (ev.button != null && ev.button !== 0) return;
  const topo = state.topology || { nodes: [] };
  const node = (topo.nodes || []).find(n => n.id === nodeId);
  if (!node) return;
  const point = topologyPointFromEvent(ev);
  state.drag = {
    nodeId,
    pointerId: ev.pointerId,
    offsetX: point.x - Number(node.x || 0),
    offsetY: point.y - Number(node.y || 0),
    moved: false
  };
  q('topologySvg').classList.add('dragging');
  showNodeDetails(node);
  ev.preventDefault();
}
function updateNodeDrag(ev) {
  const drag = state.drag;
  if (!drag) return;
  if (ev.pointerId != null && drag.pointerId != null && ev.pointerId !== drag.pointerId) return;
  const topo = state.topology || { nodes: [] };
  const node = (topo.nodes || []).find(n => n.id === drag.nodeId);
  if (!node) return;
  const point = topologyPointFromEvent(ev);
  const pos = clampNodePosition(node, point.x - drag.offsetX, point.y - drag.offsetY);
  const dx = Math.abs(pos.x - Number(node.x || 0));
  const dy = Math.abs(pos.y - Number(node.y || 0));
  if (dx < 0.15 && dy < 0.15) return;
  node.x = pos.x;
  node.y = pos.y;
  state.customPositions[node.id] = { x: pos.x, y: pos.y };
  state.drag.moved = true;
  renderTopology();
  showNodeDetails(node);
  ev.preventDefault();
}
function endNodeDrag(ev) {
  const drag = state.drag;
  if (!drag) return;
  if (ev && ev.pointerId != null && drag.pointerId != null && ev.pointerId !== drag.pointerId) return;
  state.drag = null;
  q('topologySvg').classList.remove('dragging');
  if (drag.moved) {
    saveTopologyLayout();
    state.suppressNodeClickUntil = Date.now() + 250;
    q('leftStatus').textContent = 'Topology layout updated and saved in this browser.';
  }
}
async function resetTopologyLayout() {
  state.customPositions = {};
  saveTopologyLayout();
  q('leftStatus').textContent = 'Topology layout reset to the default controller view.';
  await refresh();
}
function renderSparkline(id, values, color) {
  const svg = q(id);
  if (!svg) return;
  const clean = (values || []).map(Number).filter(v => Number.isFinite(v));
  if (!clean.length) {
    svg.innerHTML = '';
    return;
  }
  const w = 180;
  const h = 42;
  if (clean.length === 1) {
    svg.innerHTML = `<circle cx="${w / 2}" cy="${h / 2}" r="3.5" fill="${color}"></circle>`;
    return;
  }
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = Math.max(max - min, 0.001);
  const pts = clean.map((v, i) => {
    const x = (i / (clean.length - 1)) * (w - 8) + 4;
    const y = h - 4 - ((v - min) / span) * (h - 10);
    return `${x},${y}`;
  }).join(' ');
  svg.innerHTML = `<polyline fill="none" stroke="${color}" stroke-width="2.5" points="${pts}"></polyline>`;
}
function renderMultiSparkline(id, series, forcedMax = null) {
  const svg = q(id);
  if (!svg) return;
  const usable = (series || [])
    .map(s => ({
      color: s && s.color ? s.color : '#58d6ff',
      points: Array.isArray(s && s.points) ? s.points.map(Number).filter(v => Number.isFinite(v)) : []
    }))
    .filter(s => s.points.length);
  if (!usable.length) {
    svg.innerHTML = '';
    return;
  }
  const w = 180;
  const h = 42;
  const maxLen = Math.max(...usable.map(s => s.points.length));
  if (maxLen === 1) {
    svg.innerHTML = usable.map((s, idx) => {
      const cx = 24 + idx * 18;
      return `<circle cx="${cx}" cy="${h / 2}" r="3.2" fill="${s.color}"></circle>`;
    }).join('');
    return;
  }
  let max = Number(forcedMax);
  if (!Number.isFinite(max)) {
    max = Math.max(...usable.flatMap(s => s.points), 0.001);
  }
  max = Math.max(max, 0.001);
  const baseline = `<line x1="4" y1="${h - 4}" x2="${w - 4}" y2="${h - 4}" stroke="rgba(255,255,255,0.16)" stroke-width="1"></line>`;
  const lines = usable.map(s => {
    const pts = s.points.map((v, i) => {
      const x = s.points.length === 1 ? (w / 2) : (i / (s.points.length - 1)) * (w - 8) + 4;
      const y = h - 4 - (Math.max(0, v) / max) * (h - 10);
      return `${x},${y}`;
    }).join(' ');
    return `<polyline fill="none" stroke="${s.color}" stroke-width="2.4" points="${pts}"></polyline>`;
  }).join('');
  svg.innerHTML = baseline + lines;
}
function renderChartLegend(id, series) {
  const el = q(id);
  if (!el) return;
  const items = (series || []).filter(s => s && s.label);
  el.innerHTML = items.map(s => `<span><i style="background:${s.color || '#58d6ff'}"></i>${s.label}</span>`).join('');
}
function lastSeriesValue(series) {
  if (!series || !Array.isArray(series.points) || !series.points.length) return null;
  const value = Number(series.points[series.points.length - 1]);
  return Number.isFinite(value) ? value : null;
}
function setReportLink(id, enabled, href) {
  const el = q(id);
  if (!el) return;
  el.href = href;
  el.setAttribute('aria-disabled', enabled ? 'false' : 'true');
}
function scenarioConfig(forcedKey = null) {
  const selectedKey = forcedKey || (q('scenarioSelect') ? q('scenarioSelect').value : 'campus');
  return SCENARIO_CATALOG[selectedKey] || SCENARIO_CATALOG.campus;
}
function setScenario(key) {
  if (q('scenarioSelect') && SCENARIO_CATALOG[key]) {
    q('scenarioSelect').value = key;
  }
  renderScenarioPicker();
}
function renderScenarioPicker() {
  const current = scenarioConfig().key;
  document.querySelectorAll('[data-scenario-card]').forEach(card => {
    card.classList.toggle('active', card.getAttribute('data-scenario-card') === current);
  });
  const hint = q('scenarioHint');
  if (hint) {
    const scenario = scenarioConfig();
    hint.textContent = `Selected traffic demo: ${scenario.label}`;
  }
}
async function runScenarioByKey(key) {
  setScenario(key);
  await startStressDemo();
}

async function api(url, opts) {
  const r = await fetch(url, opts || {});
  const text = await r.text();
  try { return JSON.parse(text); } catch { return { error: text || ('HTTP ' + r.status) }; }
}

function wireTabs() {
  document.querySelectorAll('.tab[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab[data-tab]').forEach(x => x.classList.remove('active'));
      btn.classList.add('active');
      const key = btn.getAttribute('data-tab');
      ['topology','traffic','heat'].forEach(k => q('view-' + k).classList.toggle('active', k === key));
    });
  });
  document.querySelectorAll('.tab[data-right]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab[data-right]').forEach(x => x.classList.remove('active'));
      btn.classList.add('active');
      const key = btn.getAttribute('data-right');
      ['metrics','events','flows','ops'].forEach(k => q('right-' + k).classList.toggle('active', k === key));
    });
  });
}

function updateHeader() {
  const m = state.metrics || {};
  const topo = state.topology || { nodes: [] };
  const d = state.dashboard || {};
  const health = d.health || {};
  const sw = (m.connected_switches || []).length;
  const online = sw > 0;
  const core = Number(m.core_primary_mbps || 0);
  const policyProfiles = m.priority_profiles || {};
  const classCount = Object.keys(policyProfiles).length;
  q('bController').textContent = 'SDN controller: ' + (online ? 'Online' : 'Offline');
  q('bController').className = 'badge ' + (online ? 'good' : 'bad');
  q('bSwitches').textContent = 'OpenFlow switches: ' + sw;
  q('bCore').textContent = 'Protected path load: ' + core.toFixed(1) + ' Mbps';
  q('bCore').className = 'badge ' + utilClass(core / Math.max(1, Number(m.congest_high_mbps || 1)) * 100);
  q('bPolicy').textContent = 'Traffic policy: ' + classCount + ' class(es)' + (m.reroute_active ? ' + adaptive reroute' : '');
  q('bPolicy').className = 'badge ' + (classCount > 0 ? 'good' : 'bad');
  q('bHealth').textContent = 'Service health: ' + titleize(health.label || 'unknown');
  q('bHealth').className = 'badge ' + String(health.class_name || '');

  q('mCoreLoad').textContent = core.toFixed(1) + ' Mbps';
  q('mCoreLoad').className = 'v ' + utilClass(core / Math.max(1, Number(m.congest_high_mbps || 1)) * 100);
  q('mThreshold').textContent = 'Congestion threshold low/high: ' + Number(m.congest_low_mbps || 0).toFixed(1) + '/' + Number(m.congest_high_mbps || 0).toFixed(1) + ' Mbps';
  const hosts = (topo.nodes || []).filter(n => n.kind === 'host' || n.kind === 'dynamic').length;
  q('mHosts').textContent = String(hosts);
}

function renderExecutiveSummary() {
  const m = state.metrics || {};
  const topo = state.topology || { nodes: [], links: [] };
  const d = state.dashboard || {};
  const route = d.route_overview || {};
  const health = d.health || {};
  const flows = d.active_flow_rules || {};
  const nodes = topo.nodes || [];
  const links = topo.links || [];
  const switches = nodes.filter(n => n.kind === 'switch');
  const endpoints = nodes.filter(n => n.kind !== 'switch');
  const dynamicCount = endpoints.filter(n => n.kind === 'dynamic').length;
  const hottest = links.slice().sort((a, b) => Number(b.util || 0) - Number(a.util || 0))[0];
  const controllerOnline = (m.connected_switches || []).length > 0;
  q('sumController').textContent = controllerOnline ? 'Online' : 'Offline';
  q('sumController').className = 'summaryValue ' + (controllerOnline ? 'good' : 'bad');
  q('sumControllerSub').textContent =
    `${switches.length} switches discovered | ${Number(flows.total || 0)} active OpenFlow rules`;

  q('sumEndpoints').textContent = String(endpoints.length);
  q('sumEndpoints').className = 'summaryValue';
  q('sumEndpointsSub').textContent =
    `${dynamicCount} dashboard-added endpoints | ${switches.length} switching nodes`;

  if (hottest) {
    q('sumHotLink').textContent = `${fmt(hottest.util, 0)}%`;
    q('sumHotLink').className = 'summaryValue ' + utilClass(Number(hottest.util || 0));
    q('sumHotLinkSub').textContent =
      `${hottest.src} > ${hottest.dst} | ${fmt(hottest.mbps, 1)} Mbps live traffic`;
  } else {
    q('sumHotLink').textContent = 'Idle';
    q('sumHotLink').className = 'summaryValue';
    q('sumHotLinkSub').textContent = 'No sampled links are active yet.';
  }

  const policyText = m.reroute_active ? 'Adaptive' : 'Normal';
  q('sumPolicy').textContent = policyText;
  q('sumPolicy').className = 'summaryValue ' + (m.reroute_active ? 'warn' : 'good');
  q('sumPolicySub').textContent =
    `${route.short_status || health.summary || 'Protected route ready'} | QoS ${m.student_throttle_active ? 'active' : 'standby'}`;
}

function renderInventory() {
  const topo = state.topology || { nodes: [] };
  const switches = topo.nodes.filter(n => n.kind === 'switch');
  const hosts = topo.nodes.filter(n => n.kind !== 'switch');
  q('switchList').innerHTML = switches.map(s => {
    const util = pct(s.util || 0);
    return `<div class="item"><div class="itemMain">${nodeGlyphMarkup('switch', 'switch')}<div class="itemText"><div class="itemTitle">${s.label}</div><small>${s.id}</small></div></div><div class="itemMeta"><div class="chip accent">Load ${util.toFixed(0)}%</div><div class="itemActions"><button class="miniBtn" type="button" data-node-view="${s.id}">View</button></div></div></div>`;
  }).join('') || '<div class="item"><small>No switches</small></div>';
  q('hostList').innerHTML = hosts.map(h => {
    const ip = h.ip ? ' — ' + h.ip : '';
    const label = h.label || h.id;
    const category = categoryLabel(h.category);
    const newBadge = h.kind === 'dynamic' ? '<div class="chip accent">new</div>' : '';
    const removeBtn = h.removable
      ? `<button class="miniBtn danger" type="button" data-device-remove="${h.id}">Remove</button>`
      : `<button class="miniBtn" type="button" disabled>Protected</button>`;
    return `<div class="item"><div class="itemMain">${nodeGlyphMarkup(h.kind, h.category)}<div class="itemText"><div class="itemTitle">${label}</div><small>${h.id}${ip}</small></div></div><div class="itemMeta"><div class="chip">${category}</div><div class="itemActions"><button class="miniBtn" type="button" data-node-view="${h.id}">Config</button>${removeBtn}</div>${newBadge}</div></div>`;
  }).join('') || '<div class="item"><small>No hosts</small></div>';
  document.querySelectorAll('[data-node-view]').forEach(btn => {
    btn.addEventListener('click', () => selectNode(btn.getAttribute('data-node-view'), false));
  });
  document.querySelectorAll('[data-device-remove]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const nodeId = btn.getAttribute('data-device-remove');
      selectNode(nodeId, false);
      const removeBtnEl = q('btnRemoveDevice');
      if (removeBtnEl) removeBtnEl.dataset.deviceId = nodeId;
      await removeSelectedDevice();
    });
  });
}

function renderMetricsPanel() {
  const d = state.dashboard || {};
  const topo = state.topology || { links: [] };
  const links = Array.isArray(d.link_utilization) && d.link_utilization.length
    ? d.link_utilization
    : (topo.links || []);
  const bars = links.slice(0, 8).map(l => {
    const u = pct(l.util_pct != null ? l.util_pct : l.util);
    const mbps = Number(l.mbps || 0);
    const bw = Number(l.bw_mbps || 0);
    const rateText = bw > 0 ? `${mbps.toFixed(1)}/${bw.toFixed(0)} Mbps` : `${mbps.toFixed(1)} Mbps`;
    const role = String(l.route_role || 'none');
    const roleText = role === 'active' ? ' | active route' : (role === 'standby' ? ' | standby route' : '');
    const stateText = mbps < 0.1 ? ' | idle' : '';
    return `<div style="margin-bottom:8px;">
      <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--muted)"><span>${l.src}>${l.dst}${roleText}</span><span>${rateText} | ${u.toFixed(0)}%${stateText}</span></div>
      <div class="bar"><span style="width:${u}%; background:${utilColor(u)}"></span></div>
    </div>`;
  }).join('');
  q('linkBars').innerHTML = bars || '<small>No links</small>';
}

function renderDashboardInsights() {
  const d = state.dashboard || {};
  const health = d.health || {};
  const telemetry = d.telemetry || {};
  const queue = d.queue_depth || {};
  const latency = d.latency_trend || {};
  const flows = d.active_flow_rules || {};
  const route = d.route_overview || {};
  const actions = d.controller_actions || {};
  const ai = d.ai_summary || {};
  const systemMode = d.system_mode || {};
  const policyClasses = Array.isArray(d.policy_classes) ? d.policy_classes : [];
  const latestEval = d.latest_evaluation || {};
  const recentStory = Array.isArray(d.recent_story) ? d.recent_story : [];
  const storyDigest = Array.isArray(d.story_digest) ? d.story_digest : [];
  const flowExplain = Array.isArray(d.flow_explanation) ? d.flow_explanation : [];
  const whyLines = Array.isArray(d.why_explanations) ? d.why_explanations : [];
  const alerts = Array.isArray(d.alerts) ? d.alerts : [];
  const charts = d.charts || {};
  const trafficChart = charts.traffic || {};
  const pressureChart = charts.pressure || {};
  const trafficSeries = Array.isArray(trafficChart.series) ? trafficChart.series : [];
  const pressureSeries = Array.isArray(pressureChart.series) ? pressureChart.series : [];

  q('mHealth').textContent = titleize(health.label || 'unknown');
  q('mHealth').className = 'v ' + String(health.class_name || '');
  q('mHealthProof').textContent = 'Health summary: ' + String(health.summary || 'No summary yet.');
  q('mMetricsFresh').textContent =
    'Network status: metrics ' + formatAge(telemetry.metrics_age_s)
    + ` | sampled links ${Number(telemetry.active_links || 0)}/${Number(telemetry.total_links || 0)}`
    + ` | last reachability test ${formatAge(telemetry.ping_age_s)}`
    + ` | runtime API ${telemetry.runtime_ok ? 'online' : 'offline'}`;
  q('mSystemMode').textContent = [
    `Network state: ${titleize(systemMode.network_mode || 'normal')}`,
    `Traffic policy: ${titleize(systemMode.policy_mode || 'normal')}`,
    `AI control mode: ${titleize(systemMode.ai_mode || 'idle')}`,
    `Active test: ${systemMode.scenario || 'no live traffic test'}`
  ].join('\n');

  q('mRoute').textContent = titleize(route.short_status || 'unknown');
  q('mRoute').className = 'v ' + (actions.reroute_active ? 'warn' : 'good');
  q('mRouteDetail').textContent = 'Active path: ' + String(route.active_label || '-');
  q('mRouteDecision').textContent = 'Decision source: ' + String(route.decision_source || '-');
  q('mBackup').textContent = 'Standby path: ' + String(route.standby_label || '-');
  q('mWhyPane').textContent = whyLines.length
    ? whyLines.join('\n')
    : 'No routing rationale is available yet.';

  q('mThroughput').textContent = formatRateState(state.metrics && state.metrics.core_primary_mbps);
  q('mLoss').textContent = telemetry.traffic_mode
    || (state.metrics && state.metrics.student_throttle_active
      ? 'Student bulk traffic is currently rate-limited by QoS policy.'
      : 'Traffic policy state: normal');

  const lastPing = state.operations && state.operations.last_pingall_result ? state.operations.last_pingall_result : {};
  if (lastPing && lastPing.ok) {
    q('mPingLoss').textContent = fmt(lastPing.packet_loss_pct, 1) + '% loss';
    q('mPingRtt').textContent = 'Avg RTT: ' + fmt(lastPing.avg_rtt_ms, 2) + ' ms';
    q('mPingPairs').textContent = 'Host pairs tested/failed: '
      + Number(lastPing.pairs_total || 0) + '/' + Number(lastPing.pairs_failed || 0);
  } else {
    q('mPingLoss').textContent = '-';
    q('mPingRtt').textContent = 'Avg RTT: -';
    q('mPingPairs').textContent = 'Host pairs: -';
  }

  const qTotal = Number(queue.total_packets || 0);
  q('mQueueDepth').textContent = qTotal + ' pkts' + (qTotal === 0 ? ' (normal)' : '');
  q('mQueueDepth').className = 'v ' + utilClass(Number(queue.util_pct || 0));
  q('mQueueHint').textContent =
    'Estimated from Wi-Fi uplink utilization: '
    + String(queue.status || 'normal')
    + ' | util ' + fmt(queue.util_pct, 1) + '% (software estimate, not a hardware queue counter)';

  const dir = String(latency.direction || 'stable');
  const latest = Number(latency.latest_ms || 0);
  const avg = Number(latency.avg_ms || 0);
  const trendColor = dir === 'up' ? 'warn' : (dir === 'down' ? 'good' : '');
  q('mLatencyTrend').textContent = dir + ' (' + latest.toFixed(2) + ' ms)';
  q('mLatencyTrend').className = 'v ' + trendColor;
  q('mLatencyAvg').textContent = 'Average RTT: ' + avg.toFixed(2) + ' ms';
  const latencyChart = charts.latency || {};
  const latencySeries = Array.isArray(latencyChart.series) ? latencyChart.series : [];
  const latencyLine = latencySeries.length ? latencySeries[0].points : (latency.points || []).map(p => p.rtt_ms);
  renderSparkline('latencySpark', latencyLine, '#58d6ff');

  const protectedSeries = trafficSeries.find(s => s.key === 'protected_throughput_mbps');
  const wifiSeries = trafficSeries.find(s => s.key === 'wifi_load_mbps');
  const maxUtilSeries = pressureSeries.find(s => s.key === 'max_link_util_pct');
  const queueSeries = pressureSeries.find(s => s.key === 'queue_util_pct');
  const rerouteSeries = pressureSeries.find(s => s.key === 'reroute_active');
  const protectedNow = lastSeriesValue(protectedSeries);
  const wifiNow = lastSeriesValue(wifiSeries);
  const utilNow = lastSeriesValue(maxUtilSeries);
  const queueNow = lastSeriesValue(queueSeries);
  const rerouteNow = lastSeriesValue(rerouteSeries);
  const hasTrafficHistory = trafficSeries.some(s => Array.isArray(s.points) && s.points.length);
  const hasPressureHistory = pressureSeries.some(s => Array.isArray(s.points) && s.points.length);
  q('mTrafficTrend').textContent = hasTrafficHistory
    ? `Protected traffic ${formatRateState(protectedNow != null ? protectedNow : (state.metrics && state.metrics.core_primary_mbps))}`
      + ` | Wi-Fi traffic ${formatRateState(wifiNow != null ? wifiNow : (state.metrics && state.metrics.core_wifi_mbps))}`
    : 'collecting live samples';
  q('mPressureTrend').textContent = hasPressureHistory
    ? `Link util ${utilNow != null ? fmt(utilNow, 0) : '-'}% | Queue ${queueNow != null ? fmt(queueNow, 0) : '-'}% | ${Number(rerouteNow || 0) >= 50 ? 'reroute active' : 'standby path ready'}`
    : 'collecting live samples';
  q('mTrendWindow').textContent = 'Trend window: ' + (charts.window_label || 'collecting live history');
  renderMultiSparkline('trafficSpark', trafficSeries);
  renderMultiSparkline('pressureSpark', pressureSeries, 100);
  renderChartLegend('trafficLegend', trafficSeries);
  renderChartLegend('pressureLegend', pressureSeries);

  const totalFlows = Number(flows.total || 0);
  q('mActiveFlows').textContent = String(totalFlows);
  const perSwitch = flows.per_switch || {};
  const perText = Object.keys(perSwitch).length
    ? Object.entries(perSwitch).map(([k, v]) => `${k}:${v}`).join(', ')
    : '-';
  q('mFlowBySwitch').textContent = 'Per switch: ' + perText;

  if (latestEval.available) {
    const gainPct = latestEval.throughput_gain_pct != null ? `${fmt(latestEval.throughput_gain_pct, 1)}%` : 'n/a';
    const response = latestEval.response_s != null ? formatAge(latestEval.response_s) : 'n/a';
    const lines = [
      `Scenario: ${latestEval.tag || 'latest'}`,
      `Protected throughput before: ${fmt(latestEval.throughput_before_mbps, 3)} Mbps`,
      `Protected throughput after: ${fmt(latestEval.throughput_after_mbps, 3)} Mbps`,
      `Throughput improvement: +${fmt(latestEval.throughput_gain_mbps, 3)} Mbps (${gainPct})`,
      `Reachability loss before/after: ${fmt(latestEval.loss_before_pct, 1)}% -> ${fmt(latestEval.loss_after_pct, 1)}%`,
      `Latency before/after: ${fmt(latestEval.latency_before_ms, 3)} ms -> ${fmt(latestEval.latency_after_ms, 3)} ms`,
      `Congestion response time: ${response}`,
      `Reroute observed: ${latestEval.reroute_after ? 'Yes' : 'No'}`
    ];
    q('mEvalPane').textContent = lines.join('\n');
  } else {
    q('mEvalPane').textContent = 'No Stage 11 comparison report was found in results/.';
  }
  setReportLink('reportJsonLink', Boolean(latestEval.available), '/api/report/latest/json');
  setReportLink('reportCsvLink', Boolean(latestEval.available), '/api/report/latest/csv');
  setReportLink('reportMdLink', Boolean(latestEval.available), '/api/report/latest/md');

  if (!policyClasses.length) {
    q('mPolicyClasses').textContent = 'The controller has not published QoS classes yet.';
  } else {
    const grouped = {};
    for (const cls of policyClasses) {
      const key = cls.queue != null ? `q${cls.queue}` : 'q?';
      grouped[key] = grouped[key] || [];
      grouped[key].push(cls);
    }
    const order = Object.keys(grouped).sort();
    q('mPolicyClasses').textContent = order.map(key => {
      const rows = grouped[key];
      const names = rows.map(cls => cls.label).join(' / ');
      const status = rows.map(cls => titleize(cls.status)).filter((v, i, arr) => arr.indexOf(v) === i).join(', ');
      const hint = rows.map(cls => cls.live_hint).filter((v, i, arr) => arr.indexOf(v) === i)[0] || '';
      return `${key} - ${names}\n  ${status}\n  ${hint}`;
    }).join('\n\n');
  }

  const aiLines = [];
  aiLines.push(`AI control mode: ${titleize(ai.mode || 'idle')}`);
  aiLines.push(`Last evaluation time: ${formatClock(ai.last_evaluation_ts)}`);
  aiLines.push(`Last AI result: ${ai.last_result || 'No AI result published yet.'}`);
  aiLines.push(`Decision reason: ${ai.reason || 'No AI explanation published yet.'}`);
  aiLines.push(`Routing choice: ${ai.routing_choice || 'No reroute recommendation was needed.'}`);
  aiLines.push(`Action name: ${ai.action_name || 'No discrete action name was published.'}`);
  aiLines.push(`Reward value: ${ai.reward != null ? fmt(ai.reward, 3) : 'No reward published'}`);
  aiLines.push(`Exploration epsilon: ${ai.epsilon != null ? fmt(ai.epsilon, 4) : 'Not published'}`);
  aiLines.push(`Training steps: ${ai.steps != null ? Number(ai.steps || 0) : 'Not published'}`);
  aiLines.push(`Q-values: ${Object.keys(ai.q_values || {}).length ? formatPairs(ai.q_values || {}, 3) : (ai.q_values_note || 'No Q-values published')}`);
  aiLines.push(`State vector: ${Object.keys(ai.state || {}).length ? formatPairs(ai.state || {}, 3) : (ai.state_note || 'No state vector published')}`);
  q('mAiPane').textContent = aiLines.join('\n');

  if (!alerts.length) {
    q('alertsPane').textContent = 'No active network alerts.';
  } else {
    q('alertsPane').textContent = alerts
      .map(a => {
        const sev = String(a.severity || 'info').toUpperCase();
        return `[${sev}] ${a.message || ''}`;
      })
      .join('\n');
  }

  const actionLines = storyDigest.length
    ? storyDigest.slice()
      : (recentStory.length
      ? recentStory.map(item => {
          const ts = item.ts ? new Date(item.ts * 1000).toLocaleTimeString() : '-';
          const detail = item.detail ? `\n  ${item.detail}` : '';
          return `${ts}  [${String(item.source || 'system').toUpperCase()}] ${item.title || 'Event'}${detail}`;
        })
      : ['No controller decision timeline is available yet.']);
  q('mControllerActions').textContent = actionLines.join('\n');

  q('mFlowExplain').textContent = flowExplain.length
    ? flowExplain.join('\n')
    : 'No OpenFlow programming summary is available yet.';
}

function renderFooter() {
  const dash = state.dashboard || {};
  const telemetry = dash.telemetry || {};
  const lastPing = state.operations && state.operations.last_pingall_result ? state.operations.last_pingall_result : {};
  const uptime = (Date.now() / 1000) - state.pageStartTs;
  q('footerStatus').textContent =
    `Last refresh: ${new Date((state.lastRefreshTs || Date.now()) * 1000).toLocaleTimeString()}`
    + ` | Dashboard uptime: ${formatAge(uptime)}`
    + ` | Metrics age: ${formatAge(telemetry.metrics_age_s)}`
    + ` | Last successful pingall: ${lastPing && lastPing.ok ? formatAge(telemetry.ping_age_s) + ' ago' : 'not run from dashboard yet'}`;
}

function showNodeDetails(node, options = {}) {
  if (!node) return;
  state.selectedNode = node.id;
  const label = node.label || node.id;
  const routeRole = node.route_role && node.route_role !== 'none' ? ` | ${node.route_role} route` : '';
  const category = node.category ? ` | ${categoryLabel(node.category)}` : '';
  q('selectedNode').textContent =
    'Selected: ' + label + ' [' + node.id + ']' + (node.ip ? ' (' + node.ip + ')' : '') + category + routeRole;
  if (options.inspect) {
    openDeviceModal();
    inspectSelectedNode(node.id, Boolean(options.force));
  } else {
    syncSelectedInspector();
  }
}

function renderTopology() {
  const topo = state.topology || { nodes: [], links: [] };
  const svg = q('topologySvg');
  const map = new Map((topo.nodes || []).map(n => [n.id, n]));
  svg.innerHTML = '';

  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const mkActive = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
  mkActive.setAttribute('id', 'arrowActive');
  mkActive.setAttribute('viewBox', '0 0 10 10');
  mkActive.setAttribute('refX', '9');
  mkActive.setAttribute('refY', '5');
  mkActive.setAttribute('markerWidth', '7');
  mkActive.setAttribute('markerHeight', '7');
  mkActive.setAttribute('orient', 'auto-start-reverse');
  const mkActivePath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  mkActivePath.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z');
  mkActivePath.setAttribute('fill', '#58d6ff');
  mkActive.appendChild(mkActivePath);
  defs.appendChild(mkActive);

  const mkStandby = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
  mkStandby.setAttribute('id', 'arrowStandby');
  mkStandby.setAttribute('viewBox', '0 0 10 10');
  mkStandby.setAttribute('refX', '9');
  mkStandby.setAttribute('refY', '5');
  mkStandby.setAttribute('markerWidth', '6');
  mkStandby.setAttribute('markerHeight', '6');
  mkStandby.setAttribute('orient', 'auto-start-reverse');
  const mkStandbyPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  mkStandbyPath.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z');
  mkStandbyPath.setAttribute('fill', '#8aa1bf');
  mkStandby.appendChild(mkStandbyPath);
  defs.appendChild(mkStandby);
  svg.appendChild(defs);

  const flow = [];
  (topo.links || []).forEach((l, idx) => {
    const a = map.get(l.src), b = map.get(l.dst);
    if (!a || !b) return;
    const util = pct(l.util || 0);
    const role = String(l.route_role || 'none');

    if (role === 'active') {
      const glow = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      glow.setAttribute('x1', a.x); glow.setAttribute('y1', a.y);
      glow.setAttribute('x2', b.x); glow.setAttribute('y2', b.y);
      glow.setAttribute('stroke', '#58d6ff');
      glow.setAttribute('stroke-width', '10');
      glow.setAttribute('stroke-linecap', 'round');
      glow.setAttribute('opacity', '0.18');
      svg.appendChild(glow);
    }

    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', a.x); line.setAttribute('y1', a.y);
    line.setAttribute('x2', b.x); line.setAttribute('y2', b.y);
    line.setAttribute('stroke', role === 'active' ? '#58d6ff' : (role === 'standby' ? '#8aa1bf' : utilColor(util)));
    line.setAttribute('stroke-width', role === 'active' ? 6.5 : (util >= 80 ? 4.5 : 3.2));
    line.setAttribute('stroke-linecap', 'round');
    line.setAttribute('opacity', role === 'standby' ? '0.5' : '0.92');
    if (role === 'standby') line.setAttribute('stroke-dasharray', '10 6');
    if (role === 'active') line.setAttribute('marker-end', 'url(#arrowActive)');
    if (role === 'standby') line.setAttribute('marker-end', 'url(#arrowStandby)');
    svg.appendChild(line);

    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    const mbps = Number(l.mbps || 0);
    text.textContent = `${mbps.toFixed(1)} Mb/s | ${util.toFixed(0)}%`;
    text.setAttribute('x', (a.x + b.x) / 2 + 4);
    text.setAttribute('y', (a.y + b.y) / 2 - 4);
    text.setAttribute('fill', role === 'active' ? '#92ebff' : '#b9c7da');
    text.setAttribute('font-size', role === 'active' ? '12.5' : '11');
    svg.appendChild(text);

    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('r', String(Math.max(2, Math.min(5, util / 20))));
    dot.setAttribute('fill', role === 'active' ? '#58d6ff' : utilColor(util));
    dot.setAttribute('opacity', '0.85');
    svg.appendChild(dot);
    flow.push({ dot, a, b, speed: 0.15 + Math.max(util, role === 'active' ? 15 : 0) / 180 });
  });

  (topo.nodes || []).forEach((n) => {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.style.cursor = 'pointer';
    g.addEventListener('pointerdown', ev => beginNodeDrag(ev, n.id));
    g.addEventListener('click', ev => {
      if (Date.now() < state.suppressNodeClickUntil) {
        ev.preventDefault();
        ev.stopPropagation();
        return;
      }
      showNodeDetails(n, { inspect: true });
    });

    const isSwitch = n.kind === 'switch';
    const routeRole = String(n.route_role || 'none');
    const size = nodeSize(n);
    const w = size.width;
    const h = size.height;
    const glyph = nodeGlyphInfo(n.kind, n.category);
    const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    r.setAttribute('x', n.x - w / 2);
    r.setAttribute('y', n.y - h / 2);
    r.setAttribute('rx', '7');
    r.setAttribute('ry', '7');
    r.setAttribute('width', w);
    r.setAttribute('height', h);
    r.setAttribute('stroke', routeRole === 'active'
      ? '#58d6ff'
      : (routeRole === 'standby' ? '#8aa1bf' : (isSwitch ? '#6be6f6' : '#ffd38b')));
    r.setAttribute('stroke-width', routeRole === 'active' ? '2.2' : '1.5');
    r.setAttribute('fill', routeRole === 'active'
      ? (isSwitch ? '#133546' : '#3b2c18')
      : (isSwitch ? '#102a33' : '#332614'));

    const iconBg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    iconBg.setAttribute('x', n.x - w / 2 + 8);
    iconBg.setAttribute('y', n.y - 10);
    iconBg.setAttribute('width', glyph.text.length > 2 ? 28 : 24);
    iconBg.setAttribute('height', 20);
    iconBg.setAttribute('rx', '7');
    iconBg.setAttribute('fill',
      glyph.cls === 'switch' ? 'rgba(99,214,255,0.22)' :
      (glyph.cls === 'service_node' ? 'rgba(120,189,255,0.22)' :
      (glyph.cls === 'lab_device' ? 'rgba(110,231,183,0.22)' :
      (glyph.cls === 'iot' ? 'rgba(255,107,107,0.22)' : 'rgba(255,179,71,0.22)')))
    );
    iconBg.setAttribute('stroke', 'rgba(255,255,255,0.12)');

    const iconText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    iconText.textContent = glyph.text;
    iconText.setAttribute('x', n.x - w / 2 + (glyph.text.length > 2 ? 22 : 20));
    iconText.setAttribute('y', n.y + 4);
    iconText.setAttribute('text-anchor', 'middle');
    iconText.setAttribute('fill', '#edf5ff');
    iconText.setAttribute('font-size', '10.5');
    iconText.setAttribute('font-weight', '800');

    const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.textContent = n.label || n.id;
    t.setAttribute('x', n.x + 10);
    t.setAttribute('y', n.y + 4);
    t.setAttribute('text-anchor', 'middle');
    t.setAttribute('fill', '#e9f3ff');
    t.setAttribute('font-size', isSwitch ? '13' : '12');
    t.setAttribute('font-weight', routeRole === 'active' ? '700' : '600');

    g.appendChild(r);
    g.appendChild(iconBg);
    g.appendChild(iconText);
    g.appendChild(t);
    svg.appendChild(g);
  });

  if (state.selectedNode) {
    const selected = (topo.nodes || []).find(n => n.id === state.selectedNode);
    if (selected) showNodeDetails(selected, { inspect: false });
  }
  state.flowAnim = flow;
}

function animateFlow() {
  const t = Date.now() / 1000;
  for (const f of state.flowAnim) {
    const p = (t * f.speed) % 1;
    const x = f.a.x + (f.b.x - f.a.x) * p;
    const y = f.a.y + (f.b.y - f.a.y) * p;
    f.dot.setAttribute('cx', x);
    f.dot.setAttribute('cy', y);
  }
  requestAnimationFrame(animateFlow);
}

function renderEvents() {
  const story = Array.isArray(state.dashboard && state.dashboard.recent_story)
    ? state.dashboard.recent_story
    : [];
  if (story.length) {
    q('eventsPane').textContent = story.map(item => {
      const ts = item.ts ? new Date(item.ts * 1000).toLocaleTimeString() : '-';
      const detail = item.detail ? `\n  ${item.detail}` : '';
      return `${ts}  [${String(item.source || 'system').toUpperCase()}] ${item.title || 'Event'}${detail}`;
    }).join('\n\n');
    return;
  }
  const e = state.events || [];
  q('eventsPane').textContent = e.map(x => {
    const ts = x.ts ? new Date(x.ts * 1000).toLocaleTimeString() : '-';
    return `${ts}  ${x.event}  ${JSON.stringify(x)}`;
  }).join('\n') || 'No controller events yet.';
}

function renderOperations() {
  const ops = state.operations || {};
  const rows = [];
  const running = ops.running_stress_clients || [];
  if (running.length) {
    rows.push(`Active traffic-test clients: ${running.join(', ')}`);
  } else {
    rows.push('Active traffic-test clients: none');
  }

  const ping = ops.last_pingall_result || {};
  if (ping.ok) {
    rows.push(
      `Latest pingall -> loss ${Number(ping.packet_loss_pct || 0).toFixed(1)}%, `
      + `avg RTT ${Number(ping.avg_rtt_ms || 0).toFixed(2)} ms, `
      + `failed pairs ${Number(ping.pairs_failed || 0)}`
    );
    const slow = Array.isArray(ping.slowest_pairs) ? ping.slowest_pairs.slice(0, 5) : [];
    if (slow.length) {
      rows.push('Top high-latency pairs:');
      for (const p of slow) {
        rows.push(`  ${p.src} -> ${p.dst} : ${Number(p.avg_rtt_ms || 0).toFixed(2)} ms`);
      }
    }
  } else {
    rows.push('Latest reachability test: not executed yet from the dashboard.');
  }

  const events = Array.isArray(ops.events) ? ops.events : [];
  rows.push('');
  rows.push('Recent action log:');
  rows.push(...events.slice(-12).map(e => {
    const ts = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : '-';
    return `${ts}  ${e.op}  ${e.status}  ${JSON.stringify(e)}`;
  }));
  q('opsPane').textContent = rows.join('\n');

  const m = state.metrics || {};
  const d = state.dashboard || {};
  const route = d.route_overview || {};
  const evalProof = d.latest_evaluation || {};
  const core = Number(m.core_primary_mbps || 0);
  const wifi = Number(m.core_wifi_mbps || 0);
  q('trafficText').textContent =
    `Protected traffic scope: ${route.scope || 'Campus protected service'}\n` +
    `Primary forwarding path: ${route.active_label || '-'}\n` +
    `Standby forwarding path: ${route.standby_label || '-'}\n` +
    `Core-to-server throughput: ${core.toFixed(2)} Mbps\n` +
    `Core-to-Wi-Fi throughput: ${wifi.toFixed(2)} Mbps\n` +
    `Active load-test clients: ${running.length ? running.join(', ') : 'none'}\n` +
    `QoS policy state: ${m.student_throttle_active ? 'Student bulk traffic rate-limited' : 'Normal'}\n` +
    `Latest measured throughput gain: ${evalProof.available ? `${fmt(evalProof.throughput_gain_mbps, 3)} Mbps` : 'run Stage 11 to populate'}\n` +
    `Run "Start traffic test" and watch link colors, routing state, and throughput cards update live.`;
}

async function loadFlows() {
  const sw = q('flowSwitch').value;
  const data = await api('/api/flows?switch=' + encodeURIComponent(sw));
  if (data.error) {
    q('flowsPane').textContent = 'Flow-table fetch error:\n' + JSON.stringify(data, null, 2);
    return;
  }
  q('flowsPane').textContent = data.output || '(no output)';
}

async function addDevice(ev) {
  ev.preventDefault();
  const payload = {
    name: q('devName').value.trim(),
    ip: q('devIp').value.trim(),
    attach_switch: q('devSwitch').value,
    category: q('devCategory').value,
    bandwidth_mbps: Number(q('devBw').value || 50)
  };
  const campusMatch = /^10\.0\.0\.(\d{1,3})$/.exec(payload.ip);
  if (!campusMatch) {
    q('leftStatus').textContent = 'Add endpoint failed: use a campus IP inside 10.0.0.0/24.';
    return;
  }
  const hostOctet = Number(campusMatch[1]);
  if (!Number.isInteger(hostOctet) || hostOctet <= 0 || hostOctet >= 255) {
    q('leftStatus').textContent = 'Add endpoint failed: choose a valid host IP inside 10.0.0.1-10.0.0.254.';
    return;
  }
  const existingNodes = (state.topology && Array.isArray(state.topology.nodes)) ? state.topology.nodes : [];
  const duplicateIp = existingNodes.find(node => String(node.ip || '').trim() === payload.ip);
  if (duplicateIp) {
    q('leftStatus').textContent = `Add endpoint failed: ${payload.ip} is already assigned to ${duplicateIp.label || duplicateIp.id}.`;
    return;
  }
  const data = await api('/api/devices', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (data.error) {
    q('leftStatus').textContent = 'Add endpoint failed: ' + data.error;
    return;
  }
  const createdId = data.device && data.device.name ? data.device.name : payload.name;
  const displayName = data.device && data.device.display_name ? data.device.display_name : payload.name;
  q('leftStatus').textContent = 'Endpoint added: ' + displayName + ' [' + createdId + '] as ' + categoryLabel(payload.category);
  q('devName').value = '';
  q('devIp').value = '';
  q('devCategory').value = 'user_device';
  await refresh();
  selectNode(createdId, true);
}

async function runPingall() {
  const data = await api('/api/actions/pingall', { method: 'POST' });
  if (data.ok) {
    q('leftStatus').textContent = data.message || 'Reachability test completed';
  } else {
    q('leftStatus').textContent = data.message || data.error || 'Reachability test failed';
  }
  await refresh();
}

async function startStressDemo() {
  const scenario = scenarioConfig();
  const data = await api('/api/actions/start-stress', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(scenario.payload)
  });
  q('leftStatus').textContent = data.message || data.error || `${scenario.label} command sent`;
  await refresh();
}

async function stopStressDemo() {
  const data = await api('/api/actions/stop-stress', { method: 'POST' });
  q('leftStatus').textContent = data.message || data.error || 'Stop traffic-test command sent';
  await refresh();
}

function renderHeat() {
  const topo = state.topology || { links: [] };
  const links = topo.links || [];
  const avg = links.length ? links.reduce((a, b) => a + (b.util || 0), 0) / links.length : 0;
  const hot = links.filter(l => (l.util || 0) >= 80).map(l => `${l.src}>${l.dst}`);
  const m = state.metrics || {};
  const ops = state.operations || {};
  const running = ops.running_stress_clients || [];
  const congestedPorts = Array.isArray(m.congested_ports) ? m.congested_ports : [];
  const congestedPortText = congestedPorts.length
    ? congestedPorts.map(p => `s${p.dpid}-p${p.port}`).join(', ')
    : 'none';
  const profiles = m.priority_profiles || {};
  const examQ = profiles.exam_traffic ? profiles.exam_traffic.queue : '-';
  const authQ = profiles.authentication_traffic ? profiles.authentication_traffic.queue : '-';
  const browseQ = profiles.normal_browsing ? profiles.normal_browsing.queue : '-';
  const bulkQ = profiles.entertainment_bulk_download ? profiles.entertainment_bulk_download.queue : '-';
  const route = state.dashboard && state.dashboard.route_overview ? state.dashboard.route_overview : {};
  const evalProof = state.dashboard && state.dashboard.latest_evaluation ? state.dashboard.latest_evaluation : {};
  q('heatText').textContent =
    `Average link utilization: ${avg.toFixed(1)}%\n` +
    `Hot links (>=80% utilization): ${hot.length ? hot.join(', ') : 'none'}\n` +
    `Controller-marked congested ports: ${congestedPortText}\n` +
    `QoS queue mapping: exam(q${examQ}), auth(q${authQ}), normal(q${browseQ}), bulk(q${bulkQ})\n` +
    `Traffic policy state: ${(m.reroute_active) ? 'Adaptive reroute active' : 'Normal'}\n` +
    `Active forwarding path: ${route.active_label || '-'}\n` +
    `Student bulk QoS control: ${(m.student_throttle_active) ? 'ON (priority enforcement active)' : 'OFF'}\n` +
    `Active load-test clients: ${running.length ? running.join(', ') : 'none'}\n` +
    `Latest evaluation throughput gain: ${evalProof.available ? fmt(evalProof.throughput_gain_mbps, 3) + ' Mbps' : 'n/a'}`;
}

async function refresh() {
  const [m, e, t, ops, dash, automation] = await Promise.all([
    api('/api/metrics'),
    api('/api/events?limit=20'),
    api('/api/topology'),
    api('/api/operations'),
    api('/api/dashboard'),
    api('/api/network/automation')
  ]);
  state.metrics = m.error ? {} : m;
  state.events = Array.isArray(e) ? e : [];
  state.topology = t.error ? { nodes: [], links: [] } : t;
  applyCustomTopologyLayout(state.topology);
  state.operations = ops.error ? {} : ops;
  state.dashboard = dash.error ? {} : dash;
  state.automation = automation && !automation.error ? automation : {};
  state.lastRefreshTs = Date.now() / 1000;

  fillNetworkSettingsForm(state.metrics || {});
  renderNetworkAutomationPanel();
  updateHeader();
  renderExecutiveSummary();
  renderInventory();
  renderMetricsPanel();
  renderDashboardInsights();
  renderEvents();
  renderOperations();
  renderTopology();
  syncSelectedInspector();
  renderHeat();
  renderFooter();
}

async function boot() {
  loadTopologyLayout();
  wireTabs();
  renderScenarioPicker();
  if (q('scenarioSelect')) {
    q('scenarioSelect').addEventListener('change', renderScenarioPicker);
  }
  document.querySelectorAll('[data-scenario-card]').forEach(card => {
    card.addEventListener('click', ev => {
      if (ev.target && ev.target.closest('[data-scenario-run], [data-scenario-select]')) return;
      const key = card.getAttribute('data-scenario-card');
      setScenario(key);
      const scenario = scenarioConfig(key);
      q('leftStatus').textContent = 'Traffic demo selected: ' + scenario.label;
    });
  });
  document.querySelectorAll('[data-scenario-select]').forEach(btn => {
    btn.addEventListener('click', ev => {
      ev.stopPropagation();
      const key = btn.getAttribute('data-scenario-select');
      setScenario(key);
      const scenario = scenarioConfig(key);
      q('leftStatus').textContent = 'Traffic demo selected: ' + scenario.label;
    });
  });
  document.querySelectorAll('[data-scenario-run]').forEach(btn => {
    btn.addEventListener('click', async ev => {
      ev.stopPropagation();
      const key = btn.getAttribute('data-scenario-run');
      await runScenarioByKey(key);
    });
  });
  q('btnRefresh').addEventListener('click', refresh);
  q('btnResetLayout').addEventListener('click', resetTopologyLayout);
  q('btnRefreshDevice').addEventListener('click', refreshSelectedDevice);
  q('btnEditDevice').addEventListener('click', openSelectedDeviceEditor);
  q('btnRemoveDevice').addEventListener('click', removeSelectedDevice);
  q('btnCancelEditDevice').addEventListener('click', cancelDeviceEdit);
  q('btnCloseDeviceModal').addEventListener('click', closeDeviceModal);
  q('deviceModal').addEventListener('click', ev => {
    if (ev.target === q('deviceModal')) closeDeviceModal();
  });
  q('btnLoadFlows').addEventListener('click', loadFlows);
  q('deviceForm').addEventListener('submit', addDevice);
  q('deviceEditForm').addEventListener('submit', saveDeviceConfig);
  q('settingsForm').addEventListener('submit', saveNetworkSettings);
  q('automationCommandForm').addEventListener('submit', runAutomationCommand);
  q('autoVlanForm').addEventListener('submit', autoConfigureSwitch);
  q('vlanAssignForm').addEventListener('submit', assignDeviceToVlan);
  q('vlanInterconnectForm').addEventListener('submit', updateVlanInterconnect);
  q('btnResetSettings').addEventListener('click', resetNetworkSettingsForm);
  q('btnClearAutoVlan').addEventListener('click', clearSwitchAutomation);
  q('autoVlanSwitch').addEventListener('change', () => {
    if (q('vlanSwitch')) q('vlanSwitch').value = q('autoVlanSwitch').value;
    if (q('interconnectSwitch')) q('interconnectSwitch').value = q('autoVlanSwitch').value;
    refreshVlanDeviceOptions();
    refreshInterconnectOptions();
  });
  q('vlanSwitch').addEventListener('change', refreshVlanDeviceOptions);
  q('interconnectSwitch').addEventListener('change', refreshInterconnectOptions);
  ['cfgHighMbps', 'cfgLowMbps', 'cfgPortHigh', 'cfgPortLow'].forEach(id => {
    const el = q(id);
    if (el) {
      el.addEventListener('input', markNetworkSettingsDirty);
      el.addEventListener('change', markNetworkSettingsDirty);
    }
  });
  q('btnPingall').addEventListener('click', runPingall);
  q('btnStartStress').addEventListener('click', startStressDemo);
  q('btnStopStress').addEventListener('click', stopStressDemo);
  window.addEventListener('keydown', ev => {
    if (ev.key === 'Escape' && state.deviceModalOpen) closeDeviceModal();
  });
  window.addEventListener('pointermove', updateNodeDrag);
  window.addEventListener('pointerup', endNodeDrag);
  window.addEventListener('pointercancel', endNodeDrag);
  await refresh();
  await loadFlows();
  setInterval(refresh, 2000);
  requestAnimationFrame(animateFlow);
}

boot();
</script>
</body>
</html>
"""


class DashboardService:
    def __init__(
        self,
        metrics_file,
        events_file,
        topology_state_file,
        runtime_api_base,
        ryu_base,
        manual_settings_file=None,
        network_automation_file=None,
    ):
        self.metrics_file = metrics_file
        self.events_file = events_file
        self.topology_state_file = topology_state_file
        self.runtime_api_base = runtime_api_base.rstrip("/")
        self.ryu_base = ryu_base.rstrip("/")
        self.manual_settings_file = manual_settings_file or os.getenv(
            "CAMPUS_MANUAL_SETTINGS_FILE", "/tmp/campus_manual_settings.json"
        )
        self.network_automation_file = network_automation_file or os.getenv(
            "CAMPUS_NETWORK_AUTOMATION_FILE", "/tmp/campus_network_automation.json"
        )
        self._port_samples = {}  # {(switch, port): (total_bytes, ts)}
        self.results_dir = os.path.join(
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "results"
        )
        self._runtime_discovery_span = int(
            os.getenv("CAMPUS_RUNTIME_API_SCAN_SPAN", "20")
        )
        self._last_runtime_discovery_ts = 0.0
        self._chart_history = deque(
            maxlen=max(12, int(os.getenv("CAMPUS_DASHBOARD_HISTORY_LIMIT", "40")))
        )
        self._chart_lock = threading.Lock()
        self._segment_history = deque(
            maxlen=max(20, int(os.getenv("CAMPUS_SEGMENT_HISTORY_LIMIT", "60")))
        )
        self._segment_lock = threading.Lock()

    def _discover_runtime_api_base(self):
        try:
            parsed = urllib_parse.urlparse(self.runtime_api_base)
            scheme = parsed.scheme or "http"
            host = parsed.hostname or "127.0.0.1"
            start_port = int(parsed.port or 9091)
        except Exception:
            scheme = "http"
            host = "127.0.0.1"
            start_port = 9091

        span = max(1, self._runtime_discovery_span)
        candidates = [start_port]
        for base_port in (9091, start_port):
            for port in range(base_port, base_port + span):
                candidates.append(port)

        seen = set()
        for port in candidates:
            if port in seen:
                continue
            seen.add(port)
            candidate = f"{scheme}://{host}:{port}"
            req = urllib_request.Request(candidate + "/health", method="GET")
            try:
                with urllib_request.urlopen(req, timeout=1.2) as resp:
                    raw = resp.read().decode("utf-8")
                    payload = json.loads(raw) if raw else {}
                    if isinstance(payload, dict) and payload.get("ok"):
                        self.runtime_api_base = candidate
                        return True
            except Exception:
                continue
        return False

    def _read_json_file(self, path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _read_events(self, limit=30):
        if not os.path.exists(self.events_file):
            return []
        rows = []
        try:
            with open(self.events_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
        return rows[-limit:]

    def _read_topology_state(self):
        state = self._read_json_file(self.topology_state_file)
        if not isinstance(state, dict):
            return {"nodes": [], "links": []}
        nodes = state.get("nodes", [])
        links = state.get("links", [])
        if not isinstance(nodes, list):
            nodes = []
        if not isinstance(links, list):
            links = []
        return {"nodes": nodes, "links": links}

    def current_network_settings(self, metrics=None):
        source = metrics if isinstance(metrics, dict) else self._read_json_file(self.metrics_file)
        if not isinstance(source, dict):
            source = {}
        return {
            "congest_high_mbps": float(source.get("congest_high_mbps", 120.0) or 120.0),
            "congest_low_mbps": float(source.get("congest_low_mbps", 80.0) or 80.0),
            "port_congest_high_pct": float(
                source.get("port_congest_high_pct", 80.0) or 80.0
            ),
            "port_congest_low_pct": float(
                source.get("port_congest_low_pct", 65.0) or 65.0
            ),
        }

    def publish_network_settings(self, settings):
        payload = {
            "ts": time.time(),
            "note": "dashboard_manual_override",
            "source": "dashboard_ui",
        }
        payload.update(settings)
        _write_json_atomic(self.manual_settings_file, payload)
        return payload

    @staticmethod
    def _is_valid_mac(text):
        return bool(
            re.fullmatch(
                r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}",
                str(text or "").strip().lower(),
            )
        )

    def _load_network_automation_state(self):
        payload = self._read_json_file(self.network_automation_file)
        if not isinstance(payload, dict):
            return {"switches": {}}
        switches = payload.get("switches", {})
        if not isinstance(switches, dict):
            switches = {}
        return {
            "switches": switches,
            "source": str(payload.get("source", "dashboard_ui")),
            "updated_ts": float(payload.get("updated_ts", time.time()) or time.time()),
        }

    def _available_switch_vlan_devices(self):
        out = {}
        for device in self._load_devices():
            switch_name = str(device.get("attach_switch", "")).strip().lower()
            try:
                switch_port = int(device.get("switch_port"))
            except Exception:
                switch_port = 0
            mac = str(device.get("mac", "")).strip().lower()
            if (
                not switch_name
                or switch_port <= 0
                or not self._is_valid_mac(mac)
            ):
                continue
            out.setdefault(switch_name, []).append(
                {
                    "name": str(device.get("name", "")).strip(),
                    "display_name": str(
                        device.get("display_name")
                        or device.get("label")
                        or device.get("name")
                        or ""
                    ).strip(),
                    "switch_port": switch_port,
                    "mac": mac,
                    "category": str(device.get("category", "")).strip(),
                }
            )
        for members in out.values():
            members.sort(key=lambda row: (int(row.get("switch_port", 0)), row.get("display_name", "")))
        return out

    def _build_network_automation_view(self, state=None):
        base = state if isinstance(state, dict) else self._load_network_automation_state()
        available = self._available_switch_vlan_devices()
        switches_view = {}
        summary = {
            "managed_switches": 0,
            "vlans": 0,
            "members": 0,
            "interconnects": 0,
        }

        for switch_name, switch_cfg in sorted((base.get("switches") or {}).items()):
            sw = str(switch_name or "").strip().lower()
            if not re.fullmatch(r"s\d+", sw) or not isinstance(switch_cfg, dict):
                continue

            device_index = {
                row["name"]: row
                for row in available.get(sw, [])
            }
            vlan_map = {}
            raw_vlans = switch_cfg.get("vlans", {})
            if isinstance(raw_vlans, dict):
                for vlan_key, vlan_cfg in sorted(raw_vlans.items(), key=lambda item: int(item[0])):
                    try:
                        vlan_id = int(vlan_key)
                    except Exception:
                        continue
                    if vlan_id <= 0 or vlan_id > 4094:
                        continue
                    members = []
                    seen_names = set()
                    raw_members = vlan_cfg.get("members", []) if isinstance(vlan_cfg, dict) else []
                    if not isinstance(raw_members, list):
                        raw_members = []
                    for member in raw_members:
                        if not isinstance(member, dict):
                            continue
                        name = str(member.get("device", "")).strip()
                        live = device_index.get(name)
                        if not live or name in seen_names:
                            continue
                        members.append(
                            {
                                "device": name,
                                "display_name": live.get("display_name", name),
                                "port": int(live.get("switch_port", 0)),
                                "mac": live.get("mac", ""),
                            }
                        )
                        seen_names.add(name)
                    if members:
                        members.sort(key=lambda row: (int(row.get("port", 0)), row.get("display_name", "")))
                        vlan_map[str(vlan_id)] = {"members": members}
                        summary["vlans"] += 1
                        summary["members"] += len(members)

            allow_between = []
            raw_allow = switch_cfg.get("allow_between", [])
            seen_pairs = set()
            if isinstance(raw_allow, list):
                for item in raw_allow:
                    if isinstance(item, dict):
                        a = item.get("src_vlan")
                        b = item.get("dst_vlan")
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        a, b = item[0], item[1]
                    else:
                        continue
                    try:
                        vlan_a = int(a)
                        vlan_b = int(b)
                    except Exception:
                        continue
                    pair = (min(vlan_a, vlan_b), max(vlan_a, vlan_b))
                    if pair[0] <= 0 or pair[1] > 4094 or pair[0] == pair[1]:
                        continue
                    if str(pair[0]) not in vlan_map or str(pair[1]) not in vlan_map:
                        continue
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    allow_between.append(
                        {"src_vlan": pair[0], "dst_vlan": pair[1]}
                    )
                    summary["interconnects"] += 1

            if vlan_map or allow_between:
                switches_view[sw] = {
                    "vlans": vlan_map,
                    "allow_between": allow_between,
                }
                summary["managed_switches"] += 1

        return {
            "switches": switches_view,
            "available_devices": available,
            "summary": summary,
            "source": str(base.get("source", "dashboard_ui")),
            "updated_ts": float(base.get("updated_ts", time.time()) or time.time()),
            "network_automation_file": self.network_automation_file,
        }

    def _write_network_automation_state(self, state):
        payload = {
            "switches": state.get("switches", {}),
            "source": "dashboard_ui",
            "updated_ts": time.time(),
        }
        _write_json_atomic(self.network_automation_file, payload)
        return payload

    @staticmethod
    def _cleanup_switch_automation(switch_cfg):
        if not isinstance(switch_cfg, dict):
            return False
        vlans = switch_cfg.get("vlans", {})
        if not isinstance(vlans, dict):
            vlans = {}
            switch_cfg["vlans"] = vlans

        cleaned_vlans = {}
        for vlan_key, vlan_cfg in list(vlans.items()):
            try:
                vlan_id = int(vlan_key)
            except Exception:
                continue
            members = vlan_cfg.get("members", []) if isinstance(vlan_cfg, dict) else []
            if not isinstance(members, list):
                members = []
            valid_members = []
            seen_devices = set()
            for member in members:
                if not isinstance(member, dict):
                    continue
                device_name = str(member.get("device", "")).strip()
                if not device_name or device_name in seen_devices:
                    continue
                valid_members.append(member)
                seen_devices.add(device_name)
            if valid_members:
                cleaned_vlans[str(vlan_id)] = {"members": valid_members}
        switch_cfg["vlans"] = cleaned_vlans

        allow_between = switch_cfg.get("allow_between", [])
        cleaned_allow = []
        seen_pairs = set()
        if isinstance(allow_between, list):
            for item in allow_between:
                try:
                    src_vlan = int(item.get("src_vlan"))
                    dst_vlan = int(item.get("dst_vlan"))
                except Exception:
                    continue
                pair = (min(src_vlan, dst_vlan), max(src_vlan, dst_vlan))
                if (
                    pair[0] <= 0
                    or pair[1] > 4094
                    or pair[0] == pair[1]
                    or str(pair[0]) not in cleaned_vlans
                    or str(pair[1]) not in cleaned_vlans
                    or pair in seen_pairs
                ):
                    continue
                seen_pairs.add(pair)
                cleaned_allow.append({"src_vlan": pair[0], "dst_vlan": pair[1]})
        switch_cfg["allow_between"] = cleaned_allow
        return bool(cleaned_vlans or cleaned_allow)

    def assign_device_vlan(self, switch_name, device_name, vlan_id):
        switch_name = str(switch_name or "").strip().lower()
        device_name = str(device_name or "").strip()
        vlan_id = int(vlan_id)
        if not re.fullmatch(r"s\d+", switch_name):
            raise ValueError("switch must look like s1, s2, s3, ...")
        if vlan_id <= 0 or vlan_id > 4094:
            raise ValueError("vlan_id must be between 1 and 4094")

        available = self._available_switch_vlan_devices()
        live_device = None
        for row in available.get(switch_name, []):
            if row.get("name") == device_name:
                live_device = row
                break
        if not live_device:
            raise ValueError("device is not attached to the selected switch")

        state = self._load_network_automation_state()
        switches = state.setdefault("switches", {})
        for sw_name in list(switches.keys()):
            switch_existing = switches.get(sw_name)
            if not isinstance(switch_existing, dict):
                switches.pop(sw_name, None)
                continue
            vlans_existing = switch_existing.get("vlans", {})
            if not isinstance(vlans_existing, dict):
                vlans_existing = {}
                switch_existing["vlans"] = vlans_existing
            for key in list(vlans_existing.keys()):
                vlan_cfg = vlans_existing.get(key, {})
                members = vlan_cfg.get("members", []) if isinstance(vlan_cfg, dict) else []
                if not isinstance(members, list):
                    members = []
                members = [
                    member
                    for member in members
                    if str(member.get("device", "")).strip() != device_name
                ]
                if members:
                    vlans_existing[key] = {"members": members}
                else:
                    vlans_existing.pop(key, None)
            if not self._cleanup_switch_automation(switch_existing):
                switches.pop(sw_name, None)

        switch_cfg = switches.setdefault(
            switch_name, {"vlans": {}, "allow_between": []}
        )
        vlans = switch_cfg.setdefault("vlans", {})
        if not isinstance(vlans, dict):
            vlans = {}
            switch_cfg["vlans"] = vlans
        if not isinstance(switch_cfg.get("allow_between"), list):
            switch_cfg["allow_between"] = []

        for key in list(vlans.keys()):
            vlan_cfg = vlans.get(key, {})
            members = vlan_cfg.get("members", []) if isinstance(vlan_cfg, dict) else []
            if not isinstance(members, list):
                members = []
            members = [
                member
                for member in members
                if str(member.get("device", "")).strip() != device_name
            ]
            if members:
                vlans[key] = {"members": members}
            else:
                vlans.pop(key, None)

        members = vlans.setdefault(str(vlan_id), {"members": []}).setdefault("members", [])
        members.append(
            {
                "device": device_name,
                "label": live_device.get("display_name", device_name),
                "port": int(live_device.get("switch_port", 0)),
                "mac": live_device.get("mac", ""),
            }
        )
        members.sort(key=lambda row: int(row.get("port", 0)))
        self._cleanup_switch_automation(switch_cfg)
        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    def auto_configure_switch_vlans(
        self, switch_name, vlan_ids, allow_between=None
    ):
        switch_name = str(switch_name or "").strip().lower()
        if not re.fullmatch(r"s\d+", switch_name):
            raise ValueError("switch must look like s1, s2, s3, ...")

        normalized_vlans = []
        seen_vlans = set()
        for raw_vlan in vlan_ids or []:
            try:
                vlan_id = int(raw_vlan)
            except Exception:
                continue
            if vlan_id <= 0 or vlan_id > 4094 or vlan_id in seen_vlans:
                continue
            normalized_vlans.append(vlan_id)
            seen_vlans.add(vlan_id)
        if not normalized_vlans:
            raise ValueError("provide one or more VLAN IDs")

        available = self._available_switch_vlan_devices()
        devices = list(available.get(switch_name, []))
        if not devices:
            raise ValueError("no eligible endpoints are attached to the selected switch")
        if len(normalized_vlans) < len(devices):
            raise ValueError(
                f"{switch_name} has {len(devices)} eligible endpoints; provide at least {len(devices)} VLAN IDs for automatic one-to-one configuration"
            )

        switch_cfg = {"vlans": {}, "allow_between": []}
        for device, vlan_id in zip(devices, normalized_vlans):
            switch_cfg["vlans"].setdefault(str(vlan_id), {"members": []})["members"].append(
                {
                    "device": device.get("name"),
                    "label": device.get("display_name", device.get("name")),
                    "port": int(device.get("switch_port", 0)),
                    "mac": device.get("mac", ""),
                }
            )

        link_pairs = []
        seen_pairs = set()
        for item in allow_between or []:
            if isinstance(item, dict):
                a = item.get("src_vlan")
                b = item.get("dst_vlan")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                a, b = item[0], item[1]
            else:
                continue
            try:
                vlan_a = int(a)
                vlan_b = int(b)
            except Exception:
                continue
            pair = (min(vlan_a, vlan_b), max(vlan_a, vlan_b))
            if (
                pair[0] <= 0
                or pair[1] > 4094
                or pair[0] == pair[1]
                or str(pair[0]) not in switch_cfg["vlans"]
                or str(pair[1]) not in switch_cfg["vlans"]
                or pair in seen_pairs
            ):
                continue
            seen_pairs.add(pair)
            link_pairs.append({"src_vlan": pair[0], "dst_vlan": pair[1]})
        switch_cfg["allow_between"] = link_pairs

        state = self._load_network_automation_state()
        switches = state.setdefault("switches", {})
        switches[switch_name] = switch_cfg
        self._cleanup_switch_automation(switch_cfg)
        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    def clear_switch_automation(self, switch_name):
        switch_name = str(switch_name or "").strip().lower()
        if not re.fullmatch(r"s\d+", switch_name):
            raise ValueError("switch must look like s1, s2, s3, ...")
        state = self._load_network_automation_state()
        switches = state.setdefault("switches", {})
        if switch_name not in switches:
            raise ValueError("no automation policy exists on the selected switch")
        switches.pop(switch_name, None)
        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    def auto_configure_whole_network(self, vlan_ids, allow_between=None):
        normalized_vlans = []
        seen_vlans = set()
        for raw_vlan in vlan_ids or []:
            try:
                vlan_id = int(raw_vlan)
            except Exception:
                continue
            if vlan_id <= 0 or vlan_id > 4094 or vlan_id in seen_vlans:
                continue
            normalized_vlans.append(vlan_id)
            seen_vlans.add(vlan_id)
        if not normalized_vlans:
            raise ValueError("provide one or more VLAN IDs")

        available = self._available_switch_vlan_devices()
        if not available:
            raise ValueError("no eligible endpoints are available for automation")

        state = {"switches": {}}
        for switch_name, devices in sorted(available.items()):
            if not devices:
                continue
            if len(normalized_vlans) < len(devices):
                raise ValueError(
                    f"{switch_name} has {len(devices)} eligible endpoints; provide at least {len(devices)} VLAN IDs for automatic configuration"
                )
            switch_cfg = {"vlans": {}, "allow_between": []}
            for device, vlan_id in zip(devices, normalized_vlans):
                switch_cfg["vlans"].setdefault(
                    str(vlan_id), {"members": []}
                )["members"].append(
                    {
                        "device": device.get("name"),
                        "label": device.get("display_name", device.get("name")),
                        "port": int(device.get("switch_port", 0)),
                        "mac": device.get("mac", ""),
                    }
                )

            link_pairs = []
            seen_pairs = set()
            for item in allow_between or []:
                if isinstance(item, dict):
                    a = item.get("src_vlan")
                    b = item.get("dst_vlan")
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    a, b = item[0], item[1]
                else:
                    continue
                try:
                    vlan_a = int(a)
                    vlan_b = int(b)
                except Exception:
                    continue
                pair = (min(vlan_a, vlan_b), max(vlan_a, vlan_b))
                if (
                    pair[0] <= 0
                    or pair[1] > 4094
                    or pair[0] == pair[1]
                    or str(pair[0]) not in switch_cfg["vlans"]
                    or str(pair[1]) not in switch_cfg["vlans"]
                    or pair in seen_pairs
                ):
                    continue
                seen_pairs.add(pair)
                link_pairs.append({"src_vlan": pair[0], "dst_vlan": pair[1]})
            switch_cfg["allow_between"] = link_pairs
            self._cleanup_switch_automation(switch_cfg)
            if switch_cfg["vlans"] or switch_cfg["allow_between"]:
                state["switches"][switch_name] = switch_cfg

        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    def clear_all_network_automation(self):
        state = {"switches": {}}
        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    def restore_open_connectivity(self, switch_name=None):
        state = self._load_network_automation_state()
        switches = state.setdefault("switches", {})
        if switch_name:
            switches.pop(str(switch_name or "").strip().lower(), None)
        else:
            state["switches"] = {}
        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    def remove_vlan_from_all_switches(self, vlan_id):
        vlan_id = int(vlan_id)
        state = self._load_network_automation_state()
        switches = state.setdefault("switches", {})
        removed = False
        for switch_name in list(switches.keys()):
            switch_cfg = switches.get(switch_name)
            if not isinstance(switch_cfg, dict):
                continue
            vlans = switch_cfg.get("vlans", {})
            if not isinstance(vlans, dict) or str(vlan_id) not in vlans:
                continue
            vlans.pop(str(vlan_id), None)
            removed = True
            allow_between = switch_cfg.get("allow_between", [])
            if isinstance(allow_between, list):
                switch_cfg["allow_between"] = [
                    item
                    for item in allow_between
                    if int(item.get("src_vlan", 0)) != vlan_id
                    and int(item.get("dst_vlan", 0)) != vlan_id
                ]
            if not self._cleanup_switch_automation(switch_cfg):
                switches.pop(switch_name, None)
        if not removed:
            raise ValueError(f"vlan {vlan_id} is not configured on any managed switch")
        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    def set_vlan_interconnect_scope(self, vlan_a, vlan_b, enabled, switch_name=None):
        vlan_a = int(vlan_a)
        vlan_b = int(vlan_b)
        if switch_name:
            return self.set_vlan_interconnect(switch_name, vlan_a, vlan_b, enabled)

        state = self._load_network_automation_state()
        switches = state.setdefault("switches", {})
        touched = False
        for sw_name in list(switches.keys()):
            switch_cfg = switches.get(sw_name)
            if not isinstance(switch_cfg, dict):
                continue
            vlans = switch_cfg.get("vlans", {})
            if str(vlan_a) not in vlans or str(vlan_b) not in vlans:
                continue
            pair = {"src_vlan": min(vlan_a, vlan_b), "dst_vlan": max(vlan_a, vlan_b)}
            allow_between = switch_cfg.setdefault("allow_between", [])
            allow_between = [
                item
                for item in allow_between
                if not (
                    int(item.get("src_vlan", 0)) == pair["src_vlan"]
                    and int(item.get("dst_vlan", 0)) == pair["dst_vlan"]
                )
            ]
            if enabled:
                allow_between.append(pair)
                allow_between.sort(
                    key=lambda item: (int(item["src_vlan"]), int(item["dst_vlan"]))
                )
            switch_cfg["allow_between"] = allow_between
            if not self._cleanup_switch_automation(switch_cfg):
                switches.pop(sw_name, None)
            touched = True
        if not touched:
            raise ValueError(
                f"no managed switch currently has both VLAN {vlan_a} and VLAN {vlan_b}"
            )
        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    @staticmethod
    def _parse_command_vlan_ids(command_text):
        match = re.search(
            r"\bvlan(?:s)?\b\s*([0-9][0-9,\s]*)",
            str(command_text or ""),
            flags=re.IGNORECASE,
        )
        if not match:
            return []
        values = []
        seen = set()
        for token in re.findall(r"\d+", match.group(1)):
            vlan_id = int(token)
            if vlan_id <= 0 or vlan_id > 4094 or vlan_id in seen:
                continue
            values.append(vlan_id)
            seen.add(vlan_id)
        return values

    @staticmethod
    def _parse_command_vlan_pairs(command_text):
        pairs = []
        seen = set()
        for a_text, b_text in re.findall(r"(\d+)\s*-\s*(\d+)", str(command_text or "")):
            vlan_a = int(a_text)
            vlan_b = int(b_text)
            pair = (min(vlan_a, vlan_b), max(vlan_a, vlan_b))
            if (
                pair[0] <= 0
                or pair[1] > 4094
                or pair[0] == pair[1]
                or pair in seen
            ):
                continue
            seen.add(pair)
            pairs.append(pair)
        return pairs

    def execute_network_automation_intent(self, command_text):
        raw = str(command_text or "").strip()
        if not raw:
            raise ValueError("command text is required")

        compact = re.sub(r"\s+", " ", raw).strip()
        lowered = compact.lower()
        whole_network = bool(
            re.search(
                r"\b(whole network|entire network|all switches|network wide|network-wide|campus)\b",
                lowered,
            )
        )
        switch_match = re.search(r"\bs(\d+)\b", lowered)
        if not switch_match and not whole_network:
            if "broadcast" in lowered:
                raise ValueError(
                    "this command box automates network configuration, not human-message broadcasting. Try 'configure s3 with vlan 10,20,30' or 'configure the whole network with vlan 10,20,30'."
                )
            raise ValueError(
                "include a target switch such as s3, or say 'whole network' in the command"
            )
        switch_name = f"s{int(switch_match.group(1))}" if switch_match else None
        vlan_ids = self._parse_command_vlan_ids(compact)
        vlan_pairs = self._parse_command_vlan_pairs(compact)
        open_connectivity_intent = bool(
            re.search(
                r"\b(talk to each other|communicate with each other|reach each other|full connectivity|open communication|allow all devices|all devices can talk)\b",
                lowered,
            )
        )

        if "broadcast" in lowered and not (
            vlan_ids
            or vlan_pairs
            or re.search(
                r"\b(clear|reset|remove|delete|allow|permit|enable|block|disallow|deny|configure|auto-configure|autoconfigure|setup|set up|apply|create)\b",
                lowered,
            )
        ):
            raise ValueError(
                "this command box automates network policy, not human-message broadcasting. Try 'configure s3 with vlan 10,20,30' or 'configure the whole network with vlan 10,20,30'."
            )

        if open_connectivity_intent:
            automation = self.restore_open_connectivity(
                None if whole_network else switch_name
            )
            target = "the whole network" if whole_network else (switch_name or "the selected scope")
            return {
                "action": "restore_connectivity",
                "switch": switch_name or "whole_network",
                "vlan_ids": [],
                "allow_between": [],
                "message": f"Restored open communication across {target} by clearing isolation policies.",
                "automation": automation,
            }

        if re.search(r"\b(clear|reset)\b", lowered):
            automation = (
                self.clear_all_network_automation()
                if whole_network
                else self.clear_switch_automation(switch_name)
            )
            return {
                "action": "clear_network" if whole_network else "clear_switch",
                "switch": switch_name or "whole_network",
                "vlan_ids": [],
                "allow_between": [],
                "message": (
                    "Controller automation cleared from the whole network."
                    if whole_network
                    else f"Controller automation cleared from {switch_name}."
                ),
                "automation": automation,
            }

        if re.search(r"\b(remove|delete)\b", lowered) and re.search(r"\bvlan", lowered):
            if not vlan_ids:
                raise ValueError("include the VLAN ID to remove, for example remove vlan 30 from s3")
            if len(vlan_ids) > 1:
                raise ValueError("remove one VLAN at a time in the command interface")
            automation = (
                self.remove_vlan_from_all_switches(vlan_ids[0])
                if whole_network
                else self.remove_vlan_assignment(switch_name, vlan_ids[0])
            )
            return {
                "action": "remove_vlan",
                "switch": switch_name or "whole_network",
                "vlan_ids": vlan_ids,
                "allow_between": [],
                "message": (
                    f"VLAN {vlan_ids[0]} was removed from the whole network automation policy."
                    if whole_network
                    else f"VLAN {vlan_ids[0]} was removed from {switch_name}."
                ),
                "automation": automation,
            }

        if re.search(r"\b(block|disallow|deny)\b", lowered):
            if not vlan_pairs:
                raise ValueError("include a VLAN pair to block, for example block 10-20 on s3")
            automation = None
            for vlan_a, vlan_b in vlan_pairs:
                automation = self.set_vlan_interconnect_scope(
                    vlan_a, vlan_b, False, switch_name=switch_name
                ) if not whole_network else self.set_vlan_interconnect_scope(
                    vlan_a, vlan_b, False, switch_name=None
                )
            return {
                "action": "block_interconnect",
                "switch": switch_name or "whole_network",
                "vlan_ids": [],
                "allow_between": vlan_pairs,
                "message": "Blocked VLAN communication on %s for %s."
                % (
                    "the whole network" if whole_network else switch_name,
                    ", ".join(f"{a}-{b}" for a, b in vlan_pairs),
                ),
                "automation": automation,
            }

        configure_like = (
            re.search(r"\b(configure|auto-configure|autoconfigure|setup|set up|apply|create)\b", lowered)
            or (vlan_ids and re.search(r"\bvlan", lowered))
        )
        if configure_like:
            if not vlan_ids:
                raise ValueError("include the VLAN plan, for example configure s3 with vlan 10,20,30")
            automation = (
                self.auto_configure_whole_network(
                    vlan_ids,
                    vlan_pairs
                    if re.search(r"\b(allow|permit|enable)\b", lowered)
                    else [],
                )
                if whole_network
                else self.auto_configure_switch_vlans(
                    switch_name,
                    vlan_ids,
                    vlan_pairs
                    if re.search(r"\b(allow|permit|enable)\b", lowered)
                    else [],
                )
            )
            return {
                "action": "auto_configure_network" if whole_network else "auto_configure",
                "switch": switch_name or "whole_network",
                "vlan_ids": vlan_ids,
                "allow_between": vlan_pairs
                if re.search(r"\b(allow|permit|enable)\b", lowered)
                else [],
                "message": "Controller auto-configured %s with VLANs %s."
                % (
                    "the whole network" if whole_network else switch_name,
                    ", ".join(str(vlan) for vlan in vlan_ids),
                ),
                "automation": automation,
            }

        if re.search(r"\b(allow|permit|enable)\b", lowered):
            if not vlan_pairs:
                raise ValueError("include a VLAN pair to allow, for example allow 10-20 on s3")
            automation = None
            for vlan_a, vlan_b in vlan_pairs:
                automation = self.set_vlan_interconnect_scope(
                    vlan_a, vlan_b, True, switch_name=switch_name
                ) if not whole_network else self.set_vlan_interconnect_scope(
                    vlan_a, vlan_b, True, switch_name=None
                )
            return {
                "action": "allow_interconnect",
                "switch": switch_name or "whole_network",
                "vlan_ids": [],
                "allow_between": vlan_pairs,
                "message": "Allowed VLAN communication on %s for %s."
                % (
                    "the whole network" if whole_network else switch_name,
                    ", ".join(f"{a}-{b}" for a, b in vlan_pairs),
                ),
                "automation": automation,
            }

        raise ValueError(
            "could not understand the command. Try something like 'configure s3 with vlan 10,20,30 and allow 10-20'"
        )

    def remove_vlan_assignment(self, switch_name, vlan_id, device_name=None):
        switch_name = str(switch_name or "").strip().lower()
        vlan_id = int(vlan_id)
        state = self._load_network_automation_state()
        switches = state.get("switches", {})
        switch_cfg = switches.get(switch_name)
        if not isinstance(switch_cfg, dict):
            raise ValueError("switch automation config not found")
        vlans = switch_cfg.get("vlans", {})
        if not isinstance(vlans, dict) or str(vlan_id) not in vlans:
            raise ValueError("vlan configuration not found on the selected switch")

        if device_name:
            device_name = str(device_name).strip()
            members = vlans[str(vlan_id)].get("members", [])
            members = [
                member
                for member in members
                if str(member.get("device", "")).strip() != device_name
            ]
            if members:
                vlans[str(vlan_id)] = {"members": members}
            else:
                vlans.pop(str(vlan_id), None)
        else:
            vlans.pop(str(vlan_id), None)

        allow_between = switch_cfg.get("allow_between", [])
        if isinstance(allow_between, list):
            switch_cfg["allow_between"] = [
                item
                for item in allow_between
                if int(item.get("src_vlan", 0)) != vlan_id
                and int(item.get("dst_vlan", 0)) != vlan_id
            ]

        if not self._cleanup_switch_automation(switch_cfg):
            switches.pop(switch_name, None)
        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    def set_vlan_interconnect(self, switch_name, vlan_a, vlan_b, enabled):
        switch_name = str(switch_name or "").strip().lower()
        vlan_a = int(vlan_a)
        vlan_b = int(vlan_b)
        if not re.fullmatch(r"s\d+", switch_name):
            raise ValueError("switch must look like s1, s2, s3, ...")
        if vlan_a == vlan_b:
            raise ValueError("select two different VLAN IDs")
        if vlan_a <= 0 or vlan_a > 4094 or vlan_b <= 0 or vlan_b > 4094:
            raise ValueError("vlan IDs must be between 1 and 4094")

        state = self._load_network_automation_state()
        switches = state.setdefault("switches", {})
        switch_cfg = switches.setdefault(
            switch_name, {"vlans": {}, "allow_between": []}
        )
        vlans = switch_cfg.setdefault("vlans", {})
        if str(vlan_a) not in vlans or str(vlan_b) not in vlans:
            raise ValueError("both VLANs must exist on the switch before linking them")

        pair = {"src_vlan": min(vlan_a, vlan_b), "dst_vlan": max(vlan_a, vlan_b)}
        allow_between = switch_cfg.setdefault("allow_between", [])
        allow_between = [
            item
            for item in allow_between
            if not (
                int(item.get("src_vlan", 0)) == pair["src_vlan"]
                and int(item.get("dst_vlan", 0)) == pair["dst_vlan"]
            )
        ]
        if enabled:
            allow_between.append(pair)
            allow_between.sort(key=lambda item: (int(item["src_vlan"]), int(item["dst_vlan"])))
        switch_cfg["allow_between"] = allow_between
        if not self._cleanup_switch_automation(switch_cfg):
            switches.pop(switch_name, None)
        self._write_network_automation_state(state)
        return self._build_network_automation_view(state)

    def _runtime_request(self, method, path, payload=None, timeout=30):
        def _send(base_url):
            url = base_url.rstrip("/") + path
            data = None
            headers = {}
            if payload is not None:
                data = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
            req = urllib_request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib_request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return True, json.loads(raw) if raw else {}, "ok"
            except urllib_error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body) if body else {}
                except Exception:
                    parsed = {"error": body or str(exc)}
                if isinstance(parsed, dict):
                    parsed.setdefault("_http_status", int(getattr(exc, "code", 0) or 0))
                return False, parsed, "http"
            except Exception as exc:
                return (
                    False,
                    {
                        "error": f"runtime api unreachable: {exc}",
                        "runtime_api": base_url,
                    },
                    "transport",
                )

        ok, resp, err_type = _send(self.runtime_api_base)
        if ok:
            return True, resp

        now = time.time()
        status_code = 0
        if isinstance(resp, dict):
            try:
                status_code = int(resp.get("_http_status", 0) or 0)
            except Exception:
                status_code = 0

        should_retry_with_discovery = (
            err_type == "transport"
            or (err_type == "http" and status_code in {404, 405, 503})
        )
        if should_retry_with_discovery and (
            now - self._last_runtime_discovery_ts >= 2.0
        ):
            self._last_runtime_discovery_ts = now
            if self._discover_runtime_api_base():
                ok_retry, resp_retry, _ = _send(self.runtime_api_base)
                if ok_retry:
                    return True, resp_retry
                return False, resp_retry

        return False, resp

    def _ryu_get_json(self, path, timeout=5):
        url = self.ryu_base.rstrip("/") + path
        req = urllib_request.Request(url, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return True, json.loads(raw) if raw else {}
        except Exception as exc:
            return False, {"error": str(exc)}

    @staticmethod
    def _switch_to_dpid(switch):
        text = str(switch or "").strip().lower()
        if re.fullmatch(r"s\d+", text):
            return int(text[1:])
        if text.isdigit():
            return int(text)
        return None

    def _dump_flows_via_ryu(self, switch):
        dpid_int = self._switch_to_dpid(switch)
        if dpid_int is None:
            return False, f"unsupported switch name: {switch}"
        ok, payload = self._ryu_get_json(f"/stats/flow/{dpid_int}", timeout=5)
        if not ok or not isinstance(payload, dict):
            err = payload.get("error", "unknown ryu error") if isinstance(payload, dict) else str(payload)
            return False, err
        rows = payload.get(str(dpid_int), payload.get(dpid_int, []))
        if not isinstance(rows, list):
            rows = []

        def _sort_key(row):
            if not isinstance(row, dict):
                return (0, 0, 0, 0)
            return (
                int(row.get("table_id", 0) or 0),
                -int(row.get("priority", 0) or 0),
                -int(row.get("packet_count", 0) or 0),
                -int(row.get("byte_count", 0) or 0),
            )

        lines = [
            f"Flow dump via Ryu REST for {switch} (dpid={dpid_int})",
            f"rules={len(rows)}",
            "",
        ]
        for idx, row in enumerate(sorted(rows, key=_sort_key), start=1):
            if not isinstance(row, dict):
                continue
            match = row.get("match", {})
            actions = row.get("actions", [])
            lines.append(
                "[%d] table=%s priority=%s packets=%s bytes=%s duration=%ss cookie=%s"
                % (
                    idx,
                    row.get("table_id", 0),
                    row.get("priority", 0),
                    row.get("packet_count", 0),
                    row.get("byte_count", 0),
                    row.get("duration_sec", 0),
                    row.get("cookie", 0),
                )
            )
            lines.append("    match: " + (json.dumps(match, sort_keys=True) if match else "{}"))
            if isinstance(actions, list):
                action_text = ", ".join(str(a) for a in actions) if actions else "(drop)"
            else:
                action_text = str(actions)
            lines.append("    actions: " + action_text)
        return True, "\n".join(lines).strip()

    def _dump_flows(self, switch):
        cmds = [
            ["sudo", "-n", "ovs-ofctl", "-O", "OpenFlow13", "dump-flows", switch],
            ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", switch],
        ]
        last_err = ""
        for cmd in cmds:
            try:
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if cp.returncode == 0:
                    return True, cp.stdout.strip()
                last_err = (cp.stderr or cp.stdout).strip()
            except Exception as exc:
                last_err = str(exc)
        ok_ryu, output_ryu = self._dump_flows_via_ryu(switch)
        if ok_ryu:
            return True, output_ryu
        return False, (
            "Flow dump unavailable from ovs-ofctl, and Ryu fallback failed.\n"
            + (last_err or output_ryu)
        )

    def _parse_dump_ports(self, text):
        port_totals = {}
        current_port = None
        for line in text.splitlines():
            m = re.search(r"^\s*port\s+(\d+):", line)
            if m:
                current_port = int(m.group(1))
                port_totals.setdefault(current_port, 0)
                continue
            if current_port is None:
                continue
            for val in re.findall(r"bytes=(\d+)", line):
                port_totals[current_port] += int(val)
        return port_totals

    def _dump_port_totals(self, switch):
        cmds = [
            ["sudo", "-n", "ovs-ofctl", "-O", "OpenFlow13", "dump-ports", switch],
            ["ovs-ofctl", "-O", "OpenFlow13", "dump-ports", switch],
        ]
        last_err = ""
        for cmd in cmds:
            try:
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if cp.returncode == 0:
                    return True, self._parse_dump_ports(cp.stdout)
                last_err = (cp.stderr or cp.stdout).strip()
            except Exception as exc:
                last_err = str(exc)

        dpid_int = self._switch_to_dpid(switch)
        if dpid_int is not None:
            ok_ryu, payload = self._ryu_get_json(f"/stats/port/{dpid_int}", timeout=5)
            if ok_ryu and isinstance(payload, dict):
                rows = payload.get(str(dpid_int), payload.get(dpid_int, []))
                if not isinstance(rows, list):
                    rows = []
                totals = {}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    port_no = row.get("port_no")
                    if str(port_no).upper() == "LOCAL":
                        continue
                    try:
                        port_int = int(port_no)
                    except Exception:
                        continue
                    totals[port_int] = int(row.get("rx_bytes", 0) or 0) + int(
                        row.get("tx_bytes", 0) or 0
                    )
                if totals:
                    return True, totals
                return True, {}
            if isinstance(payload, dict):
                last_err = payload.get("error", last_err) or last_err
        return False, last_err

    def _sample_link_mbps(self, switches):
        switch_port_mbps = {}
        now = time.time()
        for sw in switches:
            ok, port_totals = self._dump_port_totals(sw)
            if not ok:
                continue
            for port_no, total_bytes in port_totals.items():
                key = (sw, int(port_no))
                prev = self._port_samples.get(key)
                self._port_samples[key] = (int(total_bytes), now)
                if not prev:
                    continue
                prev_total, prev_ts = prev
                elapsed = max(now - prev_ts, 1e-6)
                delta = max(int(total_bytes) - int(prev_total), 0)
                switch_port_mbps[key] = (delta * 8.0) / elapsed / 1_000_000.0
        return switch_port_mbps

    def _build_topology(self, metrics):
        topo = self._read_topology_state()
        m = metrics or {}
        core_mbps = float(m.get("core_primary_mbps", 0.0))
        reroute = bool(m.get("reroute_active", False))
        route = self._build_route_overview(m)
        active_link_keys = {
            frozenset((src, dst))
            for src, dst in route.get("active_links", [])
            if src and dst
        }
        standby_link_keys = {
            frozenset((src, dst))
            for src, dst in route.get("standby_links", [])
            if src and dst
        }
        active_nodes = set(route.get("active_nodes", []))
        standby_nodes = set(route.get("standby_nodes", []))
        default_bw = {
            frozenset(("s1", "s2")): 1000.0,
            frozenset(("s1", "s3")): 1000.0,
            frozenset(("s1", "s4")): 1000.0,
            frozenset(("s1", "s5")): 1000.0,
            frozenset(("s1", "h_server")): 1000.0,
            frozenset(("s3", "h_server_b")): 1000.0,
            frozenset(("s2", "h_it1")): 100.0,
            frozenset(("s2", "h_it2")): 100.0,
            frozenset(("s3", "h_net1")): 100.0,
            frozenset(("s3", "h_net2")): 100.0,
            frozenset(("s4", "h_staff1")): 100.0,
            frozenset(("s4", "h_staff2")): 100.0,
            frozenset(("s5", "h_wifi1")): 50.0,
            frozenset(("s5", "h_wifi2")): 50.0,
        }

        out_nodes = []
        for node in topo.get("nodes", []):
            n = dict(node)
            n["label"] = n.get("label", n.get("id", "node"))
            if isinstance(n.get("ip"), str) and "/" in n["ip"]:
                n["ip"] = n["ip"].split("/", 1)[0]
            node_id = str(n.get("id", ""))
            if n.get("kind") in {"host", "dynamic"}:
                n["category"] = _normalize_device_category(
                    n.get("category") or _default_host_category(node_id)
                )
            if node_id in active_nodes:
                n["route_role"] = "active"
            elif node_id in standby_nodes:
                n["route_role"] = "standby"
            else:
                n["route_role"] = "none"
            out_nodes.append(n)

        switch_names = [
            n["id"] for n in out_nodes if n.get("kind") == "switch" and "id" in n
        ]
        metric_port_mbps = {}
        raw_port_mbps = m.get("switch_port_mbps", {})
        if isinstance(raw_port_mbps, dict):
            for dpid, ports in raw_port_mbps.items():
                try:
                    sw_name = "s%s" % int(dpid)
                except Exception:
                    continue
                if not isinstance(ports, dict):
                    continue
                for port_no, mbps in ports.items():
                    try:
                        metric_port_mbps[(sw_name, int(port_no))] = float(mbps)
                    except Exception:
                        continue

        port_mbps = dict(metric_port_mbps)
        fallback_samples = self._sample_link_mbps(switch_names)
        for key, value in fallback_samples.items():
            port_mbps.setdefault(key, value)

        out_links = []
        for link in topo.get("links", []):
            l = dict(link)
            src = str(l.get("src", ""))
            dst = str(l.get("dst", ""))
            src_port = l.get("src_port")
            dst_port = l.get("dst_port")
            try:
                bw = float(l.get("bw_mbps", 0.0) or 0.0)
            except Exception:
                bw = 0.0
            if bw <= 0:
                bw = default_bw.get(frozenset((src, dst)), 0.0)
            l["bw_mbps"] = round(float(bw), 3)

            mbps = None
            try:
                if src.startswith("s") and src_port is not None:
                    mbps = port_mbps.get((src, int(src_port)))
            except Exception:
                pass
            try:
                if mbps is None and dst.startswith("s") and dst_port is not None:
                    mbps = port_mbps.get((dst, int(dst_port)))
            except Exception:
                pass

            if mbps is None and (
                (src == "s1" and dst == "h_server") or (src == "h_server" and dst == "s1")
            ):
                mbps = core_mbps

            if mbps is None:
                util = 0.0
                mbps = 0.0
            elif bw > 0:
                util = max(0.0, min(100.0, (mbps / bw) * 100.0))
            else:
                util = max(0.0, min(100.0, mbps))

            link_key = frozenset((src, dst))
            if link_key in active_link_keys:
                l["route_role"] = "active"
            elif link_key in standby_link_keys:
                l["route_role"] = "standby"
            else:
                l["route_role"] = "none"
            l["mbps"] = round(float(mbps), 3)
            l["util"] = round(float(util), 2)
            out_links.append(l)

        switch_utils = {sw: [] for sw in switch_names}
        for l in out_links:
            if str(l.get("src", "")).startswith("s"):
                switch_utils.setdefault(l["src"], []).append(float(l.get("util", 0.0)))
            if str(l.get("dst", "")).startswith("s"):
                switch_utils.setdefault(l["dst"], []).append(float(l.get("util", 0.0)))

        for n in out_nodes:
            if n.get("kind") == "switch":
                vals = switch_utils.get(n.get("id", ""), [])
                n["util"] = round(sum(vals) / len(vals), 2) if vals else 0.0

        return {
            "nodes": out_nodes,
            "links": out_links,
            "controller_online": bool(m.get("connected_switches")),
            "reroute_active": reroute,
            "core_mbps": core_mbps,
            "route_overview": route,
        }

    @staticmethod
    def _human_event_name(name):
        text = str(name or "").replace("_", " ").strip()
        return text.capitalize() if text else "Event"

    def _build_route_overview(self, metrics):
        reroute = bool(metrics.get("reroute_active", False))
        last_choice = str(metrics.get("last_ml_routing_choice") or "").strip()
        last_note = str(metrics.get("last_ml_note") or "").strip()
        dqn_enabled = bool(metrics.get("dqn_integration_enabled", False))
        if reroute:
            active_label = (
                "IT lab -> core -> networking switch -> backup service "
                "(10.0.0.101 rewritten from 10.0.0.100)"
            )
            active_nodes = ["s2", "s1", "s3", "h_server_b"]
            short_status = "backup path engaged"
        else:
            active_label = "IT lab -> core -> primary service (10.0.0.100)"
            active_nodes = ["s2", "s1", "h_server"]
            short_status = "primary path active"

        standby_label = (
            "IT lab -> core -> primary service (10.0.0.100)"
            if reroute
            else (
                "IT lab -> core -> networking switch -> backup service "
                "(10.0.0.101 rewritten from 10.0.0.100)"
            )
        )
        standby_nodes = ["s2", "s1", "h_server"] if reroute else ["s2", "s1", "s3", "h_server_b"]
        active_links = list(zip(active_nodes[:-1], active_nodes[1:]))
        standby_links = list(zip(standby_nodes[:-1], standby_nodes[1:]))

        if last_choice:
            decision_source = "DQN route selection" if dqn_enabled else "External ML/policy hook"
            reason = f"{decision_source} chose {last_choice}."
        elif reroute:
            decision_source = "Congestion threshold policy"
            reason = (
                "Core-to-service throughput crossed the congestion threshold, "
                "so the protected ICMP service moved to the backup path."
            )
        else:
            decision_source = "Normal policy state"
            reason = (
                "No active reroute is required, so the protected ICMP service "
                "stays on the direct primary path."
            )
        if last_note:
            reason += f" Source note: {last_note}."

        return {
            "scope": "Protected ICMP service from IT lab hosts to the campus service IP",
            "short_status": short_status,
            "active_label": active_label,
            "standby_label": standby_label,
            "decision_source": decision_source,
            "reason": reason,
            "active_nodes": active_nodes,
            "standby_nodes": standby_nodes,
            "active_links": active_links,
            "standby_links": standby_links,
        }

    def _load_devices(self):
        topo = self._read_topology_state()
        nodes = topo.get("nodes", [])
        node_kinds = {str(n.get("id", "")): str(n.get("kind", "")) for n in nodes}
        attach_switch = {}
        for link in topo.get("links", []):
            if not isinstance(link, dict):
                continue
            src = str(link.get("src", ""))
            dst = str(link.get("dst", ""))
            src_kind = node_kinds.get(src, "")
            dst_kind = node_kinds.get(dst, "")
            if src_kind == "switch" and dst_kind in {"host", "dynamic"}:
                attach_switch[dst] = src
            elif dst_kind == "switch" and src_kind in {"host", "dynamic"}:
                attach_switch[src] = dst
        devices = []
        for n in nodes:
            if n.get("kind") in {"dynamic", "host"}:
                category = _normalize_device_category(
                    n.get("category") or _default_host_category(n.get("id"))
                )
                devices.append(
                    {
                        "name": n.get("id"),
                        "display_name": n.get("label", n.get("id")),
                        "ip": n.get("ip", ""),
                        "attach_switch": attach_switch.get(n.get("id"), ""),
                        "category": category,
                        "category_label": _device_category_label(category),
                        "kind": n.get("kind", "host"),
                        "mac": n.get("mac", ""),
                        "default_intf": n.get("default_intf", ""),
                        "host_interface": n.get("host_interface", ""),
                        "switch_interface": n.get("switch_interface", ""),
                        "switch_port": n.get("switch_port"),
                        "bandwidth_mbps": n.get("bandwidth_mbps"),
                        "delay": n.get("delay", ""),
                        "removable": bool(n.get("removable", False)),
                        "management_origin": n.get("management_origin", ""),
                        "route_role": n.get("route_role", "none"),
                    }
                )
        return devices

    def _load_device(self, name):
        name = str(name or "").strip()
        for device in self._load_devices():
            if str(device.get("name", "")).strip() == name:
                return device
        return None

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            return float(value or 0.0)
        except Exception:
            return default

    @staticmethod
    def _port_stat(metrics, dpid, port_no):
        switch_stats = metrics.get("switch_port_stats", {}) if isinstance(metrics, dict) else {}
        if not isinstance(switch_stats, dict):
            return {}
        bucket = switch_stats.get(str(int(dpid)), switch_stats.get(int(dpid), {}))
        if not isinstance(bucket, dict):
            return {}
        row = bucket.get(str(int(port_no)), bucket.get(int(port_no), {}))
        return row if isinstance(row, dict) else {}

    def _build_segment_analytics(self, metrics):
        threshold_pct = self._safe_float(metrics.get("port_congest_high_pct", 80.0), 80.0)
        throughput_high = self._safe_float(metrics.get("congest_high_mbps", 120.0), 120.0)
        segment_rows = []

        for profile in SEGMENT_TRAFFIC_PROFILES:
            port_rows = []
            total_mbps = 0.0
            util_vals = []
            max_util = 0.0
            congested = False

            for dpid, port_no in profile.get("ports", ()):
                row = self._port_stat(metrics, dpid, port_no)
                mbps = self._safe_float(row.get("mbps", 0.0))
                util = self._safe_float(row.get("util_pct", 0.0))
                total_mbps += mbps
                max_util = max(max_util, util)
                if row:
                    util_vals.append(util)
                    congested = congested or bool(row.get("congested", False))
                    port_rows.append(
                        {
                            "switch": f"s{int(dpid)}",
                            "port": int(port_no),
                            "mbps": round(mbps, 3),
                            "util_pct": round(util, 3),
                            "capacity_mbps": round(
                                self._safe_float(row.get("capacity_mbps", 0.0)), 3
                            ),
                        }
                    )

            avg_util = (sum(util_vals) / len(util_vals)) if util_vals else 0.0
            if congested or max_util >= threshold_pct:
                status = "congested"
            elif max_util >= max(20.0, threshold_pct * 0.6) or total_mbps >= max(
                5.0, throughput_high * 0.15
            ):
                status = "busy"
            elif total_mbps >= 0.1:
                status = "active"
            else:
                status = "idle"

            segment_rows.append(
                {
                    "key": profile["key"],
                    "label": profile["label"],
                    "description": profile["description"],
                    "color": profile["color"],
                    "mbps": round(total_mbps, 3),
                    "avg_util_pct": round(avg_util, 3),
                    "max_util_pct": round(max_util, 3),
                    "status": status,
                    "ports": port_rows,
                }
            )

        sample = {"ts": time.time()}
        for row in segment_rows:
            sample[row["key"]] = row["mbps"]
            sample[f'{row["key"]}_util'] = row["max_util_pct"]

        with self._segment_lock:
            if self._segment_history and (
                sample["ts"] - float(self._segment_history[-1].get("ts", 0.0))
            ) < 1.0:
                self._segment_history[-1] = sample
            else:
                self._segment_history.append(sample)
            history = list(self._segment_history)

        if history:
            window_s = max(
                0.0, float(history[-1].get("ts", 0.0)) - float(history[0].get("ts", 0.0))
            )
            window_label = "last %s sample(s) (~%ss)" % (
                len(history),
                int(round(window_s)),
            )
        else:
            window_label = "warming up segment history"

        peaks = {}
        previous = history[-2] if len(history) >= 2 else {}
        for row in segment_rows:
            key = row["key"]
            current = self._safe_float(row.get("mbps", 0.0))
            prior = self._safe_float(previous.get(key, current))
            delta = current - prior
            if delta > 5.0:
                trend = "up"
            elif delta < -5.0:
                trend = "down"
            else:
                trend = "steady"
            row["trend"] = trend
            row["delta_mbps"] = round(delta, 3)
            peaks[key] = round(
                max(self._safe_float(point.get(key, 0.0)) for point in history) if history else current,
                3,
            )
            row["peak_window_mbps"] = peaks[key]

        def _series(key, label, color):
            return {
                "key": key,
                "label": label,
                "color": color,
                "points": [
                    round(self._safe_float(point.get(key, 0.0)), 3) for point in history
                ],
            }

        busiest = max(segment_rows, key=lambda row: row.get("mbps", 0.0), default=None)
        hot_segments = [
            {
                "key": row["key"],
                "label": row["label"],
                "status": row["status"],
                "mbps": row["mbps"],
                "max_util_pct": row["max_util_pct"],
            }
            for row in segment_rows
            if row["status"] in {"busy", "congested"}
        ]
        observations = []
        if busiest and self._safe_float(busiest.get("mbps", 0.0)) >= 0.1:
            observations.append(
                "%s is the busiest segment at %.2f Mbps (peak %.2f Mbps in this window)."
                % (
                    busiest["label"],
                    self._safe_float(busiest.get("mbps", 0.0)),
                    self._safe_float(busiest.get("peak_window_mbps", 0.0)),
                )
            )
        else:
            observations.append(
                "All monitored campus segments are currently idle or below sustained sampling thresholds."
            )
        if hot_segments:
            observations.append(
                "Segments above normal operating range: "
                + ", ".join(
                    "%s (%s, %.1f%% util)"
                    % (
                        row["label"],
                        row["status"],
                        self._safe_float(row.get("max_util_pct", 0.0)),
                    )
                    for row in hot_segments
                )
                + "."
            )
        else:
            observations.append(
                "No monitored segment is currently over the configured congestion threshold."
            )
        if bool(metrics.get("reroute_active", False)):
            observations.append(
                "Protected service handling is in adaptive mode, so the primary path is being defended during congestion."
            )
        elif int(metrics.get("congested_ports_count", 0) or 0) > 0:
            observations.append(
                "Controller-detected congestion is present, but reroute policy has not been latched at this instant."
            )

        return {
            "window_label": window_label,
            "segments": segment_rows,
            "history": {
                "series": [
                    _series(profile["key"], profile["label"], profile["color"])
                    for profile in SEGMENT_TRAFFIC_PROFILES
                ]
            },
            "analysis": {
                "busiest_segment": (
                    {
                        "key": busiest["key"],
                        "label": busiest["label"],
                        "mbps": busiest["mbps"],
                        "status": busiest["status"],
                    }
                    if busiest
                    else None
                ),
                "hot_segments": hot_segments,
                "congestion_evidence": {
                    "congested_ports_count": int(
                        metrics.get("congested_ports_count", 0) or 0
                    ),
                    "reroute_active": bool(metrics.get("reroute_active", False)),
                    "student_throttle_active": bool(
                        metrics.get("student_throttle_active", False)
                    ),
                    "throughput_threshold_low_mbps": round(
                        self._safe_float(metrics.get("congest_low_mbps", 0.0)), 3
                    ),
                    "throughput_threshold_high_mbps": round(
                        self._safe_float(metrics.get("congest_high_mbps", 0.0)), 3
                    ),
                    "port_threshold_high_pct": round(threshold_pct, 3),
                },
                "observations": observations,
            },
        }

    def _build_live_charts(self, metrics, queue_depth, latency, max_link_util_pct):
        sample = {
            "ts": time.time(),
            "protected_throughput_mbps": self._safe_float(
                metrics.get("core_primary_mbps", 0.0)
            ),
            "wifi_load_mbps": self._safe_float(metrics.get("core_wifi_mbps", 0.0)),
            "queue_util_pct": self._safe_float(queue_depth.get("util_pct", 0.0)),
            "max_link_util_pct": self._safe_float(max_link_util_pct),
            "latency_ms": self._safe_float(latency.get("latest_ms", 0.0)),
            "reroute_active": 100.0 if bool(metrics.get("reroute_active", False)) else 0.0,
        }
        with self._chart_lock:
            if self._chart_history and (
                sample["ts"] - float(self._chart_history[-1].get("ts", 0.0))
            ) < 1.0:
                self._chart_history[-1] = sample
            else:
                self._chart_history.append(sample)
            history = list(self._chart_history)

        if history:
            window_s = max(
                0.0, float(history[-1].get("ts", 0.0)) - float(history[0].get("ts", 0.0))
            )
            window_label = "last %s sample(s) (~%ss)" % (
                len(history),
                int(round(window_s)),
            )
        else:
            window_label = "warming up live history"

        def _series(key, label, color):
            return {
                "key": key,
                "label": label,
                "color": color,
                "points": [
                    round(self._safe_float(point.get(key, 0.0)), 3) for point in history
                ],
            }

        return {
            "window_label": window_label,
            "traffic": {
                "series": [
                    _series(
                        "protected_throughput_mbps",
                        "Protected service",
                        "#58d6ff",
                    ),
                    _series("wifi_load_mbps", "Wi-Fi bulk load", "#f0a73b"),
                ]
            },
            "pressure": {
                "series": [
                    _series("max_link_util_pct", "Max link util", "#2bc17f"),
                    _series("queue_util_pct", "Queue pressure", "#f25959"),
                    _series("reroute_active", "Reroute active", "#8aa1bf"),
                ]
            },
            "latency": {
                "series": [
                    _series("latency_ms", "Latency RTT", "#58d6ff"),
                ]
            },
        }

    def _collect_active_flow_rules(self, metrics):
        connected = metrics.get("connected_switches", []) if isinstance(metrics, dict) else []
        per_switch = {}
        total = 0
        for dpid in connected:
            try:
                dpid_int = int(str(dpid))
            except Exception:
                continue
            ok, payload = self._ryu_get_json(f"/stats/flow/{dpid_int}", timeout=4)
            if not ok or not isinstance(payload, dict):
                continue
            rows = payload.get(str(dpid_int), payload.get(dpid_int, []))
            if not isinstance(rows, list):
                rows = []
            active = [r for r in rows if isinstance(r, dict) and int(r.get("priority", 0)) > 0]
            sw = f"s{dpid_int}"
            per_switch[sw] = len(active)
            total += len(active)
        return {"total": int(total), "per_switch": per_switch}

    def _build_latency_trend(self, operations):
        points = []
        events = operations.get("events", []) if isinstance(operations, dict) else []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("op") != "pingall":
                continue
            if ev.get("status") != "ok":
                continue
            try:
                points.append(
                    {
                        "ts": float(ev.get("ts", 0.0)),
                        "rtt_ms": float(ev.get("avg_rtt_ms", 0.0)),
                        "loss_pct": float(ev.get("packet_loss_pct", 0.0)),
                    }
                )
            except Exception:
                continue

        last = operations.get("last_pingall_result", {}) if isinstance(operations, dict) else {}
        if isinstance(last, dict) and last.get("ok"):
            try:
                points.append(
                    {
                        "ts": float(last.get("ts", time.time())),
                        "rtt_ms": float(last.get("avg_rtt_ms", 0.0)),
                        "loss_pct": float(last.get("packet_loss_pct", 0.0)),
                    }
                )
            except Exception:
                pass

        points = sorted(points, key=lambda x: x.get("ts", 0.0))[-12:]
        latest = points[-1]["rtt_ms"] if points else 0.0
        avg = (sum(p["rtt_ms"] for p in points) / len(points)) if points else 0.0
        if len(points) >= 2:
            delta = points[-1]["rtt_ms"] - points[-2]["rtt_ms"]
            if delta > 0.5:
                direction = "up"
            elif delta < -0.5:
                direction = "down"
            else:
                direction = "stable"
        else:
            direction = "stable"
        return {
            "direction": direction,
            "latest_ms": round(float(latest), 3),
            "avg_ms": round(float(avg), 3),
            "points": points,
        }

    def _estimate_queue_depth(self, metrics):
        switch_util = metrics.get("switch_port_util_pct", {}) if isinstance(metrics, dict) else {}
        s5 = switch_util.get("5", switch_util.get(5, {})) if isinstance(switch_util, dict) else {}
        if not isinstance(s5, dict):
            s5 = {}
        u2 = float(s5.get("2", 0.0) or 0.0)
        u3 = float(s5.get("3", 0.0) or 0.0)
        util = max(0.0, min(100.0, (u2 + u3) / 2.0))
        # Estimated queue occupancy in packets for dashboard visibility.
        p2 = int(max(0.0, min(600.0, u2 * 5.0)))
        p3 = int(max(0.0, min(600.0, u3 * 5.0)))
        total = p2 + p3
        if total >= 700:
            status = "high"
        elif total >= 300:
            status = "elevated"
        else:
            status = "normal"
        return {
            "util_pct": round(util, 3),
            "total_packets": int(total),
            "per_port_packets": {"s5-eth2": p2, "s5-eth3": p3},
            "status": status,
        }

    def _build_health_summary(self, metrics, operations, link_utilization):
        now = time.time()
        metrics_ts = float(metrics.get("ts", 0.0) or 0.0) if isinstance(metrics, dict) else 0.0
        metrics_age_s = max(0.0, now - metrics_ts) if metrics_ts > 0.0 else None
        connected = len(metrics.get("connected_switches", [])) if isinstance(metrics, dict) else 0
        last_ping = operations.get("last_pingall_result", {}) if isinstance(operations, dict) else {}
        running = operations.get("running_stress_clients", []) if isinstance(operations, dict) else []
        active_links = sum(
            1 for link in link_utilization if float(link.get("mbps", 0.0) or 0.0) >= 0.1
        )
        total_links = len(link_utilization)
        has_port_stats = bool(metrics.get("switch_port_stats")) if isinstance(metrics, dict) else False
        core_mbps = float(metrics.get("core_primary_mbps", 0.0) or 0.0) if isinstance(metrics, dict) else 0.0
        congested_ports = int(metrics.get("congested_ports_count", 0) or 0) if isinstance(metrics, dict) else 0

        loss = None
        failed_pairs = 0
        ping_age_s = None
        if isinstance(last_ping, dict) and last_ping.get("ok"):
            loss = float(last_ping.get("packet_loss_pct", 0.0) or 0.0)
            failed_pairs = int(last_ping.get("pairs_failed", 0) or 0)
            ping_ts = float(last_ping.get("ts", 0.0) or 0.0)
            if ping_ts <= 0.0 and isinstance(operations, dict):
                for ev in reversed(operations.get("events", [])):
                    if (
                        isinstance(ev, dict)
                        and ev.get("op") == "pingall"
                        and ev.get("status") == "ok"
                    ):
                        ping_ts = float(ev.get("ts", 0.0) or 0.0)
                        if ping_ts > 0.0:
                            break
            if ping_ts > 0.0:
                ping_age_s = max(0.0, now - ping_ts)

        if connected <= 0:
            label = "controller offline"
            class_name = "bad"
            summary = "No OpenFlow switches are currently connected to the controller."
        elif ping_age_s is not None and ping_age_s > 120.0:
            label = "health check stale"
            class_name = "warn"
            summary = (
                "The last end-to-end ping check is %s old, so rerun pingall before "
                "treating the previous loss figure as current."
            ) % format(ping_age_s, ".1f")
        elif loss is not None and loss >= 5.0:
            label = "critical"
            class_name = "bad"
            summary = f"Pingall loss is {loss:.1f}% with {failed_pairs} failed host pairs."
        elif loss is not None and loss > 0.0:
            label = "degraded"
            class_name = "warn"
            summary = f"Pingall loss is {loss:.1f}% with {failed_pairs} failed host pairs."
        elif metrics_age_s is not None and metrics_age_s > 12.0:
            label = "network data stale"
            class_name = "warn"
            summary = f"OpenFlow metrics are {metrics_age_s:.1f}s old, so the dashboard may lag reality."
        elif congested_ports > 0 or bool(metrics.get("reroute_active", False)):
            label = "adaptive response active"
            class_name = "warn"
            summary = (
                f"The controller is reacting to congestion on {congested_ports} port(s); "
                "the network is still reachable, but protection mode is active."
            )
        else:
            label = "healthy"
            class_name = "good"
            summary = "Controller connectivity and the latest end-to-end ping test are healthy."

        if active_links > 0 or core_mbps >= 0.1:
            traffic_mode = "Live traffic is being sampled from OpenFlow port statistics."
        elif running:
            traffic_mode = (
                "Traffic generation is running, but the latest OpenFlow sample has not "
                "yet captured a sustained non-zero transfer."
            )
        else:
            traffic_mode = (
                "Links are mostly idle right now. Zero Mbps values mean no sustained "
                "sampled transfer, not a placeholder metric."
            )

        return {
            "label": label,
            "class_name": class_name,
            "summary": summary,
            "metrics_age_s": round(metrics_age_s, 3) if metrics_age_s is not None else None,
            "ping_age_s": round(ping_age_s, 3) if ping_age_s is not None else None,
            "connected_switches": connected,
            "active_links": active_links,
            "total_links": total_links,
            "has_port_stats": has_port_stats,
            "traffic_mode": traffic_mode,
        }

    def _build_policy_classes(self, metrics, operations):
        profiles = metrics.get("priority_profiles", {}) if isinstance(metrics, dict) else {}
        running = set(operations.get("running_stress_clients", [])) if isinstance(operations, dict) else set()
        order = [
            "exam_traffic",
            "authentication_traffic",
            "normal_browsing",
            "entertainment_bulk_download",
            "critical",
            "student_bulk",
        ]
        classes = []
        for name in order:
            profile = profiles.get(name)
            if not isinstance(profile, dict):
                continue
            if name == "student_bulk":
                status = "active throttle" if bool(profile.get("throttle_active")) else "armed"
                live_hint = (
                    "Student Wi-Fi bulk traffic is being pushed into the throttle queue."
                    if bool(profile.get("throttle_active"))
                    else "Throttle queue is ready and will activate during congestion."
                )
            elif name == "entertainment_bulk_download":
                status = "demo traffic active" if running else "waiting for bulk demo"
                live_hint = (
                    "Film download demo clients are currently generating bulk traffic."
                    if running
                    else "This class becomes visible when the Wi-Fi film-download demo is started."
                )
            elif name == "critical":
                status = "always protected"
                live_hint = "IT/staff/control traffic is kept in the highest-priority queue."
            elif name == "exam_traffic":
                status = "always protected"
                live_hint = "Exam platform traffic is pinned to the highest-priority queue."
            elif name == "authentication_traffic":
                status = "always protected"
                live_hint = "DHCP/RADIUS authentication traffic is pinned to the highest-priority queue."
            else:
                status = "normal service class"
                live_hint = "General application traffic uses the normal service queue."
            classes.append(
                {
                    "name": name,
                    "label": self._human_event_name(name),
                    "description": str(profile.get("description", "")),
                    "queue": profile.get("queue"),
                    "match": profile.get("match", "traffic class"),
                    "status": status,
                    "live_hint": live_hint,
                }
            )
        return classes

    def _build_ai_summary(self, metrics, events):
        events = events if isinstance(events, list) else []
        dqn_enabled = bool(metrics.get("dqn_integration_enabled", False))
        pending = bool(metrics.get("dqn_pending_decision", False))
        reroute_active = bool(metrics.get("reroute_active", False))
        last_choice = str(metrics.get("last_ml_routing_choice") or "").strip()
        last_action_name = str(metrics.get("dqn_last_action_name") or "").strip()
        trigger_reason = str(metrics.get("dqn_last_trigger_reason") or "").replace("_", " ").strip()
        note = str(metrics.get("last_ml_note") or "").strip()
        q_values = metrics.get("last_ml_q_values", {})
        state = metrics.get("last_ml_state", {})
        q_values = q_values if isinstance(q_values, dict) else {}
        state = state if isinstance(state, dict) else {}

        action_ts = float(metrics.get("last_ml_action_ts", 0.0) or 0.0)
        trigger_ts = float(metrics.get("dqn_last_trigger_ts", 0.0) or 0.0)
        decision_ts = float(metrics.get("dqn_last_decision_ts", 0.0) or 0.0)

        recent_ml_event = None
        for ev in reversed(events):
            if not isinstance(ev, dict):
                continue
            if ev.get("event") in {
                "ml_threshold_update",
                "ml_routing_choice",
                "dqn_decision_applied",
                "dqn_decision_requested",
            }:
                recent_ml_event = ev
                break

        recent_event_ts = (
            float(recent_ml_event.get("ts", 0.0) or 0.0) if isinstance(recent_ml_event, dict) else 0.0
        )
        last_eval_ts = max(action_ts, trigger_ts, decision_ts, recent_event_ts)
        threshold_text = "high %.1f / low %.1f Mbps" % (
            float(metrics.get("congest_high_mbps", 0.0) or 0.0),
            float(metrics.get("congest_low_mbps", 0.0) or 0.0),
        )

        if pending:
            mode = "decision active"
            result = "waiting for a routing decision"
            reason = (
                "The controller detected %s and is waiting for the next ML response."
                % (trigger_reason or "a congestion event")
            )
        elif last_choice:
            mode = "observing"
            result = "recommended %s" % last_choice.replace("_", " ")
            reason = note or (
                "The latest ML-scored state recommended this route selection."
            )
        elif dqn_enabled or recent_ml_event is not None:
            mode = "observing"
            if reroute_active:
                result = "keeping protection enabled"
                reason = (
                    "Congestion is still above the release threshold, so the protected path stays engaged."
                )
            else:
                result = "no reroute needed"
                reason = note or (
                    "All monitored links are below the active reroute threshold (%s)."
                    % threshold_text
                )
        else:
            mode = "idle"
            result = "policy-only control in this snapshot"
            reason = (
                "No ML hook has published a new routing recommendation yet, so the baseline policy engine "
                "is carrying normal traffic."
            )

        return {
            "mode": mode,
            "last_evaluation_ts": last_eval_ts if last_eval_ts > 0.0 else None,
            "last_result": result,
            "reason": reason,
            "routing_choice": last_choice or None,
            "action_name": last_action_name or None,
            "trigger_reason": trigger_reason or None,
            "reward": metrics.get("last_ml_reward"),
            "epsilon": metrics.get("last_ml_epsilon"),
            "steps": metrics.get("last_ml_steps"),
            "action_index": metrics.get("last_ml_action_index"),
            "q_values": q_values,
            "state": state,
            "q_values_note": (
                "No scored action table was published for the latest cycle."
                if not q_values
                else ""
            ),
            "state_note": (
                "No state vector was published for the latest cycle."
                if not state
                else ""
            ),
        }

    def _build_system_mode(self, metrics, operations, health, ai_summary):
        running = (
            operations.get("running_stress_clients", [])
            if isinstance(operations, dict)
            else []
        )
        reroute = bool(metrics.get("reroute_active", False))
        congested_ports = int(metrics.get("congested_ports_count", 0) or 0)
        if reroute:
            network_mode = "rerouting"
        elif congested_ports > 0:
            network_mode = "congestion watch"
        elif running:
            network_mode = "demo active"
        elif str(health.get("label", "")).lower() in {"critical", "degraded"}:
            network_mode = "degraded"
        else:
            network_mode = "normal"

        if reroute:
            policy_mode = "protected service"
        elif running:
            policy_mode = "bulk demo"
        else:
            policy_mode = "normal"

        ai_mode = str(ai_summary.get("mode", "idle") or "idle")
        scenario = (
            "traffic test active for %s"
            % ", ".join(running)
            if running
            else "no live traffic test"
        )
        return {
            "network_mode": network_mode,
            "policy_mode": policy_mode,
            "ai_mode": ai_mode,
            "scenario": scenario,
        }

    def _build_why_explanations(self, metrics, health, route, ai_summary):
        reroute = bool(metrics.get("reroute_active", False))
        if reroute:
            primary_reason = (
                "Why the standby path is carrying traffic: %s" % route.get("reason", "")
            )
        else:
            primary_reason = (
                "Why the primary path stayed active: %s" % route.get("reason", "")
            )
        standby_reason = (
            "Why the backup path is ready: backup protection is armed for service continuity."
            if not reroute
            else "Why the direct path is still ready: the primary path remains available as recovery target."
        )
        ai_reason = "Why AI took this position: %s" % ai_summary.get("reason", "No AI explanation published.")
        telemetry_reason = "Why some values may read 0.0: %s" % health.get(
            "traffic_mode",
            "No network-status explanation available.",
        )
        return [primary_reason, standby_reason, ai_reason, telemetry_reason]

    def _build_story_digest(self, events, operations):
        lines = []
        controller_events = [ev for ev in (events or []) if isinstance(ev, dict)]
        ml_updates = [ev for ev in controller_events if ev.get("event") == "ml_threshold_update"]
        if ml_updates:
            last = ml_updates[-1]
            lines.append(
                "Threshold auto-adjusted %s time(s)." % len(ml_updates[-12:])
            )
            lines.append(
                "Current thresholds: low %.1f Mbps / high %.1f Mbps."
                % (
                    float(last.get("low", 0.0) or 0.0),
                    float(last.get("high", 0.0) or 0.0),
                )
            )
            ts = float(last.get("ts", 0.0) or 0.0)
            if ts > 0.0:
                lines.append(
                    "Last update: %s." % time.strftime("%I:%M:%S %p", time.localtime(ts))
                )

        major = None
        for ev in reversed(controller_events):
            if ev.get("event") in {
                "policy_activated",
                "policy_deactivated",
                "throughput_congestion_on",
                "throughput_congestion_off",
                "dqn_decision_applied",
                "ml_routing_choice",
            }:
                major = self._summarize_controller_event(ev)
                break
        if major:
            lines.append("Last major action: %s." % major.get("title", "controller event"))
            if major.get("detail"):
                lines.append(major["detail"])

        runtime_events = (
            operations.get("events", []) if isinstance(operations, dict) else []
        )
        runtime_major = None
        for ev in reversed(runtime_events):
            if ev.get("op") in {"pingall", "start_stress", "stop_stress", "add_host"}:
                runtime_major = self._summarize_runtime_event(ev)
                break
        if runtime_major:
            lines.append("Latest runtime action: %s." % runtime_major.get("title", "runtime event"))
            if runtime_major.get("detail"):
                lines.append(runtime_major["detail"])

        return lines or ["No controller decision timeline is available yet."]

    def _build_flow_explanation(self, metrics, flow_rules, route):
        lines = []
        policy_rules = metrics.get("policy_engine_rules", {}) if isinstance(metrics, dict) else {}
        if policy_rules:
            per_switch = ", ".join(
                f"s{int(dpid)}:{int(count)}"
                for dpid, count in sorted(policy_rules.items(), key=lambda x: int(x[0]))
            )
            lines.append(f"Policy engine rules programmed per switch: {per_switch}.")
        total_flows = int(flow_rules.get("total", 0) or 0) if isinstance(flow_rules, dict) else 0
        if total_flows > 0:
            per_switch = flow_rules.get("per_switch", {}) if isinstance(flow_rules, dict) else {}
            per_switch_text = ", ".join(
                f"{sw}:{int(cnt)}" for sw, cnt in sorted(per_switch.items())
            )
            lines.append(
                f"Live active OpenFlow entries visible through Ryu stats: {total_flows}"
                + (f" ({per_switch_text})." if per_switch_text else ".")
            )
        flow_mods = int(metrics.get("controller_flow_mods", 0) or 0) if isinstance(metrics, dict) else 0
        packet_ins = int(metrics.get("controller_packet_ins", 0) or 0) if isinstance(metrics, dict) else 0
        mac_learns = int(metrics.get("controller_mac_learns", 0) or 0) if isinstance(metrics, dict) else 0
        lines.append(
            "Controller activity so far: "
            f"{flow_mods} flow_mods, {packet_ins} packet_ins, {mac_learns} MAC learns."
        )
        if bool(metrics.get("reroute_active", False)):
            lines.append(
                "Adaptive ICMP protection is ON. IT-lab traffic to 10.0.0.100 is rewritten "
                "toward the backup server and forwarded along the highlighted backup path."
            )
        else:
            lines.append(
                "Adaptive ICMP protection is OFF. Protected traffic remains on the direct "
                "primary-service path until congestion crosses the configured threshold."
            )
        lines.append(
            "Use the Flows tab for a switch-level dump of exact matches, priorities, cookies, and actions."
        )
        return lines

    def _summarize_controller_event(self, ev):
        if not isinstance(ev, dict):
            return None
        event = str(ev.get("event", "")).strip()
        if not event:
            return None
        detail = ""
        if event == "throughput_congestion_on":
            detail = "Core-to-service load %.2f Mbps crossed high threshold %.2f Mbps." % (
                float(ev.get("mbps", 0.0) or 0.0),
                float(ev.get("high", 0.0) or 0.0),
            )
        elif event == "throughput_congestion_off":
            detail = "Core-to-service load fell back to %.2f Mbps." % (
                float(ev.get("mbps", 0.0) or 0.0),
            )
        elif event == "policy_activated":
            detail = "Adaptive reroute and student QoS throttle were enabled."
        elif event == "policy_deactivated":
            detail = "Adaptive reroute and student QoS throttle were cleared."
        elif event == "ml_routing_choice":
            detail = "ML selected %s." % str(ev.get("routing_choice", "unknown"))
        elif event == "ml_threshold_update":
            detail = "ML adjusted thresholds to high %.1f / low %.1f Mbps." % (
                float(ev.get("high", 0.0) or 0.0),
                float(ev.get("low", 0.0) or 0.0),
            )
        elif event == "policy_engine_programmed":
            detail = "Switch s%s received %s class-based rules." % (
                int(ev.get("dpid", 0) or 0),
                int(ev.get("rule_count", 0) or 0),
            )
        elif event == "switch_connected":
            detail = "Switch s%s connected to the controller." % int(ev.get("dpid", 0) or 0)
        return {
            "ts": float(ev.get("ts", 0.0) or 0.0),
            "source": "controller",
            "title": self._human_event_name(event),
            "detail": detail,
        }

    def _summarize_runtime_event(self, ev):
        if not isinstance(ev, dict):
            return None
        op = str(ev.get("op", "")).strip()
        if not op:
            return None
        status = str(ev.get("status", "")).strip()
        detail = ""
        if op == "pingall" and status == "ok":
            detail = "Loss %.1f%%, avg RTT %.2f ms." % (
                float(ev.get("packet_loss_pct", 0.0) or 0.0),
                float(ev.get("avg_rtt_ms", 0.0) or 0.0),
            )
        elif op == "start_stress" and status == "ok":
            detail = "Started %s-second %s demo for %s." % (
                int(ev.get("seconds", 0) or 0),
                str(ev.get("mode", "traffic")),
                ", ".join(ev.get("clients", [])) or "selected clients",
            )
        elif op == "stop_stress" and status == "ok":
            detail = "Stopped stress clients: %s." % (
                ", ".join(ev.get("stopped_clients", [])) or "none",
            )
        elif op == "add_host" and status == "ok":
            detail = "Added %s (%s) on %s at %.0f Mbps as %s." % (
                str(ev.get("display_name", ev.get("host_id", "device"))),
                str(ev.get("ip", "")),
                str(ev.get("attach_switch", "")),
                float(ev.get("bandwidth_mbps", 0.0) or 0.0),
                _device_category_label(ev.get("category")),
            )
        return {
            "ts": float(ev.get("ts", 0.0) or 0.0),
            "source": "runtime",
            "title": f"{self._human_event_name(op)} [{status or 'status'}]",
            "detail": detail,
        }

    def _build_recent_story(self, events, operations):
        story = []
        for ev in events[-30:] if isinstance(events, list) else []:
            item = self._summarize_controller_event(ev)
            if item:
                story.append(item)
        runtime_events = operations.get("events", []) if isinstance(operations, dict) else []
        for ev in runtime_events[-20:]:
            item = self._summarize_runtime_event(ev)
            if item:
                story.append(item)
        story.sort(key=lambda x: x.get("ts", 0.0))
        return story[-14:]

    def _load_latest_stage11_report(self):
        try:
            candidates = glob.glob(
                os.path.join(self.results_dir, "stage11_comparison*.json")
            )
        except Exception:
            candidates = []
        if not candidates:
            return {"available": False}
        latest = max(candidates, key=lambda path: os.path.getmtime(path))
        payload = self._read_json_file(latest)
        if not isinstance(payload, dict):
            return {"available": False, "path": latest}
        base, _ = os.path.splitext(latest)
        csv_path = base + ".csv"
        md_path = base + ".md"
        before = payload.get("before_adaptive", {})
        after = payload.get("after_adaptive", {})
        throughput_before = float(before.get("throughput_mbps", 0.0) or 0.0)
        throughput_after = float(after.get("throughput_mbps", 0.0) or 0.0)
        gain = throughput_after - throughput_before
        gain_pct = (gain / throughput_before * 100.0) if throughput_before > 0 else None
        return {
            "available": True,
            "path": latest,
            "json_path": latest,
            "csv_path": csv_path if os.path.isfile(csv_path) else None,
            "md_path": md_path if os.path.isfile(md_path) else None,
            "tag": payload.get("tag", os.path.basename(latest)),
            "ts": payload.get("ts"),
            "throughput_before_mbps": round(throughput_before, 3),
            "throughput_after_mbps": round(throughput_after, 3),
            "throughput_gain_mbps": round(gain, 3),
            "throughput_gain_pct": round(gain_pct, 3) if gain_pct is not None else None,
            "loss_before_pct": float(before.get("pingall_loss_pct", 0.0) or 0.0),
            "loss_after_pct": float(after.get("pingall_loss_pct", 0.0) or 0.0),
            "latency_before_ms": float(before.get("latency_avg_ms", 0.0) or 0.0),
            "latency_after_ms": float(after.get("latency_avg_ms", 0.0) or 0.0),
            "response_s": after.get("congestion_response_s"),
            "reroute_before": bool(before.get("reroute_observed", False)),
            "reroute_after": bool(after.get("reroute_observed", False)),
            "adaptive_policy_activated": bool(after.get("policy_activated_count", 0)),
            "results": payload.get("measurable_project_results", []),
        }

    def _build_alerts(self, metrics, operations, queue_depth, latency):
        alerts = []
        connected = metrics.get("connected_switches", []) if isinstance(metrics, dict) else []
        if not connected:
            alerts.append({"severity": "critical", "message": "Controller has no connected switches."})
        congested_count = int(metrics.get("congested_ports_count", 0) or 0)
        if congested_count > 0:
            alerts.append(
                {
                    "severity": "warning",
                    "message": f"{congested_count} congested port(s) detected by controller.",
                }
            )
        if bool(metrics.get("reroute_active", False)):
            alerts.append({"severity": "warning", "message": "Adaptive reroute policy is active."})

        last_ping = operations.get("last_pingall_result", {}) if isinstance(operations, dict) else {}
        if isinstance(last_ping, dict) and last_ping.get("ok"):
            loss = float(last_ping.get("packet_loss_pct", 0.0) or 0.0)
            if loss > 0.0:
                sev = "critical" if loss >= 5.0 else "warning"
                alerts.append({"severity": sev, "message": f"Pingall reports packet loss: {loss:.1f}%."})

        if queue_depth.get("status") in {"high", "elevated"}:
            sev = "critical" if queue_depth.get("status") == "high" else "warning"
            alerts.append(
                {
                    "severity": sev,
                    "message": (
                        "Estimated Wi-Fi queue depth is %s (%s packets)."
                        % (queue_depth.get("status"), queue_depth.get("total_packets"))
                    ),
                }
            )

        latest_latency = float(latency.get("latest_ms", 0.0) or 0.0)
        if latest_latency >= 20.0:
            sev = "critical" if latest_latency >= 40.0 else "warning"
            alerts.append(
                {
                    "severity": sev,
                    "message": f"Latency trend is elevated ({latest_latency:.2f} ms).",
                }
            )
        return alerts

    def build_dashboard_snapshot(self, metrics, events, topology, operations):
        queue_depth = self._estimate_queue_depth(metrics)
        latency = self._build_latency_trend(operations)
        flow_rules = self._collect_active_flow_rules(metrics)
        route = topology.get("route_overview", {}) if isinstance(topology, dict) else {}
        links = topology.get("links", []) if isinstance(topology, dict) else []
        link_utilization = []
        max_link_util_pct = 0.0
        for link in links:
            if not isinstance(link, dict):
                continue
            util = float(link.get("util", 0.0) or 0.0)
            max_link_util_pct = max(max_link_util_pct, util)
            link_utilization.append(
                {
                    "src": link.get("src"),
                    "dst": link.get("dst"),
                    "util_pct": round(util, 3),
                    "mbps": float(link.get("mbps", 0.0) or 0.0),
                    "bw_mbps": float(link.get("bw_mbps", 0.0) or 0.0),
                    "route_role": link.get("route_role", "none"),
                }
            )
        link_utilization.sort(key=lambda x: x.get("util_pct", 0.0), reverse=True)
        health = self._build_health_summary(metrics, operations, link_utilization)
        alerts = self._build_alerts(metrics, operations, queue_depth, latency)
        recent_story = self._build_recent_story(events, operations)
        ai_summary = self._build_ai_summary(metrics, events)
        system_mode = self._build_system_mode(metrics, operations, health, ai_summary)
        latest_evaluation = self._load_latest_stage11_report()
        charts = self._build_live_charts(
            metrics, queue_depth, latency, max_link_util_pct
        )
        segment_analytics = self._build_segment_analytics(metrics)

        return {
            "ts": time.time(),
            "health": health,
            "charts": charts,
            "segment_analytics": segment_analytics,
            "system_mode": system_mode,
            "queue_depth": queue_depth,
            "latency_trend": latency,
            "link_utilization": link_utilization,
            "alerts": alerts,
            "route_overview": route,
            "policy_classes": self._build_policy_classes(metrics, operations),
            "flow_explanation": self._build_flow_explanation(metrics, flow_rules, route),
            "why_explanations": self._build_why_explanations(
                metrics, health, route, ai_summary
            ),
            "recent_story": recent_story,
            "story_digest": self._build_story_digest(events, operations),
            "latest_evaluation": latest_evaluation,
            "ai_summary": ai_summary,
            "active_flow_rules": flow_rules,
            "controller_actions": {
                "reroute_active": bool(metrics.get("reroute_active", False)),
                "dqn_integration_enabled": bool(
                    metrics.get("dqn_integration_enabled", False)
                ),
                "dqn_pending_decision": bool(metrics.get("dqn_pending_decision", False)),
                "dqn_last_trigger_reason": metrics.get("dqn_last_trigger_reason"),
                "dqn_last_trigger_ts": metrics.get("dqn_last_trigger_ts"),
                "dqn_last_decision_ts": metrics.get("dqn_last_decision_ts"),
                "last_ml_routing_choice": metrics.get("last_ml_routing_choice"),
                "last_ml_q_values": metrics.get("last_ml_q_values", {}),
                "last_ml_state": metrics.get("last_ml_state", {}),
                "last_ml_reward": metrics.get("last_ml_reward"),
                "last_ml_epsilon": metrics.get("last_ml_epsilon"),
                "last_ml_steps": metrics.get("last_ml_steps"),
                "last_ml_action_index": metrics.get("last_ml_action_index"),
                "last_ml_note": metrics.get("last_ml_note"),
                "last_ml_action_ts": metrics.get("last_ml_action_ts"),
                "dqn_last_action_name": metrics.get("dqn_last_action_name"),
                "recent_policy_events": events[-10:],
                "recent_runtime_events": operations.get("events", [])[-10:]
                if isinstance(operations, dict)
                else [],
            },
            "telemetry": {
                "metrics_age_s": health.get("metrics_age_s"),
                "ping_age_s": health.get("ping_age_s"),
                "active_links": health.get("active_links", 0),
                "total_links": health.get("total_links", 0),
                "has_port_stats": health.get("has_port_stats", False),
                "traffic_mode": health.get("traffic_mode", ""),
                "runtime_ok": bool(operations.get("ok", False))
                if isinstance(operations, dict)
                else False,
            },
            "summary": {
                "connected_switches": len(metrics.get("connected_switches", []))
                if isinstance(metrics, dict)
                else 0,
                "nodes": len(topology.get("nodes", [])) if isinstance(topology, dict) else 0,
                "links": len(topology.get("links", [])) if isinstance(topology, dict) else 0,
                "max_link_util_pct": round(max_link_util_pct, 3),
                "alerts_count": len(alerts),
            },
        }


def create_app(service: DashboardService):
    app = Flask(__name__)

    def _is_transient_pingall_error(resp):
        if not isinstance(resp, dict):
            return False
        try:
            status_code = int(resp.get("_http_status", 0) or 0)
        except Exception:
            status_code = 0
        detail = " ".join(
            str(resp.get(key, "")) for key in ("error", "message", "runtime_api")
        ).lower()
        transient_terms = (
            "busy",
            "delayed",
            "unreachable",
            "connection refused",
            "timed out",
            "timeout",
            "reset by peer",
        )
        return status_code in {404, 405, 503} or any(
            term in detail for term in transient_terms
        )

    def _run_pingall_with_retry(max_wait_s=35.0, retry_delay_s=2.0):
        deadline = time.time() + max_wait_s
        attempts = 0
        last_resp = {"error": "runtime pingall failed"}
        while True:
            attempts += 1
            ok, resp = service._runtime_request("POST", "/pingall", {}, timeout=120)
            if ok:
                return True, resp, attempts
            last_resp = resp if isinstance(resp, dict) else {"error": str(resp)}
            if time.time() >= deadline or not _is_transient_pingall_error(last_resp):
                return False, last_resp, attempts
            time.sleep(retry_delay_s)

    @app.get("/")
    def index():
        return Response(HTML_PAGE, mimetype="text/html")

    @app.get("/api/metrics")
    def api_metrics():
        data = service._read_json_file(service.metrics_file)
        if data is None:
            return (
                jsonify(
                    {
                        "error": "metrics file not found",
                        "path": service.metrics_file,
                        "reroute_active": False,
                        "connected_switches": [],
                    }
                ),
                404,
            )
        return jsonify(data)

    @app.get("/api/events")
    def api_events():
        try:
            limit = int(request.args.get("limit", "30"))
        except Exception:
            limit = 30
        limit = max(1, min(limit, 500))
        return jsonify(service._read_events(limit))

    @app.get("/api/topology")
    def api_topology():
        metrics = service._read_json_file(service.metrics_file) or {}
        return jsonify(service._build_topology(metrics))

    @app.get("/api/devices")
    def api_devices():
        return jsonify(service._load_devices())

    @app.get("/api/network/settings")
    def api_network_settings():
        return jsonify(
            {
                "ok": True,
                "settings": service.current_network_settings(),
                "manual_settings_file": service.manual_settings_file,
            }
        )

    @app.put("/api/network/settings")
    def api_update_network_settings():
        payload = request.get_json(silent=True) or {}
        try:
            settings = {
                "congest_high_mbps": float(payload.get("congest_high_mbps", 0) or 0),
                "congest_low_mbps": float(payload.get("congest_low_mbps", 0) or 0),
                "port_congest_high_pct": float(
                    payload.get("port_congest_high_pct", 0) or 0
                ),
                "port_congest_low_pct": float(
                    payload.get("port_congest_low_pct", 0) or 0
                ),
            }
        except Exception:
            return jsonify({"ok": False, "error": "all network settings must be numeric"}), 400

        if settings["congest_low_mbps"] >= settings["congest_high_mbps"]:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "low throughput threshold must stay below the high threshold",
                    }
                ),
                400,
            )
        if settings["port_congest_low_pct"] >= settings["port_congest_high_pct"]:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "low port-utilization threshold must stay below the high threshold",
                    }
                ),
                400,
            )
        if (
            settings["congest_high_mbps"] <= 0
            or settings["congest_low_mbps"] <= 0
            or settings["port_congest_high_pct"] <= 0
            or settings["port_congest_low_pct"] <= 0
        ):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "all network settings must be greater than zero",
                    }
                ),
                400,
            )

        published = service.publish_network_settings(settings)
        return jsonify(
            {
                "ok": True,
                "message": (
                    "Network policy settings published. The controller will apply the new thresholds on its next live update."
                ),
                "settings": settings,
                "published": published,
            }
        )

    @app.get("/api/network/automation")
    def api_network_automation():
        return jsonify(service._build_network_automation_view())

    @app.post("/api/network/automation/intent")
    def api_network_automation_intent():
        payload = request.get_json(silent=True) or {}
        try:
            result = service.execute_network_automation_intent(payload.get("command"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "message": result.get("message"),
                "intent": {
                    "action": result.get("action"),
                    "switch": result.get("switch"),
                    "vlan_ids": result.get("vlan_ids", []),
                    "allow_between": result.get("allow_between", []),
                },
                "automation": result.get("automation"),
            }
        )

    @app.post("/api/network/automation/auto")
    def api_auto_configure_switch():
        payload = request.get_json(silent=True) or {}
        try:
            automation = service.auto_configure_switch_vlans(
                payload.get("switch"),
                payload.get("vlan_ids") or [],
                payload.get("allow_between") or [],
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        switch_name = str(payload.get("switch", "")).strip().lower()
        vlan_ids = [
            int(value)
            for value in (payload.get("vlan_ids") or [])
            if str(value).strip().isdigit()
        ]
        return jsonify(
            {
                "ok": True,
                "message": (
                    f"Controller auto-configured {switch_name} with VLANs {', '.join(str(vlan) for vlan in vlan_ids)} using access-port order."
                ),
                "automation": automation,
            }
        )

    @app.delete("/api/network/automation/switch/<switch>")
    def api_clear_switch_automation(switch):
        try:
            automation = service.clear_switch_automation(switch)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        switch_name = str(switch).strip().lower()
        return jsonify(
            {
                "ok": True,
                "message": f"Controller automation cleared from {switch_name}.",
                "automation": automation,
            }
        )

    @app.post("/api/network/automation/vlans")
    def api_assign_vlan():
        payload = request.get_json(silent=True) or {}
        try:
            automation = service.assign_device_vlan(
                payload.get("switch"),
                payload.get("device"),
                payload.get("vlan_id"),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "message": (
                    f"Controller VLAN policy updated: {payload.get('device')} is now in VLAN {int(payload.get('vlan_id'))} on {str(payload.get('switch', '')).strip().lower()}."
                ),
                "automation": automation,
            }
        )

    @app.delete("/api/network/automation/vlans/<switch>/<int:vlan_id>")
    def api_remove_vlan(switch, vlan_id):
        try:
            automation = service.remove_vlan_assignment(switch, vlan_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "message": f"Controller VLAN policy removed for VLAN {vlan_id} on {str(switch).strip().lower()}.",
                "automation": automation,
            }
        )

    @app.delete("/api/network/automation/vlans/<switch>/<int:vlan_id>/<path:device_name>")
    def api_remove_vlan_member(switch, vlan_id, device_name):
        try:
            automation = service.remove_vlan_assignment(
                switch,
                vlan_id,
                urllib_parse.unquote(device_name),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        decoded_name = urllib_parse.unquote(device_name)
        return jsonify(
            {
                "ok": True,
                "message": f"{decoded_name} was removed from VLAN {vlan_id} on {str(switch).strip().lower()}.",
                "automation": automation,
            }
        )

    @app.post("/api/network/automation/interconnect")
    def api_set_vlan_interconnect():
        payload = request.get_json(silent=True) or {}
        enabled = bool(payload.get("enabled", True))
        try:
            automation = service.set_vlan_interconnect(
                payload.get("switch"),
                payload.get("vlan_a"),
                payload.get("vlan_b"),
                enabled,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        switch_name = str(payload.get("switch", "")).strip().lower()
        vlan_a = int(payload.get("vlan_a"))
        vlan_b = int(payload.get("vlan_b"))
        verb = "allowed" if enabled else "blocked"
        return jsonify(
            {
                "ok": True,
                "message": f"Traffic between VLAN {vlan_a} and VLAN {vlan_b} on {switch_name} is now {verb}.",
                "automation": automation,
            }
        )

    @app.post("/api/devices")
    def api_add_device():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        ip = str(payload.get("ip", "")).strip()
        attach = str(payload.get("attach_switch", "s1")).strip() or "s1"
        category = _normalize_device_category(payload.get("category"))
        bw = payload.get("bandwidth_mbps", 50)
        if not name or not ip:
            return jsonify({"error": "name and ip are required"}), 400
        try:
            ip_obj = ipaddress.ip_address(ip)
        except ValueError:
            return jsonify({"error": "invalid IPv4 address"}), 400
        campus_subnet = ipaddress.ip_network("10.0.0.0/24")
        if ip_obj not in campus_subnet or ip_obj in {
            campus_subnet.network_address,
            campus_subnet.broadcast_address,
        }:
            return (
                jsonify({"error": "device IP must be inside the campus subnet 10.0.0.0/24"}),
                400,
            )
        for device in service._load_devices():
            if str(device.get("ip", "")).strip() == ip:
                return (
                    jsonify(
                        {
                            "error": f"duplicate IP detected: {ip} is already assigned to {device.get('display_name') or device.get('name')}",
                        }
                    ),
                    409,
                )

        ok, resp = service._runtime_request(
            "POST",
            "/add_host",
            {
                "name": name,
                "ip": ip,
                "attach_switch": attach,
                "category": category,
                "bandwidth_mbps": bw,
            },
            timeout=45,
        )
        if not ok:
            return (
                jsonify(
                    {
                        "error": resp.get("error", "failed to add host"),
                        "runtime_api": service.runtime_api_base,
                    }
                ),
                503,
            )
        return jsonify(resp)

    @app.get("/api/devices/<path:name>")
    def api_device_details(name):
        device_name = urllib_parse.unquote(name)
        cached = service._load_device(device_name)
        if not cached:
          return jsonify({"error": f"device not found: {device_name}"}), 404

        ok, resp = service._runtime_request(
            "GET",
            "/device/" + urllib_parse.quote(device_name, safe=""),
            timeout=20,
        )
        if ok and isinstance(resp, dict) and isinstance(resp.get("device"), dict):
            merged = dict(cached)
            merged.update(resp.get("device", {}))
            return jsonify({"ok": True, "device": merged})

        payload = {"ok": True, "device": cached}
        if isinstance(resp, dict):
            payload["warning"] = resp.get("error", "live device details are unavailable")
            payload["runtime_api"] = service.runtime_api_base
        else:
            payload["warning"] = "live device details are unavailable"
        return jsonify(payload)

    @app.put("/api/devices/<path:name>")
    def api_update_device(name):
        device_name = urllib_parse.unquote(name)
        cached = service._load_device(device_name)
        if not cached:
            return jsonify({"ok": False, "error": f"device not found: {device_name}"}), 404
        if not bool(cached.get("removable", False)):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "only dashboard-added endpoints can be edited from the live topology",
                    }
                ),
                400,
            )

        payload = request.get_json(silent=True) or {}
        display_name = str(payload.get("display_name", "")).strip()
        ip = str(payload.get("ip", "")).strip()
        attach = str(payload.get("attach_switch", cached.get("attach_switch", "s1"))).strip() or "s1"
        category = _normalize_device_category(payload.get("category"))
        bw = payload.get("bandwidth_mbps", cached.get("bandwidth_mbps", 50))
        if not display_name or not ip:
            return jsonify({"ok": False, "error": "display_name and ip are required"}), 400
        try:
            ip_obj = ipaddress.ip_address(ip)
        except ValueError:
            return jsonify({"ok": False, "error": "invalid IPv4 address"}), 400
        campus_subnet = ipaddress.ip_network("10.0.0.0/24")
        if ip_obj not in campus_subnet or ip_obj in {
            campus_subnet.network_address,
            campus_subnet.broadcast_address,
        }:
            return (
                jsonify({"ok": False, "error": "device IP must be inside the campus subnet 10.0.0.0/24"}),
                400,
            )
        try:
            bw_value = float(bw)
        except Exception:
            return jsonify({"ok": False, "error": "bandwidth_mbps must be numeric"}), 400
        if bw_value <= 0:
            return jsonify({"ok": False, "error": "bandwidth_mbps must be greater than zero"}), 400

        for device in service._load_devices():
            if str(device.get("name", "")).strip() == device_name:
                continue
            if str(device.get("ip", "")).strip() == ip:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": f"duplicate IP detected: {ip} is already assigned to {device.get('display_name') or device.get('name')}",
                        }
                    ),
                    409,
                )

        ok, resp = service._runtime_request(
            "PUT",
            "/device/" + urllib_parse.quote(device_name, safe=""),
            {
                "display_name": display_name,
                "ip": ip,
                "attach_switch": attach,
                "category": category,
                "bandwidth_mbps": bw_value,
            },
            timeout=45,
        )
        if not ok:
            status = 503
            if isinstance(resp, dict):
                try:
                    http_status = int(resp.get("_http_status", 0) or 0)
                except Exception:
                    http_status = 0
                if http_status in {400, 404, 409}:
                    status = http_status
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": resp.get("error", "failed to update endpoint")
                        if isinstance(resp, dict)
                        else "failed to update endpoint",
                        "runtime_api": service.runtime_api_base,
                    }
                ),
                status,
            )
        return jsonify(resp)

    @app.delete("/api/devices/<path:name>")
    def api_remove_device(name):
        device_name = urllib_parse.unquote(name)
        cached = service._load_device(device_name)
        if not cached:
            return jsonify({"ok": False, "error": f"device not found: {device_name}"}), 404
        if not bool(cached.get("removable", False)):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "only dashboard-added endpoints can be removed from the live topology",
                    }
                ),
                400,
            )

        ok, resp = service._runtime_request(
            "DELETE",
            "/device/" + urllib_parse.quote(device_name, safe=""),
            timeout=30,
        )
        if not ok:
            status = 503
            if isinstance(resp, dict):
                try:
                    http_status = int(resp.get("_http_status", 0) or 0)
                except Exception:
                    http_status = 0
                if http_status in {400, 404, 409}:
                    status = http_status
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": resp.get("error", "failed to remove endpoint")
                        if isinstance(resp, dict)
                        else "failed to remove endpoint",
                        "runtime_api": service.runtime_api_base,
                    }
                ),
                status,
            )
        return jsonify(resp)

    @app.get("/api/report/latest/<fmt>")
    def api_report_latest(fmt):
        report = service._load_latest_stage11_report()
        if not report.get("available"):
            return jsonify({"error": "no Stage 11 report found"}), 404
        fmt = str(fmt or "").lower()
        if fmt not in {"json", "csv", "md"}:
            return jsonify({"error": "unsupported report format"}), 400
        path = report.get(f"{fmt}_path")
        if not path or not os.path.isfile(path):
            return jsonify({"error": f"{fmt} report not found"}), 404
        return send_file(path, as_attachment=True, download_name=os.path.basename(path))

    @app.get("/api/flows")
    def api_flows():
        switch = request.args.get("switch", "s1")
        ok, output = service._dump_flows(switch)
        return jsonify({"ok": ok, "switch": switch, "output": output})

    @app.get("/api/operations")
    def api_operations():
        ok, resp = service._runtime_request("GET", "/operations", timeout=20)
        if not ok:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": resp.get("error", "runtime operations unavailable"),
                        "runtime_api": service.runtime_api_base,
                    }
                ),
                503,
            )
        return jsonify(resp)

    @app.post("/api/actions/pingall")
    def api_pingall():
        ok, resp, attempts = _run_pingall_with_retry()
        if not ok:
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": resp.get("error", "runtime pingall failed"),
                        "attempts": attempts,
                        "runtime_api": service.runtime_api_base,
                    }
                ),
                503,
            )
        retry_count = max(0, attempts - 1)
        message = "pingall executed (loss: %s%%)" % resp.get("packet_loss_pct")
        if retry_count > 0:
            message += " after waiting for runtime readiness"
        return jsonify(
            {
                "ok": True,
                "message": message,
                "attempts": attempts,
                "transient_retries": retry_count,
                "packet_loss_pct": resp.get("packet_loss_pct"),
                "avg_rtt_ms": resp.get("avg_rtt_ms"),
                "result": resp,
            }
        )

    @app.post("/api/actions/start-stress")
    def api_start_stress():
        payload = request.get_json(silent=True) or {}
        ok, resp = service._runtime_request(
            "POST",
            "/start_stress",
            {
                "seconds": payload.get("seconds", 45),
                "iperf_port": payload.get("iperf_port", 5201),
                "reverse_download": bool(payload.get("reverse_download", True)),
                "clients": payload.get("clients"),
            },
            timeout=30,
        )
        if not ok:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": resp.get("error", "failed to start stress test"),
                        "runtime_api": service.runtime_api_base,
                    }
                ),
                503,
            )
        return jsonify(resp)

    @app.post("/api/actions/stop-stress")
    def api_stop_stress():
        ok, resp = service._runtime_request("POST", "/stop_stress", {}, timeout=20)
        if not ok:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": resp.get("error", "failed to stop stress test"),
                        "runtime_api": service.runtime_api_base,
                    }
                ),
                503,
            )
        return jsonify(resp)

    @app.get("/api/dashboard")
    def api_dashboard():
        metrics = service._read_json_file(service.metrics_file) or {}
        events = service._read_events(80)
        topo = service._build_topology(metrics)
        ok, operations = service._runtime_request("GET", "/operations", timeout=20)
        if not ok:
            operations = {"ok": False, "events": [], "last_pingall_result": {}}
        payload = service.build_dashboard_snapshot(metrics, events, topo, operations)
        return jsonify(payload)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--metrics-file", default="/tmp/campus_metrics.json")
    parser.add_argument("--events-file", default="/tmp/campus_policy_events.jsonl")
    parser.add_argument(
        "--topology-state-file", default="/tmp/campus_topology_state.json"
    )
    parser.add_argument("--runtime-api-base", default="http://127.0.0.1:9091")
    parser.add_argument(
        "--ryu-base",
        default=(
            "http://%s:%s"
            % (
                os.getenv("CAMPUS_RYU_WSAPI_HOST", "127.0.0.1"),
                os.getenv("CAMPUS_RYU_WSAPI_PORT", "8081"),
            )
        ),
    )
    parser.add_argument(
        "--manual-settings-file",
        "--ml-action-file",
        dest="manual_settings_file",
        default=os.getenv(
            "CAMPUS_MANUAL_SETTINGS_FILE", "/tmp/campus_manual_settings.json"
        ),
    )
    parser.add_argument(
        "--network-automation-file",
        default=os.getenv(
            "CAMPUS_NETWORK_AUTOMATION_FILE", "/tmp/campus_network_automation.json"
        ),
    )
    args = parser.parse_args()

    service = DashboardService(
        metrics_file=args.metrics_file,
        events_file=args.events_file,
        topology_state_file=args.topology_state_file,
        runtime_api_base=args.runtime_api_base,
        ryu_base=args.ryu_base,
        manual_settings_file=args.manual_settings_file,
        network_automation_file=args.network_automation_file,
    )
    app = create_app(service)
    print(f"Network Manager UI : http://{args.host}:{args.port}")
    print(f"Metrics file       : {args.metrics_file}")
    print(f"Events file        : {args.events_file}")
    print(f"Topology state file: {args.topology_state_file}")
    print(f"Runtime API base   : {service.runtime_api_base}")
    print(f"Ryu REST base      : {service.ryu_base}")
    print(f"Settings override  : {service.manual_settings_file}")
    print(f"Network automation : {service.network_automation_file}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
