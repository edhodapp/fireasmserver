"""L2 integration test harness for fireasmserver.

Production-bar Tier B test infrastructure per
`docs/l2/HARNESS.md`. Boots a fireasmserver guest under Firecracker
with a tap-backed virtio-net device, injects frames onto the tap,
captures frames egressing the tap, reads the guest's serial output,
and asserts against the L2 protocol spec.

This package is the *harness library* — fixtures and primitives.
The tests that use it live in `tooling/tests/integration/`.
"""

__all__ = [
    "firecracker",
    "tap0",
    "serial",
    "frames",
    "capture",
]
