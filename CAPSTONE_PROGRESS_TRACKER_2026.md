# STUDENT'S CAPSTONE PROJECT PROGRESS TRACKER 2026

Student name: MANISHIMWE Patrick  
Registration number: 25RP18267  
Department: Information and Communication Technology  
Program: Bachelor's in Information Technology  
Supervisor: BAMPIRE Delphine  
Project title: DESIGN AND IMPLEMENTATION OF AN AI-DRIVEN SOFTWARE-DEFINED NETWORKING FRAMEWORK FOR ADAPTIVE TRAFFIC MANAGEMENT IN CAMPUS NETWORKS  

## Task 1

No: 1  
Tasks: Initial project design, simulation planning, and environment setup for the AI-driven SDN framework for adaptive traffic management in campus networks. This included defining the system architecture, preparing Ubuntu 24.04 in VMware, setting up Mininet, Open vSwitch, Ryu, and the Python virtual environment, and installing supporting libraries and tools needed for simulation, controller development, dashboard services, and AI integration.  
Start: 02/03/2026  
End: 08/03/2026  
Status and comments:  
Status: Completed.  
The student initiated the implementation phase of the capstone project by preparing the development and simulation environment and defining the main architecture of the adaptive traffic-management framework. The required tools and libraries including Mininet, Open vSwitch, Ryu, Flask, NumPy, Torch, and Eventlet were installed and configured successfully. The project structure was also organized to support topology modelling, controller logic, monitoring, AI integration, testing, and documentation.  
Supervisor Comments: The supervisor advised the student to continue the work as a simulation-based implementation aligned to a real campus-network structure rather than claiming a full physical deployment.  
Signature: ____________________

## Task 2

No: 2  
Tasks: Design and implementation of the simulated campus network topology and the baseline SDN controller. This included modelling core, laboratory, staff, Wi-Fi, and server zones in Mininet and implementing a Ryu OpenFlow 1.3 controller for switch connectivity, MAC learning, and dynamic flow installation.  
Start: 09/03/2026  
End: 15/03/2026  
Status and comments:  
Status: Completed.  
The student designed the first working version of the simulated campus topology and implemented the baseline controller logic. The controller was able to process `PACKET_IN` events, learn MAC addresses, and install forwarding rules dynamically using `FLOW_MOD` messages. Connectivity and controller communication were validated through topology verification and reachability tests.  
Supervisor Comments: The supervisor advised the student to make sure the simulated topology reflects realistic campus-network zones and to keep recording the development evidence for each stage.  
Signature: ____________________

## Task 3

No: 3  
Tasks: Implementation of traffic-statistics collection, utilization monitoring, congestion detection, and adaptive policy thresholds in the SDN controller. This included periodic OpenFlow port-statistics polling, throughput and utilization calculation, event logging, and threshold-based congestion awareness for adaptive traffic management.  
Start: 16/03/2026  
End: 22/03/2026  
Status and comments:  
Status: Completed.  
The student extended the controller from basic forwarding into an adaptive traffic-management controller. Real-time traffic statistics were collected and exported, congestion detection with hysteresis was introduced, and the system was prepared to identify overloaded links and respond to changing traffic conditions. This stage established the performance-monitoring foundation for later policy decisions and AI-supported adaptation.  
Supervisor Comments: The supervisor advised the student to ensure that the congestion-detection logic clearly addresses the original problem of poor visibility, delayed response, and inefficient bandwidth utilization in campus networks.  
Signature: ____________________

## Task 4

No: 4  
Tasks: Implementation of context-aware QoS policy control, runtime API functions, and live operational management of the simulated network. This included traffic classification, queue-based prioritization, adaptive service handling, and runtime actions such as topology inspection, ping tests, stress generation, and device management through the simulation environment.  
Start: 23/03/2026  
End: 29/03/2026  
Status and comments:  
Status: Completed.  
The student implemented the main adaptive policy logic for handling different classes of traffic such as exam traffic, authentication traffic, normal services, and bulk traffic. In addition, the runtime API was developed to support live simulation operations including health checks, topology viewing, `pingall`, stress starting and stopping, and operation-history tracking. This improved the flexibility of the framework and made the simulation environment interactive and testable.  
Supervisor Comments: The supervisor advised the student to keep relating the adaptive policies to real campus-network needs and to show clearly how the solution handles traffic priority dynamically instead of using static control.  
Signature: ____________________

## Task 5

No: 5  
Tasks: Development of the first web-based dashboard and the dedicated traffic-monitoring module for the AI-driven SDN framework. This included topology visualization, metrics display, event tracking, flow visibility, switch and link utilization monitoring, warning generation, and traffic-trend observation for the simulated campus network.  
Start: 30/03/2026  
End: 05/04/2026  
Status and comments:  
Status: Completed.  
The student developed the dashboard and monitoring layer of the project. A web-based interface was created to display topology state, controller metrics, events, flows, and device information, while a separate traffic-monitoring component was implemented to poll Ryu REST statistics and present utilization, active-flow counts, warnings, and trend history. This stage improved real-time visibility and made the framework easier to observe and demonstrate.  
Supervisor Comments: The supervisor advised the student to continue strengthening the simulation, testing each monitoring feature carefully, and presenting the work as a practical simulation of a real campus-network problem.  
Signature: ____________________

## Task 6

No: 6  
Tasks: Development and integration of the DQN-based adaptive routing module with the Ryu controller and improvement of the Flask-based monitoring dashboard. This included state extraction from live metrics, reward and action design, generation of AI routing recommendations, controller-side decision handling, and dashboard support for queue depth, alerts, latency trends, and controller action summaries.  
Start: 06/04/2026  
End: 19/04/2026  
Status and comments:  
Status: Completed.  
The student implemented the DQN adaptive-routing module and integrated it with the SDN controller so that congestion events could trigger AI-assisted decisions. The dashboard was also upgraded into a more complete Flask-based monitoring platform with additional operational views such as alerts, active flow rules, latency trends, queue-depth estimation, and controller action summaries. This stage connected the AI, controller, and monitoring layers into one coordinated simulation framework.  
Supervisor Comments: The supervisor advised the student to keep the explanation clear by showing that the system combines SDN control, AI-supported analysis, and context-aware prioritization within a simulated campus-network framework.  
Signature: ____________________

## Task 7

No: 7  
Tasks: Final testing, performance evaluation, evidence generation, and preparation of submission documents for the complete AI-driven SDN framework. This included adaptive-vs-static comparison testing, automated verification scripts, result-artifact generation, runbook preparation, and final weekly-report and tracker documentation.  
Start: 20/04/2026  
End: 26/04/2026  
Status and comments:
Status: Completed.  
The student completed the end-to-end integration, testing, and evaluation of the project and produced measurable evidence from the simulation environment. The final framework supports adaptive congestion detection, policy updates, machine-learning-assisted routing decisions, dashboard-based monitoring, and automated testing. Evaluation artifacts showed that connectivity was preserved under both baseline and adaptive conditions, protected-flow throughput improved from 4.607 Mbps to 5.452 Mbps under congestion, average latency improved from 6.011 ms to 5.955 ms, and adaptive response time of 0.001 seconds was recorded. Final technical documentation and submission materials were also prepared.  
Supervisor Comments: The supervisor advised the student to present the final work clearly as a simulation-based design, implementation, and evaluation of an adaptive SDN framework for campus networks and to communicate the results in a structured and evidence-based manner.  
Signature: ____________________

Note: Students should meet their supervisors at least once a week. Meetings can be physical or online depending on the whereabouts of students and supervisors. This form is periodically presented to the department headship and a comprehensive submission should be submitted one week before the project report presentation.
