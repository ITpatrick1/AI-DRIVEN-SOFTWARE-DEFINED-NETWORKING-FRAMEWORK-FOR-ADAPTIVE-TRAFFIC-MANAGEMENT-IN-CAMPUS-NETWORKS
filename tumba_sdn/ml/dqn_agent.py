#!/usr/bin/env python3
"""
DQN Agent — Tumba College SDN
Deep Q-Network for adaptive traffic management.

State Vector (14 dimensions):
  - Per-zone link utilization (4)
  - Per-zone latency estimates (4)
  - Active academic flows, social flows
  - Exam flag, security flag
  - Core utilization, congestion count

Action Space: 16 discrete actions
Training: Experience replay, target network, epsilon-greedy
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


# ─── Action Space ───────────────────────────────────────────────────────────────
ACTION_NAMES = [
    "normal_mode",                    # 0
    "throttle_wifi_30pct",            # 1
    "throttle_wifi_70pct",            # 2
    "throttle_wifi_90pct",            # 3
    "boost_staff_lan",                # 4
    "boost_server_zone",              # 5
    "boost_lab_zone",                 # 6
    "exam_mode",                      # 7
    "peak_hour_mode",                 # 8
    "throttle_wifi_boost_staff",      # 9
    "throttle_wifi_boost_server",     # 10
    "throttle_social_boost_academic", # 11
    "emergency_staff_protection",     # 12
    "emergency_server_protection",    # 13
    "security_isolation_wifi",        # 14
    "load_balance_ds1_ds2",           # 15
]


def _sf(v, d=0.0):
    try: return float(v)
    except Exception: return float(d)


def read_json(path):
    if not os.path.exists(path): return None
    try:
        with open(path, 'r') as f: return json.load(f)
    except Exception: return None


def write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f: json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


class ReplayBuffer:
    def __init__(self, capacity):
        self.buf = deque(maxlen=max(128, capacity))

    def add(self, s, a, r, ns, done):
        self.buf.append((s, a, r, ns, done))

    def sample(self, n):
        return random.sample(self.buf, min(len(self.buf), n))

    def __len__(self):
        return len(self.buf)


class DQN(nn.Module):
    """Neural network: 256→128→64→16"""
    def __init__(self, in_dim, out_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, 256), nn.ReLU(),
            nn.Linear(256, 128), nn.ReLU(),
            nn.Linear(128, 64), nn.ReLU(),
            nn.Linear(64, out_dim),
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
    STATE_DIM = 14
    N_ACTIONS = len(ACTION_NAMES)

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.policy_net = DQN(self.STATE_DIM, self.N_ACTIONS)
        self.target_net = DQN(self.STATE_DIM, self.N_ACTIONS)
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()
        self.optimizer = optim.Adam(self.policy_net.parameters(), lr=cfg.lr)
        self.replay = ReplayBuffer(cfg.memory_size)
        self.steps = 0
        self.prev_state = None
        self.prev_action = None
        self.episode_rewards = []
        self.training_losses = []
        self._load_model()

    def _load_model(self):
        if not os.path.exists(self.cfg.model_file): return
        try:
            state = torch.load(self.cfg.model_file, map_location='cpu', weights_only=False)
            self.policy_net.load_state_dict(state['policy'])
            self.target_net.load_state_dict(state['target'])
            self.steps = state.get('steps', 0)
            print(f"Loaded model: {self.cfg.model_file} (steps={self.steps})")
        except Exception as e:
            print(f"Warning: failed to load model ({e}), starting fresh.")

    def _save_model(self):
        payload = {
            'policy': self.policy_net.state_dict(),
            'target': self.target_net.state_dict(),
            'steps': self.steps,
            'state_dim': self.STATE_DIM,
            'n_actions': self.N_ACTIONS,
            'actions': ACTION_NAMES,
            'saved_ts': time.time(),
        }
        tmp = self.cfg.model_file + '.tmp'
        torch.save(payload, tmp)
        os.replace(tmp, self.cfg.model_file)

    def _epsilon(self):
        span = max(1, self.cfg.epsilon_decay_steps)
        t = min(self.steps, span)
        return self.cfg.epsilon_start + (t / span) * (self.cfg.epsilon_end - self.cfg.epsilon_start)

    def _extract_state(self, metrics):
        """Extract 14-dim normalized state vector from controller metrics."""
        zone_metrics = metrics.get('zone_metrics', {})

        # Per-zone utilization (0-1)
        staff_util = _sf(zone_metrics.get('staff_lan', {}).get('max_utilization_pct', 0)) / 100
        server_util = _sf(zone_metrics.get('server_zone', {}).get('max_utilization_pct', 0)) / 100
        lab_util = _sf(zone_metrics.get('it_lab', {}).get('max_utilization_pct', 0)) / 100
        wifi_util = _sf(zone_metrics.get('student_wifi', {}).get('max_utilization_pct', 0)) / 100

        # Per-zone throughput as latency proxy (normalized)
        staff_tput = min(1, _sf(zone_metrics.get('staff_lan', {}).get('throughput_mbps', 0)) / 100)
        server_tput = min(1, _sf(zone_metrics.get('server_zone', {}).get('throughput_mbps', 0)) / 100)
        lab_tput = min(1, _sf(zone_metrics.get('it_lab', {}).get('throughput_mbps', 0)) / 100)
        wifi_tput = min(1, _sf(zone_metrics.get('student_wifi', {}).get('throughput_mbps', 0)) / 100)

        # Latency estimates (normalized, max 100ms)
        staff_lat = min(1, (4 + staff_util * 50) / 100)
        server_lat = min(1, (4 + server_util * 50) / 100)
        lab_lat = min(1, (5 + lab_util * 40) / 100)
        wifi_lat = min(1, (8 + wifi_util * 60) / 100)

        # Context flags
        exam_flag = _sf(metrics.get('timetable_exam_flag', 0))
        security_flag = 1.0 if metrics.get('ddos_active') or metrics.get('portscan_active') else 0.0
        congested = min(1, _sf(metrics.get('congested_ports_count', 0)) / 8)

        # Overall core utilization
        core_util = max(staff_util, server_util, lab_util, wifi_util)

        state = [
            staff_util, server_util, lab_util, wifi_util,  # 0-3
            staff_lat, server_lat, lab_lat, wifi_lat,      # 4-7
            staff_tput, server_tput,                       # 8-9 (academic/social proxy)
            exam_flag, security_flag,                      # 10-11
            core_util, congested,                          # 12-13
        ]

        features = {
            'staff_util': round(staff_util, 3), 'server_util': round(server_util, 3),
            'lab_util': round(lab_util, 3), 'wifi_util': round(wifi_util, 3),
            'staff_lat_est': round(staff_lat * 100, 1), 'server_lat_est': round(server_lat * 100, 1),
            'lab_lat_est': round(lab_lat * 100, 1), 'wifi_lat_est': round(wifi_lat * 100, 1),
            'exam_flag': exam_flag, 'security_flag': security_flag,
            'core_util': round(core_util, 3), 'congested_count': round(congested * 8),
        }
        return torch.tensor(state, dtype=torch.float32), features

    def _calculate_reward(self, features):
        """Reward function based on SLO compliance."""
        reward = 0.0
        staff_lat = features.get('staff_lat_est', 0)
        server_lat = features.get('server_lat_est', 0)
        staff_util = features.get('staff_util', 0)
        server_util = features.get('server_util', 0)
        lab_util = features.get('lab_util', 0)
        exam_flag = features.get('exam_flag', 0)
        security_flag = features.get('security_flag', 0)

        # Staff LAN SLO (weight 0.40)
        if staff_lat < 20 and staff_util < 0.8:
            reward += 40
        else:
            violation = max(0, (staff_lat - 20) / 20)
            reward -= 40 * min(1, violation)

        # Server Zone SLO (weight 0.30)
        if server_lat < 20 and server_util < 0.8:
            reward += 30
        else:
            violation = max(0, (server_lat - 20) / 20)
            reward -= 30 * min(1, violation)

        # Lab SLO during class (weight 0.15)
        if lab_util < 0.9:
            reward += 15
        else:
            reward -= 15

        # Student WiFi SLO (weight 0.10)
        if exam_flag > 0.5:
            wifi_lat = features.get('wifi_lat_est', 0)
            if wifi_lat < 20:
                reward += 10
            else:
                reward -= 10
        else:
            reward += 5  # Best effort

        # Security penalty (weight 0.05)
        if security_flag > 0.5:
            reward -= 20

        return reward

    def _select_action(self, state_vec):
        eps = self._epsilon()
        if random.random() < eps:
            action = random.randrange(self.N_ACTIONS)
        else:
            with torch.no_grad():
                q = self.policy_net(state_vec.unsqueeze(0))[0]
            action = int(torch.argmax(q).item())

        with torch.no_grad():
            q_values = self.policy_net(state_vec.unsqueeze(0))[0].tolist()
        return action, eps, q_values

    def _train_step(self):
        if not self.cfg.train:
            return
        if len(self.replay) < max(self.cfg.batch_size, self.cfg.warmup_steps):
            return

        batch = self.replay.sample(self.cfg.batch_size)
        states = torch.stack([b[0] for b in batch])
        actions = torch.tensor([b[1] for b in batch], dtype=torch.long)
        rewards = torch.tensor([b[2] for b in batch], dtype=torch.float32)
        next_states = torch.stack([b[3] for b in batch])
        dones = torch.tensor([1.0 if b[4] else 0.0 for b in batch])

        q_pred = self.policy_net(states).gather(1, actions.unsqueeze(1)).squeeze(1)
        with torch.no_grad():
            q_next = self.target_net(next_states).max(1)[0]
            q_target = rewards + self.cfg.gamma * q_next * (1 - dones)

        loss = nn.MSELoss()(q_pred, q_target)
        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.policy_net.parameters(), 1.0)
        self.optimizer.step()

        self.training_losses.append(loss.item())
        if len(self.training_losses) > 1000:
            self.training_losses = self.training_losses[-500:]

        if self.steps % self.cfg.target_update_every == 0:
            self.target_net.load_state_dict(self.policy_net.state_dict())

    def _build_action_payload(self, action_idx, features, reward, eps, q_values):
        action_name = ACTION_NAMES[action_idx]
        routing_choice = 'primary_path'
        force = False

        # Map action to routing decisions
        if action_name in ('throttle_wifi_90pct', 'peak_hour_mode',
                          'emergency_staff_protection', 'emergency_server_protection',
                          'security_isolation_wifi'):
            routing_choice = 'backup_path'
            force = True
        elif action_name == 'load_balance_ds1_ds2':
            routing_choice = 'load_balance'
            force = True

        return {
            'ts': time.time(),
            'note': 'dqn_adaptive_agent',
            'routing_choice': routing_choice,
            'force_reroute': force,
            'dqn': {
                'state': features,
                'action_index': action_idx,
                'action_name': action_name,
                'reward': round(reward, 4),
                'epsilon': round(eps, 4),
                'steps': self.steps,
                'training_losses': self.training_losses[-20:],
            },
            'q_values': {ACTION_NAMES[i]: round(v, 5) for i, v in enumerate(q_values)},
        }

    def run(self):
        print(f"DQN Agent started (mode={'train' if self.cfg.train else 'deploy'})")
        print(f"  metrics: {self.cfg.metrics_file}")
        print(f"  action:  {self.cfg.action_file}")
        print(f"  model:   {self.cfg.model_file}")

        loops = 0
        while True:
            loops += 1
            metrics = read_json(self.cfg.metrics_file)
            if not metrics:
                time.sleep(self.cfg.interval)
                if 0 < self.cfg.max_steps <= loops:
                    break
                continue

            state_vec, features = self._extract_state(metrics)
            reward = self._calculate_reward(features)
            action_idx, eps, q_values = self._select_action(state_vec)

            if self.prev_state is not None and self.prev_action is not None:
                self.replay.add(self.prev_state, self.prev_action, reward, state_vec, False)
                self._train_step()

            self.steps += 1
            self.episode_rewards.append(reward)

            payload = self._build_action_payload(action_idx, features, reward, eps, q_values)
            write_json(self.cfg.action_file, payload)

            self.prev_state = state_vec
            self.prev_action = action_idx

            if self.steps % 20 == 0:
                self._save_model()

            if self.steps % 5 == 0:
                avg_reward = sum(self.episode_rewards[-50:]) / max(1, len(self.episode_rewards[-50:]))
                print(f"step={self.steps} action={ACTION_NAMES[action_idx]} "
                      f"reward={reward:.2f} avg50={avg_reward:.2f} eps={eps:.3f}")

            if 0 < self.cfg.max_steps <= loops:
                break
            time.sleep(self.cfg.interval)

        self._save_model()
        print("DQN Agent stopped.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--metrics-file', default='/tmp/campus_metrics.json')
    p.add_argument('--action-file', default='/tmp/campus_ml_action.json')
    p.add_argument('--model-file', default='/tmp/campus_dqn_model.pt')
    p.add_argument('--interval', type=float, default=2.0)
    p.add_argument('--gamma', type=float, default=0.95)
    p.add_argument('--lr', type=float, default=0.001)
    p.add_argument('--epsilon-start', type=float, default=1.0)
    p.add_argument('--epsilon-end', type=float, default=0.01)
    p.add_argument('--epsilon-decay-steps', type=int, default=5000)
    p.add_argument('--batch-size', type=int, default=64)
    p.add_argument('--memory-size', type=int, default=50000)
    p.add_argument('--target-update-every', type=int, default=100)
    p.add_argument('--warmup-steps', type=int, default=64)
    p.add_argument('--max-steps', type=int, default=0)
    p.add_argument('--no-train', action='store_true')
    args = p.parse_args()

    cfg = AgentConfig(
        metrics_file=args.metrics_file,
        action_file=args.action_file,
        model_file=args.model_file,
        interval=max(0.5, args.interval),
        gamma=args.gamma,
        lr=args.lr,
        epsilon_start=args.epsilon_start,
        epsilon_end=args.epsilon_end,
        epsilon_decay_steps=max(10, args.epsilon_decay_steps),
        batch_size=max(8, args.batch_size),
        memory_size=max(128, args.memory_size),
        target_update_every=max(1, args.target_update_every),
        warmup_steps=max(8, args.warmup_steps),
        max_steps=max(0, args.max_steps),
        train=not args.no_train,
    )
    agent = DQNRoutingAgent(cfg)
    agent.run()


if __name__ == '__main__':
    main()
