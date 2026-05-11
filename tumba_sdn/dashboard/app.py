#!/usr/bin/env python3
"""Tumba College SDN Dashboard — Flask + SocketIO"""
import json, os, time, threading
import urllib.request
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['SECRET_KEY'] = 'tumba-sdn-2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

METRICS       = os.environ.get('CAMPUS_METRICS_FILE',        '/tmp/campus_metrics.json')
ML_ACTION     = os.environ.get('CAMPUS_ML_ACTION_FILE',      '/tmp/campus_ml_action.json')
TIMETABLE     = os.environ.get('CAMPUS_TIMETABLE_STATE',     '/tmp/campus_timetable_state.json')
TOPO_STATE    = os.environ.get('CAMPUS_TOPOLOGY_STATE_FILE', '/tmp/campus_topology_state.json')
PC_ACTIVITIES = os.environ.get('CAMPUS_PC_ACTIVITIES_FILE',  '/tmp/campus_pc_activities.json')
BASELINE      = os.environ.get('CAMPUS_BASELINE_FILE',       '/tmp/campus_baseline.json')
TOPO_API      = os.environ.get('CAMPUS_TOPO_API',            'http://127.0.0.1:9091')
PCAM_API      = os.environ.get('CAMPUS_PCAM_API',            'http://127.0.0.1:9095')

def _read(path):
    try:
        with open(path) as f: return json.load(f)
    except: return {}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/metrics')
def api_metrics():
    return jsonify(_read(METRICS))

@app.route('/api/ml_action')
def api_ml():
    return jsonify(_read(ML_ACTION))

@app.route('/api/timetable')
def api_timetable():
    return jsonify(_read(TIMETABLE))

@app.route('/api/topology')
def api_topology():
    return jsonify(_read(TOPO_STATE))

@app.route('/api/health')
def api_health():
    m = _read(METRICS)
    return jsonify({'ok': True, 'ts': time.time(),
        'switches': len(m.get('connected_switches', [])),
        'has_metrics': bool(m)})

@app.route('/api/security')
def api_security():
    m = _read(METRICS)
    return jsonify({'ddos_active': m.get('ddos_active', False),
        'security_blocked': m.get('security_blocked', 0),
        'events': m.get('events', [])[-20:]})

@app.route('/api/scenario', methods=['POST'])
def api_scenario():
    data = request.get_json() or {}
    scenario = data.get('scenario', 'congestion')
    # Write scenario trigger file
    try:
        with open('/tmp/campus_scenario_trigger.json', 'w') as f:
            json.dump({'scenario': scenario, 'ts': time.time()}, f)
        return jsonify({'ok': True, 'scenario': scenario})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/pingall', methods=['POST'])
def api_pingall():
    """Proxy pingall request to topology runtime API."""
    try:
        req = urllib.request.Request(
            f'{TOPO_API}/pingall',
            data=b'{}',
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return jsonify(data)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 502

# ─── PC Activity Manager routes ───────────────────────────────────────────────

@app.route('/api/pc_activities')
def api_pc_activities():
    """Return current per-PC activity state (reads state file directly)."""
    return jsonify(_read(PC_ACTIVITIES))

@app.route('/api/baseline')
def api_baseline():
    """Return captured baseline snapshot."""
    return jsonify(_read(BASELINE))

def _pcam_post(path: str, data: dict) -> tuple[dict, int]:
    """Forward a POST to the PC Activity Manager process."""
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            f'{PCAM_API}{path}',
            data=body,
            headers={'Content-Type': 'application/json'},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read()), 200
    except Exception as e:
        # Fall back: write to state file so UI shows the change optimistically
        return {'ok': False, 'error': str(e), 'note': 'pcam not running'}, 200

@app.route('/api/set_activity', methods=['POST'])
def api_set_activity():
    """Set activity for a single PC — proxied to PC Activity Manager."""
    data = request.get_json() or {}
    result, code = _pcam_post('/set_activity', data)
    return jsonify(result), code

@app.route('/api/capture_baseline', methods=['POST'])
def api_capture_baseline():
    """Snapshot current metrics as baseline for before/after comparison."""
    result, code = _pcam_post('/capture_baseline', {})
    return jsonify(result), code

@app.route('/api/reset_activities', methods=['POST'])
def api_reset_activities():
    """Reset all PCs to idle."""
    result, code = _pcam_post('/reset_all', {})
    return jsonify(result), code

@app.route('/api/set_scenario', methods=['POST'])
def api_set_scenario():
    """Apply a predefined multi-PC activity scenario."""
    data = request.get_json() or {}
    result, code = _pcam_post('/set_scenario', data)
    return jsonify(result), code

def _push_metrics():
    while True:
        time.sleep(2)
        m = _read(METRICS)
        if m:
            socketio.emit('metrics_update', m)
        pc = _read(PC_ACTIVITIES)
        if pc:
            socketio.emit('pc_activities_update', pc)

def run(host='0.0.0.0', port=9090):
    t = threading.Thread(target=_push_metrics, daemon=True)
    t.start()
    print(f"Dashboard: http://{host}:{port}")
    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=9090)
    args = p.parse_args()
    run(port=args.port)
