#!/usr/bin/env python3
"""
Main Controller — Tumba College SDN
Unified Ryu application loading all sub-modules.
"""
import json, os, time, threading
from collections import defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, arp, ether_types
from ryu.ofproto import ofproto_v1_3


METRICS_FILE = os.environ.get('CAMPUS_METRICS_FILE', '/tmp/campus_metrics.json')
ML_ACTION_FILE = os.environ.get('CAMPUS_ML_ACTION_FILE', '/tmp/campus_ml_action.json')
TIMETABLE_STATE = os.environ.get('CAMPUS_TIMETABLE_STATE', '/tmp/campus_timetable_state.json')

ZONE_SUBNETS = {
    'staff_lan': '10.10.0.', 'server_zone': '10.20.0.',
    'it_lab': '10.30.0.', 'student_wifi': '10.40.0.',
}

class CampusController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.port_stats = defaultdict(dict)
        self.port_stats_prev = defaultdict(dict)
        self.zone_metrics = {}
        self.congested_ports = set()
        self.ctrl_events = []
        self.ml_action = {}
        self.timetable_state = {}
        self.security_events = []
        self.security_blocked = 0
        self.ddos_blocked_ips = {}
        self.flow_count = 0
        self._monitor = hub.spawn(self._monitor_loop)
        self.logger.info("CampusController initialized")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        self.datapaths[dp.id] = dp
        # Table-miss: send to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, match, actions)
        # Allow ARP
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP)
        actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
        self._add_flow(dp, 200, match, actions)
        # Allow ICMP
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=1)
        actions = [parser.OFPActionOutput(ofp.OFPP_NORMAL)]
        self._add_flow(dp, 200, match, actions)
        self.logger.info("Switch connected: dpid=%s", dp.id)
        self._append_event('switch_connected', dpid=dp.id)
        # Install zone policies after delay
        hub.spawn(self._install_zone_policies, dp)

    def _install_zone_policies(self, dp):
        hub.sleep(1)
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        # Same-zone forwarding
        for zone, prefix in ZONE_SUBNETS.items():
            nw = prefix + '0'
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=(nw,'255.255.255.0'), ipv4_dst=(nw,'255.255.255.0'))
            actions = [parser.OFPActionOutput(ofp.OFPP_NORMAL)]
            self._add_flow(dp, 150, match, actions)
        # Zero-trust: block student_wifi → staff_lan
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
            ipv4_src=('10.40.0.0','255.255.255.0'),
            ipv4_dst=('10.10.0.0','255.255.255.0'))
        self._add_flow(dp, 300, match, [])  # DROP
        # Allow permitted cross-zone flows
        for src_nw, dst_nw, ports in [
            ('10.10.0.0','10.20.0.0',[80,443]),
            ('10.30.0.0','10.20.0.0',[80,443,22,3306]),
            ('10.40.0.0','10.20.0.0',[80,443]),
        ]:
            for port in ports:
                match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_src=(src_nw,'255.255.255.0'), ipv4_dst=(dst_nw,'255.255.255.0'),
                    ip_proto=6, tcp_dst=port)
                actions = [parser.OFPActionOutput(ofp.OFPP_NORMAL)]
                self._add_flow(dp, 250, match, actions)
        self.logger.info("Zone policies installed on dpid=%s", dp.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if not eth or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        dst = eth.dst
        src = eth.src
        dpid = dp.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofp.OFPP_FLOOD
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            if msg.buffer_id != ofp.OFP_NO_BUFFER:
                self._add_flow(dp, 10, match, actions, buffer_id=msg.buffer_id)
                return
            self._add_flow(dp, 10, match, actions)
        data = None
        if msg.buffer_id == ofp.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        for stat in ev.msg.body:
            pn = stat.port_no
            if pn >= 0xfffffff0: continue
            key = (dpid, pn)
            prev = self.port_stats_prev.get(key)
            cur = {'rx': stat.rx_bytes, 'tx': stat.tx_bytes,
                   'rx_p': stat.rx_packets, 'tx_p': stat.tx_packets, 'ts': now}
            if prev:
                dt = max(0.001, now - prev['ts'])
                mbps = ((cur['rx']-prev['rx'])+(cur['tx']-prev['tx']))*8/(dt*1e6)
                pps = max(0, cur['rx_p']-prev['rx_p'])/dt
                self.port_stats[key] = {'mbps': round(mbps,3), 'pps': round(pps,1),
                    'util_pct': round(min(100, mbps/100*100),2)}
            self.port_stats_prev[key] = cur
        self._update_zone_metrics()
        self._write_metrics()

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status(self, ev):
        msg = ev.msg
        dp = msg.datapath
        port = msg.desc
        is_up = not (port.state & dp.ofproto.OFPPS_LINK_DOWN)
        self.logger.info("Port %s on dpid=%s is %s", port.port_no, dp.id, 'UP' if is_up else 'DOWN')
        if not is_up:
            self._append_event('link_failure', dpid=dp.id, port=port.port_no)
        else:
            self._append_event('link_recovery', dpid=dp.id, port=port.port_no)

    def _add_flow(self, dp, priority, match, actions, buffer_id=None, hard_timeout=0):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        kw = dict(datapath=dp, priority=priority, match=match,
                  instructions=inst, hard_timeout=hard_timeout)
        if buffer_id is not None:
            kw['buffer_id'] = buffer_id
        dp.send_msg(parser.OFPFlowMod(**kw))
        self.flow_count += 1

    def _monitor_loop(self):
        while True:
            for dpid, dp in list(self.datapaths.items()):
                parser = dp.ofproto_parser
                req = parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY)
                dp.send_msg(req)
            self._load_ml_action()
            self._load_timetable()
            hub.sleep(2)

    def _update_zone_metrics(self):
        zone_dpid = {'staff_lan':4,'server_zone':5,'it_lab':6,'student_wifi':7}
        for zone, zdpid in zone_dpid.items():
            ports = [(d,p) for (d,p) in self.port_stats if d == zdpid]
            total = sum(self.port_stats.get(k,{}).get('mbps',0) for k in ports)
            mx = max((self.port_stats.get(k,{}).get('util_pct',0) for k in ports), default=0)
            self.zone_metrics[zone] = {
                'throughput_mbps': round(total,3), 'max_utilization_pct': round(mx,2),
                'port_count': len(ports), 'congested': mx > 70,
            }

    def _load_ml_action(self):
        try:
            if os.path.exists(ML_ACTION_FILE):
                with open(ML_ACTION_FILE) as f: self.ml_action = json.load(f)
        except: pass

    def _load_timetable(self):
        try:
            if os.path.exists(TIMETABLE_STATE):
                with open(TIMETABLE_STATE) as f: self.timetable_state = json.load(f)
        except: pass

    def _write_metrics(self):
        port_data = {}
        for (d,p), s in self.port_stats.items():
            port_data.setdefault(str(d),{})[str(p)] = s
        payload = {
            'ts': time.time(),
            'zone_metrics': self.zone_metrics,
            'switch_port_stats': port_data,
            'congested_ports_count': sum(1 for z in self.zone_metrics.values() if z.get('congested')),
            'connected_switches': sorted(str(x) for x in self.datapaths),
            'flow_count': self.flow_count,
            'ml_action': self.ml_action,
            'timetable_state': self.timetable_state,
            'events': self.ctrl_events[-50:],
            'ddos_active': bool(self.ddos_blocked_ips),
            'security_blocked': self.security_blocked,
        }
        try:
            tmp = METRICS_FILE + '.tmp'
            with open(tmp, 'w') as f: json.dump(payload, f, indent=2)
            os.replace(tmp, METRICS_FILE)
        except: pass

    def _append_event(self, name, **kw):
        evt = {'ts': time.time(), 'event': name, **kw}
        self.ctrl_events.append(evt)
        self.ctrl_events = self.ctrl_events[-200:]
