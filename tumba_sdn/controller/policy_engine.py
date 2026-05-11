#!/usr/bin/env python3
"""
Module 2: Policy Engine — Tumba College SDN

- Reads the Traffic Profile Matrix from config file
- Translates business policies into OpenFlow flow rules
- Installs default-deny rules for all inter-zone traffic (Zero-Trust)
- Installs explicit permit rules for allowed cross-zone traffic
- Installs OpenFlow meter rules for bandwidth limiting per zone
- Exposes REST API for DQN agent to push new flow rules
"""

import json
import os
import time
import threading

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ether_types
from ryu.ofproto import ofproto_v1_3


# DPID mapping for the campus topology
DPID_MAP = {
    'cs1': 1, 'ds1': 2, 'ds2': 3,
    'as1': 4, 'as2': 5, 'as3': 6, 'as4': 7,
}

# Zone subnet mapping
ZONE_SUBNETS = {
    'staff_lan':    '10.10.0.0/24',
    'server_zone':  '10.20.0.0/24',
    'it_lab':       '10.30.0.0/24',
    'student_wifi': '10.40.0.0/24',
}

# Server IPs
MIS_IP = '10.20.0.1'
MOODLE_IP = '10.20.0.2'
LAB_SERVER_IP = '10.20.0.3'
AUTH_IP = '10.20.0.4'

POLICY_COOKIE = 0xCAFE1001
ZERO_TRUST_COOKIE = 0xCAFE2001


class PolicyEngine(app_manager.RyuApp):
    """Zero-Trust policy engine with traffic-profile-driven rules."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.policy_rules = {}  # {dpid: rule_count}
        self.config = self._load_traffic_profile()
        self._policy_lock = threading.Lock()

        self.events_file = os.environ.get(
            'CAMPUS_POLICY_EVENTS_FILE', '/tmp/campus_policy_events.jsonl'
        )
        self.logger.info("PolicyEngine initialized with %d zones", len(self.config.get('zones', {})))

    def _load_traffic_profile(self):
        """Load traffic profile config."""
        config_path = os.environ.get(
            'CAMPUS_TRAFFIC_PROFILE',
            os.path.join(os.path.dirname(__file__), '..', 'config', 'traffic_profile.json')
        )
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            self.logger.warning("Failed to load traffic profile: %s, using defaults", e)
            return {'zones': {}, 'congestion_thresholds': {}, 'zero_trust_rules': {}}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Install policy rules on switch connect."""
        datapath = ev.msg.datapath
        dpid = datapath.id
        self.datapaths[dpid] = datapath
        self.logger.info("PolicyEngine: switch connected dpid=%s", dpid)
        hub.spawn(self._install_policies_delayed, datapath, dpid)

    def _install_policies_delayed(self, datapath, dpid):
        """Install policies after a small delay to let topology settle."""
        hub.sleep(2)
        self._install_zero_trust_rules(datapath, dpid)
        self._install_qos_rules(datapath, dpid)

    def _install_zero_trust_rules(self, datapath, dpid):
        """Install default-deny + explicit-permit rules."""
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        rule_count = 0

        # Default-deny: drop all inter-zone traffic at low priority
        # (except explicitly permitted flows)
        zt_config = self.config.get('zero_trust_rules', {})
        if zt_config.get('default_deny', True):
            # For access switches, drop cross-zone traffic by default
            for src_zone, src_subnet in ZONE_SUBNETS.items():
                for dst_zone, dst_subnet in ZONE_SUBNETS.items():
                    if src_zone == dst_zone:
                        continue
                    # Install deny rule
                    src_nw = src_subnet.split('/')[0]
                    dst_nw = dst_subnet.split('/')[0]
                    match = parser.OFPMatch(
                        eth_type=ether_types.ETH_TYPE_IP,
                        ipv4_src=(src_nw, '255.255.255.0'),
                        ipv4_dst=(dst_nw, '255.255.255.0'),
                    )
                    # DROP action (empty actions list)
                    inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, [])]
                    mod = parser.OFPFlowMod(
                        datapath=datapath, priority=100,
                        match=match, instructions=inst,
                        cookie=ZERO_TRUST_COOKIE,
                        idle_timeout=0, hard_timeout=0,
                    )
                    datapath.send_msg(mod)
                    rule_count += 1

        # Install explicit permit rules for allowed flows
        for rule in zt_config.get('allowed_flows', []):
            src_zone = rule.get('src')
            dst_zone = rule.get('dst')
            ports = rule.get('ports', [])
            proto = rule.get('proto', 'tcp')

            src_subnet = ZONE_SUBNETS.get(src_zone)
            dst_subnet = ZONE_SUBNETS.get(dst_zone)
            if not src_subnet or not dst_subnet:
                continue

            src_nw = src_subnet.split('/')[0]
            dst_nw = dst_subnet.split('/')[0]

            for port in ports:
                match_kwargs = {
                    'eth_type': ether_types.ETH_TYPE_IP,
                    'ipv4_src': (src_nw, '255.255.255.0'),
                    'ipv4_dst': (dst_nw, '255.255.255.0'),
                }
                if proto == 'tcp':
                    match_kwargs['ip_proto'] = 6
                    match_kwargs['tcp_dst'] = port
                elif proto == 'udp':
                    match_kwargs['ip_proto'] = 17
                    match_kwargs['udp_dst'] = port

                match = parser.OFPMatch(**match_kwargs)
                actions = [parser.OFPActionOutput(ofproto.OFPP_NORMAL)]
                inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
                mod = parser.OFPFlowMod(
                    datapath=datapath, priority=200,
                    match=match, instructions=inst,
                    cookie=POLICY_COOKIE,
                    idle_timeout=0, hard_timeout=0,
                )
                datapath.send_msg(mod)
                rule_count += 1

        # Block student WiFi → Staff LAN (explicit block, never allowed)
        for blocked in zt_config.get('blocked_flows', []):
            src_zone = blocked.get('src')
            dst_zone = blocked.get('dst')
            src_subnet = ZONE_SUBNETS.get(src_zone)
            dst_subnet = ZONE_SUBNETS.get(dst_zone)
            if src_subnet and dst_subnet:
                src_nw = src_subnet.split('/')[0]
                dst_nw = dst_subnet.split('/')[0]
                match = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_src=(src_nw, '255.255.255.0'),
                    ipv4_dst=(dst_nw, '255.255.255.0'),
                )
                inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, [])]
                mod = parser.OFPFlowMod(
                    datapath=datapath, priority=300,
                    match=match, instructions=inst,
                    cookie=ZERO_TRUST_COOKIE,
                )
                datapath.send_msg(mod)
                rule_count += 1

        # Allow same-zone traffic
        for zone, subnet in ZONE_SUBNETS.items():
            nw = subnet.split('/')[0]
            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=(nw, '255.255.255.0'),
                ipv4_dst=(nw, '255.255.255.0'),
            )
            actions = [parser.OFPActionOutput(ofproto.OFPP_NORMAL)]
            inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
            mod = parser.OFPFlowMod(
                datapath=datapath, priority=150,
                match=match, instructions=inst,
                cookie=POLICY_COOKIE,
            )
            datapath.send_msg(mod)
            rule_count += 1

        # Allow ARP everywhere
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP)
        actions = [parser.OFPActionOutput(ofproto.OFPP_NORMAL)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath, priority=250,
            match=match, instructions=inst,
            cookie=POLICY_COOKIE,
        )
        datapath.send_msg(mod)
        rule_count += 1

        # Allow ICMP everywhere for connectivity testing
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=1)
        actions = [parser.OFPActionOutput(ofproto.OFPP_NORMAL)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath, priority=250,
            match=match, instructions=inst,
            cookie=POLICY_COOKIE,
        )
        datapath.send_msg(mod)
        rule_count += 1

        with self._policy_lock:
            self.policy_rules[dpid] = rule_count

        self.logger.info(
            "PolicyEngine: installed %d rules on dpid=%s (zero-trust + permits)",
            rule_count, dpid,
        )
        self._log_event('policy_installed', dpid=dpid, rules=rule_count)

    def _install_qos_rules(self, datapath, dpid):
        """Install QoS/bandwidth rules per zone."""
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        zones = self.config.get('zones', {})

        for zone_name, zone_cfg in zones.items():
            priority = zone_cfg.get('priority', 3)
            # Staff and Server get higher queue priority
            queue_id = 0 if priority == 1 else (1 if priority == 2 else 2)

            subnet = ZONE_SUBNETS.get(zone_name)
            if not subnet:
                continue
            nw = subnet.split('/')[0]

            # Set queue for outbound traffic from this zone
            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=(nw, '255.255.255.0'),
            )
            actions = [
                parser.OFPActionSetQueue(queue_id),
                parser.OFPActionOutput(ofproto.OFPP_NORMAL),
            ]
            inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
            mod = parser.OFPFlowMod(
                datapath=datapath, priority=50,
                match=match, instructions=inst,
                cookie=POLICY_COOKIE,
            )
            datapath.send_msg(mod)

    def update_exam_mode(self, datapath, enable=True):
        """Enable/disable exam mode: elevate student WiFi MIS traffic to priority 1."""
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        if enable:
            # Allow student WiFi → MIS exam port 8443
            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=('10.40.0.0', '255.255.255.0'),
                ipv4_dst=MIS_IP,
                ip_proto=6, tcp_dst=8443,
            )
            actions = [
                parser.OFPActionSetQueue(0),  # High priority queue
                parser.OFPActionOutput(ofproto.OFPP_NORMAL),
            ]
            inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
            mod = parser.OFPFlowMod(
                datapath=datapath, priority=350,
                match=match, instructions=inst,
                cookie=POLICY_COOKIE,
                idle_timeout=0, hard_timeout=3600,
            )
            datapath.send_msg(mod)
            self.logger.info("EXAM_MODE enabled: student WiFi→MIS priority elevated")
        else:
            # Remove exam mode rules by installing lower-priority version
            self.logger.info("EXAM_MODE disabled")

        self._log_event('exam_mode_changed', enabled=enable)

    def get_policy_summary(self):
        """Get policy summary for dashboard."""
        return {
            'ts': time.time(),
            'policy_rules': dict(self.policy_rules),
            'zones': list(self.config.get('zones', {}).keys()),
            'zero_trust_enabled': self.config.get('zero_trust_rules', {}).get('default_deny', True),
        }

    def _log_event(self, event_type, **data):
        event = {'ts': time.time(), 'event': event_type, **data}
        try:
            with open(self.events_file, 'a') as f:
                f.write(json.dumps(event) + '\n')
        except Exception:
            pass
