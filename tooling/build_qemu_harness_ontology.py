#!/usr/bin/env python3
"""Build the ontology DAG for the QEMU test harness."""

from python_agent.dag_utils import save_dag, save_snapshot
from python_agent.ontology import (
    ClassSpec,
    DomainConstraint,
    Entity,
    ExternalDependency,
    FunctionSpec,
    ModuleSpec,
    Ontology,
    OntologyDAG,
    Property,
    PropertyType,
    Relationship,
)

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
            description="Filesystem path to the ELF/flat binary",
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
            description="Target VM platform",
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
        "Never use timeout to kill QEMU -- background + "
        "poll + kill."
    ),
    properties=[
        Property(
            name="pid",
            property_type=PropertyType(kind="int"),
            description="Host OS process ID of the VM",
        ),
        Property(
            name="serial_path",
            property_type=PropertyType(kind="str"),
            description=(
                "Path to serial output file "
                "(-serial file:<path>)"
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
            "cleanly. Use -serial file:<path> not "
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
            "observes."
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
]

# -- Solution Domain: Modules --

vm_launcher_mod = ModuleSpec(
    name="vm_launcher",
    responsibility=(
        "Launch QEMU or Firecracker VMs in the background, "
        "poll for readiness via serial output file, and "
        "kill cleanly. Handles both x86_64 and aarch64 "
        "with appropriate QEMU system binary and flags."
    ),
    classes=[
        ClassSpec(
            name="VMConfig",
            description=(
                "Immutable configuration for launching a VM"
            ),
            bases=["pydantic.BaseModel"],
            methods=[],
        ),
    ],
    functions=[
        FunctionSpec(
            name="launch_vm",
            parameters=[
                ("config", "VMConfig"),
            ],
            return_type="VMHandle",
            docstring=(
                "Launch a VM in the background. Returns a "
                "handle with pid and serial output path. "
                "Does not block -- caller must poll for "
                "readiness."
            ),
            preconditions=[
                "Guest image exists at config.image_path",
                "For firecracker: /dev/kvm is available",
            ],
            postconditions=[
                "VM process is running in background",
                "Serial output file exists (may be empty)",
            ],
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
                "Poll serial output file until marker "
                "string appears or timeout. Returns True "
                "if ready, False if timed out."
            ),
        ),
        FunctionSpec(
            name="kill_vm",
            parameters=[("handle", "VMHandle")],
            return_type="None",
            docstring=(
                "Kill the VM process cleanly via SIGTERM, "
                "then SIGKILL if needed. Wait for process "
                "to exit."
            ),
            postconditions=[
                "VM process no longer running",
            ],
        ),
        FunctionSpec(
            name="has_kvm",
            parameters=[],
            return_type="bool",
            docstring="Check if /dev/kvm is available.",
        ),
    ],
    dependencies=["subprocess", "pathlib", "time"],
    test_strategy=(
        "Unit test with mock subprocess. Functional test "
        "with a minimal guest image that writes a known "
        "marker to serial output, then verifies "
        "wait_for_ready and kill_vm work correctly. Test "
        "the KVM-not-available path by mocking os.access."
    ),
)

guest_builder_mod = ModuleSpec(
    name="guest_builder",
    responsibility=(
        "Assemble and link guest images using GNU as "
        "cross-toolchains and GNU ld. Selects the correct "
        "toolchain based on target arch. Returns the path "
        "to the built binary."
    ),
    functions=[
        FunctionSpec(
            name="build_guest",
            parameters=[
                ("arch", "str"),
                ("platform", "str"),
                ("source_dir", "str"),
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
            name="toolchain_for_arch",
            parameters=[("arch", "str")],
            return_type="tuple[str, str]",
            docstring=(
                "Return (assembler, linker) binary names "
                "for the target arch. E.g., "
                "('aarch64-linux-gnu-as', "
                "'aarch64-linux-gnu-ld')."
            ),
        ),
    ],
    dependencies=["subprocess", "pathlib"],
    test_strategy=(
        "Unit test toolchain_for_arch with known arches. "
        "Functional test build_guest with a minimal .S "
        "file that assembles and links successfully. Test "
        "missing-toolchain path by mocking "
        "subprocess.run to raise FileNotFoundError."
    ),
)

test_runner_mod = ModuleSpec(
    name="test_runner",
    responsibility=(
        "Orchestrate the full test cycle: build guest, "
        "launch VM, wait for ready, run test cases, kill "
        "VM, report results. This is the entry point for "
        "both pre-commit and CI."
    ),
    classes=[
        ClassSpec(
            name="TestSuite",
            description="Collection of test cases for a target",
            bases=["pydantic.BaseModel"],
        ),
        ClassSpec(
            name="SuiteResult",
            description="Aggregate results for a test suite run",
            bases=["pydantic.BaseModel"],
        ),
    ],
    functions=[
        FunctionSpec(
            name="run_suite",
            parameters=[
                ("suite", "TestSuite"),
                ("arch", "str"),
                ("platform", "str"),
            ],
            return_type="SuiteResult",
            docstring=(
                "Run a complete test suite: build, boot, "
                "test, kill, report."
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
                "Check serial output file for expected "
                "content."
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
    ],
    dependencies=[
        "vm_launcher", "guest_builder", "urllib.request",
    ],
    test_strategy=(
        "Unit test check_serial and check_http with "
        "fixtures. Integration test run_suite with a "
        "minimal guest that boots, emits a serial marker, "
        "and optionally serves HTTP. Test all failure "
        "paths: build failure, boot timeout, check "
        "mismatch, VM crash."
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
    ],
    dependencies=["argparse", "test_runner"],
    test_strategy=(
        "Test argument parsing. Test exit codes for "
        "all-pass and any-failure scenarios with mocked "
        "test_runner."
    ),
)

# -- Solution Domain: External Dependencies --

ext_deps = [
    ExternalDependency(
        name="pydantic",
        version_constraint=">=2.0",
        reason="Data models for VMConfig, TestSuite, results",
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

dag = OntologyDAG(project_name="fireasmserver-qemu-harness")
save_snapshot(dag, ontology, "initial-design")
save_dag(dag, "tooling/qemu-harness.json")

print("Saved ontology DAG to tooling/qemu-harness.json")
print(f"  Entities: {len(ontology.entities)}")
print(f"  Relationships: {len(ontology.relationships)}")
print(f"  Constraints: {len(ontology.domain_constraints)}")
print(f"  Modules: {len(ontology.modules)}")
print(
    f"  Functions: {sum(len(m.functions) for m in ontology.modules)}"
)
