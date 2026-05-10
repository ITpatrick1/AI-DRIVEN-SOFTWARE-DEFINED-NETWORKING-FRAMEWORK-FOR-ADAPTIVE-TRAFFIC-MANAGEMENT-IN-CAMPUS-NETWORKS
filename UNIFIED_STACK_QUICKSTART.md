# Unified Campus SDN Stack

This repository now runs as one merged system:

- `examples/` provides the main controller, dashboard, topology, DQN, and runtime APIs.
- `examples/timetable_engine.py` provides the timetable-aware scheduling layer.
- `examples/stakeholder_requirements.py` generates live controller, security, and DQN policy files from the Tumba stakeholder survey.
- `run.sh` is the top-level entrypoint.

## Start

```bash
sudo ./run.sh
```

This starts:

- the stakeholder-driven policy profile, when the survey CSV is present
- the timetable engine
- the controller
- the dashboard
- the topology runtime API
- the selected ML mode

Default dashboard URL:

```text
http://<your-lan-ip>:8080
```

## Stop

```bash
sudo ./run.sh stop
```

## Status

```bash
sudo ./run.sh status
```

## Useful environment variables

```bash
CAMPUS_ML_MODE=dqn
CAMPUS_DASHBOARD_PORT=8080
CAMPUS_WEB_HOLD_SECONDS=86400
CAMPUS_USE_STAKEHOLDER_PROFILE=1
CAMPUS_SURVEY_CSV="/path/to/survey.csv"
CAMPUS_ENABLE_TIMETABLE=1
CAMPUS_TIMETABLE_PORT=9092
CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS=1
```

## Notes

- If the stakeholder survey CSV is missing, the launcher clears old generated policy files and falls back to controller defaults.
- The timetable API is started locally on `127.0.0.1` and feeds the controller through the shared state file.
- `examples/stop_web_only_stack.sh` now stops the timetable process too, so the merged stack shuts down cleanly.
- The merged launcher skips the old topology-wide `pingall` gate by default, because the stakeholder-driven zero-trust policy intentionally blocks some cross-zone traffic.
