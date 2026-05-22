"""L2 integration tests — Tier B per `docs/l2/TEST_PLAN.md` §0.

Run via `sudo .venv/bin/pytest tooling/tests/integration/`.
Raw-socket frame I/O requires CAP_NET_RAW; per
`docs/l2/HARNESS.md` §3.3 we run under sudo for now.
"""
