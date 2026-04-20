#!/usr/bin/env python3

"""Stage 8 DQN adaptive routing module.

Goal:
- Receive live network state from controller metrics.
- Produce adaptive routing/threshold actions.
- Learn online from reward signals derived from congestion/latency/utilization.

Input:
- CAMPUS_METRICS_FILE (default: /tmp/campus_metrics.json)

Output:
- CAMPUS_ML_ACTION_FILE (default: /tmp/campus_ml_action.json)

This module is intentionally independent from Ryu so it can run as a separate
AI component and write recommendations that the controller consumes.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import torch
import torch.nn as nn
import torch.optim as optim


# DQN action space used by the controller ML hook.
ACTION_NAMES = [
    "keep_primary",
    "prefer_backup_path",
    "tighten_thresholds",
    "relax_thresholds",
    "emergency_throttle",
]


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json_atomic(path: str, payload: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


class ReplayBuffer:
    def __init__(self, capacity: int):
        self.buf = deque(maxlen=max(128, int(capacity)))

    def add(self, state, action, reward, next_state, done):
        self.buf.append((state, action, reward, next_state, done))

    def sample(self, n: int):
        n = min(len(self.buf), int(n))
        return random.sample(self.buf, n)

    def __len__(self):
        return len(self.buf)


class DQN(nn.Module):
    def __init__(self, in_dim: int, out_dim: int):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, 128),
            nn.ReLU(),
            nn.Linear(128, 96),
            nn.ReLU(),
            nn.Linear(96, out_dim),
        )

    def forward(self, x):
        return self.net(x)


@dataclass
class AgentConfig:
    metrics_file: str
    action_file: str
    model_file: str
    interval: float
    gamma: float
    lr: float
    epsilon_start: float
    epsilon_end: float
    epsilon_decay_steps: int
    batch_size: int
    memory_size: int
    target_update_every: int
    warmup_steps: int
    max_steps: int
    train: bool


class DQNRoutingAgent:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.state_dim = 12
        self.n_actions = len(ACTION_NAMES)

        self.policy_net = DQN(self.state_dim, self.n_actions)
        self.target_net = DQN(self.state_dim, self.n_actions)
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()
        self.optim = optim.Adam(self.policy_net.parameters(), lr=cfg.lr)

        self.replay = ReplayBuffer(cfg.memory_size)
        self.steps = 0
        self.prev_state = None
        self.prev_action = None
        self.prev_metrics = None

        self._load_model()

    def _load_model(self):
        if not os.path.exists(self.cfg.model_file):
            return
        try:
            state = torch.load(self.cfg.model_file, map_location="cpu")
            self.policy_net.load_state_dict(state["policy"])
            self.target_net.load_state_dict(state["target"])
            self.steps = int(state.get("steps", 0))
            print(f"Loaded model: {self.cfg.model_file} (steps={self.steps})")
        except Exception as exc:
            print(f"Warning: failed to load model ({exc}), starting fresh.")

    def _save_model(self):
        payload = {
            "policy": self.policy_net.state_dict(),
            "target": self.target_net.state_dict(),
            "steps": self.steps,
            "actions": ACTION_NAMES,
            "state_dim": self.state_dim,
            "saved_ts": time.time(),
        }
        tmp = self.cfg.model_file + ".tmp"
        torch.save(payload, tmp)
        os.replace(tmp, self.cfg.model_file)

    def _epsilon(self):
        span = max(1, int(self.cfg.epsilon_decay_steps))
        t = min(self.steps, span)
        frac = t / span
        return self.cfg.epsilon_start + frac * (self.cfg.epsilon_end - self.cfg.epsilon_start)

    def _extract_state(self, metrics: dict):
        # Raw signals
        core_mbps = _safe_float(metrics.get("core_primary_mbps", 0.0))
        wifi_mbps = _safe_float(metrics.get("core_wifi_mbps", 0.0))
        congested_ports = _safe_int(metrics.get("congested_ports_count", 0))
        reroute_active = 1.0 if bool(metrics.get("reroute_active", False)) else 0.0
        throttle_active = 1.0 if bool(metrics.get("student_throttle_active", False)) else 0.0
        pkt_in = _safe_float(metrics.get("controller_packet_ins", 0.0))
        flow_mod = _safe_float(metrics.get("controller_flow_mods", 0.0))

        switch_util = metrics.get("switch_port_util_pct", {}) or {}
        util_vals = []
        for _, ports in switch_util.items():
            for _, v in (ports or {}).items():
                util_vals.append(_safe_float(v))
        avg_util = sum(util_vals) / len(util_vals) if util_vals else 0.0
        max_util = max(util_vals) if util_vals else 0.0

        # Queue-related signals from Wi-Fi host ports on s5.
        s5 = switch_util.get("5", {}) or switch_util.get(5, {}) or {}
        wifi_q_util = (
            _safe_float(s5.get("2", 0.0)) + _safe_float(s5.get("3", 0.0))
        ) / 2.0
        queue_pressure = min(100.0, wifi_q_util + (15.0 if throttle_active else 0.0))

        # Latency proxy (if explicit latency unavailable in metrics).
        latency_ms = _safe_float(metrics.get("latency_ms", 0.0))
        if latency_ms <= 0.0:
            latency_ms = 4.0 + 0.3 * max_util + 1.2 * congested_ports

        # Core link utilization proxy from configured 1 Gbps core link.
        core_util_pct = min(100.0, (core_mbps / 1000.0) * 100.0)

        # Trend signal.
        prev_core = 0.0
        if self.prev_metrics is not None:
            prev_core = _safe_float(self.prev_metrics.get("core_primary_mbps", 0.0))
        core_delta = core_mbps - prev_core

        # Normalized state vector.
        state = [
            min(1.5, core_mbps / 200.0),
            min(1.5, wifi_mbps / 200.0),
            core_util_pct / 100.0,
            avg_util / 100.0,
            max_util / 100.0,
            min(1.0, congested_ports / 8.0),
            reroute_active,
            throttle_active,
            min(1.5, pkt_in / 5000.0),
            min(1.5, flow_mod / 5000.0),
            min(1.5, latency_ms / 100.0),
            min(1.5, queue_pressure / 100.0),
        ]

        feature_view = {
            "core_mbps": round(core_mbps, 3),
            "wifi_mbps": round(wifi_mbps, 3),
            "core_util_pct": round(core_util_pct, 3),
            "avg_util_pct": round(avg_util, 3),
            "max_util_pct": round(max_util, 3),
            "congested_ports_count": congested_ports,
            "estimated_latency_ms": round(latency_ms, 3),
            "queue_pressure_pct": round(queue_pressure, 3),
            "core_delta_mbps": round(core_delta, 3),
            "reroute_active": bool(reroute_active),
            "student_throttle_active": bool(throttle_active),
        }
        return torch.tensor(state, dtype=torch.float32), feature_view

    def _reward(self, feature_view: dict):
        # Reward prioritizes low congestion/latency and stable throughput.
        max_util = _safe_float(feature_view.get("max_util_pct", 0.0))
        avg_util = _safe_float(feature_view.get("avg_util_pct", 0.0))
        latency = _safe_float(feature_view.get("estimated_latency_ms", 0.0))
        congested = _safe_float(feature_view.get("congested_ports_count", 0.0))
        queue_pressure = _safe_float(feature_view.get("queue_pressure_pct", 0.0))
        core_delta = _safe_float(feature_view.get("core_delta_mbps", 0.0))

        reward = 0.0
        reward -= 0.9 * (max_util / 100.0)
        reward -= 0.5 * (avg_util / 100.0)
        reward -= 0.45 * congested
        reward -= 0.25 * (latency / 100.0)
        reward -= 0.25 * (queue_pressure / 100.0)

        # Mild penalty for sudden traffic surge volatility.
        reward -= 0.05 * abs(core_delta / 50.0)

        # Bonus for healthy state.
        if max_util < 55.0 and congested == 0:
            reward += 0.4
        if latency < 20.0:
            reward += 0.2
        return float(reward)

    def _select_action(self, state_vec):
        eps = self._epsilon()
        if random.random() < eps:
            action = random.randrange(self.n_actions)
        else:
            with torch.no_grad():
                q = self.policy_net(state_vec.unsqueeze(0))[0]
            action = int(torch.argmax(q).item())
        with torch.no_grad():
            q_values = self.policy_net(state_vec.unsqueeze(0))[0].tolist()
        return action, eps, q_values

    def _maybe_train(self):
        if not self.cfg.train:
            return
        if len(self.replay) < max(self.cfg.batch_size, self.cfg.warmup_steps):
            return
        batch = self.replay.sample(self.cfg.batch_size)
        states = torch.stack([b[0] for b in batch])
        actions = torch.tensor([b[1] for b in batch], dtype=torch.long)
        rewards = torch.tensor([b[2] for b in batch], dtype=torch.float32)
        next_states = torch.stack([b[3] for b in batch])
        dones = torch.tensor([1.0 if b[4] else 0.0 for b in batch], dtype=torch.float32)

        q_pred = self.policy_net(states).gather(1, actions.unsqueeze(1)).squeeze(1)
        with torch.no_grad():
            q_next = self.target_net(next_states).max(1)[0]
            q_target = rewards + self.cfg.gamma * q_next * (1.0 - dones)

        loss = nn.MSELoss()(q_pred, q_target)
        self.optim.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.policy_net.parameters(), 1.0)
        self.optim.step()

        if self.steps % max(1, self.cfg.target_update_every) == 0:
            self.target_net.load_state_dict(self.policy_net.state_dict())

    def _clamp_thresholds(self, high: float, low: float):
        high = max(20.0, min(180.0, float(high)))
        low = max(8.0, min(high - 1.0, float(low)))
        if low >= high:
            low = max(8.0, high * 0.5)
        return round(high, 2), round(low, 2)

    def _build_action_payload(
        self,
        action_idx: int,
        metrics: dict,
        feature_view: dict,
        reward: float,
        epsilon: float,
        q_values: list[float],
    ):
        action_name = ACTION_NAMES[action_idx]
        cur_high = _safe_float(metrics.get("congest_high_mbps", 40.0))
        cur_low = _safe_float(metrics.get("congest_low_mbps", 20.0))
        port_high = _safe_float(metrics.get("port_congest_high_pct", 80.0))
        port_low = _safe_float(metrics.get("port_congest_low_pct", 65.0))

        routing_choice = "primary_path"
        force = False

        if action_name == "keep_primary":
            routing_choice = "primary_path"
            force = False
            cur_high += 2.0
        elif action_name == "prefer_backup_path":
            routing_choice = "backup_path"
            force = True
            cur_high -= 5.0
        elif action_name == "tighten_thresholds":
            routing_choice = "primary_path"
            force = bool(metrics.get("reroute_active", False))
            cur_high -= 6.0
            port_high -= 5.0
            port_low -= 5.0
        elif action_name == "relax_thresholds":
            routing_choice = "primary_path"
            force = False
            cur_high += 6.0
            port_high += 4.0
            port_low += 3.0
        elif action_name == "emergency_throttle":
            routing_choice = "backup_path"
            force = True
            cur_high -= 10.0
            port_high -= 10.0
            port_low -= 10.0

        high, low = self._clamp_thresholds(cur_high, cur_low if cur_low > 0 else cur_high * 0.5)
        port_high = round(max(40.0, min(95.0, port_high)), 2)
        port_low = round(max(30.0, min(port_high - 1.0, port_low)), 2)

        q_map = {
            ACTION_NAMES[i]: round(float(v), 5)
            for i, v in enumerate(q_values)
        }

        payload = {
            "ts": time.time(),
            "note": "dqn_adaptive_agent",
            "routing_choice": routing_choice,
            "force_reroute": bool(force),
            "congest_high_mbps": high,
            "congest_low_mbps": low,
            "port_congest_high_pct": port_high,
            "port_congest_low_pct": port_low,
            "dqn": {
                "state": feature_view,
                "action_index": int(action_idx),
                "action_name": action_name,
                "reward": round(float(reward), 6),
                "epsilon": round(float(epsilon), 6),
                "steps": int(self.steps),
            },
            # Controller reads this top-level q_values map for event logging.
            "q_values": q_map,
        }
        return payload

    def run(self):
        print("DQN agent started")
        print("  metrics:", self.cfg.metrics_file)
        print("  action :", self.cfg.action_file)
        print("  model  :", self.cfg.model_file)
        print("  interval:", self.cfg.interval)

        loops = 0
        while True:
            loops += 1
            metrics = read_json(self.cfg.metrics_file)
            if not metrics:
                time.sleep(self.cfg.interval)
                if self.cfg.max_steps > 0 and loops >= self.cfg.max_steps:
                    break
                continue

            state_vec, feature_view = self._extract_state(metrics)
            reward = self._reward(feature_view)
            action_idx, epsilon, q_values = self._select_action(state_vec)

            if self.prev_state is not None and self.prev_action is not None:
                self.replay.add(
                    self.prev_state,
                    self.prev_action,
                    reward,
                    state_vec,
                    False,
                )
                self._maybe_train()

            self.steps += 1
            payload = self._build_action_payload(
                action_idx=action_idx,
                metrics=metrics,
                feature_view=feature_view,
                reward=reward,
                epsilon=epsilon,
                q_values=q_values,
            )
            write_json_atomic(self.cfg.action_file, payload)

            self.prev_state = state_vec
            self.prev_action = action_idx
            self.prev_metrics = metrics

            if self.steps % 20 == 0:
                self._save_model()

            print(
                "step=%s action=%s reward=%.4f eps=%.3f core=%.2fMbps max_util=%.1f%% congested=%s"
                % (
                    self.steps,
                    ACTION_NAMES[action_idx],
                    reward,
                    epsilon,
                    feature_view["core_mbps"],
                    feature_view["max_util_pct"],
                    feature_view["congested_ports_count"],
                )
            )

            if self.cfg.max_steps > 0 and loops >= self.cfg.max_steps:
                break
            time.sleep(self.cfg.interval)

        # Save at shutdown too.
        self._save_model()
        print("DQN agent stopped.")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--metrics-file", default="/tmp/campus_metrics.json")
    p.add_argument("--action-file", default="/tmp/campus_ml_action.json")
    p.add_argument("--model-file", default="/tmp/campus_dqn_model.pt")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--gamma", type=float, default=0.95)
    p.add_argument("--lr", type=float, default=0.001)
    p.add_argument("--epsilon-start", type=float, default=0.30)
    p.add_argument("--epsilon-end", type=float, default=0.05)
    p.add_argument("--epsilon-decay-steps", type=int, default=1200)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--memory-size", type=int, default=4000)
    p.add_argument("--target-update-every", type=int, default=40)
    p.add_argument("--warmup-steps", type=int, default=48)
    p.add_argument(
        "--max-steps",
        type=int,
        default=0,
        help="0 means infinite loop; >0 runs fixed steps for testing.",
    )
    p.add_argument(
        "--no-train",
        action="store_true",
        help="Disable gradient updates (inference-only mode).",
    )
    return p.parse_args()


def main():
    args = parse_args()
    cfg = AgentConfig(
        metrics_file=args.metrics_file,
        action_file=args.action_file,
        model_file=args.model_file,
        interval=max(0.5, float(args.interval)),
        gamma=float(args.gamma),
        lr=float(args.lr),
        epsilon_start=float(args.epsilon_start),
        epsilon_end=float(args.epsilon_end),
        epsilon_decay_steps=max(10, int(args.epsilon_decay_steps)),
        batch_size=max(8, int(args.batch_size)),
        memory_size=max(128, int(args.memory_size)),
        target_update_every=max(1, int(args.target_update_every)),
        warmup_steps=max(8, int(args.warmup_steps)),
        max_steps=max(0, int(args.max_steps)),
        train=not bool(args.no_train),
    )
    agent = DQNRoutingAgent(cfg)
    agent.run()


if __name__ == "__main__":
    main()
