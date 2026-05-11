# Stakeholder-Driven SDN Policy Report

Generated: 2026-05-11 15:42:55 CAT
Source CSV: `/home/patrick/Desktop/campus-sdn/Stakeholder Requirement Survey Intelligent SDN Project .csv`

## Executive Summary

- Responses analyzed: 14
- Peak pain period: afternoon
- Dominant routing policy preference: `academic_first`
- Strongest recurring issues: Very slow internet during class time, Files take too long to upload or download, Network disconnects without warning, Cannot connect to the network
- Priority order inferred for adaptive traffic control: laboratory_block > staff_lan > student_wifi

## Survey Findings

- Role mix: Student=10, ICT / Technical Staff=2, Academic Staff (Lecturer / Instructor)=1, Administrative Staff=1
- Lowest quality score window: `afternoon` (avg 2.50/5)
- Predictive scaling usefulness: 3.33/5
- Security isolation importance: Absolutely mandatory=7, Very important=4, Not a priority=1

## Derived Controller Policy

- Congestion thresholds: high `36.16 Mbps`, low `20.25 Mbps`
- Port utilization thresholds: high `71.68%`, low `56.68%`
- Security policy enabled: `True`
- Blocked zone pairs: student_wifi -> staff_lan, it_lab -> staff_lan, network_lab -> staff_lan

## Derived DQN Guidance

- Target latency: `20.0 ms`
- Target queue pressure: `60.67%`
- Reward weights: `{"avg_util_penalty": 0.588, "congestion_penalty": 0.583, "healthy_state_bonus": 0.427, "latency_penalty": 0.375, "low_latency_bonus": 0.273, "max_util_penalty": 0.997, "queue_pressure_penalty": 0.27, "volatility_penalty": 0.05}`

## Traffic Profile Matrix

### Staff LAN

- Priority: `high`
- Future requirement: MIS, finance, registration, and staff workflows remain responsive during congestion.
- Performance target: Latency < 20 ms, packet loss < 1%, uptime 99.9% during admin hours.
- Bandwidth: `100 Mbps`
- Security: Strict micro-segmentation from student and lab endpoints.

### Laboratory Block

- Priority: `critical`
- Future requirement: Online practicals, research downloads, and pre-class bandwidth reservation.
- Performance target: Latency < 25 ms and predictive bandwidth boost 10 minutes before class.
- Bandwidth: `100 Mbps`
- Security: Allow learning services; block lateral movement into staff assets.

### Student Wi-Fi

- Priority: `best_effort_with_protection`
- Future requirement: Stable access to e-learning, online exams, and research portals.
- Performance target: Latency < 40 ms outside congestion windows; shaped under overload.
- Bandwidth: `50 Mbps`
- Security: Public-zone posture with extra checks and restricted east-west access.

### Primary Service Zone

- Priority: `critical`
- Future requirement: Protected delivery of e learning, mis admin, research downloads, video lectures.
- Performance target: Reachable from all approved zones with failover target <= 10 s.
- Bandwidth: `1000 Mbps`
- Security: Only approved ports/services exposed to student and lab clients.

## Gap Analysis

- Traffic control: current `Reactive and manual congestion handling during peak academic periods.` -> proposed `Survey-tuned thresholds activate earlier and protect priority segments automatically.`
- Security isolation: current `Stakeholders are uncertain that student/public devices are isolated from protected assets.` -> proposed `Cross-zone access policy blocks student/lab lateral movement into Staff LAN and limits server exposure to approved ports.`
- Operations visibility: current `ICT staff still rely on manual troubleshooting and limited visibility into the noisy segment.` -> proposed `Dashboard + controller telemetry + DQN policy file provide a single adaptive control loop.`
- Service quality: current `Afternoon quality is the weakest period, with repeated complaints around e-learning and uploads.` -> proposed `Adaptive routing, shaping, and stakeholder-weighted latency targets prioritize protected services during the worst window.`

## Data Mining Results

### K-Means Traffic Clustering (k=3)

| Zone Label | Members | Traffic Profile | Avg Afternoon Quality |
|------------|---------|----------------|----------------------|
| Staff LAN – Administrative Workflows | 6 | 70% MIS/admin workflows, 30% general web | 1.80/5 |
| Student Wi-Fi – Streaming / Browsing | 2 | 60% streaming + social, 40% e-learning access | 3.50/5 |
| IT Lab – Encrypted Transfers | 6 | 80% encrypted file transfers, 20% e-learning | 2.80/5 |

### Time-Series Peak Analysis

| Time Window | Avg Quality | Congestion Probability | Above Limit |
|-------------|-------------|----------------------|-------------|
| 07:00 | 3.50/5 | 29.5% | no |
| 08:00 | 2.92/5 | 40.9% | no |
| 09:00 | 2.92/5 | 40.9% | no |
| 10:00 | 2.92/5 | 40.9% | no |
| 11:00 | 2.92/5 | 40.9% | no |
| 12:00 | 3.61/5 | 27.3% | no |
| 13:00 | 2.50/5 | 49.1% | no |
| 14:00 | 2.50/5 | 49.1% | no |
| 15:00 | 2.50/5 | 49.1% | no |
| 16:00 | 2.50/5 | 49.1% | no |
| 17:00 | 3.33/5 | 32.8% | no |
| 18:00 | 3.17/5 | 36.0% | no |
| 19:00 | 3.17/5 | 36.0% | no |
| 20:00 | 3.17/5 | 36.0% | no |
| 21:00 | 4.00/5 | 19.6% | no |

- Worst window: **13:00–14:00**
- Estimated peak minutes/day: **0 min**
- DQN recommendation: *Activate adaptive policy at 12:45 to pre-empt 13:00 congestion surge*

### Correlation Mining Findings

**Cloud Priority Vs Latency Tolerance** (r=0.55, n=6)
> Cloud-priority users tolerate 100s link failure vs 15s for others (r=0.55). Weak or no correlation in this sample.

**Isolation Importance Vs Afternoon Quality** (r=0.60, n=12)
> r=0.60: Positive or neutral correlation in this sample.

**Troubleshoot Hours Vs Avg Quality** (r=-1.00, n=2)
> r=-1.00: More hours spent on manual troubleshooting correlates with lower perceived network quality, validating the need for automated SDN-based management.


## Artifacts

- Report JSON: `/tmp/sdn_patrick/campus_stakeholder_report.json`
- Manual settings: `/tmp/sdn_patrick/campus_manual_settings.json`
- Security policy: `/tmp/sdn_patrick/campus_security_policy.json`
- DQN policy: `/tmp/sdn_patrick/campus_dqn_policy.json`
