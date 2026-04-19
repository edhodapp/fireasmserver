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
    Property,
    PropertyType,
    Relationship,
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
    ),
    DomainConstraint(
        name="kvm-required-for-firecracker",
        description=(
            "Firecracker requires /dev/kvm and only works "
            "for the native architecture. Skip gracefully "
            "if KVM is not available."
        ),
        entity_ids=["vm-instance"],
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
    dependencies=[
        "subprocess", "pathlib", "time",
        "threading", "logging", "os",
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
    dependencies=["subprocess", "pathlib"],
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
    dependencies=[
        "vm_launcher", "guest_builder", "urllib.request",
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
    dependencies=["argparse", "json", "test_runner"],
    test_strategy=(
        "Test argument parsing. Test exit codes for "
        "all-pass and any-failure scenarios with mocked "
        "test_runner. Test suite loading from JSON."
    ),
)

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
    entities=[guest_image, vm_instance, test_case, test_result],
    relationships=[boots_rel, runs_against_rel, produces_rel],
    domain_constraints=constraints,
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
