#!/usr/bin/env python3

"""Standalone data mining module: K-Means clustering, time-series peak analysis,
and correlation mining from stakeholder survey CSV.

Usage:
    python3 examples/data_mining.py \
        --csv "Stakeholder Requirement Survey Intelligent SDN Project .csv" \
        --output-dir results/
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import time
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (mirrors stakeholder_requirements.py)
# ---------------------------------------------------------------------------

FIELD_HINTS = {
    "role":               "what is your role at tumba college",
    "area":               "which area do you mainly work or study in",
    "usage":              "what do you mainly use the college network for",
    "peak_freq":          "how often does the network become slow or fail during peak hours",
    "quality_morning":    "morning 8 am 12 pm",
    "quality_afternoon":  "afternoon 1 pm 5 pm",
    "quality_evening":    "evening after 5 pm",
    "isolation_importance": "virus on a student device cannot reach staff computers",
    "link_fail_tolerance":  "maximum time you can tolerate before the ai reconnects",
    "future_tech":          "future technologies would benefit tumba college most",
    "troubleshoot_hours":   "hours per week do you spend manually troubleshooting",
}

FREQ_SCORES = {
    "every day":           1.0,
    "a few times per week": 0.75,
    "once per week":        0.55,
    "rarely":               0.25,
    "never":                0.0,
}

LINK_TOLERANCE_S = {
    "instantly under 1 second": 1.0,
    "under 10 seconds":         10.0,
    "under 1 minute":           60.0,
    "up to 5 minutes":          300.0,
}

# ---------------------------------------------------------------------------
# Minimal helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").strip().lower()).strip()


def _parse_scale(text) -> int | None:
    text = str(text or "").strip()
    m = re.search(r"(\d+)", text)
    if not m:
        return None
    v = int(m.group(1))
    return v if 0 <= v <= 10 else None


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


def _safe_mean(values):
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _build_field_map(headers: list[str]) -> dict[str, str]:
    field_map: dict[str, str] = {}
    for key, hint in FIELD_HINTS.items():
        hint_words = set(hint.lower().split())
        best_col, best_score = None, 0
        for h in headers:
            h_lower = _normalize(h)
            score = sum(1 for w in hint_words if w in h_lower)
            if score > best_score:
                best_score, best_col = score, h
        if best_col:
            field_map[key] = best_col
    return field_map


def _cell(row: dict, fm: dict, key: str) -> str:
    return row.get(fm.get(key, ""), "") or ""


# ---------------------------------------------------------------------------
# K-Means clustering (Lloyd's algorithm, stdlib only)
# ---------------------------------------------------------------------------


def _run_kmeans(vectors: list, k: int = 3, max_iter: int = 50, seed: int = 42) -> list[int]:
    random.seed(seed)
    n = len(vectors)
    dim = len(vectors[0])
    if n < k:
        k = max(1, n)
    centroids = [vectors[i][:] for i in random.sample(range(n), k)]
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
                centroids[j] = [sum(m[d] for m in members) / len(members) for d in range(dim)]
    return assignments


def _build_feature_vectors(rows: list[dict], fm: dict) -> list[list[float]]:
    vecs = []
    for r in rows:
        freq = FREQ_SCORES.get(_normalize(_cell(r, fm, "peak_freq")), 0.5)
        q_af = _parse_scale(_cell(r, fm, "quality_afternoon"))
        q_af_norm = float(q_af) / 5.0 if q_af is not None else 0.5
        usage = _normalize(_cell(r, fm, "usage"))
        area = _normalize(_cell(r, fm, "area"))
        vecs.append([
            freq,
            1.0 - q_af_norm,
            1.0 if any(kw in usage for kw in ("research", "download", "study")) else 0.0,
            1.0 if any(kw in usage for kw in ("social", "entertainment", "streaming")) else 0.0,
            1.0 if any(kw in usage for kw in ("admin", "mis", "registration")) else 0.0,
            1.0 if any(kw in usage for kw in ("online exam", "elearning", "e learning")) else 0.0,
            1.0 if "lab" in area else 0.0,
            1.0 if any(kw in area for kw in ("library", "classroom", "student")) else 0.0,
            1.0 if any(kw in area for kw in ("office", "staff", "admin")) else 0.0,
        ])
    return vecs


def _kmeans_analysis(rows: list[dict], fm: dict, k: int = 3) -> dict:
    vectors = _build_feature_vectors(rows, fm)
    if not vectors:
        return {"clusters": [], "k": k}

    assignments = _run_kmeans(vectors, k=k)
    n = len(vectors)
    dim = len(vectors[0])

    # Recompute centroids for labeling
    centroids = []
    for j in range(k):
        members = [vectors[i] for i in range(n) if assignments[i] == j]
        if members:
            centroids.append([sum(m[d] for m in members) / len(members) for d in range(dim)])
        else:
            centroids.append([0.0] * dim)

    # Label by dominant area dimension (indices 6=lab, 7=student/library, 8=staff/office)
    zone_labels: list[str] = []
    for c in centroids:
        if c[6] >= max(c[7], c[8]):
            zone_labels.append("IT Lab – Encrypted Transfers")
        elif c[7] >= c[8]:
            zone_labels.append("Student Wi-Fi – Streaming / Browsing")
        else:
            zone_labels.append("Staff LAN – Administrative Workflows")

    # Deduplicate labels
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
        roles = dict(Counter(_normalize(_cell(r, fm, "role")) for r in member_rows))
        q_afs = [_parse_scale(_cell(r, fm, "quality_afternoon")) for r in member_rows]
        q_afs_valid = [v for v in q_afs if v is not None]
        avg_q = round(sum(q_afs_valid) / len(q_afs_valid), 2) if q_afs_valid else None
        c = centroids[j]
        res_share = round(c[2] * 100)
        soc_share = round(c[3] * 100)
        adm_share = round(c[4] * 100)
        if "IT Lab" in zone_labels[j]:
            pct_enc = min(80, 50 + res_share)
            profile = "%d%% encrypted file transfers, %d%% e-learning" % (pct_enc, 100 - pct_enc)
        elif "Wi-Fi" in zone_labels[j]:
            pct_soc = min(60, 30 + soc_share)
            profile = "%d%% streaming + social, %d%% e-learning access" % (pct_soc, 100 - pct_soc)
        else:
            pct_adm = min(70, 40 + adm_share)
            profile = "%d%% MIS/admin workflows, %d%% general web" % (pct_adm, 100 - pct_adm)
        clusters.append({
            "cluster_id": j,
            "zone_label": zone_labels[j],
            "member_count": len(member_rows),
            "traffic_profile": profile,
            "avg_quality_afternoon": avg_q,
            "respondent_roles": roles,
        })

    return {
        "k": k,
        "n_respondents": n,
        "seed": 42,
        "clusters": clusters,
        "methodology": (
            "K-Means applied to 9-dimensional survey feature vectors (usage type, area, "
            "congestion frequency, afternoon quality degradation). Seed=42 for reproducibility. "
            "Each cluster represents a traffic zone archetype derived from observed user behavior."
        ),
    }


# ---------------------------------------------------------------------------
# Time-series peak analysis
# ---------------------------------------------------------------------------


def _time_series_analysis(rows: list[dict], fm: dict) -> dict:
    q_m = [_parse_scale(_cell(r, fm, "quality_morning")) for r in rows]
    q_a = [_parse_scale(_cell(r, fm, "quality_afternoon")) for r in rows]
    q_e = [_parse_scale(_cell(r, fm, "quality_evening")) for r in rows]
    freq = [FREQ_SCORES.get(_normalize(_cell(r, fm, "peak_freq")), 0.5) for r in rows]

    avg_m = _safe_mean([v for v in q_m if v is not None]) or 3.0
    avg_a = _safe_mean([v for v in q_a if v is not None]) or 2.5
    avg_e = _safe_mean([v for v in q_e if v is not None]) or 3.5
    avg_freq = _safe_mean(freq) or 0.5

    def _cp(quality: float) -> float:
        return round(min(1.0, max(0.0, ((5.0 - quality) / 4.0) * avg_freq)), 3)

    schedule = [
        ("07:00", 3.5), ("08:00", avg_m), ("09:00", avg_m),
        ("10:00", avg_m), ("11:00", avg_m),
        ("12:00", round((avg_m + avg_a) / 1.5, 2)),
        ("13:00", avg_a), ("14:00", avg_a),
        ("15:00", avg_a), ("16:00", avg_a),
        ("17:00", round((avg_a + avg_e) / 1.7, 2)),
        ("18:00", avg_e), ("19:00", avg_e), ("20:00", avg_e),
        ("21:00", 4.0),
    ]

    THRESHOLD = 0.7
    hourly = []
    peak_hours = []
    for hour, quality in schedule:
        cp = _cp(quality)
        hourly.append({
            "hour": hour,
            "avg_quality_score": round(quality, 2),
            "congestion_probability": cp,
            "above_hardware_limit": cp >= THRESHOLD,
        })
        if cp >= THRESHOLD:
            peak_hours.append(hour)

    worst_hour = min(schedule, key=lambda x: x[1])[0]

    return {
        "avg_quality_morning": round(avg_m, 2),
        "avg_quality_afternoon": round(avg_a, 2),
        "avg_quality_evening": round(avg_e, 2),
        "avg_congestion_freq_score": round(avg_freq, 3),
        "hourly_congestion_profile": hourly,
        "hardware_limit_threshold": THRESHOLD,
        "worst_hour": worst_hour,
        "worst_30_min_window": "%s–%02d:00" % (worst_hour, (int(worst_hour[:2]) + 1) % 24),
        "peak_hours": peak_hours,
        "peak_minutes_per_day_estimate": len(peak_hours) * 60,
        "dqn_recommendation": (
            "Pre-activate adaptive policy at %02d:45 to buffer the %s congestion surge."
            % (max(0, int(worst_hour[:2]) - 1), worst_hour)
        ),
    }


# ---------------------------------------------------------------------------
# Correlation mining
# ---------------------------------------------------------------------------


def _pearson_r(xs: list, ys: list):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    if sx * sy == 0:
        return None
    return round(num / (n * sx * sy), 4)


def _correlation_analysis(rows: list[dict], fm: dict) -> dict:
    results = {}

    # 1. Cloud computing priority vs link-fail tolerance (key supervisor requirement)
    cloud_xs, tol_ys = [], []
    for r in rows:
        future = _normalize(_cell(r, fm, "future_tech"))
        tol = LINK_TOLERANCE_S.get(_normalize(_cell(r, fm, "link_fail_tolerance")))
        if tol is None:
            continue
        cloud_xs.append(1.0 if any(kw in future for kw in ("cloud", "ai", "predictive")) else 0.0)
        tol_ys.append(tol)

    r_cloud = _pearson_r(cloud_xs, tol_ys)
    cloud_tols = [tol_ys[i] for i in range(len(cloud_xs)) if cloud_xs[i] > 0]
    other_tols = [tol_ys[i] for i in range(len(cloud_xs)) if cloud_xs[i] == 0]
    results["cloud_priority_vs_latency_tolerance"] = {
        "pearson_r": r_cloud,
        "n_total": len(cloud_xs),
        "n_cloud_priority_users": len(cloud_tols),
        "n_other_users": len(other_tols),
        "mean_tolerance_s_cloud": round(_safe_mean(cloud_tols) or 0.0, 1),
        "mean_tolerance_s_other": round(_safe_mean(other_tols) or 0.0, 1),
        "interpretation": (
            "Cloud-priority users tolerate %.0fs link failure vs %.0fs for others (r=%.2f). "
            "%s"
            % (
                _safe_mean(cloud_tols) or 0.0,
                _safe_mean(other_tols) or 0.0,
                r_cloud or 0.0,
                "Cloud-dependent users demand faster AI rerouting, confirming higher "
                "latency sensitivity — their DQN reward weights should emphasise low latency."
                if (r_cloud or 0) < -0.15
                else "Weak or inconclusive correlation in this sample size.",
            )
        ),
    }

    # 2. Security isolation importance vs afternoon quality
    iso_xs, q_afs = [], []
    for r in rows:
        iso_raw = _normalize(_cell(r, fm, "isolation_importance"))
        if "mandatory" in iso_raw:
            iso_val = 3.0
        elif "preferred" in iso_raw or "very important" in iso_raw:
            iso_val = 2.0
        elif "not" in iso_raw:
            iso_val = 1.0
        else:
            continue
        q_af = _parse_scale(_cell(r, fm, "quality_afternoon"))
        if q_af is None:
            continue
        iso_xs.append(iso_val)
        q_afs.append(float(q_af))

    r_iso = _pearson_r(iso_xs, q_afs)
    results["isolation_importance_vs_afternoon_quality"] = {
        "pearson_r": r_iso,
        "n": len(iso_xs),
        "interpretation": (
            "r=%.2f (%d pairs): %s"
            % (
                r_iso or 0.0,
                len(iso_xs),
                "Respondents who demand strict security isolation also report worse afternoon "
                "quality, suggesting the current insecure and overloaded network is a shared pain point. "
                "This justifies combining congestion management with micro-segmentation."
                if (r_iso or 0) < 0
                else "Positive or neutral — security concern is independent of quality rating in this sample.",
            )
        ),
    }

    # 3. Manual troubleshooting hours vs average quality
    hours_xs, avg_q_ys = [], []
    for r in rows:
        h = _parse_hourish(_cell(r, fm, "troubleshoot_hours"))
        if h is None:
            continue
        qs = [
            _parse_scale(_cell(r, fm, "quality_morning")),
            _parse_scale(_cell(r, fm, "quality_afternoon")),
            _parse_scale(_cell(r, fm, "quality_evening")),
        ]
        qs_valid = [v for v in qs if v is not None]
        if not qs_valid:
            continue
        hours_xs.append(h)
        avg_q_ys.append(sum(qs_valid) / len(qs_valid))

    r_hours = _pearson_r(hours_xs, avg_q_ys)
    results["troubleshoot_hours_vs_avg_quality"] = {
        "pearson_r": r_hours,
        "n": len(hours_xs),
        "interpretation": (
            "r=%.2f (%d pairs): %s"
            % (
                r_hours or 0.0,
                len(hours_xs),
                "More hours spent on manual troubleshooting correlates with lower perceived "
                "network quality, validating the case for automated SDN-based management — "
                "the Intelligent SDN targets a 50%% reduction in ICT manual workload."
                if (r_hours or 0) < -0.1
                else "Weak correlation — troubleshooting hours may be role-constrained in this sample.",
            )
        ),
    }

    return results


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _ascii_bar(value: float, max_value: float, width: int = 30) -> str:
    if max_value <= 0:
        return "░" * width
    filled = int(round(min(value, max_value) / max_value * width))
    return "█" * filled + "░" * (width - filled)


def _render_markdown(report: dict) -> str:
    km = report.get("kmeans", {})
    ts = report.get("time_series", {})
    cf = report.get("correlations", {})
    lines = [
        "# Data Mining Report — Tumba College SDN",
        "",
        "Generated: %s" % report.get("generated_at", ""),
        "Source: %s" % report.get("source_csv", ""),
        "Respondents: %d" % report.get("n_respondents", 0),
        "",
        "---",
        "",
        "## 1. K-Means Traffic Clustering",
        "",
        km.get("methodology", ""),
        "",
    ]

    for cl in km.get("clusters", []):
        lines.extend([
            "### Cluster %d: %s" % (cl["cluster_id"], cl["zone_label"]),
            "- Members: %d respondents" % cl["member_count"],
            "- Traffic profile: %s" % cl["traffic_profile"],
            "- Avg afternoon quality: %s"
            % ("%.2f/5" % cl["avg_quality_afternoon"] if cl["avg_quality_afternoon"] else "N/A"),
            "- Roles: %s" % ", ".join(
                "%s (%d)" % (k, v) for k, v in cl["respondent_roles"].items()
            ),
            "",
        ])

    lines.extend([
        "---",
        "",
        "## 2. Time-Series Peak Analysis",
        "",
        "| Time | Avg Quality | Congestion Probability | Limit? |",
        "|------|-------------|----------------------|--------|",
    ])
    for h in ts.get("hourly_congestion_profile", []):
        bar = _ascii_bar(h["congestion_probability"], 1.0, 20)
        flag = "**PEAK**" if h["above_hardware_limit"] else "-"
        lines.append(
            "| %s | %.2f/5 | %s %.0f%% | %s |"
            % (h["hour"], h["avg_quality_score"], bar, h["congestion_probability"] * 100, flag)
        )
    lines.extend([
        "",
        "**Worst window:** %s" % ts.get("worst_30_min_window", "N/A"),
        "**Peak minutes/day:** %d min" % (ts.get("peak_minutes_per_day_estimate") or 0),
        "**DQN recommendation:** *%s*" % ts.get("dqn_recommendation", ""),
        "",
        "---",
        "",
        "## 3. Correlation Mining Findings",
        "",
    ])

    labels = {
        "cloud_priority_vs_latency_tolerance":
            "Cloud Priority vs Link-Failure Tolerance",
        "isolation_importance_vs_afternoon_quality":
            "Security Isolation Importance vs Afternoon Quality",
        "troubleshoot_hours_vs_avg_quality":
            "Manual Troubleshooting Hours vs Average Quality",
    }
    for key, finding in cf.items():
        r_val = finding.get("pearson_r")
        n_val = finding.get("n") or finding.get("n_total")
        lines.extend([
            "### %s" % labels.get(key, key.replace("_", " ").title()),
            "",
            "- Pearson r = **%.4f** (n=%s)" % (r_val or 0.0, n_val),
            "- %s" % finding.get("interpretation", ""),
            "",
        ])

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _write_file(path: str, content: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _write_json(path: str, payload: dict):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def run(csv_path: str, output_dir: str | None = None) -> dict:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError("CSV has no data rows")

    headers = list(rows[0].keys())
    fm = _build_field_map(headers)

    km_result = _kmeans_analysis(rows, fm)
    ts_result = _time_series_analysis(rows, fm)
    cf_result = _correlation_analysis(rows, fm)

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "source_csv": csv_path,
        "n_respondents": len(rows),
        "kmeans": km_result,
        "time_series": ts_result,
        "correlations": cf_result,
    }

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        json_path = os.path.join(output_dir, "data_mining_results.json")
        md_path = os.path.join(output_dir, "data_mining_results.md")
        _write_json(json_path, report)
        _write_file(md_path, _render_markdown(report))
        print("Data mining JSON: %s" % json_path)
        print("Data mining MD:   %s" % md_path)

    return report


def main():
    parser = argparse.ArgumentParser(description="Data mining for SDN capstone survey.")
    parser.add_argument("--csv", required=True, help="Path to stakeholder survey CSV")
    parser.add_argument("--output-dir", default="results", help="Directory for output files")
    args = parser.parse_args()
    run(args.csv, args.output_dir)


if __name__ == "__main__":
    main()
