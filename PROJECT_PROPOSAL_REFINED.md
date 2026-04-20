# Project Proposal Summary

## Project Title

DESIGN AND IMPLEMENTATION OF AN AI-DRIVEN SOFTWARE-DEFINED NETWORKING FRAMEWORK FOR ADAPTIVE TRAFFIC MANAGEMENT IN CAMPUS NETWORKS

## Project Approach

This project is implemented as a simulation-based prototype using Mininet, Ryu, Open vSwitch, and supporting AI components. It does not claim full deployment on a physical campus network. Instead, it focuses on designing, simulating, testing, and evaluating an adaptive SDN framework that can later guide improvement of real campus networks.

## Problem Statement

Traditional campus networks are mostly static and depend on fixed configuration, fixed routing rules, and limited adaptability. As the number of users, devices, applications, and digital learning services continues to increase, traffic demand also increases, leading to congestion, delay, and inefficient bandwidth utilization. In many cases, network administrators do not have enough real-time visibility into traffic conditions, link utilization, and performance changes, which makes it difficult to respond quickly when traffic spikes occur.

Another major challenge is that routing decisions in traditional networks are not intelligent. Traffic often continues to follow normal paths even when those paths are congested, while other network resources remain underused. In addition, traffic prioritization is usually static, meaning the network cannot easily change priority according to the current situation, such as online exams, e-learning platforms, or critical academic services. Because of these limitations, campus users may experience slow response, unstable connectivity, reduced quality of service, and poor overall network performance.

This project therefore addresses the need for a more adaptive and intelligent traffic-management framework for campus networks. The proposed solution uses Software-Defined Networking together with AI-supported traffic analysis and dynamic policy control to improve visibility, detect congestion in real time, and support more responsive traffic-management decisions in a simulated campus-network environment.

## Innovation of the System

The innovation of this system is not the invention of SDN itself, but the integration of Software-Defined Networking, AI-driven traffic analytics, and context-aware policy control into one adaptive traffic-management framework. Unlike traditional networks that rely on distributed control, fixed routing behaviour, and mostly static priority handling, this system provides centralized visibility, automatic congestion detection, and dynamic flow-rule updates through an SDN controller.

The system also introduces context-based priority control. Instead of assigning permanent priority to one network segment, it can prioritize traffic according to the current situation, for example giving higher priority to exam traffic, learning services, or other important applications when needed. This makes traffic management more flexible, responsive, and intelligent because routing and service quality are adjusted according to real-time network conditions rather than fixed assumptions.

## Uniqueness of the System

The uniqueness of this system is that it combines SDN-based centralized control, AI-supported traffic analysis, and context-aware dynamic prioritization in one framework. Many existing approaches either monitor traffic without adaptive action, or apply fixed priority to a whole segment without considering real-time context. In contrast, this system is designed to monitor traffic conditions, detect congestion, and make adaptive decisions about which traffic should be prioritized, rerouted, or controlled at a particular time.

Its uniqueness also comes from the fact that prioritization is situation-based rather than permanently assigned. The framework can respond differently depending on network conditions and service importance, which is more suitable for campus environments where user demand changes across learning, administrative, and general-access services. Therefore, the project offers a more flexible and intelligent traffic-management approach than traditional static network-control methods.
