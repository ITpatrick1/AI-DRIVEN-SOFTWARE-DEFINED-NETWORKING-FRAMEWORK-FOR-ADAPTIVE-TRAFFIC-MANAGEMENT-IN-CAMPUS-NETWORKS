#!/usr/bin/env python3
"""
Module 1: Topology Manager — Tumba College SDN

- Discovers and maintains real-time network topology graph
- Detects link failures via OpenFlow Port-Status messages
- Triggers self-healing rerouting when any link goes down
- Publishes topology events to other modules via internal event bus
- Stores topology state in memory as a NetworkX graph
"""

import json
import os
import time
import threading
from collections import defaultdict

import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event
from ryu.lib.packet import packet, ethernet, lldp


class TopologyManager(app_manager.RyuApp):
    """Discovers and maintains the network topology graph."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.graph = nx.Graph()
        self.datapaths = {}
        self.links = {}  # {(src_dpid, dst_dpid): link_info}
        self.switch_ports = defaultdict(dict)  # {dpid: {port_no: status}}
        self.link_events = []
        self.topology_lock = threading.Lock()

        self.state_file = os.environ.get(
            'CAMPUS_TOPOLOGY_GRAPH_FILE', '/tmp/campus_topology_graph.json'
        )
        self.events_file = os.environ.get(
            'CAMPUS_TOPOLOGY_EVENTS_FILE', '/tmp/campus_topology_events.jsonl'
        )

        self._monitor_thread = hub.spawn(self._monitor_loop)
        self.logger.info("TopologyManager initialized")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Handle new switch connection."""
        datapath = ev.msg.datapath
        dpid = datapath.id
        self.datapaths[dpid] = datapath
        self.logger.info("Switch connected: dpid=%s", dpid)

        with self.topology_lock:
            self.graph.add_node(dpid, type='switch', dpid=dpid)
            self._publish_event('switch_connected', dpid=dpid)
            self._write_state()

        # Install table-miss flow entry (send to controller)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath, priority=0, match=match, instructions=inst
        )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        """Handle switch connect/disconnect."""
        datapath = ev.datapath
        dpid = datapath.id
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dpid] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if dpid in self.datapaths:
                del self.datapaths[dpid]
                self.logger.warning("Switch disconnected: dpid=%s", dpid)
                with self.topology_lock:
                    if dpid in self.graph:
                        self.graph.remove_node(dpid)
                    self._publish_event('switch_disconnected', dpid=dpid)
                    self._write_state()

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """Handle port status changes — key for link failure detection."""
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        ofproto = dp.ofproto
        port = msg.desc

        reason_map = {
            ofproto.OFPPR_ADD: 'ADD',
            ofproto.OFPPR_DELETE: 'DELETE',
            ofproto.OFPPR_MODIFY: 'MODIFY',
        }
        reason = reason_map.get(msg.reason, 'UNKNOWN')
        port_no = port.port_no
        is_up = not (port.state & ofproto.OFPPS_LINK_DOWN)

        self.logger.info(
            "Port status change: dpid=%s port=%s reason=%s state=%s",
            dpid, port_no, reason, 'UP' if is_up else 'DOWN'
        )

        with self.topology_lock:
            self.switch_ports[dpid][port_no] = {
                'state': 'up' if is_up else 'down',
                'reason': reason,
                'ts': time.time(),
            }

            if not is_up:
                # Link failure detected — remove affected links from graph
                neighbors = list(self.graph.neighbors(dpid)) if dpid in self.graph else []
                for neighbor in neighbors:
                    edge_data = self.graph.get_edge_data(dpid, neighbor)
                    if edge_data and edge_data.get('src_port') == port_no:
                        self.graph.remove_edge(dpid, neighbor)
                        self.logger.warning(
                            "LINK_FAILURE: dpid=%s port=%s → neighbor=%s",
                            dpid, port_no, neighbor
                        )
                        self._publish_event(
                            'link_failure',
                            src_dpid=dpid, src_port=port_no,
                            dst_dpid=neighbor,
                            detection_ts=time.time(),
                        )
            else:
                self._publish_event(
                    'port_up', dpid=dpid, port=port_no,
                )
            self._write_state()

    def get_shortest_path(self, src_dpid, dst_dpid):
        """Compute shortest path using Dijkstra on the topology graph."""
        with self.topology_lock:
            try:
                return nx.dijkstra_path(self.graph, src_dpid, dst_dpid, weight='weight')
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                return None

    def get_all_paths(self, src_dpid, dst_dpid, max_paths=5):
        """Get multiple paths for load balancing."""
        with self.topology_lock:
            try:
                return list(nx.all_simple_paths(self.graph, src_dpid, dst_dpid, cutoff=6))[:max_paths]
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                return []

    def is_link_up(self, src_dpid, dst_dpid):
        """Check if link between two switches is up."""
        with self.topology_lock:
            return self.graph.has_edge(src_dpid, dst_dpid)

    def add_link(self, src_dpid, src_port, dst_dpid, dst_port, weight=1):
        """Add or update a link in the topology graph."""
        with self.topology_lock:
            self.graph.add_edge(
                src_dpid, dst_dpid,
                src_port=src_port, dst_port=dst_port,
                weight=weight, ts=time.time(),
            )
            self.links[(src_dpid, dst_dpid)] = {
                'src_port': src_port, 'dst_port': dst_port,
                'weight': weight, 'ts': time.time(),
            }

    def get_topology_summary(self):
        """Get current topology summary for dashboard."""
        with self.topology_lock:
            return {
                'ts': time.time(),
                'switches': list(self.graph.nodes()),
                'links': [
                    {
                        'src': u, 'dst': v,
                        'src_port': d.get('src_port'),
                        'dst_port': d.get('dst_port'),
                    }
                    for u, v, d in self.graph.edges(data=True)
                ],
                'switch_count': self.graph.number_of_nodes(),
                'link_count': self.graph.number_of_edges(),
                'connected': nx.is_connected(self.graph) if self.graph.number_of_nodes() > 1 else True,
            }

    def _publish_event(self, event_type, **data):
        """Publish topology event."""
        event = {
            'ts': time.time(),
            'event': event_type,
            **data,
        }
        self.link_events.append(event)
        self.link_events = self.link_events[-500:]

        try:
            with open(self.events_file, 'a') as f:
                f.write(json.dumps(event) + '\n')
        except Exception:
            pass

    def _write_state(self):
        """Write topology state to file for other modules."""
        state = self.get_topology_summary()
        state['events'] = self.link_events[-50:]
        try:
            tmp = self.state_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.logger.error("Failed to write topology state: %s", e)

    def _monitor_loop(self):
        """Periodic topology state refresh."""
        while True:
            hub.sleep(5)
            with self.topology_lock:
                self._write_state()
