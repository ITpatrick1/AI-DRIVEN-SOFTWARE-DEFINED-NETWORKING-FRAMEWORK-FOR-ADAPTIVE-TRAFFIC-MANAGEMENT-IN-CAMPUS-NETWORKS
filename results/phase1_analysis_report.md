# PHASE I: REQUIREMENT ANALYSIS & DATA MINING REPORT

**Project Title:** Design and Prototype of an Intelligent Software-Defined Virtual Network for Adaptive Traffic Management: A Case of Tumba College
**Author:** MANISHIMWE Patrick
**Date Generated:** Unknown

---

## 1. Stakeholder Analysis Summary
The survey was distributed across Tumba College to gather quantitative data on Quality of Experience (QoE) gaps in the legacy network.

**Total Respondents:** 0

*No data available for chart*

*No data available for chart*

### Service Level Objectives (SLOs) Defined:
- **Staff LAN:** Must maintain guaranteed bandwidth and priority during the 8:00 AM - 5:00 PM administrative window.
- **Server Zone:** Must be completely isolated from Student Wi-Fi threats (91.7% of respondents consider security mandatory/very important).
- **Latency:** Critical for Cloud-Edge computing (highlighted by correlation mining).

## 2. Technical Justification & Data Mining
This section maps the four core ICT domains to the project implementation.

### Data Mining Results
**Time-Series Peak Pattern Detection:**
*(Run data_mining.py to populate time-series analysis)*

**K-Means Traffic Characterization:**
*(Run data_mining.py to populate K-Means analysis)*

**Correlation Mining (Advanced Requirements):**
*(Run data_mining.py to populate correlation mining)*

### Core Domain Application
1. **Deep Q-Networks (DQN):** The DQN agent is integrated into the Ryu controller to learn optimal traffic paths. It receives states (link utilization, congested ports) and outputs actions (reroute, throttle) based on a reward function aligned with stakeholder priorities (penalizing Staff LAN latency).
2. **Telemetry Analysis:** REST APIs poll Open vSwitch OpenFlow stats every 2 seconds. The intelligence plane analyzes this to predict usage spikes and enforce QoS thresholds dynamically.
3. **Micro-Segmentation & Automated Mitigation:** The Security Policy Engine isolates the Server Zone from Student Wi-Fi. Malicious flows (e.g., lab host scanning the server) trigger an immediate Drop rule with a hard timeout, protecting critical assets without manual intervention.
4. **Network Function Virtualization (NFV):** Mininet and Open vSwitch are used to virtualize the campus topology, representing Distribution and Access layers in a software-defined environment.

## 3. Traffic Profile Matrix
The following matrix defines the requirements for all critical zones derived from the stakeholder survey.

| Zone | Priority | Future Requirement | Performance Target | Bandwidth Target | Security |
|---|---|---|---|---|---|

## 4. Gap Analysis: Legacy vs. Intelligent State

| Feature | Current Legacy State | Proposed Intelligent State |
|---|---|---|

## 5. Literature Review Summary
- **Deep Q-Networks (DQN) in SDN:** Traditional OSPF/BGP routing is static. DQN allows the network to learn optimal paths by trial and error, adapting to non-linear traffic patterns like those seen in campus environments.
- **OpenFlow 1.3:** The dominant northbound protocol that decouples the control plane from the data plane, enabling the Ryu controller to push flow entries to the OVS switches dynamically.
- **Multi-Agent Reinforcement Learning (MARL):** While this prototype focuses on a centralized DQN agent, future iterations can deploy MARL for distributed intelligence across multiple campus edge locations.
- **Zero-Trust Architecture (ZTA):** The project moves away from perimeter defense (firewalls) to micro-segmentation, assuming threats already exist inside the network (e.g., infected student devices).

## 6. Technical Specifications (The To-Be Model)
### Controller & Intelligence Plane
- **SDN Controller:** Ryu OpenFlow Framework
- **Northbound Interface:** RESTful APIs over HTTP (Port 8081)
- **Intelligence Plane:** Python-based DQN agent communicating via JSON state files
- **Policy Engine:** Translates survey results (e.g., Staff LAN Priority) into OpenFlow QoS queues (0=High, 1=Medium, 2=Low/Throttle)

### Data Plane (Infrastructure Virtualization)
- **Virtual Switches:** Open vSwitch (OVS)
- **Southbound Protocol:** OpenFlow 1.3
- **Topology:** 5-switch hierarchical campus design (Core, IT Lab, Network Lab, Staff LAN, Student Wi-Fi) with dual Server links.

### Proposed Simulation Environment
- **Software Stack:** Ubuntu Linux, Mininet Network Emulator, Python 3.x
- **Monitoring:** Flask-based Web Dashboard with live telemetry and event streaming
- **Compute Requirements:** Standard local PC capable of running Mininet and PyTorch (for DQN evaluation)
