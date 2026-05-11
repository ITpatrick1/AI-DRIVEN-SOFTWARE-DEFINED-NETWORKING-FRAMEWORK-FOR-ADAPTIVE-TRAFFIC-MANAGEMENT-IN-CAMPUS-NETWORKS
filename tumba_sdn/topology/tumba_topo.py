#!/usr/bin/env python3
"""
Tumba College of Technology — Campus SDN Topology (Mininet)

Topology:
    Internet (NAT)
          |
       Core Switch (cs1) — OpenFlow 1.3
          |         |
         DS1        DS2        ← Distribution Switches
        / | \      / | \
      AS1 AS2    AS3 AS4       ← Access Switches (one per zone)

Zone mapping:
  AS1 → Staff LAN        (VLAN 10) — 6 virtual hosts
  AS2 → Server Zone      (VLAN 20) — 4 virtual hosts (MIS, DHCP, Auth, Moodle)
  AS3 → IT/Network Lab   (VLAN 30) — 4 virtual hosts
  AS4 → Student Wi-Fi    (VLAN 40) — 10 virtual hosts

All switches run OVS with OpenFlow 1.3.
DS1-DS2 inter-link = 1000 Mbps (redundant path for self-healing).
Each Access Switch uplink = 100 Mbps.
"""

import os
import sys
import json
import time
import threading
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel, info, error
from mininet.cli import CLI


class TumbaCollegeTopo:
    """Reusable Python class that builds the Tumba College campus topology."""

    # Zone configuration
    ZONES = {
        'staff_lan':    {'vlan': 10, 'switch': 'as1', 'hosts': 6, 'prefix': 'h_staff', 'subnet': '10.10.0'},
        'server_zone':  {'vlan': 20, 'switch': 'as2', 'hosts': 4, 'prefix': 'h_srv',   'subnet': '10.20.0'},
        'it_lab':       {'vlan': 30, 'switch': 'as3', 'hosts': 4, 'prefix': 'h_lab',   'subnet': '10.30.0'},
        'student_wifi': {'vlan': 40, 'switch': 'as4', 'hosts': 10,'prefix': 'h_wifi',  'subnet': '10.40.0'},
    }

    # Server role names for server zone
    SERVER_ROLES = {1: 'mis', 2: 'dhcp', 3: 'auth', 4: 'moodle'}

    def __init__(self, controller_ip='127.0.0.1', controller_port=6653):
        self.controller_ip = controller_ip
        self.controller_port = controller_port
        self.net = None
        self.hosts = {}
        self.switches = {}
        self.topology_state = {}

    def build(self):
        """Build and return the Mininet network."""
        info('*** Building Tumba College topology\n')

        self.net = Mininet(
            controller=RemoteController,
            switch=OVSSwitch,
            link=TCLink,
            autoSetMacs=True,
            autoStaticArp=True,
        )

        # Add controller
        c0 = self.net.addController(
            'c0',
            controller=RemoteController,
            ip=self.controller_ip,
            port=self.controller_port,
            protocols='OpenFlow13',
        )

        # ─── Core Switch ───
        cs1 = self.net.addSwitch('cs1', dpid='0000000000000001', protocols='OpenFlow13')
        self.switches['cs1'] = cs1

        # ─── Distribution Switches ───
        ds1 = self.net.addSwitch('ds1', dpid='0000000000000002', protocols='OpenFlow13')
        ds2 = self.net.addSwitch('ds2', dpid='0000000000000003', protocols='OpenFlow13')
        self.switches['ds1'] = ds1
        self.switches['ds2'] = ds2

        # ─── Access Switches ───
        as1 = self.net.addSwitch('as1', dpid='0000000000000004', protocols='OpenFlow13')  # Staff LAN
        as2 = self.net.addSwitch('as2', dpid='0000000000000005', protocols='OpenFlow13')  # Server Zone
        as3 = self.net.addSwitch('as3', dpid='0000000000000006', protocols='OpenFlow13')  # IT Lab
        as4 = self.net.addSwitch('as4', dpid='0000000000000007', protocols='OpenFlow13')  # Student WiFi
        self.switches.update({'as1': as1, 'as2': as2, 'as3': as3, 'as4': as4})

        # ─── Core-to-Distribution links (1 Gbps) ───
        self.net.addLink(cs1, ds1, bw=1000, delay='1ms')
        self.net.addLink(cs1, ds2, bw=1000, delay='1ms')

        # ─── DS1-DS2 Redundant Inter-link (1 Gbps for self-healing) ───
        self.net.addLink(ds1, ds2, bw=1000, delay='1ms')

        # ─── Distribution-to-Access links (100 Mbps each) ───
        self.net.addLink(ds1, as1, bw=100, delay='2ms')   # DS1 → Staff LAN
        self.net.addLink(ds1, as2, bw=100, delay='2ms')   # DS1 → Server Zone
        self.net.addLink(ds2, as3, bw=100, delay='2ms')   # DS2 → IT Lab
        self.net.addLink(ds2, as4, bw=100, delay='2ms')   # DS2 → Student WiFi

        # ─── Add hosts per zone ───
        for zone_name, zone_cfg in self.ZONES.items():
            switch = self.switches[zone_cfg['switch']]
            for i in range(1, zone_cfg['hosts'] + 1):
                if zone_name == 'server_zone' and i in self.SERVER_ROLES:
                    host_name = f"h_{self.SERVER_ROLES[i]}"
                else:
                    host_name = f"{zone_cfg['prefix']}{i}"

                ip = f"{zone_cfg['subnet']}.{i}/24"
                host = self.net.addHost(
                    host_name,
                    ip=ip,
                    defaultRoute=f"via {zone_cfg['subnet']}.254",
                )
                self.hosts[host_name] = host
                # Host-to-access-switch link (10 Mbps for WiFi, 100 Mbps for others)
                host_bw = 10 if zone_name == 'student_wifi' else 100
                self.net.addLink(host, switch, bw=host_bw, delay='1ms')

        return self.net

    def start(self):
        """Start the network."""
        if self.net is None:
            self.build()
        info('*** Starting network\n')
        self.net.start()

        # Set OpenFlow 1.3 on all switches
        for sw_name, sw in self.switches.items():
            sw.cmd(f'ovs-vsctl set Bridge {sw_name} protocols=OpenFlow13')

        info('*** Network started successfully\n')
        self._update_topology_state()
        return self.net

    def stop(self):
        """Stop the network."""
        if self.net:
            info('*** Stopping network\n')
            self.net.stop()

    def get_host(self, name):
        """Get a host by name."""
        return self.hosts.get(name)

    def get_switch(self, name):
        """Get a switch by name."""
        return self.switches.get(name)

    def get_zone_hosts(self, zone_name):
        """Get all hosts in a zone."""
        zone_cfg = self.ZONES.get(zone_name, {})
        prefix = zone_cfg.get('prefix', '')
        return [h for name, h in self.hosts.items() if name.startswith(prefix)]

    def get_zone_switch(self, zone_name):
        """Get the access switch for a zone."""
        zone_cfg = self.ZONES.get(zone_name, {})
        return self.switches.get(zone_cfg.get('switch'))

    def _update_topology_state(self):
        """Update topology state for dashboard consumption."""
        nodes = []
        links = []

        # Collect switch info
        for name, sw in self.switches.items():
            dpid = sw.dpid if hasattr(sw, 'dpid') else name
            nodes.append({
                'id': name,
                'type': 'switch',
                'dpid': str(dpid),
                'label': self._switch_label(name),
            })

        # Collect host info
        for name, host in self.hosts.items():
            ip = host.IP() if host.IP() else ''
            mac = ''
            try:
                mac = host.MAC()
            except Exception:
                pass
            zone = self._host_zone(name)
            nodes.append({
                'id': name,
                'type': 'host',
                'ip': ip,
                'mac': mac,
                'zone': zone,
                'label': self._host_label(name),
            })

        # Collect link info
        for link in self.net.links:
            i1 = link.intf1
            i2 = link.intf2
            bw = 0
            for attr in ('bw',):
                for intf in (i1, i2):
                    params = getattr(intf, 'params', {})
                    if isinstance(params, dict) and attr in params:
                        bw = params[attr]
                        break
            links.append({
                'src': i1.node.name,
                'dst': i2.node.name,
                'src_intf': i1.name,
                'dst_intf': i2.name,
                'bw_mbps': bw,
            })

        self.topology_state = {
            'ts': time.time(),
            'nodes': nodes,
            'links': links,
        }

        # Write state file for dashboard
        state_file = os.environ.get('CAMPUS_TOPOLOGY_STATE_FILE', '/tmp/campus_topology_state.json')
        try:
            tmp = state_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self.topology_state, f, indent=2)
            os.replace(tmp, state_file)
        except Exception as e:
            error(f'Failed to write topology state: {e}\n')

    @staticmethod
    def _switch_label(name):
        labels = {
            'cs1': 'Core Switch',
            'ds1': 'Distribution Switch 1',
            'ds2': 'Distribution Switch 2',
            'as1': 'Staff LAN Switch',
            'as2': 'Server Zone Switch',
            'as3': 'IT Lab Switch',
            'as4': 'Student WiFi Switch',
        }
        return labels.get(name, name)

    @staticmethod
    def _host_label(name):
        labels = {
            'h_mis': 'MIS Server',
            'h_dhcp': 'DHCP Server',
            'h_auth': 'Auth Server',
            'h_moodle': 'Moodle Server',
        }
        if name in labels:
            return labels[name]
        if name.startswith('h_staff'):
            return f"Staff PC {name.split('h_staff')[1]}"
        if name.startswith('h_lab'):
            return f"Lab PC {name.split('h_lab')[1]}"
        if name.startswith('h_wifi'):
            return f"WiFi Device {name.split('h_wifi')[1]}"
        return name

    @staticmethod
    def _host_zone(name):
        if name.startswith('h_staff'):
            return 'staff_lan'
        if name.startswith('h_srv') or name.startswith('h_mis') or name.startswith('h_dhcp') or name.startswith('h_auth') or name.startswith('h_moodle'):
            return 'server_zone'
        if name.startswith('h_lab'):
            return 'it_lab'
        if name.startswith('h_wifi'):
            return 'student_wifi'
        return 'unknown'

    def run_pingall(self):
        """Run pingall and return results."""
        if not self.net:
            return {'ok': False, 'error': 'Network not started'}
        results = []
        hosts = sorted(self.net.hosts, key=lambda h: h.name)
        total_sent = 0
        total_recv = 0
        for src in hosts:
            for dst in hosts:
                if src == dst:
                    continue
                raw = src.cmd(f'ping -c1 -W1 {dst.IP()}')
                sent, recv, loss, rtt = self._parse_ping(raw)
                total_sent += sent
                total_recv += recv
                results.append({
                    'src': src.name, 'dst': dst.name,
                    'sent': sent, 'received': recv,
                    'loss_pct': loss, 'avg_rtt_ms': rtt,
                })
        overall_loss = 100.0 * (total_sent - total_recv) / max(1, total_sent)
        return {
            'ok': True,
            'ts': time.time(),
            'packet_loss_pct': round(overall_loss, 2),
            'pairs': results[:200],
        }

    @staticmethod
    def _parse_ping(raw):
        import re
        sent, recv, loss, rtt = 1, 0, 100.0, 0.0
        m = re.search(r'(\d+)\s+packets transmitted,\s+(\d+)\s+received', raw)
        if m:
            sent = int(m.group(1))
            recv = int(m.group(2))
            loss = 100.0 * (sent - recv) / max(1, sent)
        m2 = re.search(r'rtt min/avg/max/mdev = [\d.]+/([\d.]+)/', raw)
        if m2 and recv > 0:
            rtt = float(m2.group(1))
        return sent, recv, round(loss, 2), round(rtt, 3)


class TopologyRuntimeAPI:
    """HTTP API for dashboard to interact with live topology."""

    def __init__(self, topo, host='0.0.0.0', port=9091):
        self.topo = topo
        self.host = host
        self.port = port
        self.server = None

    def start(self):
        handler = self._make_handler(self.topo)
        self.server = ThreadingHTTPServer((self.host, self.port), handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        info(f'*** Topology API listening on http://{self.host}:{self.port}\n')

    @staticmethod
    def _make_handler(topo):
        class Handler(BaseHTTPRequestHandler):
            _topo = topo
            def log_message(self, *args): pass

            def _send_json(self, data, code=200):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == '/health':
                    self._send_json({'ok': True, 'service': 'topology_runtime'})
                elif self.path == '/topology':
                    self._topo._update_topology_state()
                    self._send_json(self._topo.topology_state)
                else:
                    self._send_json({'error': 'not found'}, 404)

            def do_POST(self):
                if self.path == '/pingall':
                    result = self._topo.run_pingall()
                    self._send_json(result)
                elif self.path == '/link_down':
                    body = self._read_body()
                    src = body.get('src', 'ds1')
                    dst = body.get('dst', 'cs1')
                    try:
                        link = None
                        for l in self._topo.net.links:
                            if (l.intf1.node.name == src and l.intf2.node.name == dst) or \
                               (l.intf1.node.name == dst and l.intf2.node.name == src):
                                link = l
                                break
                        if link:
                            link.intf1.ifconfig('down')
                            link.intf2.ifconfig('down')
                            self._send_json({'ok': True, 'msg': f'Link {src}-{dst} down'})
                        else:
                            self._send_json({'ok': False, 'error': f'Link {src}-{dst} not found'}, 404)
                    except Exception as e:
                        self._send_json({'ok': False, 'error': str(e)}, 500)
                elif self.path == '/link_up':
                    body = self._read_body()
                    src = body.get('src', 'ds1')
                    dst = body.get('dst', 'cs1')
                    try:
                        for l in self._topo.net.links:
                            if (l.intf1.node.name == src and l.intf2.node.name == dst) or \
                               (l.intf1.node.name == dst and l.intf2.node.name == src):
                                l.intf1.ifconfig('up')
                                l.intf2.ifconfig('up')
                                self._send_json({'ok': True, 'msg': f'Link {src}-{dst} up'})
                                return
                        self._send_json({'ok': False, 'error': 'Link not found'}, 404)
                    except Exception as e:
                        self._send_json({'ok': False, 'error': str(e)}, 500)
                else:
                    self._send_json({'error': 'not found'}, 404)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                self.end_headers()

            def _read_body(self):
                n = int(self.headers.get('Content-Length', 0))
                if n > 0:
                    return json.loads(self.rfile.read(n))
                return {}

        return Handler


def main():
    parser = argparse.ArgumentParser(description='Tumba College Campus SDN Topology')
    parser.add_argument('--controller-ip', default='127.0.0.1')
    parser.add_argument('--controller-port', type=int, default=6653)
    parser.add_argument('--api-port', type=int, default=9091)
    parser.add_argument('--no-cli', action='store_true')
    args = parser.parse_args()

    setLogLevel('info')

    topo = TumbaCollegeTopo(
        controller_ip=args.controller_ip,
        controller_port=args.controller_port,
    )
    net = topo.build()
    topo.start()

    # Start runtime API
    api = TopologyRuntimeAPI(topo, port=args.api_port)
    api.start()

    if args.no_cli:
        info('*** Running in headless mode (Ctrl+C to stop)\n')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        CLI(net)

    topo.stop()


if __name__ == '__main__':
    main()
