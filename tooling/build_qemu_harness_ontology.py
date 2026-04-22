#!/usr/bin/env python3
"""Build the ontology DAG for the QEMU test harness.

Now importing from the forked ``ontology`` module under
``tooling/src/ontology/`` (O1, 2026-04-19) instead of
``python_agent.ontology``. See O-series commits on ``main`` for
the fork rationale; short version is the SysE-grade schema
extensions need to land in this project without coordination
through the python_agent session.
"""

from ontology import (
    ClassSpec,
    DomainConstraint,
    Entity,
    ExternalDependency,
    FunctionSpec,
    ModuleSpec,
    Ontology,
    PerformanceConstraint,
    Property,
    PropertyType,
    Relationship,
    VerificationCase,
)
from ontology.dag import (
    dag_transaction,
    git_snapshot_label,
    snapshot_if_changed,
)

DAG_PATH = "tooling/qemu-harness.json"
PROJECT_NAME = "fireasmserver-qemu-harness"

# -- Problem Domain: Entities --

guest_image = Entity(
    id="guest-image",
    name="GuestImage",
    description=(
        "An assembled and linked bare-metal binary "
        "that boots directly in a VM. Produced by GNU as + "
        "GNU ld from arch/<isa>/platform/<vm>/ sources."
    ),
    properties=[
        Property(
            name="path",
            property_type=PropertyType(kind="str"),
            description=(
                "Filesystem path to the ELF/flat binary. "
                "Validated: no '..' traversal, resolved "
                "to absolute."
            ),
        ),
        Property(
            name="arch",
            property_type=PropertyType(
                kind="enum", reference=["x86_64", "aarch64"],
            ),
            description="Target ISA",
        ),
        Property(
            name="platform",
            property_type=PropertyType(
                kind="enum",
                reference=["qemu", "firecracker"],
            ),
            description=(
                "Target VM platform. Literal type -- "
                "Pydantic rejects invalid values."
            ),
        ),
    ],
)

vm_instance = Entity(
    id="vm-instance",
    name="VMInstance",
    description=(
        "A running QEMU or Firecracker process hosting a "
        "guest image. Launched in the background, polled for "
        "readiness, killed cleanly after tests complete. "
        "Popen object tracked in a thread-safe registry "
        "keyed by PID. Never use timeout to kill QEMU -- "
        "background + poll + kill."
    ),
    properties=[
        Property(
            name="pid",
            property_type=PropertyType(kind="int"),
            description=(
                "Host OS process ID. Popen object stored "
                "separately in _proc_registry (not "
                "Pydantic-serializable)."
            ),
        ),
        Property(
            name="serial_path",
            property_type=PropertyType(kind="str"),
            description=(
                "Path to serial output file "
                "(-serial file:<path>). Validated: no "
                "'..' traversal. Truncated on launch."
            ),
        ),
        Property(
            name="stderr_path",
            property_type=PropertyType(kind="str"),
            description=(
                "Path to VMM stderr capture file. "
                "Created alongside serial_path as "
                "<serial_path>.stderr. Truncated on launch."
            ),
        ),
        Property(
            name="platform",
            property_type=PropertyType(
                kind="enum",
                reference=["qemu", "firecracker"],
            ),
        ),
        Property(
            name="arch",
            property_type=PropertyType(
                kind="enum", reference=["x86_64", "aarch64"],
            ),
        ),
    ],
)

test_case = Entity(
    id="test-case",
    name="TestCase",
    description=(
        "A single verification to run against a booted VM "
        "guest. Checks are performed from the host side -- "
        "either parsing serial output or making HTTP "
        "requests to the guest."
    ),
    properties=[
        Property(
            name="name",
            property_type=PropertyType(kind="str"),
            description="Human-readable test name",
        ),
        Property(
            name="check_type",
            property_type=PropertyType(
                kind="enum",
                reference=["serial", "http"],
            ),
            description=(
                "serial: check serial output file for "
                "expected markers. http: make HTTP request "
                "and verify response."
            ),
        ),
        Property(
            name="expected",
            property_type=PropertyType(kind="str"),
            description="Expected output or response content",
        ),
    ],
)

test_result = Entity(
    id="test-result",
    name="TestResult",
    description="Pass/fail outcome of a single test case.",
    properties=[
        Property(
            name="passed",
            property_type=PropertyType(kind="bool"),
        ),
        Property(
            name="actual",
            property_type=PropertyType(kind="str"),
            description="Actual output received",
        ),
        Property(
            name="message",
            property_type=PropertyType(kind="str"),
            description="Failure detail if not passed",
            required=False,
        ),
    ],
)

# Perf-budget anchor entities. Each exists so a
# PerformanceConstraint can reference a concrete thing that owns
# the budget, per D049's first-class-referents rule.

fsa_transition = Entity(
    id="fsa-transition",
    name="FSATransition",
    description=(
        "A single state transition in an FSA automaton under "
        "D043's runtime model. One transition is the unit "
        "against which FSA_TRANSITION_BUDGET_NS is enforced: "
        "handler-body execution plus dispatch bookkeeping."
    ),
    properties=[
        Property(
            name="from_state",
            property_type=PropertyType(kind="int"),
            description="Current state index before the transition.",
        ),
        Property(
            name="event_code",
            property_type=PropertyType(kind="int"),
            description="Event that triggered the transition.",
        ),
        Property(
            name="to_state",
            property_type=PropertyType(kind="int"),
            description="State index the automaton moves to.",
        ),
    ],
)

ethernet_frame = Entity(
    id="ethernet-frame",
    name="EthernetFrame",
    description=(
        "A single Ethernet II frame crossing the L2 boundary. "
        "Per ETH-001..ETH-005, fixed 14-byte header + 46..1500 "
        "payload + 4-byte FCS. The unit against which the L2 "
        "throughput budgets (1 Gbps floor, 10 Gbps target per "
        "D040) are enforced on both RX and TX paths."
    ),
    properties=[
        Property(
            name="length",
            property_type=PropertyType(kind="int"),
            description=(
                "Total frame length including header and FCS, in "
                "bytes. 64..1518 untagged, 64..1522 with VLAN."
            ),
        ),
        Property(
            name="ether_type",
            property_type=PropertyType(kind="int"),
            description=(
                "Big-endian 16-bit EtherType (≥ 0x0600) or "
                "802.3 length (< 0x0600; out of scope per "
                "ETH-002)."
            ),
        ),
    ],
)

# -- Problem Domain: virtio-net device -----------------------------
#
# The MMIO-addressable virtio-net endpoint exposed by Firecracker.
# Anchor for VIO-* / VIO-F-* / VIO-Q-* / VIO-R-* / VIO-T-*
# constraints in the L2 requirements.
virtio_net_device = Entity(
    id="virtio-net-device",
    name="VirtioNetDevice",
    description=(
        "The virtio-net device exposed by Firecracker at MMIO "
        "0xC0001000 (MEM_32BIT_DEVICES per the VMM layout). The "
        "L2 driver owns the full Virtio 1.2 §2.1.2 init sequence "
        "(reset → ACKNOWLEDGE → DRIVER → feature negotiation → "
        "FEATURES_OK → virtqueue init → DRIVER_OK) plus the "
        "split-virtqueue descriptor / available / used rings "
        "that upper layers consume for RX and TX."
    ),
    properties=[
        Property(
            name="mmio_base",
            property_type=PropertyType(kind="int"),
            description=(
                "Physical MMIO base address. 0xC0001000 on "
                "Firecracker x86_64 per MEM_32BIT_DEVICES."
            ),
        ),
        Property(
            name="queue_size",
            property_type=PropertyType(kind="int"),
            description=(
                "Driver-negotiated virtqueue size, "
                "min(QueueNumMax, VIRTQ_MAX_SIZE=256). Both "
                "RX and TX queues share this value in MVP."
            ),
        ),
    ],
)

# The ARP packet format (RFC 826). Anchor for ARP-001..011 in
# L2 requirements. ARP runs over Ethernet but its packet shape
# is a distinct object from the frame that carries it, so
# ARP-002 ("HTYPE=1, PTYPE=0x0800, HLEN=6, PLEN=4") and peers
# get their own entity rather than being shoehorned into
# ``ethernet-frame``.
arp_packet = Entity(
    id="arp-packet",
    name="ARPPacket",
    description=(
        "A single ARP packet per RFC 826, carried inside an "
        "Ethernet frame with EtherType 0x0806. Fixed-layout "
        "header (HTYPE, PTYPE, HLEN, PLEN, OP) followed by "
        "Sender HW / Sender Protocol / Target HW / Target "
        "Protocol addresses."
    ),
    properties=[
        Property(
            name="operation",
            property_type=PropertyType(
                kind="enum", reference=["request", "reply"],
            ),
            description=(
                "OP field: 1=REQUEST, 2=REPLY (RFC 826)."
            ),
        ),
        Property(
            name="sender_hw_addr",
            property_type=PropertyType(kind="str"),
            description=(
                "6-byte Sender Hardware Address (MAC)."
            ),
        ),
        Property(
            name="sender_protocol_addr",
            property_type=PropertyType(kind="str"),
            description=(
                "4-byte Sender Protocol Address (IPv4)."
            ),
        ),
        Property(
            name="target_hw_addr",
            property_type=PropertyType(kind="str"),
            description=(
                "6-byte Target Hardware Address (MAC). "
                "Zero-filled on REQUEST, populated on REPLY."
            ),
        ),
        Property(
            name="target_protocol_addr",
            property_type=PropertyType(kind="str"),
            description=(
                "4-byte Target Protocol Address (IPv4). "
                "Anchor for ARP-004 / ARP-011 local-IP checks."
            ),
        ),
    ],
)


observability_event_site = Entity(
    id="observability-event-site",
    name="ObservabilityEventSite",
    description=(
        "A single OBS_EMIT macro instance in the runtime code. "
        "Each site bears a fixed cost when observability is "
        "disabled, a slightly higher cost when enabled but "
        "category-gated-off, and the intrinsic emission cost "
        "when both verbosity and category pass. Per the "
        "observability proposal (docs/observability.md, "
        "pre-D050) and D046's assembly-deferral bar, the "
        "disabled-path cost must stay inside a tight budget "
        "so shipping observability everywhere doesn't erode "
        "the L2 frame-rate target."
    ),
    properties=[
        Property(
            name="category",
            property_type=PropertyType(kind="str"),
            description=(
                "Category name: core / uart / virtio / fsa / "
                "eth / arp / ip / tcp / http / timer / "
                "perf_sample / reserved."
            ),
        ),
        Property(
            name="min_level",
            property_type=PropertyType(kind="int"),
            description=(
                "Lowest verbosity level at which this site "
                "emits (0=off, 1=error, 2=warn, 3=info, "
                "4=debug, 5=trace)."
            ),
        ),
    ],
)

# -- Problem Domain: Relationships --

boots_rel = Relationship(
    source_entity_id="vm-instance",
    target_entity_id="guest-image",
    name="boots",
    cardinality="many_to_one",
    description="A VM instance boots exactly one guest image",
)

runs_against_rel = Relationship(
    source_entity_id="test-case",
    target_entity_id="vm-instance",
    name="runs_against",
    cardinality="many_to_one",
    description="Test cases run against a booted VM instance",
)

produces_rel = Relationship(
    source_entity_id="test-case",
    target_entity_id="test-result",
    name="produces",
    cardinality="one_to_one",
    description="Each test case produces one result",
)

# -- Problem Domain: Constraints --

constraints = [
    DomainConstraint(
        name="no-native-execution",
        description=(
            "Assembly code must NEVER be executed natively "
            "on the host. All execution happens inside a VM "
            "(QEMU or Firecracker)."
        ),
        entity_ids=["guest-image", "vm-instance"],
        rationale=(
            "D003 (100% assembly) — host-native execution of "
            "bare-metal targets is undefined and dangerous; "
            "a VM is the boundary."
        ),
        implementation_refs=[
            "tooling/src/qemu_harness/vm_launcher.py:launch_vm",
            "tooling/tracer_bullet/run_local.sh",
        ],
        verification_refs=[
            "tooling/tests/test_vm_launcher.py",
            "tooling/hooks/pre_push.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="clean-vm-kill",
        description=(
            "Never use timeout to kill QEMU. Launch in "
            "background, poll serial output file, then kill "
            "cleanly via Popen.terminate/kill when registry "
            "entry exists, or bare PID signals with waitpid "
            "as fallback. Use -serial file:<path> not "
            "-serial mon:stdio."
        ),
        entity_ids=["vm-instance"],
        rationale=(
            "CLAUDE.md QEMU gotchas: `timeout` doesn't flush "
            "`-serial file:` output; mon:stdio breaks under "
            "stdout redirection."
        ),
        implementation_refs=[
            "tooling/src/qemu_harness/vm_launcher.py:kill_vm",
            "tooling/src/qemu_harness/vm_launcher.py:_proc_registry",
        ],
        verification_refs=[
            "tooling/tests/test_vm_launcher.py",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="host-side-verification",
        description=(
            "All test verification is from the host side. "
            "The guest has no test infrastructure inside "
            "it -- it just does what it does, and the host "
            "observes. Serial output read in binary mode "
            "with errors='replace' to handle non-UTF-8."
        ),
        entity_ids=["test-case", "test-result"],
        rationale=(
            "Bare-metal assembly targets can't host pytest; "
            "the host observes serial output + exit signals "
            "and interprets them as test outcomes."
        ),
        implementation_refs=[
            "tooling/src/qemu_harness/test_runner.py",
            "tooling/tracer_bullet/run_local.sh",
        ],
        verification_refs=[
            "tooling/tests/test_test_runner.py",
            "tooling/tests/test_crc32_ieee.py",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="kvm-required-for-firecracker",
        description=(
            "Firecracker requires /dev/kvm and only works "
            "for the native architecture. Skip gracefully "
            "if KVM is not available."
        ),
        entity_ids=["vm-instance"],
        rationale=(
            "GHA hosted arm64 runners don't expose /dev/kvm, "
            "so the aarch64/firecracker cell can't run in CI; "
            "it's exercised locally via the Pi-side tracer "
            "bullet instead."
        ),
        implementation_refs=[
            "tooling/tracer_bullet/run_local.sh",
            ".github/workflows/cd-matrix.yml",
        ],
        verification_refs=[
            "tooling/tracer_bullet/pi_aarch64_firecracker.sh",
            "tooling/hooks/pre_push.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="path-traversal-rejected",
        description=(
            "All file paths (image_path, serial_path) are "
            "validated to reject '..' components and "
            "resolved to absolute form. Pydantic "
            "field_validator enforces at construction."
        ),
        entity_ids=["guest-image", "vm-instance"],
        rationale=(
            "Defensive against accidental or adversarial "
            "path traversal in test-harness inputs; pydantic "
            "validation is the single chokepoint all VM "
            "configs pass through."
        ),
        implementation_refs=[
            "tooling/src/qemu_harness/vm_launcher.py:_reject_traversal",
            "tooling/src/qemu_harness/vm_launcher.py:VMConfig",
        ],
        verification_refs=[
            "tooling/tests/test_vm_launcher.py",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="blocked-qemu-args",
        description=(
            "extra_args on VMConfig are validated against "
            "a blocklist of dangerous QEMU flags: "
            "-monitor, -vnc, -chardev, -netdev, -nic, "
            "-spice. Pydantic field_validator rejects "
            "at construction."
        ),
        entity_ids=["vm-instance"],
        rationale=(
            "Blocklist prevents test harnesses from opening "
            "monitor/vnc/network surfaces that could expose "
            "the host. Noted as brittle (allowlist would be "
            "stronger) in Gemini review; developer-tool scope "
            "keeps blocklist acceptable for now."
        ),
        implementation_refs=[
            "tooling/src/qemu_harness/vm_launcher.py:VMConfig",
        ],
        verification_refs=[
            "tooling/tests/test_vm_launcher.py",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="thread-safe-registry",
        description=(
            "The Popen process registry is protected by "
            "threading.Lock. The Python harness runs on "
            "Linux where concurrent launch_vm/kill_vm "
            "from different threads is a real scenario."
        ),
        entity_ids=["vm-instance"],
        rationale=(
            "Python harness is host-side code running on "
            "Linux under stdlib threading; concurrent "
            "launch_vm/kill_vm from different threads is a "
            "real scenario (pytest-xdist, concurrent "
            "integration runs). Bare-metal single-threaded "
            "FSA reasoning does NOT apply here per "
            "`feedback_tooling_is_not_target.md`."
        ),
        implementation_refs=[
            "tooling/src/qemu_harness/vm_launcher.py:_proc_lock",
            "tooling/src/qemu_harness/vm_launcher.py:_register_proc",
            "tooling/src/qemu_harness/vm_launcher.py:_unregister_proc",
        ],
        verification_refs=[
            "tooling/tests/test_vm_launcher.py",
        ],
        status="implemented",
    ),
    # -- L2 product constraints (2026-04-22 authoring pass) --
    # Virtio 1.2 §2.1.2 init sequence, implemented on
    # x86_64/firecracker. Each row carries impl_refs into
    # boot.S's fail-path labels (the diagnostic endpoints that
    # prove the happy path reached THAT step) and verify_refs
    # into the tracer-bullet marker-check it produces.
    DomainConstraint(
        name="VIO-001",
        description=(
            "On init, reset the device by writing 0 to the "
            "Device Status MMIO register (Virtio 1.2 §2.1.2 "
            "step 1). Post-reset state verified via the "
            "shared .status_fail endpoint (VIO-003)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="D038 L2 methodology; Virtio 1.2 §2.1.2 step 1.",
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:.status_fail",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-002",
        description=(
            "Set ACKNOWLEDGE (bit 0) in Status after reset "
            "(Virtio 1.2 §2.1.2 step 2). Post-ACK state "
            "verified via the shared .status_fail endpoint "
            "(VIO-003)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.1.2 step 2.",
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:.status_fail",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-003",
        description=(
            "Set DRIVER (bit 1) in Status after ACKNOWLEDGE "
            "(Virtio 1.2 §2.1.2 step 3). Full expected state "
            "after steps 1-3 cross-checked via equality compare "
            "against ACK|DRIVER (0x03); mismatch routes through "
            ".status_fail and VIO-009."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.1.2 step 3.",
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:.status_fail",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-004",
        description=(
            "Read device feature bits via DeviceFeaturesSel / "
            "DeviceFeatures, select the driver's subset, write "
            "it back via DriverFeaturesSel / DriverFeatures "
            "(Virtio 1.2 §2.1.2 step 4). MVP subset is "
            "VIRTIO_F_VERSION_1 only; VIO-F-001 governs it, "
            "VIO-F-002 and VIO-F-006 are declared deviations, "
            "and VIO-F-003..005/007 remain in REQUIREMENTS.md "
            "pending formalisation into the ontology."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.1.2 step 4.",
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:"
            ".features_fail_no_v1",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-005",
        description=(
            "Set FEATURES_OK (bit 3) in Status after driver-"
            "features write (Virtio 1.2 §2.1.2 step 5)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.1.2 step 5.",
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:"
            ".features_fail_rejected",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-006",
        description=(
            "Re-read Status; MUST abort init if FEATURES_OK is "
            "not still set (Virtio 1.2 §2.1.2 step 6). Equality "
            "compare against ACK|DRIVER|FEATURES_OK catches both "
            "FEATURES_OK-cleared and unexpected stray bits."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.1.2 step 6.",
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:"
            ".features_fail_rejected",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-007",
        description=(
            "Read device-specific config, discover + initialize "
            "virtqueues (Virtio 1.2 §2.1.2 step 7). Slice 1: "
            "QueueSel + QueueNumMax discovery emits "
            "QUEUES:RX=<hex> TX=<hex>. Slice 2: QueueNum "
            "(clamped to VIRTQ_MAX_SIZE=256), QueueDesc / "
            "QueueDriver / QueueDevice physical addresses "
            "programmed, QueueReady toggled with read-back "
            "verify. Emits QUEUES:READY on success."
        ),
        entity_ids=["virtio-net-device"],
        rationale=(
            "Virtio 1.2 §2.1.2 step 7 + §4.2.2 register "
            "semantics. Memory allocation per D043 (static "
            "pools)."
        ),
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:"
            ".queue_ready_fail",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-008",
        description=(
            "Set DRIVER_OK (bit 2) in Status; device is now "
            "live (Virtio 1.2 §2.1.2 step 8). Read-back equality-"
            "compares against ACK|DRIVER|FEATURES_OK|DRIVER_OK "
            "(0x0F); mismatch routes through .driver_ok_fail + "
            "VIO-009."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.1.2 step 8.",
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:"
            ".driver_ok_fail",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-009",
        description=(
            "On any fatal driver error, set FAILED (bit 7) in "
            "Status (Virtio 1.2 §2.1.2). Shared .set_failed "
            "helper is the single halt endpoint for every "
            "post-MagicValue failure path — .status_fail, "
            ".features_fail_no_v1, .features_fail_rejected, "
            ".queue_fail_rx, .queue_fail_tx, .queue_ready_fail, "
            ".driver_ok_fail all route through it. "
            ".virtio_fail bypasses it: on a MagicValue "
            "mismatch the device's register layout is "
            "unverified and a Status write would be blind."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.1.2 error-handling requirement.",
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:.set_failed",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-F-001",
        description=(
            "MUST negotiate VIRTIO_F_VERSION_1 (bit 32) — we "
            "are a modern driver (Virtio 1.2 §6). MVP driver-"
            "features subset is VERSION_1 only; everything else "
            "(MAC / STATUS / MQ / CSUM / GSO / EVENT_IDX) stays "
            "off until the code path that consumes it lands."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §6. Legacy / transitional not supported.",
        implementation_refs=[
            "arch/x86_64/platform/firecracker/boot.S:"
            ".features_fail_no_v1",
        ],
        verification_refs=[
            "tooling/tracer_bullet/run_local.sh",
        ],
        status="implemented",
    ),
    DomainConstraint(
        name="VIO-F-002",
        description=(
            "MUST negotiate VIRTIO_NET_F_MAC (bit 5) if the "
            "device offers it, and read MAC from config space "
            "(Virtio 1.2 §5.1.3)."
        ),
        entity_ids=["virtio-net-device"],
        rationale=(
            "DEVIATION: MVP feature-negotiation commit "
            "(2026-04-19) accepts no device-specific features. "
            "MAC read is deferred to the VIO-007 device-config "
            "follow-up. Until that lands, the driver acts as "
            "if the device did not offer MAC — a locally-"
            "administered random MAC would be generated if "
            "this shipped. Tracked so it doesn't silently "
            "become a latent bug."
        ),
        implementation_refs=[],
        verification_refs=[],
        status="deviation",
    ),
    DomainConstraint(
        name="VIO-F-006",
        description=(
            "SHOULD NOT negotiate VIRTIO_NET_F_CSUM / "
            "VIRTIO_NET_F_GUEST_CSUM unless our TCP/UDP stack "
            "handles partial checksums (Virtio 1.2 §5.1.3)."
        ),
        entity_ids=["virtio-net-device"],
        rationale=(
            "DEVIATION by spec intent: L4 checksum handling is "
            "a future layer. Leaving CSUM unnegotiated means "
            "the device computes full checksums and the driver "
            "presents checksummed frames — simpler MVP posture. "
            "Revisit when L4 TCP/UDP layer lands."
        ),
        implementation_refs=[],
        verification_refs=[],
        status="deviation",
    ),
    # -- L2 formalization pass (2026-04-21): stubs for every L2
    # requirement in docs/l2/REQUIREMENTS.md that did not already
    # have a first-class DomainConstraint row. All carry the
    # REQUIREMENTS.md-declared status. Empty impl/verify refs are
    # intentional — these requirements are not yet implemented;
    # when they are, the committer's L2 work flips status + adds
    # refs. Lets TEST_PLAN.md §9's partial-coverage
    # VerificationCases start citing them.

    # -- Section 1: IEEE 802.3 Ethernet framing (except ETH-005
    # which already has a first-class row below at status=
    # implemented) --
    DomainConstraint(
        name="ETH-001",
        description=(
            "Frame layout: 6-byte DA, 6-byte SA, 2-byte "
            "EtherType/Length, payload, 4-byte FCS."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §3.1.1.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-002",
        description=(
            "EtherType ≥ 0x0600 identifies Ethernet II; smaller "
            "values are 802.3 Length + LLC header. We support "
            "Ethernet II only (LLC/SNAP out of scope)."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §3.2.6; RFC 894.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-003",
        description=(
            "Minimum frame size: 64 bytes including FCS "
            "(512 bit-times). Relaxed to 60 if driver strips FCS."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §3.2.7.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-004",
        description=(
            "Maximum frame size: 1518 bytes untagged, 1522 with "
            "one VLAN tag. Jumbo-frame rules tracked separately."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §3.2.7.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-006",
        description=(
            "MUST accept broadcast DA FF:FF:FF:FF:FF:FF."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §4.2.2.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-007",
        description=(
            "MUST accept multicast DA (group-bit-set) matching "
            "configured filter set (virtio-net multicast MAC "
            "filter list)."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §4.2.2.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-008",
        description=(
            "MUST accept unicast DA equal to the device's "
            "assigned MAC."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §4.2.2.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-009",
        description=(
            "MUST discard frames with incorrect FCS. Typically "
            "signaled by virtio host."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §4.2.4.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-010",
        description=(
            "MUST discard runt frames (<64 bytes incl. FCS). "
            "Guard against runt-length L2 attacks."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §3.2.7.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-011",
        description=(
            "MUST discard oversized frames (>1518 untagged, "
            ">1522 tagged, >jumbo cap if negotiated)."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §3.2.7.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-012",
        description=(
            "MUST pad short outgoing frames to the 64-byte "
            "minimum. Pad byte value unspecified; convention is "
            "zero."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §4.2.3.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-013",
        description=(
            "SHOULD zero-fill padding bytes. Conservative "
            "choice; avoids info leaks."
        ),
        entity_ids=["ethernet-frame"],
        rationale="Convention; no formal IEEE requirement.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-014",
        description=(
            "Inter-frame gap: 96 bit-times at the negotiated "
            "link speed."
        ),
        entity_ids=["ethernet-frame"],
        rationale=(
            "IEEE 802.3-2018 §4.2.3.3. N/A — virtio abstracts "
            "PHY; host handles IFG."
        ),
        status="n_a",
    ),
    DomainConstraint(
        name="ETH-015",
        description=(
            "Source MAC MUST have unicast bit clear (LSb of "
            "first byte = 0)."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §4.1.2.1.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-016",
        description=(
            "MUST NOT emit a frame with source MAC = broadcast "
            "or multicast. Sanity check at TX."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3-2018 §4.1.2.1.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-017",
        description=(
            "MAC address locally-administered bit (2nd LSb of "
            "first byte) is informational only; we don't treat "
            "L/A MACs differently."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802c.",
        status="spec",
    ),
    DomainConstraint(
        name="ETH-018",
        description=(
            "MUST silently discard Ethernet PAUSE frames "
            "(EtherType 0x8808, MAC control opcode 0x0001) and "
            "increment a rx_pause_dropped counter. Acting on "
            "received pause is a separate flow-control module, "
            "explicitly deferred per D045's 'stays deferred' "
            "list."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.3x-1997 §31B.1.",
        status="spec",
    ),

    # -- Section 2: IEEE 802.1Q VLAN tagging --
    DomainConstraint(
        name="VLAN-001",
        description=(
            "4-byte tag inserted after SA: TPID=0x8100, TCI = "
            "3-bit PCP + 1-bit DEI + 12-bit VID. RX parses "
            "unconditionally; TX inserts when "
            "tx_request_t.vid != 0."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.1Q-2022 §9.6; D045.",
        status="spec",
    ),
    DomainConstraint(
        name="VLAN-002",
        description=(
            "Tagged-frame EtherType field is at byte offset 16 "
            "(not 12). RX re-reads EtherType at offset 16 after "
            "tag detection."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.1Q-2022 §9.5.",
        status="spec",
    ),
    DomainConstraint(
        name="VLAN-003",
        description=(
            "VID 0x000 = priority-tagged (no VLAN membership); "
            "0xFFF reserved. VID extracted into per-frame "
            "metadata regardless."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.1Q-2022 §9.6.1.",
        status="spec",
    ),
    DomainConstraint(
        name="VLAN-004",
        description=(
            "PCP field maps to 802.1p priority classes 0–7. "
            "Propagated into per-frame metadata; QoS routing is "
            "upper-layer."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.1Q-2022 §6.9.",
        status="spec",
    ),
    DomainConstraint(
        name="VLAN-005",
        description=(
            "MUST silently discard tagged frames on a port that "
            "is not VLAN-capable."
        ),
        entity_ids=["ethernet-frame"],
        rationale=(
            "IEEE 802.1Q-2022 §8. DEVIATION: D045 reverses this "
            "behavior — we parse tagged frames rather than "
            "discard. Kept for historical reference; obsoleted "
            "by the D045 design."
        ),
        status="deviation",
    ),
    DomainConstraint(
        name="VLAN-006",
        description=(
            "802.1ad Q-in-Q (outer TPID 0x88A8). RX recognizes "
            "outer + inner tag; extracts both VIDs to metadata. "
            "TX can emit Q-in-Q when two vid fields are "
            "non-zero (MVP default: one-level tagging)."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802.1ad-2005.",
        status="spec",
    ),
    DomainConstraint(
        name="VLAN-007",
        description=(
            "802.1Qbb Priority Flow Control (PFC)."
        ),
        entity_ids=["ethernet-frame"],
        rationale=(
            "IEEE 802.1Qbb. DEVIATION: D045 keeps PFC deferred "
            "— additive feature, doesn't reshape parser."
        ),
        status="deviation",
    ),
    DomainConstraint(
        name="VLAN-008",
        description=(
            "VLAN filter management via control queue "
            "(VIRTIO_NET_CTRL_VLAN_ADD / _DEL)."
        ),
        entity_ids=["ethernet-frame"],
        rationale=(
            "Virtio 1.2 §5.1.6.5.3. DEVIATION: D045 keeps "
            "filter management deferred — additive on already-"
            "designed control-queue interface. MVP accepts "
            "all VIDs."
        ),
        status="deviation",
    ),

    # -- Section 3: ARP (RFC 826 / 5227) --
    DomainConstraint(
        name="ARP-001",
        description="ARP frame EtherType = 0x0806.",
        entity_ids=["arp-packet", "ethernet-frame"],
        rationale="RFC 826.",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-002",
        description=(
            "ARP packet: HTYPE=1 (Ethernet), PTYPE=0x0800 "
            "(IPv4), HLEN=6, PLEN=4."
        ),
        entity_ids=["arp-packet"],
        rationale="RFC 826.",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-003",
        description="OP=1 is REQUEST, OP=2 is REPLY.",
        entity_ids=["arp-packet"],
        rationale="RFC 826.",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-004",
        description=(
            "MUST answer an ARP REQUEST whose Target Protocol "
            "Address equals a local IP."
        ),
        entity_ids=["arp-packet"],
        rationale="RFC 826 §3.",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-005",
        description=(
            "MUST update cache entry on REPLY from an address "
            "we have a pending REQUEST for."
        ),
        entity_ids=["arp-packet"],
        rationale="RFC 826 §3.",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-006",
        description=(
            "SHOULD also update cache on any ARP packet whose "
            "Sender HW/Protocol Addresses are in the cache "
            "(opportunistic)."
        ),
        entity_ids=["arp-packet"],
        rationale="RFC 826 §3.",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-007",
        description=(
            "MUST NOT add a cache entry solely on observed "
            "traffic — only on ARP packets."
        ),
        entity_ids=["arp-packet"],
        rationale="RFC 826 §3.",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-008",
        description=(
            "Cache entries MUST age out after an "
            "implementation-defined TTL. Convention: 20 min for "
            "complete, 3 min for incomplete."
        ),
        entity_ids=["arp-packet"],
        rationale="RFC 826 §3 (implicit).",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-009",
        description=(
            "SHOULD emit gratuitous ARP on interface-up / "
            "IP-assignment (ACD per RFC 5227)."
        ),
        entity_ids=["arp-packet"],
        rationale="RFC 5227 §2.1.1.",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-010",
        description=(
            "SHOULD defend local IP against conflicting claim."
        ),
        entity_ids=["arp-packet"],
        rationale="RFC 5227 §2.4.",
        status="spec",
    ),
    DomainConstraint(
        name="ARP-011",
        description=(
            "MUST NOT respond to REQUEST whose Target Protocol "
            "Address is not a local IP."
        ),
        entity_ids=["arp-packet"],
        rationale="RFC 826 §3.",
        status="spec",
    ),

    # -- Section 4.2: Feature negotiation (VIO-F-001/002/006
    # already above; adding the remaining four) --
    DomainConstraint(
        name="VIO-F-003",
        description=(
            "MAY negotiate VIRTIO_NET_F_STATUS (bit 16) to "
            "observe link state."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.3.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-F-004",
        description=(
            "MAY negotiate VIRTIO_NET_F_MQ (bit 22) for "
            "multi-queue."
        ),
        entity_ids=["virtio-net-device"],
        rationale=(
            "Virtio 1.2 §5.1.3. DEVIATION per D053: MVP ships "
            "single-queue (RX queue 0 + TX queue 1). MQ is a "
            "scale-out optimization, not a correctness "
            "requirement; deferred until measured single-queue "
            "throughput stops meeting the D040 frame-rate "
            "targets."
        ),
        status="deviation",
    ),
    DomainConstraint(
        name="VIO-F-005",
        description=(
            "MAY negotiate VIRTIO_NET_F_CTRL_VQ (bit 17) to "
            "expose the control queue. Needed for MAC-filter / "
            "MQ / VLAN config."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.3.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-F-007",
        description=(
            "MUST NOT negotiate VIRTIO_F_EVENT_IDX (bit 29) "
            "unless we implement the event-suppression protocol "
            "correctly. Optimization; may skip for MVP."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.6.7.",
        status="spec",
    ),

    # -- Section 4.3: Virtqueue layout (split virtqueue) --
    DomainConstraint(
        name="VIO-Q-001",
        description=(
            "Split virtqueue has three areas: Descriptor "
            "Table, Available Ring, Used Ring. Packed queues "
            "out of MVP scope."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-Q-002",
        description=(
            "Descriptor Table: array of 16-byte descriptors "
            "(addr, len, flags, next)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.5.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-Q-003",
        description=(
            "Descriptor flags: NEXT=1, WRITE=2, INDIRECT=4."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.5.1.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-Q-004",
        description=(
            "Available Ring: idx + ring[] of descriptor-table "
            "indices; updated by driver."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.6.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-Q-005",
        description=(
            "Used Ring: idx + ring[] of used-descriptor + "
            "length; updated by device."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.8.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-Q-006",
        description=(
            "Queue size MUST be power-of-2, ≤ queue_size "
            "(from device), queried from Common config."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §4.1.4.3.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-Q-007",
        description=(
            "Descriptor Table alignment: 16 bytes; Available "
            "Ring: 2 bytes; Used Ring: 4 bytes. Modern device "
            "uses queue_desc / queue_driver / queue_device "
            "registers."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-Q-008",
        description=(
            "Proper memory barriers around ring-index updates."
        ),
        entity_ids=["virtio-net-device"],
        rationale=(
            "Virtio 1.2 §2.7.11. Cross-references D039 §2 "
            "(DMA/cache coherence); bugs here are of the "
            "'silently corrupts data at high throughput' class, "
            "not the 'returns an error' class."
        ),
        status="spec",
    ),

    # -- Section 4.4: Receive path --
    DomainConstraint(
        name="VIO-R-001",
        description=(
            "Receive queue index = 0 (single-queue) or "
            "0,2,4,... (MQ)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.2.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-R-002",
        description=(
            "Pre-populate RX queue with buffers marked WRITE."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.3.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-R-003",
        description=(
            "Each incoming packet is prefixed with a "
            "virtio_net_hdr (length depends on negotiated "
            "features)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-R-004",
        description=(
            "virtio_net_hdr.flags, .gso_type, .hdr_len, "
            ".gso_size, .csum_start, .csum_offset, "
            ".num_buffers — our handler MUST at minimum read "
            "num_buffers."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.1.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-R-005",
        description=(
            "MUST handle multi-descriptor RX when "
            "num_buffers > 1 (VIRTIO_NET_F_MRG_RXBUF)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.3.1.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-R-006",
        description=(
            "After consuming a descriptor, return it to the "
            "Available Ring."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.13.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-R-007",
        description=(
            "Notify the device of returned buffers via Queue "
            "Notify register (unless EVENT_IDX suppresses it)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.12.",
        status="spec",
    ),

    # -- Section 4.5: Transmit path --
    DomainConstraint(
        name="VIO-T-001",
        description=(
            "Transmit queue index = 1 (single-queue) or "
            "1,3,5,... (MQ)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.2.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-T-002",
        description=(
            "Each outgoing packet MUST be preceded by a "
            "virtio_net_hdr."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.2.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-T-003",
        description=(
            "If VIRTIO_NET_F_CSUM not negotiated, "
            "virtio_net_hdr.flags.NEEDS_CSUM MUST be 0. "
            "Consistent with VIO-F-006 deviation."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.2.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-T-004",
        description=(
            "If GSO features not negotiated, "
            "virtio_net_hdr.gso_type MUST be GSO_NONE. "
            "MVP: no GSO."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.2.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-T-005",
        description=(
            "Submit descriptor chain: (hdr, payload_0, "
            "payload_1, …) with only the first writable flag "
            "clear (RX flag doesn't apply at TX)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.5.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-T-006",
        description=(
            "Wait for Used Ring index advancement, then "
            "reclaim buffers."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §2.7.8.",
        status="spec",
    ),

    # -- Section 4.6: Control path (only in scope if VIO-F-005
    # is negotiated; MAC filter / VLAN filter / MQ config /
    # announce) --
    DomainConstraint(
        name="VIO-C-001",
        description=(
            "Control queue index = MAX_QUEUE_PAIRS * 2 "
            "(or 2 for single-queue)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.2.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-C-002",
        description=(
            "Command format: class + cmd + data + ack; device "
            "writes ack byte at end."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.5.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-C-003",
        description=(
            "SHOULD support VIRTIO_NET_CTRL_MAC_TABLE_SET to "
            "populate the multicast filter (paired with "
            "VIO-F-005 + VIRTIO_NET_F_CTRL_RX)."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.5.2.",
        status="spec",
    ),
    DomainConstraint(
        name="VIO-C-004",
        description=(
            "If VLAN in scope, SHOULD support "
            "VIRTIO_NET_CTRL_VLAN_ADD / _DEL."
        ),
        entity_ids=["virtio-net-device"],
        rationale="Virtio 1.2 §5.1.6.5.3.",
        status="spec",
    ),

    # -- Section 5: Cross-cutting (MAC address semantics) --
    DomainConstraint(
        name="MAC-001",
        description=(
            "MAC address format: 6 bytes, big-endian on the "
            "wire."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802-2014.",
        status="spec",
    ),
    DomainConstraint(
        name="MAC-002",
        description=(
            "OUI (first 3 bytes) assigned by IEEE. Locally "
            "administered MACs have bit 1 of first byte set."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802-2014.",
        status="spec",
    ),
    DomainConstraint(
        name="MAC-003",
        description=(
            "Multicast MACs: first byte LSb = 1. Broadcast = "
            "FF:FF:FF:FF:FF:FF."
        ),
        entity_ids=["ethernet-frame"],
        rationale="IEEE 802-2014.",
        status="spec",
    ),
    DomainConstraint(
        name="MAC-004",
        description=(
            "Multicast IPv4 mapping: "
            "01:00:5E:<lower-23-of-IP>. For L3-side, but L2 "
            "filter must accept."
        ),
        entity_ids=["ethernet-frame"],
        rationale="RFC 1112.",
        status="spec",
    ),
    DomainConstraint(
        name="MAC-005",
        description=(
            "Multicast IPv6 mapping: 33:33:<lower-32-of-IPv6>."
        ),
        entity_ids=["ethernet-frame"],
        rationale="RFC 2464.",
        status="spec",
    ),

    # -- L2 product constraints: Ethernet-layer primitives --
    DomainConstraint(
        name="ETH-005",
        description=(
            "FCS = CRC-32 over DA..payload, polynomial "
            "0xedb88320 reflected (IEEE 802.3-2018 §3.2.9). "
            "x86_64 uses PCLMULQDQ fold-by-1 when available, "
            "slicing-by-8 fallback. AArch64 uses FEAT_CRC32 "
            "native. Both arches verified against IEEE 802.3 / "
            "zlib vectors. TX-path bswap32+complement conversion "
            "for on-the-wire FCS still pending; virtio-net "
            "typically offloads FCS."
        ),
        entity_ids=["ethernet-frame"],
        rationale=(
            "IEEE 802.3-2018 §3.2.9. D032 crypto-math strategy "
            "(ISA-idiomatic, macros-first, constant-time)."
        ),
        implementation_refs=[
            "arch/x86_64/crypto/crc32_ieee.S",
            "arch/aarch64/crypto/crc32_ieee.S",
        ],
        verification_refs=[
            "tooling/tests/test_crc32_ieee.py",
            "tooling/crypto_tests/crc32_test.c",
        ],
        status="implemented",
    ),
]

# -- Problem Domain: Performance Constraints --
#
# Quantitative budgets with first-class numeric data. Each row
# pairs a metric identifier (what the measurement harness emits)
# with a numeric budget, a comparison direction, a measurement
# source, and a status in the spec/tested/implemented lifecycle.
# The audit tool (future O3) will correlate metric names here
# against emitted samples and flag out-of-budget behaviour.
#
# Status legend:
#   spec        — budget written down; no impl or measurement yet
#   tested      — impl + at least one measurement; not every
#                 derived case closed
#   implemented — full coverage; under verification the system
#                 satisfies direction(budget)
#   deviation   — system does NOT satisfy the constraint
#   n_a         — scoped out for the current profile

performance_constraints = [
    PerformanceConstraint(
        name="fsa-transition-budget",
        description=(
            "A single FSA transition (handler-body plus "
            "dispatch bookkeeping) MUST complete within the "
            "budget. The target is tight because real code "
            "paths that land on the L2 hot path ride on top of "
            "the FSA engine, and blown transition budgets "
            "compound across every derived driver."
        ),
        entity_ids=["fsa-transition"],
        metric="fsa_transition_ns",
        budget=100.0,
        unit="ns",
        direction="max",
        measured_via=(
            "OSACA static analysis per handler + runtime perf "
            "ratchet once FSA engine lands (D040 baseline TBD)."
        ),
        rationale="D043 FSA runtime model, §FSA_TRANSITION_BUDGET_NS.",
        implementation_refs=[],
        verification_refs=[],
        status="spec",
    ),
    PerformanceConstraint(
        name="l2-frame-rate-floor",
        description=(
            "Minimum sustained L2 throughput at which the "
            "system must remain correct and non-lossy. Floor "
            "is the gating line for the first release; missing "
            "this is an SEV-0-class regression."
        ),
        entity_ids=["ethernet-frame"],
        metric="l2_throughput_bps",
        budget=1_000_000_000.0,
        unit="bps",
        direction="min",
        measured_via=(
            "End-to-end synthetic-traffic run under the pre-"
            "push integration suite once L2 TX/RX paths are "
            "wired; OSACA per hot instruction window meanwhile."
        ),
        rationale=(
            "D040 perf-regression ratchet; docs/l2/DESIGN.md §2 "
            "Latency/Throughput targets."
        ),
        implementation_refs=[],
        verification_refs=[],
        status="spec",
    ),
    PerformanceConstraint(
        name="l2-frame-rate-target",
        description=(
            "Target sustained L2 throughput for the shipping "
            "product. At 10 Gbps the wire-time budget is "
            "~67 ns for a minimum-size (64-byte) frame and "
            "~1230 ns for a maximum-size (1518-byte) frame "
            "(both figures are on-wire, including the 20-byte "
            "preamble+SFD+IPG overhead the MAC actually sees). "
            "Every cross-cutting feature (CRC, barriers, "
            "observability, VLAN handling) must fit inside "
            "the relevant per-frame budget for the traffic mix "
            "that cell exercises; the perf ratchet (D040) "
            "pins both small-frame and large-frame baselines "
            "once measured."
        ),
        entity_ids=["ethernet-frame"],
        metric="l2_throughput_bps",
        budget=10_000_000_000.0,
        unit="bps",
        direction="min",
        measured_via=(
            "Same harness as l2-frame-rate-floor; target is "
            "compared against measured sustained rate under a "
            "1518-byte maximum-frame workload."
        ),
        rationale=(
            "D040 perf-regression ratchet; docs/l2/DESIGN.md §2."
        ),
        implementation_refs=[],
        verification_refs=[],
        status="spec",
    ),
    PerformanceConstraint(
        name="obs-disabled-path-budget",
        description=(
            "An OBS_EMIT event site whose verbosity gate is "
            "OFF must add no more than this many cycles to "
            "the hot path. Enables shipping observability "
            "everywhere without eroding frame-rate budgets."
        ),
        entity_ids=["observability-event-site"],
        metric="obs_disabled_path_cycles",
        budget=5.0,
        unit="cycles",
        direction="max",
        measured_via=(
            "OSACA port-pressure analysis on the OBS_EMIT "
            "macro expansion with verbosity gate asserting "
            "not-taken; cross-checked with a tight-loop "
            "microbenchmark once the macro lands."
        ),
        rationale=(
            "docs/observability.md pre-D050 proposal; derived "
            "from frame-rate-target budget math at 10 Gbps."
        ),
        implementation_refs=[],
        verification_refs=[],
        status="spec",
    ),
    PerformanceConstraint(
        name="obs-category-gated-budget",
        description=(
            "An OBS_EMIT event site whose verbosity gate is "
            "ON but category bitmask is off must add no more "
            "than this many cycles. Slightly looser than the "
            "disabled-path budget because it performs one "
            "additional load + bit-test."
        ),
        entity_ids=["observability-event-site"],
        metric="obs_category_gated_cycles",
        budget=8.0,
        unit="cycles",
        direction="max",
        measured_via=(
            "Same harness as obs-disabled-path-budget, with "
            "verbosity gate asserting taken and category "
            "bitmask asserting not-taken."
        ),
        rationale=(
            "docs/observability.md pre-D050 proposal."
        ),
        implementation_refs=[],
        verification_refs=[],
        status="spec",
    ),
]

# -- Solution Domain: Modules --

vm_launcher_mod = ModuleSpec(
    name="vm_launcher",
    responsibility=(
        "Launch QEMU or Firecracker VMs in the background, "
        "poll for readiness via serial output file, and "
        "kill cleanly. QEMU uses -serial file: for direct "
        "serial capture; Firecracker has no equivalent so "
        "stdout is redirected into serial_path and the VMM's "
        "own logger is diverted to a sibling .fc-log file. "
        "Handles both x86_64 and aarch64. Maintains a "
        "thread-safe Popen registry for safe process "
        "lifecycle management. Validates paths and QEMU "
        "arguments. Logs all lifecycle events."
    ),
    classes=[
        ClassSpec(
            name="VMConfig",
            description=(
                "Immutable configuration for launching a VM. "
                "Platform is Literal['qemu','firecracker']. "
                "Pydantic validators reject path traversal "
                "and blocked QEMU arguments."
            ),
            bases=["pydantic.BaseModel"],
            methods=[
                FunctionSpec(
                    name="no_traversal",
                    parameters=[("v", "str")],
                    return_type="str",
                    docstring=(
                        "field_validator: reject '..' in "
                        "image_path and serial_path, resolve "
                        "to absolute."
                    ),
                ),
                FunctionSpec(
                    name="no_blocked_args",
                    parameters=[("v", "list[str]")],
                    return_type="list[str]",
                    docstring=(
                        "field_validator: reject QEMU flags "
                        "in _BLOCKED_ARGS set."
                    ),
                ),
            ],
        ),
        ClassSpec(
            name="VMHandle",
            description=(
                "Handle to a running VM process. Includes "
                "pid, serial_path, stderr_path, arch, "
                "platform. Popen object stored separately "
                "in _proc_registry."
            ),
            bases=["pydantic.BaseModel"],
        ),
    ],
    functions=[
        FunctionSpec(
            name="_reject_traversal",
            parameters=[("path", "str")],
            return_type="str",
            docstring=(
                "Reject paths with '..' components. "
                "Return resolved absolute path."
            ),
        ),
        FunctionSpec(
            name="_register_proc",
            parameters=[
                ("proc", "subprocess.Popen[bytes]"),
            ],
            return_type="None",
            docstring=(
                "Register a Popen in the thread-safe "
                "registry."
            ),
        ),
        FunctionSpec(
            name="_get_proc",
            parameters=[("pid", "int")],
            return_type="subprocess.Popen[bytes] | None",
            docstring="Look up a registered Popen by PID.",
        ),
        FunctionSpec(
            name="_unregister_proc",
            parameters=[("pid", "int")],
            return_type="None",
            docstring="Remove a Popen from the registry.",
        ),
        FunctionSpec(
            name="launch_vm",
            parameters=[("config", "VMConfig")],
            return_type="VMHandle",
            docstring=(
                "Launch a VM in the background. Truncates "
                "serial and stderr files. Dispatches to "
                "_launch_qemu or _launch_firecracker by "
                "platform. Does not block."
            ),
            preconditions=[
                "Guest image exists at config.image_path",
                "For firecracker: /dev/kvm is available",
            ],
            postconditions=[
                "VM process is running in background",
                "Serial and stderr files exist (truncated)",
                "Popen registered in _proc_registry",
            ],
        ),
        FunctionSpec(
            name="_launch_qemu",
            parameters=[("config", "VMConfig")],
            return_type="VMHandle",
            docstring=(
                "Spawn QEMU with -serial file: redirecting "
                "the UART straight to serial_path."
            ),
        ),
        FunctionSpec(
            name="_launch_firecracker",
            parameters=[("config", "VMConfig")],
            return_type="VMHandle",
            docstring=(
                "Spawn Firecracker via --no-api with a "
                "generated JSON config. Redirects stdout to "
                "serial_path (Firecracker has no equivalent "
                "of -serial file:) and diverts VMM logger "
                "lines to a sibling .fc-log so the serial "
                "stream stays mostly clean."
            ),
            preconditions=["/dev/kvm is available"],
        ),
        FunctionSpec(
            name="_firecracker_log_path",
            parameters=[("serial_path", "str")],
            return_type="str",
            docstring=(
                "Co-located VMM log path: serial_path + "
                "'.fc-log'."
            ),
        ),
        FunctionSpec(
            name="_firecracker_config_path",
            parameters=[("serial_path", "str")],
            return_type="str",
            docstring=(
                "Co-located JSON config path: serial_path + "
                "'.fc-config.json'."
            ),
        ),
        FunctionSpec(
            name="_firecracker_vm_id",
            parameters=[("serial_path", "str")],
            return_type="str",
            docstring=(
                "Derive a stable per-launch microVM id from "
                "Path(serial_path).stem. Tests use unique "
                "tmpdirs so the stem is unique per test."
            ),
        ),
        FunctionSpec(
            name="_firecracker_config_dict",
            parameters=[("config", "VMConfig")],
            return_type="dict[str, Any]",
            docstring=(
                "Build the JSON config Firecracker expects. "
                "drives is empty (no rootfs needed for "
                "diagnostic boots), logger is set to a "
                "sibling .fc-log file."
            ),
        ),
        FunctionSpec(
            name="_firecracker_args",
            parameters=[
                ("config_path", "str"), ("vm_id", "str"),
            ],
            return_type="list[str]",
            docstring=(
                "Build the firecracker --no-api command line: "
                "[firecracker, --no-api, --config-file <path>, "
                "--id <vm_id>]."
            ),
        ),
        FunctionSpec(
            name="wait_for_ready",
            parameters=[
                ("handle", "VMHandle"),
                ("marker", "str"),
                ("timeout_sec", "float"),
            ],
            return_type="bool",
            docstring=(
                "Poll serial output until marker appears "
                "or timeout. Opens file once, reads in "
                "binary mode with 4KB chunks, uses sliding "
                "window of marker_len bytes to avoid O(N^2) "
                "search. Returns True if ready."
            ),
        ),
        FunctionSpec(
            name="_kill_via_proc",
            parameters=[
                ("proc", "subprocess.Popen[bytes]"),
            ],
            return_type="None",
            docstring=(
                "Kill using Popen API: terminate, wait 5s, "
                "kill if needed. Immune to PID recycling."
            ),
        ),
        FunctionSpec(
            name="_try_waitpid",
            parameters=[("pid", "int")],
            return_type="bool",
            docstring=(
                "Try to reap a child process via "
                "os.waitpid with WNOHANG. Returns True "
                "if reaped or not a child."
            ),
        ),
        FunctionSpec(
            name="_signal_pid",
            parameters=[("pid", "int"), ("sig", "int")],
            return_type="bool",
            docstring=(
                "Send signal to PID. Return False if "
                "process gone."
            ),
        ),
        FunctionSpec(
            name="_poll_pid_exit",
            parameters=[
                ("pid", "int"), ("timeout", "float"),
            ],
            return_type="bool",
            docstring=(
                "Poll until PID exits or timeout via "
                "waitpid + signal check."
            ),
        ),
        FunctionSpec(
            name="_kill_via_pid",
            parameters=[("pid", "int")],
            return_type="None",
            docstring=(
                "Fallback kill using bare PID. SIGTERM, "
                "wait, SIGKILL, reap via waitpid."
            ),
        ),
        FunctionSpec(
            name="kill_vm",
            parameters=[("handle", "VMHandle")],
            return_type="None",
            docstring=(
                "Kill VM cleanly. Uses Popen API if "
                "registered, falls back to bare PID. "
                "Unregisters from _proc_registry. "
                "Reaps child to prevent zombies."
            ),
            postconditions=[
                "VM process no longer running",
                "Popen removed from registry",
            ],
        ),
        FunctionSpec(
            name="has_kvm",
            parameters=[],
            return_type="bool",
            docstring="Check if /dev/kvm is available.",
        ),
    ],
    internal_module_refs=[],
    external_imports=[
        "json", "logging", "os", "pathlib", "pydantic",
        "subprocess", "threading", "time", "typing",
    ],
    test_strategy=(
        "Unit test with mock subprocess. Autouse fixture "
        "clears _proc_registry before/after each test. "
        "Tests verify: qemu launch registers Popen, stderr "
        "captured to file, serial truncated on launch, "
        "path traversal rejected, blocked args rejected, "
        "platform Literal validated, marker found/timeout "
        "in wait_for_ready, kill via Popen vs bare PID "
        "fallback, waitpid reaps zombies. Firecracker tests "
        "verify --no-api command shape, JSON config file "
        "materializes with correct fields (drives, logger, "
        "boot-source), config/log/stderr files all created "
        "on disk, no-kvm raises RuntimeError. Helper tests "
        "cover the path-derivation functions. Random test "
        "order verified via pytest-randomly."
    ),
)

guest_builder_mod = ModuleSpec(
    name="guest_builder",
    responsibility=(
        "Assemble and link guest images using GNU as "
        "cross-toolchains and GNU ld. Selects the correct "
        "toolchain based on (arch, platform) -- ELF class "
        "depends on boot protocol: x86_64 qemu uses Multiboot1 "
        "(ELF32), x86_64 firecracker uses PVH (ELF64). "
        "Returns the path to the built binary."
    ),
    classes=[
        ClassSpec(
            name="Toolchain",
            description=(
                "Assembler path, linker path, and arch-specific "
                "as/ld flag lists. Plain Python class (not "
                "pydantic) — constructed directly by "
                "toolchain_for."
            ),
        ),
    ],
    functions=[
        FunctionSpec(
            name="build_guest",
            parameters=[
                ("arch", "str"),
                ("platform", "str"),
                ("source_dir", "str"),
                ("build_dir", "str | None"),
            ],
            return_type="Path",
            docstring=(
                "Assemble and link the guest image for the "
                "given arch/platform. Returns path to the "
                "output binary."
            ),
            preconditions=[
                "Cross-toolchain installed for target arch",
                "Source files exist in source_dir",
            ],
            postconditions=[
                "Binary exists at returned path",
            ],
        ),
        FunctionSpec(
            name="toolchain_for",
            parameters=[
                ("arch", "str"), ("platform", "str"),
            ],
            return_type="Toolchain",
            docstring=(
                "Return the Toolchain (assembler, linker, "
                "as_flags, ld_flags) for the (arch, platform) "
                "target. Raises ValueError on unsupported "
                "combinations."
            ),
        ),
    ],
    internal_module_refs=[],
    external_imports=["subprocess", "pathlib"],
    test_strategy=(
        "Unit test toolchain_for with all supported (arch, "
        "platform) combos plus rejection cases. Functional "
        "test build_guest with a minimal .S file that "
        "assembles and links successfully. Test missing-"
        "toolchain path by mocking subprocess.run to raise "
        "FileNotFoundError."
    ),
)

test_runner_mod = ModuleSpec(
    name="test_runner",
    responsibility=(
        "Orchestrate the full test cycle: build guest, "
        "launch VM, wait for ready, run test cases, kill "
        "VM, report results. Serial output read in binary "
        "mode with errors='replace'. This is the entry "
        "point for both pre-commit and CI."
    ),
    classes=[
        ClassSpec(
            name="TestCase",
            description=(
                "Pydantic model implementing the `test-case` "
                "problem-domain Entity."
            ),
            bases=["pydantic.BaseModel"],
        ),
        ClassSpec(
            name="TestResult",
            description=(
                "Pydantic model implementing the `test-result` "
                "problem-domain Entity."
            ),
            bases=["pydantic.BaseModel"],
        ),
        ClassSpec(
            name="TestSuite",
            description="Collection of test cases for a target",
            bases=["pydantic.BaseModel"],
        ),
        ClassSpec(
            name="SuiteResult",
            description=(
                "Aggregate results for a test suite run. "
                "Has all_passed property."
            ),
            bases=["pydantic.BaseModel"],
        ),
    ],
    functions=[
        FunctionSpec(
            name="run_suite",
            parameters=[
                ("suite", "TestSuite"),
                ("build_dir", "str | None"),
                ("serial_path", "str | None"),
            ],
            return_type="SuiteResult",
            docstring=(
                "Run a complete test suite: build, boot, "
                "test, kill, report. Skips gracefully if "
                "Firecracker without KVM."
            ),
        ),
        FunctionSpec(
            name="check_serial",
            parameters=[
                ("serial_path", "str"),
                ("expected", "str"),
            ],
            return_type="TestResult",
            docstring=(
                "Check serial output for expected content. "
                "Reads in binary mode with errors='replace'."
            ),
        ),
        FunctionSpec(
            name="check_http",
            parameters=[
                ("host", "str"),
                ("port", "int"),
                ("expected", "str"),
            ],
            return_type="TestResult",
            docstring=(
                "Make HTTP GET request and verify response "
                "body matches expected."
            ),
        ),
        FunctionSpec(
            name="_build_image",
            parameters=[
                ("suite", "TestSuite"),
                ("build_dir", "str | None"),
            ],
            return_type="Path | TestResult",
            docstring=(
                "Build guest image. Return Path on success "
                "or TestResult on failure."
            ),
        ),
        FunctionSpec(
            name="_boot_and_test",
            parameters=[
                ("handle", "VMHandle"),
                ("suite", "TestSuite"),
            ],
            return_type="list[TestResult]",
            docstring=(
                "Wait for ready, run cases, return results."
            ),
        ),
        FunctionSpec(
            name="_should_skip",
            parameters=[("suite", "TestSuite")],
            return_type="str | None",
            docstring=(
                "Return skip reason if suite can't run, "
                "or None if runnable."
            ),
        ),
        FunctionSpec(
            name="_run_case",
            parameters=[
                ("case", "TestCase"),
                ("handle", "VMHandle"),
            ],
            return_type="TestResult",
            docstring=(
                "Run a single test case against a booted VM."
            ),
        ),
    ],
    internal_module_refs=["vm_launcher", "guest_builder"],
    external_imports=[
        "pathlib", "pydantic", "subprocess",
        "urllib.error", "urllib.request",
    ],
    test_strategy=(
        "Unit test check_serial and check_http with "
        "fixtures. Integration test run_suite with mocked "
        "build/launch/wait/kill. Test all failure paths: "
        "build failure, boot timeout, check mismatch, "
        "Firecracker skip without KVM."
    ),
)

cli_mod = ModuleSpec(
    name="cli",
    responsibility=(
        "Command-line interface for the test harness. "
        "Supports running a single arch/platform combo or "
        "the full matrix. Reports pass/fail per test with "
        "exit code 0 on all-pass, 1 on any failure."
    ),
    functions=[
        FunctionSpec(
            name="main",
            parameters=[("argv", "list[str] | None")],
            return_type="int",
            docstring=(
                "Entry point. Parse args, run tests, "
                "report, return exit code."
            ),
        ),
        FunctionSpec(
            name="parse_args",
            parameters=[("argv", "list[str] | None")],
            return_type="argparse.Namespace",
            docstring="Parse command-line arguments.",
        ),
        FunctionSpec(
            name="_load_suite",
            parameters=[("path", "str")],
            return_type="TestSuite",
            docstring="Load a test suite from a JSON file.",
        ),
        FunctionSpec(
            name="_print_result",
            parameters=[("result", "SuiteResult")],
            return_type="None",
            docstring="Print test results for one suite.",
        ),
    ],
    internal_module_refs=["test_runner"],
    external_imports=["argparse", "json"],
    test_strategy=(
        "Test argument parsing. Test exit codes for "
        "all-pass and any-failure scenarios with mocked "
        "test_runner. Test suite loading from JSON."
    ),
)

# -- Verification Domain: Test-plan traceability --
#
# One VerificationCase per TEST_PLAN.md §9 row whose `covers`
# list fully resolves against constraints declared above.
# Partial-coverage and ontology-missing rows are deferred to a
# follow-up pass that formalises the remaining L2 requirements
# (ETH-001..018 minus 005, MAC-*, VLAN-*, ARP-*, VIO-Q/R/T/C/F-*
# 003..007) as first-class DomainConstraints. Listing them as
# stubs here without their requirements would hide the gap the
# audit tool exists to surface.
#
# `eth-fcs-primitive` is `passing` — CRC-32 IEEE unit tests
# already pass on both arches (Slice D047 + crypto_tests/
# test_crc32_ieee.c). All others start `planned`.

verification_cases = [
    VerificationCase(
        name="eth-fcs-primitive",
        covers=["ETH-005"],
        tier="A",
        status="passing",
        implementation_refs=[
            "tooling/tests/test_crc32_ieee.py",
            "tooling/crypto_tests/crc32_test.c",
        ],
        rationale=(
            "Unit-level verification of the CRC-32 IEEE 802.3 "
            "primitive on both arches against zlib vectors. "
            "Virtio-net frame-level FCS behaviour (eth-fcs-virtio) "
            "is separate."
        ),
    ),
    VerificationCase(
        name="virtio-init-sequence",
        covers=[
            "VIO-001", "VIO-002", "VIO-003", "VIO-004",
            "VIO-005", "VIO-006", "VIO-007", "VIO-008",
            "VIO-009",
        ],
        tier="B",
        status="planned",
        implementation_refs=[],
        rationale=(
            "Integration verification of the full Virtio 1.2 "
            "§2.1.2 device init handshake. Current tracer-bullet "
            "marker checks in run_local.sh are the de-facto "
            "cover, but a dedicated integration test that "
            "asserts on both happy-path markers and each "
            ".*_fail endpoint hasn't been written yet."
        ),
    ),
]


# -- Solution Domain: External Dependencies --

ext_deps = [
    ExternalDependency(
        name="pydantic",
        version_constraint=">=2.0",
        reason=(
            "Data models for VMConfig, VMHandle, "
            "TestSuite, TestCase, TestResult, SuiteResult. "
            "field_validator for path/args validation. "
            "Literal type for platform."
        ),
    ),
]

# -- Assemble and Save --

ontology = Ontology(
    entities=[
        guest_image, vm_instance, test_case, test_result,
        fsa_transition, ethernet_frame, virtio_net_device,
        arp_packet, observability_event_site,
    ],
    relationships=[boots_rel, runs_against_rel, produces_rel],
    domain_constraints=constraints,
    performance_constraints=performance_constraints,
    verification_cases=verification_cases,
    modules=[
        vm_launcher_mod, guest_builder_mod,
        test_runner_mod, cli_mod,
    ],
    external_dependencies=ext_deps,
)

# D049: dag_transaction wraps the load-modify-save cycle under
# an fcntl.flock, so a concurrently-running builder (a side
# session, a parallel CI job) can't overwrite this one's update.
# Inside the transaction, snapshot_if_changed makes re-runs on
# unchanged content a true no-op. Label embeds the git HEAD SHA
# for source-level cross-reference with the DAG generation.
label = git_snapshot_label()
with dag_transaction(DAG_PATH, project_name=PROJECT_NAME) as dag:
    _node_id, created = snapshot_if_changed(dag, ontology, label)

print(f"Saved ontology DAG to {DAG_PATH}")
print(f"  Entities: {len(ontology.entities)}")
print(f"  Relationships: {len(ontology.relationships)}")
print(f"  Constraints: {len(ontology.domain_constraints)}")
print(
    f"  Performance constraints: "
    f"{len(ontology.performance_constraints)}"
)
print(
    f"  Verification cases: "
    f"{len(ontology.verification_cases)}"
)
print(f"  Modules: {len(ontology.modules)}")
print(
    f"  Functions: "
    f"{sum(len(m.functions) for m in ontology.modules)}"
)
print(
    f"  Classes: "
    f"{sum(len(m.classes) for m in ontology.modules)}"
)
print(f"  DAG nodes: {len(dag.nodes)}")
if created:
    print(f"  Appended snapshot: {label}")
else:
    print("  No content change — snapshot skipped (idempotent)")
