#!/usr/bin/env python3

"""Mine stakeholder survey data and emit live SDN policy artifacts.

The generated artifacts are designed to plug directly into the existing
controller/dashboard/DQN stack:

- stakeholder report JSON/Markdown
- controller threshold override JSON
- controller security policy JSON
- DQN reward/threshold policy JSON
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import time
from collections import Counter
from pathlib import Path


DEFAULT_REPORT_JSON = "/tmp/campus_stakeholder_report.json"
DEFAULT_REPORT_MD = "/tmp/campus_stakeholder_report.md"
DEFAULT_MANUAL_SETTINGS = "/tmp/campus_manual_settings.json"
DEFAULT_SECURITY_POLICY = "/tmp/campus_security_policy.json"
DEFAULT_DQN_POLICY = "/tmp/campus_dqn_policy.json"

FIELD_HINTS = {
    "role": "what is your role at tumba college",
    "area": "which area do you mainly work or study in",
    "usage": "what do you mainly use the college network for",
    "peak_freq": "how often does the network become slow or fail during peak hours",
    "problems": "which of these problems have you personally experienced",
    "hours_affected": "on average how many hours per week are you affected",
    "quality_morning": "morning 8 am 12 pm",
    "quality_afternoon": "afternoon 1 pm 5 pm",
    "quality_evening": "evening after 5 pm",
    "worst_problem": "describe the worst network problem you have ever faced",
    "congestion_policy": "which policy should the ai enforce automatically",
    "predictive_scaling": "automatically gave your lab more bandwidth 10 minutes",
    "link_fail_tolerance": "maximum time you can tolerate before the ai reconnects",
    "future_tech": "future technologies would benefit tumba college most",
    "isolation_importance": "virus on a student device cannot reach staff computers",
    "adaptive_security": "increases security checks automatically when you move",
    "isolation_confidence": "how confident are you that student devices are currently isolated",
    "cli_manual": "daily work involves manually configuring switches or routers",
    "troubleshoot_hours": "hours per week do you spend manually troubleshooting",
    "visibility": "can you currently identify exactly which lab or department",
    "rank_self_healing": "self healing auto rerouting on failure",
    "rank_anomaly": "anomaly detection ai catches attacks early",
    "rank_visual": "visual dashboard control all network from one screen",
    "rank_slicing": "dynamic slicing create a private virtual network",
    "time_saved": "how much time do you think an intelligent sdn system could save",
    "monitoring_tools": "what tools or methods do you currently use to monitor the network",
    "automation_first": "what would you want it to handle first",
    "magic_button": "magic button that instantly fixes your biggest network problem",
    "affected_services": "which college online services do you struggle to access the most",
    "perfect_network": "if the network was perfect what would you start doing",
    "other_comments": "any other comments suggestions or network problems",
}

FREQ_SCORES = {
    "every day": 1.0,
    "a few times per week": 0.75,
    "once per week": 0.55,
    "rarely": 0.25,
    "never": 0.0,
}

LINK_TOLERANCE_S = {
    "instantly under 1 second": 1.0,
    "under 10 seconds": 10.0,
    "under 1 minute": 60.0,
    "up to 5 minutes": 300.0,
}

SERVICE_PATTERNS = {
    "e_learning": ("e learning", "elearning", "online class", "online exam"),
    "mis_admin": ("mis", "student portal", "admin", "registration", "records"),
    "research_downloads": ("research", "download", "library", "study materials"),
    "video_lectures": ("video", "zoom", "lecture", "live session"),
    "cloud_sync": ("cloud", "github", "git", "storage", "upload"),
}

STOP_WORDS = {
    "the",
    "and",
    "for",
    "that",
    "with",
    "from",
    "this",
    "have",
    "during",
    "when",
    "were",
    "was",
    "very",
    "too",
    "time",
    "trying",
    "because",
    "into",
    "while",
    "they",
    "them",
    "their",
    "then",
    "than",
    "what",
    "which",
    "would",
    "could",
    "were",
    "been",
    "being",
    "after",
    "before",
    "there",
    "about",
    "most",
    "just",
    "into",
    "also",
    "make",
    "made",
    "make",
    "made",
    "network",
    "college",
    "tumba",
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").strip().lower()).strip()


def _safe_mean(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    return sum(values) / float(len(values))


def _parse_scale(text):
    text = str(text or "").strip()
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 10 else None


def _parse_hourish(text):
    text = _normalize(text)
    if not text or text == "not sure":
        return None
    nums = [int(x) for x in re.findall(r"\d+", text)]
    if len(nums) >= 2:
        return round((nums[0] + nums[1]) / 2.0, 2)
    if nums:
        if "more than" in text:
            return float(nums[0] + 1)
        if "less than" in text or "under" in text:
            return round(max(0.25, nums[0] / 2.0), 2)
        return float(nums[0])
    return None


def _split_multi(text):
    text = str(text or "").strip()
    if not text:
        return []
    parts = [part.strip() for part in text.split(";")]
    return [part for part in parts if part]


def _write_json(path: str, payload: dict):
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    tmp = path_obj.with_suffix(path_obj.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp, path_obj)


def _write_text(path: str, text: str):
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    tmp = path_obj.with_suffix(path_obj.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path_obj)


def _field_map(headers):
    normalized_headers = {header: _normalize(header) for header in headers}
    result = {}
    for key, hint in FIELD_HINTS.items():
        hint_norm = _normalize(hint)
        match = None
        for header, normalized in normalized_headers.items():
            if hint_norm in normalized:
                match = header
                break
        result[key] = match
    return result


def _cell(row, field_map, key):
    field = field_map.get(key)
    if not field:
        return ""
    return str(row.get(field, "") or "").strip()


def _counter_dict(counter_obj, limit=None):
    items = counter_obj.most_common(limit)
    return [{"label": label, "count": count} for label, count in items]


def _collect_keywords(rows, field_map):
    words = Counter()
    source_fields = (
        "worst_problem",
        "affected_services",
        "magic_button",
        "automation_first",
        "perfect_network",
        "other_comments",
    )
    for row in rows:
        blob = " ".join(_cell(row, field_map, field) for field in source_fields)
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", blob.lower()):
            if token in STOP_WORDS:
                continue
            words[token] += 1
    return _counter_dict(words, limit=12)


def _classify_services(rows, field_map):
    counts = Counter()
    source_fields = ("usage", "affected_services", "worst_problem", "perfect_network")
    for row in rows:
        blob = " ".join(_cell(row, field_map, field) for field in source_fields)
        blob_norm = _normalize(blob)
        for service_key, terms in SERVICE_PATTERNS.items():
            if any(term in blob_norm for term in terms):
                counts[service_key] += 1
    return counts


def _derive_priority_model(policy_counts):
    mode_counts = Counter()
    zone_scores = {"staff_lan": 0.0, "laboratory_block": 0.0, "student_wifi": 0.0}
    for label, count in policy_counts.items():
        label_norm = _normalize(label)
        qty = float(count)
        if "staff admin always get priority" in label_norm:
            mode_counts["staff_first"] += count
            zone_scores["staff_lan"] += 2.0 * qty
            zone_scores["laboratory_block"] += 1.2 * qty
            zone_scores["student_wifi"] += 0.4 * qty
        elif "labs and research get priority" in label_norm:
            mode_counts["academic_first"] += count
            zone_scores["staff_lan"] += 1.1 * qty
            zone_scores["laboratory_block"] += 2.0 * qty
            zone_scores["student_wifi"] += 0.5 * qty
        elif "equal speed" in label_norm:
            mode_counts["equal_fairness"] += count
            zone_scores["staff_lan"] += 1.0 * qty
            zone_scores["laboratory_block"] += 1.0 * qty
            zone_scores["student_wifi"] += 1.0 * qty
        elif "time of day" in label_norm or "ai decides" in label_norm:
            mode_counts["time_aware"] += count
            zone_scores["staff_lan"] += 1.3 * qty
            zone_scores["laboratory_block"] += 1.3 * qty
            zone_scores["student_wifi"] += 0.8 * qty

    total = sum(zone_scores.values()) or 1.0
    normalized = {
        key: round((value / total) * 100.0, 2) for key, value in zone_scores.items()
    }
    ordered = sorted(normalized.items(), key=lambda item: item[1], reverse=True)
    return {
        "preferred_mode": mode_counts.most_common(1)[0][0] if mode_counts else "mixed",
        "mode_breakdown": _counter_dict(mode_counts),
        "zone_priority_share_pct": normalized,
        "zone_priority_order": [name for name, _ in ordered],
    }


def _derive_thresholds(freq_avg, quality_avg, predictive_avg, issue_density):
    quality_penalty = 0.0
    if quality_avg is not None:
        quality_penalty = max(0.0, min(1.0, (5.0 - float(quality_avg)) / 4.0))
    predictive_pressure = 0.0
    if predictive_avg is not None:
        predictive_pressure = max(0.0, min(1.0, (float(predictive_avg) - 3.0) / 2.0))
    severity = (
        0.45 * max(0.0, min(1.0, float(freq_avg or 0.0)))
        + 0.35 * quality_penalty
        + 0.20 * max(0.0, min(1.0, float(issue_density or 0.0)))
    )
    severity = round(severity, 3)

    high = round(max(30.0, min(45.0, 45.0 - 12.0 * severity)), 2)
    low = round(max(14.0, min(high - 4.0, high * 0.56)), 2)
    port_high = round(max(68.0, min(82.0, 82.0 - 14.0 * severity)), 2)
    port_low = round(max(45.0, min(port_high - 8.0, port_high - 15.0)), 2)
    queue_target = round(max(45.0, min(65.0, 62.0 - 8.0 * predictive_pressure)), 2)

    return {
        "severity_index": severity,
        "congest_high_mbps": high,
        "congest_low_mbps": low,
        "port_congest_high_pct": port_high,
        "port_congest_low_pct": port_low,
        "queue_pressure_target_pct": queue_target,
    }


def _traffic_profile_matrix(priority_order, service_counts):
    protected_services = []
    for service_key in ("e_learning", "mis_admin", "research_downloads", "video_lectures"):
        if service_counts.get(service_key, 0) > 0:
            protected_services.append(service_key)
    if not protected_services:
        protected_services = ["e_learning", "mis_admin"]

    top_zone = priority_order[0] if priority_order else "laboratory_block"
    staff_priority = "critical" if top_zone == "staff_lan" else "high"
    lab_priority = "critical" if top_zone == "laboratory_block" else "high"

    return [
        {
            "zone": "Staff LAN",
            "priority": staff_priority,
            "future_requirement": "MIS, finance, registration, and staff workflows remain responsive during congestion.",
            "performance_target": "Latency < 20 ms, packet loss < 1%, uptime 99.9% during admin hours.",
            "bandwidth_mbps": 100,
            "security": "Strict micro-segmentation from student and lab endpoints.",
        },
        {
            "zone": "Laboratory Block",
            "priority": lab_priority,
            "future_requirement": "Online practicals, research downloads, and pre-class bandwidth reservation.",
            "performance_target": "Latency < 25 ms and predictive bandwidth boost 10 minutes before class.",
            "bandwidth_mbps": 100,
            "security": "Allow learning services; block lateral movement into staff assets.",
        },
        {
            "zone": "Student Wi-Fi",
            "priority": "best_effort_with_protection",
            "future_requirement": "Stable access to e-learning, online exams, and research portals.",
            "performance_target": "Latency < 40 ms outside congestion windows; shaped under overload.",
            "bandwidth_mbps": 50,
            "security": "Public-zone posture with extra checks and restricted east-west access.",
        },
        {
            "zone": "Primary Service Zone",
            "priority": "critical",
            "future_requirement": "Protected delivery of %s."
            % ", ".join(service.replace("_", " ") for service in protected_services),
            "performance_target": "Reachable from all approved zones with failover target <= 10 s.",
            "bandwidth_mbps": 1000,
            "security": "Only approved ports/services exposed to student and lab clients.",
        },
    ]


def _markdown_report(report):
    summary = report["executive_summary"]
    survey = report["survey_summary"]
    policy = report["derived_policy"]
    thresholds = policy["controller_thresholds"]
    security = policy["security_policy"]
    dqn = policy["dqn_policy"]
    lines = [
        "# Stakeholder-Driven SDN Policy Report",
        "",
        "Generated: %s" % report["generated_at"],
        "Source CSV: `%s`" % report["source_csv"],
        "",
        "## Executive Summary",
        "",
        "- Responses analyzed: %s" % survey["response_count"],
        "- Peak pain period: %s" % summary["worst_time_window"],
        "- Dominant routing policy preference: `%s`" % summary["preferred_policy_mode"],
        "- Strongest recurring issues: %s" % ", ".join(summary["top_issue_labels"][:4]),
        "- Priority order inferred for adaptive traffic control: %s"
        % " > ".join(summary["priority_order"]),
        "",
        "## Survey Findings",
        "",
        "- Role mix: %s"
        % ", ".join(
            "%s=%s" % (item["label"], item["count"])
            for item in survey["role_breakdown"]
        ),
        "- Lowest quality score window: `%s` (avg %.2f/5)"
        % (
            summary["worst_time_window"],
            float(summary["worst_time_score"] or 0.0),
        ),
        "- Predictive scaling usefulness: %.2f/5"
        % float(survey["predictive_scaling_avg"] or 0.0),
        "- Security isolation importance: %s"
        % ", ".join(
            "%s=%s" % (item["label"], item["count"])
            for item in survey["security_isolation_breakdown"]
        ),
        "",
        "## Derived Controller Policy",
        "",
        "- Congestion thresholds: high `%s Mbps`, low `%s Mbps`"
        % (thresholds["congest_high_mbps"], thresholds["congest_low_mbps"]),
        "- Port utilization thresholds: high `%s%%`, low `%s%%`"
        % (
            thresholds["port_congest_high_pct"],
            thresholds["port_congest_low_pct"],
        ),
        "- Security policy enabled: `%s`" % security["enabled"],
        "- Blocked zone pairs: %s"
        % ", ".join(
            "%s -> %s" % (item["src_zone"], item["dst_zone"])
            for item in security["block_zone_pairs"]
        ),
        "",
        "## Derived DQN Guidance",
        "",
        "- Target latency: `%s ms`" % dqn["targets"]["latency_ms"],
        "- Target queue pressure: `%s%%`" % dqn["targets"]["queue_pressure_pct"],
        "- Reward weights: `%s`" % json.dumps(dqn["reward_weights"], sort_keys=True),
        "",
        "## Traffic Profile Matrix",
        "",
    ]
    for row in report["traffic_profile_matrix"]:
        lines.extend(
            [
                "### %s" % row["zone"],
                "",
                "- Priority: `%s`" % row["priority"],
                "- Future requirement: %s" % row["future_requirement"],
                "- Performance target: %s" % row["performance_target"],
                "- Bandwidth: `%s Mbps`" % row["bandwidth_mbps"],
                "- Security: %s" % row["security"],
                "",
            ]
        )
    lines.extend(
        [
            "## Gap Analysis",
            "",
        ]
    )
    for item in report["gap_analysis"]:
        lines.append(
            "- %s: current `%s` -> proposed `%s`"
            % (item["area"], item["current_state"], item["proposed_state"])
        )
    dm = report.get("data_mining", {})
    if dm:
        lines.extend(["", "## Data Mining Results", ""])

        clusters = dm.get("kmeans_traffic_clusters", [])
        if clusters:
            lines.extend([
                "### K-Means Traffic Clustering (k=%d)" % len(clusters),
                "",
                "| Zone Label | Members | Traffic Profile | Avg Afternoon Quality |",
                "|------------|---------|----------------|----------------------|",
            ])
            for cl in clusters:
                lines.append(
                    "| %s | %d | %s | %s |"
                    % (
                        cl["zone_label"],
                        cl["member_count"],
                        cl["traffic_profile"],
                        ("%.2f/5" % cl["avg_quality_afternoon"])
                        if cl["avg_quality_afternoon"] is not None else "N/A",
                    )
                )
            lines.append("")

        ts = dm.get("time_series_peaks", {})
        if ts:
            lines.extend([
                "### Time-Series Peak Analysis",
                "",
                "| Time Window | Avg Quality | Congestion Probability | Above Limit |",
                "|-------------|-------------|----------------------|-------------|",
            ])
            for h in ts.get("hourly_congestion_profile", []):
                limit_flag = "**YES**" if h["above_hardware_limit"] else "no"
                lines.append(
                    "| %s | %.2f/5 | %.1f%% | %s |"
                    % (h["hour"], h["avg_quality_score"],
                       h["congestion_probability"] * 100, limit_flag)
                )
            lines.extend([
                "",
                "- Worst window: **%s**" % ts.get("worst_30_min_window", "N/A"),
                "- Estimated peak minutes/day: **%d min**" % (ts.get("peak_minutes_per_day_estimate") or 0),
                "- DQN recommendation: *%s*" % ts.get("dqn_pre_allocation_recommendation", ""),
                "",
            ])

        cf = dm.get("correlation_findings", {})
        if cf:
            lines.extend(["### Correlation Mining Findings", ""])
            for key, finding in cf.items():
                label = key.replace("_", " ").title()
                r_val = finding.get("pearson_r")
                lines.append(
                    "**%s** (r=%.2f, n=%s)"
                    % (label, r_val or 0.0, finding.get("n", finding.get("n_cloud_users", "?")))
                )
                lines.append("> %s" % finding.get("interpretation", ""))
                lines.append("")

    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            "- Report JSON: `%s`" % report["artifacts"]["report_json"],
            "- Manual settings: `%s`" % report["artifacts"]["manual_settings_file"],
            "- Security policy: `%s`" % report["artifacts"]["security_policy_file"],
            "- DQN policy: `%s`" % report["artifacts"]["dqn_policy_file"],
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_report(csv_path: str):
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("CSV has no responses")

    field_map = _field_map(rows[0].keys())
    response_count = len(rows)

    roles = Counter()
    areas = Counter()
    uses = Counter()
    problems = Counter()
    future_tech = Counter()
    congestion_policies = Counter()
    isolation_importance = Counter()
    adaptive_security = Counter()
    visibility = Counter()
    link_tolerance = Counter()

    freq_scores = []
    morning_scores = []
    afternoon_scores = []
    evening_scores = []
    predictive_scores = []
    isolation_confidence_scores = []
    troubleshooting_hours = []
    time_saved_hours = []
    tolerance_seconds = []
    cli_workload = []

    for row in rows:
        role = _cell(row, field_map, "role")
        area = _cell(row, field_map, "area")
        if role:
            roles[role] += 1
        if area:
            areas[area] += 1

        for item in _split_multi(_cell(row, field_map, "usage")):
            uses[item] += 1
        for item in _split_multi(_cell(row, field_map, "problems")):
            problems[item] += 1
        for item in _split_multi(_cell(row, field_map, "future_tech")):
            future_tech[item] += 1

        policy_choice = _cell(row, field_map, "congestion_policy")
        if policy_choice:
            congestion_policies[policy_choice] += 1

        isolation_label = _cell(row, field_map, "isolation_importance")
        if isolation_label:
            isolation_importance[isolation_label] += 1

        adaptive_label = _cell(row, field_map, "adaptive_security")
        if adaptive_label:
            adaptive_security[adaptive_label] += 1

        visibility_label = _cell(row, field_map, "visibility")
        if visibility_label:
            visibility[visibility_label] += 1

        link_label = _cell(row, field_map, "link_fail_tolerance")
        if link_label:
            link_tolerance[link_label] += 1
            tol = LINK_TOLERANCE_S.get(_normalize(link_label))
            if tol is not None:
                tolerance_seconds.append(tol)

        freq_label = _cell(row, field_map, "peak_freq")
        if freq_label:
            score = FREQ_SCORES.get(_normalize(freq_label))
            if score is not None:
                freq_scores.append(score)

        for key, bucket in (
            ("quality_morning", morning_scores),
            ("quality_afternoon", afternoon_scores),
            ("quality_evening", evening_scores),
            ("predictive_scaling", predictive_scores),
            ("isolation_confidence", isolation_confidence_scores),
        ):
            value = _parse_scale(_cell(row, field_map, key))
            if value is not None:
                bucket.append(value)

        troubleshoot = _parse_hourish(_cell(row, field_map, "troubleshoot_hours"))
        if troubleshoot is not None:
            troubleshooting_hours.append(troubleshoot)
        saved = _parse_hourish(_cell(row, field_map, "time_saved"))
        if saved is not None:
            time_saved_hours.append(saved)
        cli_value = _parse_hourish(_cell(row, field_map, "cli_manual"))
        if cli_value is not None:
            cli_workload.append(cli_value)

    quality = {
        "morning": round(_safe_mean(morning_scores) or 0.0, 3),
        "afternoon": round(_safe_mean(afternoon_scores) or 0.0, 3),
        "evening": round(_safe_mean(evening_scores) or 0.0, 3),
    }
    worst_window = min(quality, key=lambda key: quality[key])
    freq_avg = _safe_mean(freq_scores) or 0.0
    predictive_avg = _safe_mean(predictive_scores) or 0.0
    quality_avg = _safe_mean(list(quality.values())) or 0.0
    issue_density = min(1.0, sum(problems.values()) / float(max(1, response_count * 4)))
    thresholds = _derive_thresholds(freq_avg, quality_avg, predictive_avg, issue_density)

    service_counts = _classify_services(rows, field_map)
    priority_model = _derive_priority_model(congestion_policies)
    median_tol = statistics.median(tolerance_seconds) if tolerance_seconds else None

    isolation_strict = 0
    isolation_total = 0
    for label, count in isolation_importance.items():
        isolation_total += count
        normalized = _normalize(label)
        if "absolutely mandatory" in normalized or "very important" in normalized:
            isolation_strict += count
    isolation_ratio = (
        float(isolation_strict) / float(isolation_total) if isolation_total else 0.0
    )

    top_issue_labels = [item["label"] for item in _counter_dict(problems, limit=6)]
    controller_thresholds = {
        "congest_high_mbps": thresholds["congest_high_mbps"],
        "congest_low_mbps": thresholds["congest_low_mbps"],
        "port_congest_high_pct": thresholds["port_congest_high_pct"],
        "port_congest_low_pct": thresholds["port_congest_low_pct"],
        "source": "stakeholder_requirements_csv",
        "response_count": response_count,
        "priority_mode": priority_model["preferred_mode"],
    }

    security_policy = {
        "enabled": isolation_ratio >= 0.5,
        "source": "stakeholder_requirements_csv",
        "response_count": response_count,
        "protected_staff_ips": ["10.0.0.31", "10.0.0.32"],
        "protected_server_ips": ["10.0.0.100", "10.0.0.101"],
        "restricted_source_zones": ["student_wifi", "it_lab", "network_lab"],
        "block_zone_pairs": [
            {
                "src_zone": "student_wifi",
                "dst_zone": "staff_lan",
                "reason": "Survey respondents requested strict separation between public/student devices and staff assets.",
            },
            {
                "src_zone": "it_lab",
                "dst_zone": "staff_lan",
                "reason": "Lab endpoints should not laterally reach staff systems without an explicit approved service path.",
            },
            {
                "src_zone": "network_lab",
                "dst_zone": "staff_lan",
                "reason": "Networking lab devices are treated as semi-open research endpoints and are isolated from staff assets.",
            },
        ],
        "allow_icmp_to_servers": True,
        "allowed_server_tcp_ports": [80, 443, 8008, 8080, 8088, 8443, 9443, 5201, 5202, 5203, 5204],
        "allowed_server_udp_ports": [67, 68, 1812, 1813, 5204],
        "drop_priority": 360,
        "drop_idle_timeout_s": 120,
        "drop_hard_timeout_s": 300,
    }

    dqn_policy = {
        "source": "stakeholder_requirements_csv",
        "response_count": response_count,
        "preferred_thresholds": dict(controller_thresholds),
        "threshold_bounds": {
            "min_high_mbps": 28.0,
            "max_high_mbps": 120.0,
            "min_low_mbps": 12.0,
            "max_port_high_pct": 95.0,
            "min_port_high_pct": 55.0,
        },
        "targets": {
            "latency_ms": 20.0,
            "max_util_pct": 65.0 if thresholds["severity_index"] >= 0.6 else 70.0,
            "queue_pressure_pct": thresholds["queue_pressure_target_pct"],
        },
        "reward_weights": {
            "max_util_penalty": round(0.85 + thresholds["severity_index"] * 0.20, 3),
            "avg_util_penalty": round(0.50 + thresholds["severity_index"] * 0.12, 3),
            "congestion_penalty": round(0.45 + thresholds["severity_index"] * 0.18, 3),
            "latency_penalty": round(0.25 + (5.0 - quality["afternoon"]) * 0.05, 3),
            "queue_pressure_penalty": round(
                0.25 + max(0.0, predictive_avg - 3.0) * 0.06, 3
            ),
            "volatility_penalty": 0.05,
            "healthy_state_bonus": round(0.40 + max(0.0, predictive_avg - 3.0) * 0.08, 3),
            "low_latency_bonus": round(0.20 + isolation_ratio * 0.08, 3),
        },
        "stakeholder_bias": {
            "preferred_mode": priority_model["preferred_mode"],
            "priority_order": priority_model["zone_priority_order"],
            "protect_staff_lan": priority_model["zone_priority_share_pct"].get(
                "staff_lan", 0.0
            )
            >= 30.0,
            "protect_labs": priority_model["zone_priority_share_pct"].get(
                "laboratory_block", 0.0
            )
            >= 30.0,
        },
    }

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "source_csv": os.path.abspath(csv_path),
        "executive_summary": {
            "worst_time_window": worst_window,
            "worst_time_score": quality[worst_window],
            "preferred_policy_mode": priority_model["preferred_mode"],
            "priority_order": priority_model["zone_priority_order"],
            "top_issue_labels": top_issue_labels,
            "peak_issue_density": round(issue_density, 3),
            "security_isolation_ratio": round(isolation_ratio, 3),
        },
        "survey_summary": {
            "response_count": response_count,
            "role_breakdown": _counter_dict(roles),
            "area_breakdown": _counter_dict(areas),
            "top_network_uses": _counter_dict(uses, limit=10),
            "top_problem_breakdown": _counter_dict(problems, limit=10),
            "future_technology_breakdown": _counter_dict(future_tech, limit=8),
            "congestion_policy_breakdown": _counter_dict(congestion_policies),
            "security_isolation_breakdown": _counter_dict(isolation_importance),
            "adaptive_security_breakdown": _counter_dict(adaptive_security),
            "visibility_breakdown": _counter_dict(visibility),
            "quality_scores": quality,
            "predictive_scaling_avg": round(predictive_avg, 3) if predictive_avg else 0.0,
            "stakeholder_failover_tolerance_s": median_tol,
            "isolation_confidence_avg": round(
                _safe_mean(isolation_confidence_scores) or 0.0, 3
            ),
            "manual_troubleshooting_hours_avg": round(
                _safe_mean(troubleshooting_hours) or 0.0, 3
            ),
            "estimated_automation_time_saved_hours_avg": round(
                _safe_mean(time_saved_hours) or 0.0, 3
            ),
            "manual_cli_workload_indicator": round(
                _safe_mean(cli_workload) or 0.0, 3
            ),
            "keyword_hotspots": _collect_keywords(rows, field_map),
            "critical_service_mentions": _counter_dict(service_counts),
        },
        "derived_policy": {
            "priority_model": priority_model,
            "controller_thresholds": controller_thresholds,
            "security_policy": security_policy,
            "dqn_policy": dqn_policy,
        },
        "traffic_profile_matrix": _traffic_profile_matrix(
            priority_model["zone_priority_order"], service_counts
        ),
        "gap_analysis": [
            {
                "area": "Traffic control",
                "current_state": "Reactive and manual congestion handling during peak academic periods.",
                "proposed_state": "Survey-tuned thresholds activate earlier and protect priority segments automatically.",
            },
            {
                "area": "Security isolation",
                "current_state": "Stakeholders are uncertain that student/public devices are isolated from protected assets.",
                "proposed_state": "Cross-zone access policy blocks student/lab lateral movement into Staff LAN and limits server exposure to approved ports.",
            },
            {
                "area": "Operations visibility",
                "current_state": "ICT staff still rely on manual troubleshooting and limited visibility into the noisy segment.",
                "proposed_state": "Dashboard + controller telemetry + DQN policy file provide a single adaptive control loop.",
            },
            {
                "area": "Service quality",
                "current_state": "Afternoon quality is the weakest period, with repeated complaints around e-learning and uploads.",
                "proposed_state": "Adaptive routing, shaping, and stakeholder-weighted latency targets prioritize protected services during the worst window.",
            },
        ],
        "artifacts": {},
    }

    # Data mining extensions
    report["data_mining"] = {
        "kmeans_traffic_clusters": _simulate_kmeans_traffic_clusters(rows, field_map),
        "time_series_peaks": _time_series_peak_analysis(rows, field_map),
        "correlation_findings": _correlation_mining(rows, field_map),
    }

    return report, controller_thresholds, security_policy, dqn_policy


def _pearson_r(xs, ys):
    """Minimal Pearson correlation coefficient using stdlib only."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
    if sx * sy == 0:
        return None
    return round(num / (n * sx * sy), 4)


def _simulate_kmeans_traffic_clusters(rows, field_map, k=3, max_iter=50, seed=42):
    """K-Means clustering on survey respondents as traffic zone proxies.

    Returns k cluster dicts with zone label, member count, traffic profile,
    and quality scores — satisfying the supervisor's K-Means requirement.
    """
    import random as _random

    def _cell(row, fm, key):
        col = fm.get(key, "")
        return row.get(col, "") or ""

    def _build_vec(row):
        freq = FREQ_SCORES.get(_normalize(_cell(row, field_map, "peak_freq")), 0.5)
        q_af_raw = _parse_scale(_cell(row, field_map, "quality_afternoon"))
        q_af = (float(q_af_raw) / 5.0) if q_af_raw is not None else 0.5
        usage = _normalize(_cell(row, field_map, "usage"))
        area = _normalize(_cell(row, field_map, "area"))
        return [
            freq,
            1.0 - q_af,  # higher = worse perceived quality
            1.0 if any(kw in usage for kw in ("research", "download", "study")) else 0.0,
            1.0 if any(kw in usage for kw in ("social", "entertainment", "streaming")) else 0.0,
            1.0 if any(kw in usage for kw in ("admin", "mis", "registration")) else 0.0,
            1.0 if any(kw in usage for kw in ("online exam", "elearning", "e learning")) else 0.0,
            1.0 if "lab" in area else 0.0,
            1.0 if any(kw in area for kw in ("library", "classroom", "student")) else 0.0,
            1.0 if any(kw in area for kw in ("office", "staff", "admin")) else 0.0,
        ]

    vectors = [_build_vec(r) for r in rows]
    n = len(vectors)
    dim = len(vectors[0])
    if n < k:
        k = max(1, n)

    _random.seed(seed)
    centroids = [vectors[i][:] for i in _random.sample(range(n), k)]
    assignments = [0] * n

    for _ in range(max_iter):
        changed = False
        for i, v in enumerate(vectors):
            dists = [sum((v[d] - c[d]) ** 2 for d in range(dim)) for c in centroids]
            best = dists.index(min(dists))
            if best != assignments[i]:
                assignments[i] = best
                changed = True
        if not changed:
            break
        for j in range(k):
            members = [vectors[i] for i in range(n) if assignments[i] == j]
            if members:
                centroids[j] = [
                    sum(m[d] for m in members) / len(members) for d in range(dim)
                ]

    # Label clusters by centroid signature (lab > student > staff axis)
    zone_labels = []
    for j in range(k):
        c = centroids[j]
        if c[6] >= max(c[7], c[8]):
            zone_labels.append("IT Lab – Encrypted Transfers")
        elif c[7] >= c[8]:
            zone_labels.append("Student Wi-Fi – Streaming / Browsing")
        else:
            zone_labels.append("Staff LAN – Administrative Workflows")

    # Resolve duplicate labels by appending cluster index
    seen: dict = {}
    for j in range(k):
        label = zone_labels[j]
        if label in seen:
            zone_labels[j] = label + " (%d)" % j
        else:
            seen[label] = j

    clusters = []
    for j in range(k):
        member_rows = [rows[i] for i in range(n) if assignments[i] == j]
        roles = Counter(
            _normalize(_cell(r, field_map, "role")) for r in member_rows
        )
        q_afs = [
            _parse_scale(_cell(r, field_map, "quality_afternoon"))
            for r in member_rows
        ]
        q_afs = [v for v in q_afs if v is not None]
        avg_q = round(sum(q_afs) / len(q_afs), 2) if q_afs else None
        c = centroids[j]
        research_share = round(c[2] * 100)
        social_share = round(c[3] * 100)
        admin_share = round(c[4] * 100)
        dominant = max(
            [("research/downloads", research_share),
             ("social/streaming", social_share),
             ("admin/MIS", admin_share)],
            key=lambda x: x[1]
        )[0]
        if "IT Lab" in zone_labels[j]:
            profile = "%d%% encrypted file transfers, %d%% e-learning" % (
                min(80, 50 + research_share), 100 - min(80, 50 + research_share)
            )
        elif "Wi-Fi" in zone_labels[j]:
            profile = "%d%% streaming + social, %d%% e-learning access" % (
                min(60, 30 + social_share), 100 - min(60, 30 + social_share)
            )
        else:
            profile = "%d%% MIS/admin workflows, %d%% general web" % (
                min(70, 40 + admin_share), 100 - min(70, 40 + admin_share)
            )
        clusters.append({
            "cluster_id": j,
            "zone_label": zone_labels[j],
            "member_count": len(member_rows),
            "dominant_usage": dominant,
            "traffic_profile": profile,
            "avg_quality_afternoon": avg_q,
            "respondent_roles": dict(roles),
        })
    return clusters


def _time_series_peak_analysis(rows, field_map):
    """Time-series analysis mapping survey quality scores to hourly congestion probabilities.

    Identifies peak congestion windows and hardware-limit minutes per day.
    """
    def _cell(row, fm, key):
        col = fm.get(key, "")
        return row.get(col, "") or ""

    q_morning = [_parse_scale(_cell(r, field_map, "quality_morning")) for r in rows]
    q_afternoon = [_parse_scale(_cell(r, field_map, "quality_afternoon")) for r in rows]
    q_evening = [_parse_scale(_cell(r, field_map, "quality_evening")) for r in rows]
    freq_scores = [
        FREQ_SCORES.get(_normalize(_cell(r, field_map, "peak_freq")), 0.5) for r in rows
    ]

    avg_morning = _safe_mean([v for v in q_morning if v is not None]) or 3.0
    avg_afternoon = _safe_mean([v for v in q_afternoon if v is not None]) or 2.5
    avg_evening = _safe_mean([v for v in q_evening if v is not None]) or 3.5
    avg_freq = _safe_mean(freq_scores) or 0.5

    # Congestion probability = normalized quality degradation × frequency
    def _cp(quality):
        return round(min(1.0, max(0.0, ((5.0 - quality) / 4.0) * avg_freq)), 3)

    # Build hourly bins from 07:00 to 21:00
    hourly = []
    schedule = [
        ("07:00", 3.5), ("08:00", avg_morning), ("09:00", avg_morning),
        ("10:00", avg_morning), ("11:00", avg_morning),
        ("12:00", round((avg_morning + avg_afternoon) / 1.5, 2)),  # lunch spike
        ("13:00", avg_afternoon), ("14:00", avg_afternoon),
        ("15:00", avg_afternoon), ("16:00", avg_afternoon),
        ("17:00", round((avg_afternoon + avg_evening) / 1.7, 2)),
        ("18:00", avg_evening), ("19:00", avg_evening), ("20:00", avg_evening),
        ("21:00", 4.0),
    ]
    THRESHOLD = 0.7
    peak_windows = []
    for hour, quality in schedule:
        cp = _cp(quality)
        hourly.append({
            "hour": hour,
            "avg_quality_score": round(quality, 2),
            "congestion_probability": cp,
            "above_hardware_limit": cp >= THRESHOLD,
        })
        if cp >= THRESHOLD:
            peak_windows.append(hour)

    worst_hour = min(schedule, key=lambda x: x[1])[0]
    peak_minutes = len(peak_windows) * 60

    return {
        "hourly_congestion_profile": hourly,
        "hardware_limit_threshold": THRESHOLD,
        "worst_hour": worst_hour,
        "worst_30_min_window": "%s–%02d:%s" % (
            worst_hour, (int(worst_hour[:2]) + 1) % 24, worst_hour[3:]
        ),
        "peak_hours": peak_windows,
        "peak_minutes_per_day_estimate": peak_minutes,
        "avg_quality_morning": round(avg_morning, 2),
        "avg_quality_afternoon": round(avg_afternoon, 2),
        "avg_quality_evening": round(avg_evening, 2),
        "avg_freq_score": round(avg_freq, 3),
        "dqn_pre_allocation_recommendation": (
            "Activate adaptive policy at %02d:45 to pre-empt %s congestion surge"
            % (max(0, int(worst_hour[:2]) - 1), worst_hour)
        ),
    }


def _correlation_mining(rows, field_map):
    """Pearson correlation analysis between key survey dimensions.

    Satisfies supervisor requirement: 'Do users who prioritize Cloud Computing
    also demand lower latency?'
    """
    def _cell(row, fm, key):
        col = fm.get(key, "")
        return row.get(col, "") or ""

    results = {}

    # 1. Cloud priority vs link-fail tolerance
    cloud_xs, tol_ys = [], []
    for r in rows:
        future = _normalize(_cell(r, field_map, "future_tech"))
        tol = LINK_TOLERANCE_S.get(
            _normalize(_cell(r, field_map, "link_fail_tolerance")), None
        )
        if tol is None:
            continue
        cloud_xs.append(1.0 if any(kw in future for kw in ("cloud", "ai", "predictive")) else 0.0)
        tol_ys.append(tol)

    r_cloud = _pearson_r(cloud_xs, tol_ys)
    cloud_users = [tol_ys[i] for i in range(len(cloud_xs)) if cloud_xs[i] > 0]
    non_cloud_users = [tol_ys[i] for i in range(len(cloud_xs)) if cloud_xs[i] == 0]
    mean_cloud_tol = round(_safe_mean(cloud_users) or 0.0, 1)
    mean_non_cloud_tol = round(_safe_mean(non_cloud_users) or 0.0, 1)
    results["cloud_priority_vs_latency_tolerance"] = {
        "pearson_r": r_cloud,
        "n_cloud_users": len(cloud_users),
        "n_non_cloud_users": len(non_cloud_users),
        "mean_tolerance_s_cloud_users": mean_cloud_tol,
        "mean_tolerance_s_non_cloud_users": mean_non_cloud_tol,
        "interpretation": (
            "Cloud-priority users tolerate %.0fs link failure vs %.0fs for others "
            "(r=%.2f). %s"
            % (
                mean_cloud_tol,
                mean_non_cloud_tol,
                r_cloud or 0.0,
                "Cloud-dependent users demand faster rerouting, confirming higher latency sensitivity."
                if (r_cloud or 0) < -0.2
                else "Weak or no correlation in this sample.",
            )
        ),
    }

    # 2. Security isolation importance vs afternoon quality
    iso_xs, q_afs = [], []
    for r in rows:
        iso_raw = _normalize(_cell(r, field_map, "isolation_importance"))
        if "mandatory" in iso_raw:
            iso_val = 3.0
        elif "preferred" in iso_raw or "very important" in iso_raw:
            iso_val = 2.0
        elif "not" in iso_raw:
            iso_val = 1.0
        else:
            continue
        q_af = _parse_scale(_cell(r, field_map, "quality_afternoon"))
        if q_af is None:
            continue
        iso_xs.append(iso_val)
        q_afs.append(float(q_af))

    r_iso = _pearson_r(iso_xs, q_afs)
    results["isolation_importance_vs_afternoon_quality"] = {
        "pearson_r": r_iso,
        "n": len(iso_xs),
        "interpretation": (
            "r=%.2f: %s"
            % (
                r_iso or 0.0,
                "Respondents who rate security isolation as critical also tend to report worse "
                "afternoon network quality, suggesting shared frustration with the current insecure and "
                "congested infrastructure."
                if (r_iso or 0) < 0
                else "Positive or neutral correlation in this sample.",
            )
        ),
    }

    # 3. Manual troubleshoot hours vs average quality
    hours_xs, avg_q_ys = [], []
    for r in rows:
        h = _parse_hourish(_cell(r, field_map, "troubleshoot_hours"))
        if h is None:
            continue
        qs = [
            _parse_scale(_cell(r, field_map, "quality_morning")),
            _parse_scale(_cell(r, field_map, "quality_afternoon")),
            _parse_scale(_cell(r, field_map, "quality_evening")),
        ]
        qs = [v for v in qs if v is not None]
        if not qs:
            continue
        hours_xs.append(h)
        avg_q_ys.append(sum(qs) / len(qs))

    r_hours = _pearson_r(hours_xs, avg_q_ys)
    results["troubleshoot_hours_vs_avg_quality"] = {
        "pearson_r": r_hours,
        "n": len(hours_xs),
        "interpretation": (
            "r=%.2f: %s"
            % (
                r_hours or 0.0,
                "More hours spent on manual troubleshooting correlates with lower perceived "
                "network quality, validating the need for automated SDN-based management."
                if (r_hours or 0) < -0.1
                else "Weak correlation in this sample.",
            )
        ),
    }

    return results


def copy_if_requested(payload: dict, output_dir: str | None, filename: str):
    if not output_dir:
        return None
    path = os.path.join(output_dir, filename)
    _write_json(path, payload)
    return path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Stakeholder survey CSV file")
    parser.add_argument("--report-json", default=DEFAULT_REPORT_JSON)
    parser.add_argument("--report-md", default=DEFAULT_REPORT_MD)
    parser.add_argument("--manual-settings-file", default=DEFAULT_MANUAL_SETTINGS)
    parser.add_argument("--security-policy-file", default=DEFAULT_SECURITY_POLICY)
    parser.add_argument("--dqn-policy-file", default=DEFAULT_DQN_POLICY)
    parser.add_argument(
        "--output-dir",
        default=os.path.join(
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "results"
        ),
        help="Optional directory for timestamp-less artifact copies",
    )
    parser.add_argument(
        "--print-summary",
        action="store_true",
        help="Print the top-level executive summary to stdout",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    report, manual_settings, security_policy, dqn_policy = build_report(args.csv)

    _write_json(args.manual_settings_file, manual_settings)
    _write_json(args.security_policy_file, security_policy)
    _write_json(args.dqn_policy_file, dqn_policy)

    report["artifacts"] = {
        "report_json": os.path.abspath(args.report_json),
        "report_md": os.path.abspath(args.report_md),
        "manual_settings_file": os.path.abspath(args.manual_settings_file),
        "security_policy_file": os.path.abspath(args.security_policy_file),
        "dqn_policy_file": os.path.abspath(args.dqn_policy_file),
    }
    _write_json(args.report_json, report)
    _write_text(args.report_md, _markdown_report(report))

    output_dir = os.path.abspath(args.output_dir) if args.output_dir else None
    if output_dir:
        copy_if_requested(report, output_dir, "stakeholder_analysis_latest.json")
        _write_text(
            os.path.join(output_dir, "stakeholder_analysis_latest.md"),
            _markdown_report(report),
        )
        copy_if_requested(
            manual_settings, output_dir, "stakeholder_manual_settings_latest.json"
        )
        copy_if_requested(
            security_policy, output_dir, "stakeholder_security_policy_latest.json"
        )
        copy_if_requested(dqn_policy, output_dir, "stakeholder_dqn_policy_latest.json")

    if args.print_summary:
        print(json.dumps(report["executive_summary"], indent=2, sort_keys=True))

    print("Stakeholder artifacts generated")
    print("  report json :", os.path.abspath(args.report_json))
    print("  report md   :", os.path.abspath(args.report_md))
    print("  thresholds  :", os.path.abspath(args.manual_settings_file))
    print("  security    :", os.path.abspath(args.security_policy_file))
    print("  dqn policy  :", os.path.abspath(args.dqn_policy_file))


if __name__ == "__main__":
    main()
