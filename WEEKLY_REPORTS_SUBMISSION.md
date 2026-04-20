# Learning Outcome 3 Weekly Report Submission

Student Name: [Your Name]  
Student ID: [Your Student ID]  
Course/Module: [Your Course Name]  
Project Title: DESIGN AND IMPLEMENTATION OF AN AI-DRIVEN SOFTWARE-DEFINED NETWORKING FRAMEWORK FOR ADAPTIVE TRAFFIC MANAGEMENT IN CAMPUS NETWORKS  

Project Note: The implementation presented in this report was carried out as a simulated prototype using Mininet, Ryu, and supporting tools. It does not claim full physical deployment on a real campus network. Instead, it focuses on the design, simulation, testing, and evaluation of an adaptive SDN framework that can later inform real campus-network improvement.

## Weekly report 6 March 2026

This week I focused on defining the scope of my capstone project and preparing the development environment. The project is based on designing an AI-driven software-defined networking framework that can monitor traffic, detect congestion, and automatically apply adaptive traffic-management policies in a simulated campus environment. To prepare for implementation, I installed and configured Ubuntu 24.04 in VMware, Open vSwitch, Mininet, and a Python virtual environment for the supporting software stack. I also configured the main development tools required for the project, including Ryu, Flask, NumPy, Torch, and Eventlet.

An important achievement this week was creating a reliable setup and validation process so that the environment could support later stages of controller development and network emulation. I used the Stage 1 verifier in `examples/verify_stage1_requirements.sh` to confirm the availability of the major dependencies and services. The main challenge at this stage was ensuring compatibility between the SDN tools and the Python environment, but this was addressed by isolating the packages in a virtual environment and checking each dependency step by step.

By the end of the week, I had a stable project foundation and a clear roadmap covering requirements, topology modelling, controller logic, monitoring, machine learning integration, dashboard development, and evaluation. In the next stage, I planned to build the baseline simulated campus topology and implement the initial SDN controller.

## Weekly report 13 March 2026

This week I implemented the first functional version of the simulated campus network topology and the baseline controller logic. I designed a five-zone topology in `examples/campus_topology.py` to represent realistic campus segments such as the core network, IT lab, networking lab, staff LAN, student Wi-Fi zone, and service servers. This topology was developed as a simulation model for testing traffic-management behavior in a software-defined network environment.

Alongside the topology, I developed the initial Ryu controller in `examples/campus_controller.py` using OpenFlow 1.3. The controller performs essential learning-switch operations such as processing `PACKET_IN` events, learning MAC addresses, and installing forwarding rules dynamically through `FLOW_MOD` messages. I verified the implementation with the Stage 2 and Stage 3 verification scripts, which confirm controller connectivity, switch registration, full `pingall` reachability, and flow installation behavior.

The main challenge this week was correctly aligning the topology design with switch port mappings, host addressing, and controller expectations. I resolved this by using fixed DPID and port definitions and by carrying out repeated connectivity tests. At the end of the week, the simulation topology was operational and the controller was successfully forwarding traffic inside the emulated environment. My next focus was to add traffic statistics collection and congestion awareness to the controller.

## Weekly report 20 March 2026

This week I extended the project from a basic SDN controller into an adaptive traffic-management platform. I implemented traffic statistics collection in the controller using periodic OpenFlow port statistics requests, and I added metric export fields such as per-switch throughput, utilization percentage, and detailed port statistics. These metrics are written to the controller output file and later used for monitoring, automation, and machine learning decisions.

I also implemented congestion-detection logic using threshold-based monitoring with hysteresis so that the controller can distinguish between normal load and true congestion. After that, I added the policy engine that classifies traffic into different priority levels. In this stage, exam traffic, authentication traffic, normal web traffic, and bulk transfer traffic are handled differently through queue-based QoS rules. This work is reflected in the Stage 4, Stage 5, and Stage 6 scripts, especially `examples/verify_stage4_stats.sh`, `examples/verify_stage5_congestion.sh`, and `examples/verify_stage6_policy.sh`.

The major challenge was avoiding unstable policy behavior when link usage rises and falls rapidly. I addressed this by introducing separate high and low congestion thresholds so that the system does not keep switching policies on and off unnecessarily. By the end of this week, the system could measure traffic conditions, detect congestion, and apply policy-based prioritization. The next step was to expose the network to runtime control and visual monitoring tools.

## Weekly report 27 March 2026

This week I focused on usability and runtime control of the simulated campus network. I enhanced `examples/campus_topology.py` with a runtime API that allows external tools to interact with the active Mininet environment. The API supports operations such as checking system health, viewing topology state, running `pingall`, adding devices, starting a traffic stress workload, stopping the workload, and reviewing operation history. This made the project a more complete simulation platform for experimentation and demonstration.

At the same time, I built the first version of the web-based network dashboard in `examples/campus_dashboard.py`. The dashboard provides visibility into topology layout, link utilization, events, flows, devices, and controller actions. I also connected the dashboard to runtime simulation actions so that a user can trigger network tests and workload generation directly from the browser interface. This stage improved the practicality of the project by making the SDN framework easier to observe, test, and demonstrate.

The main difficulty was keeping the dashboard synchronized with the live network state and ensuring that user-triggered actions returned clear results. I solved this by improving operation logging, health checks, and structured JSON responses between components. By the end of the week, the project had both a control API and a functional monitoring interface. My next goal was to build a dedicated monitoring service and begin the machine learning module.

## Weekly report 3 April 2026

This week I worked on advanced monitoring and the start of adaptive intelligence in the project. I created a dedicated monitoring service in `examples/traffic_monitor.py` that polls the Ryu REST API for switch, port, and flow statistics. The monitoring module reports important operational indicators such as link utilization, warning conditions, active flow counts, and short-term traffic trends. This added a clearer analytical layer on top of the raw controller metrics and helped improve the visibility of network behavior during stress tests.

In parallel, I started the Stage 8 reinforcement learning component by implementing the DQN-based routing agent in `examples/dqn_routing_agent.py`. The module includes state extraction from live metrics, an action space for adaptive decisions, and a reward function for evaluating outcomes. The DQN agent writes its recommendations to a machine-readable action file so that the controller can later use those decisions in live routing policy changes. I validated the monitoring and DQN structure using the corresponding Stage 7 and Stage 8 verification scripts.

The challenge this week was selecting state features that were simple enough to compute in real time but still meaningful for adaptive routing decisions. I addressed this by using congestion indicators, throughput proxies, queue pressure, and estimated latency in the DQN state. At the end of the week, the system had both a separate monitoring module and a functional learning-based decision engine. My next step was full integration between the DQN agent, the controller, and the dashboard.

## Weekly report 17 April 2026

This week I concentrated on full system integration. I connected the DQN routing agent to the Ryu controller so that the controller can request a decision when congestion appears, consume the generated recommendation, and install or remove adaptive flow rules accordingly inside the simulated environment. This integration is handled in `examples/campus_controller.py` and verified through `examples/verify_stage9_dqn_ryu_integration.sh`. The controller now records machine-learning-related metrics such as pending decisions, last decision time, last action name, and trigger reasons.

I also upgraded the web dashboard into a more complete Flask-based monitoring platform. The Stage 10 improvements added a `/api/dashboard` endpoint and new user-facing panels for queue depth, latency trend, alerts, active flow rules, and controller action summaries. These enhancements transformed the dashboard from a simple display page into a stronger operational interface for viewing the network state and controller behavior in near real time.

The main challenge was ensuring reliable communication among multiple components: topology runtime API, controller metrics, dashboard services, and the DQN agent. I reduced this risk by using structured JSON files, explicit timeouts, readiness checks, and fallback endpoint detection in the scripts. By the end of this week, the project had become a connected full-stack SDN simulation platform rather than a set of isolated modules. My next focus was the final testing, evaluation, and documentation phase.

## Weekly report 24 April 2026

This week I completed the testing, evaluation, and final packaging of the capstone project. I developed the Stage 11 evaluation workflow in `examples/adaptive_eval.py`, together with `examples/run_stage11_evaluation.sh` and `examples/verify_stage11_testing.sh`, to compare the behavior of the network before and after adaptive control under congestion. The evaluation measures connectivity, throughput, packet delivery, latency, response time, and rerouting evidence, which provides measurable proof of project performance.

The results generated in the `results/` directory show that the adaptive approach preserved full connectivity and improved protected-flow throughput under congestion from 4.607 Mbps to 5.452 Mbps. Average latency also improved slightly from 6.011 ms to 5.955 ms, while packet delivery remained at 100%. The adaptive response time recorded in the Stage 11 comparison report was 0.001 seconds. In addition to evaluation, I prepared offline execution support with `examples/prepare_offline_bundle.sh`, `examples/install_offline_bundle.sh`, and the operational documentation in `examples/CAMPUS_RUNBOOK.md`.

The main challenge in this final phase was making the evaluation repeatable and evidence-based rather than descriptive only. I solved this by introducing a controlled bottleneck, scripted stress generation, structured artifact output, and automated verification. At the end of this week, the project reached a complete simulation-based prototype state with adaptive SDN control, intelligent policy behavior, machine learning integration, a web dashboard, and measurable evaluation evidence.

## Final submission summary

Overall, this capstone project achieved its objective of designing, implementing, and evaluating an AI-driven SDN framework for adaptive traffic management in campus networks using Mininet, Ryu, OpenFlow, Flask, and reinforcement learning concepts. The final system includes a realistic simulated campus topology, a working SDN controller, live monitoring, congestion detection, QoS policy handling, DQN-assisted adaptive routing, a web dashboard, verification scripts, and measurable evaluation results. The project demonstrates both practical software-defined networking skills and the ability to integrate networking, automation, monitoring, and intelligent decision-making into one complete simulation-based solution for campus traffic management.
