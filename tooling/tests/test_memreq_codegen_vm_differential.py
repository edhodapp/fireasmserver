"""Differential test — codegen output runs cleanly under the VM.

This is the load-bearing assertion for task #28: bytes produced by
`memreq_codegen.encoding.encode_bytecode` evaluate to the right
integer under `memlayout.bytecode.run_bytecode` for a battery of
representative expressions. Both sides share the wire ABI by
design; this test catches drift between codegen and interpreter
that no unit test on either side alone would surface.

The asm-side interpreter is the third leg of this triangle and is
covered by `tooling/memlayout_diffharness/`. That suite drives the
.S interpreter on concrete (cpu, profile, code) triples and
compares against `memlayout.bytecode` — so codegen → memlayout →
asm is transitively verified.
"""

from __future__ import annotations

import pytest

from memlayout.bytecode import run_bytecode
from memlayout.models import CpuCharacteristics, TuningProfile
from memreq_codegen.encoding import (
    OP_ALIGN_UP,
    OP_CALL_THUNK,
    OP_CPU,
    OP_DIV_LIT,
    OP_LIT,
    OP_MUL,
    OP_TUNING,
    SIZE_BYTECODE_BYTES,
    Op,
    encode_bytecode,
)
from memreq_codegen.schema import RegionDecl, to_op


@pytest.fixture(name="cpu")
def fixture_cpu() -> CpuCharacteristics:
    return CpuCharacteristics(
        l1d_line_bytes=64,
        l1d_bytes=32 * 1024,
        l1i_bytes=32 * 1024,
        l2_bytes=512 * 1024,
        l3_bytes_per_cluster=2 * 1024 * 1024,
        cores_sharing_l2=2,
        cores_sharing_l3=4,
        hw_prefetcher_stride_lines=4,
        detected_model_id=0xABCD,
    )


@pytest.fixture(name="profile")
def fixture_profile() -> TuningProfile:
    return TuningProfile(
        rx_queue_depth=256,
        tx_queue_depth=256,
        rx_buffer_bytes_hint=2048,
        actor_pool_size_per_core=64,
        tls_session_cache_entries=128,
        worker_core_count=4,
    )


def _eval(
    ops: list[Op],
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> int:
    """Encode then evaluate against the VM.

    Pass the full padded buffer — the VM stops at END semantically
    (only when END appears at an opcode position, not inside a
    payload). Naive truncation on the first 0x00 byte would corrupt
    LIT payloads that contain zero bytes (e.g., LIT 4096 = 0x00001000).
    """
    blob = encode_bytecode(ops, SIZE_BYTECODE_BYTES)
    return run_bytecode(blob, cpu, profile)


class TestLiteralEval:
    """LIT-only evaluation: encoder + VM agree on plain literals."""

    def test_lit_alone(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        assert _eval([Op(OP_LIT, 4096)], cpu, profile) == 4096

    def test_lit_max_u32(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        assert _eval(
            [Op(OP_LIT, 0xFFFFFFFF)], cpu, profile,
        ) == 0xFFFFFFFF


class TestCpuEval:
    """CPU opcode dereferences CpuCharacteristics by positional id."""

    def test_l1d_line_bytes_is_first_field(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        # CpuCharacteristics field 0 is l1d_line_bytes (64 in fixture).
        assert _eval([Op(OP_CPU, 0)], cpu, profile) == 64

    def test_detected_model_id_is_last_field(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        # detected_model_id is field 8 (the ninth field).
        assert _eval([Op(OP_CPU, 8)], cpu, profile) == 0xABCD


class TestTuningEval:
    """TUNING opcode dereferences TuningProfile by positional id."""

    def test_rx_queue_depth_is_first_field(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        assert _eval([Op(OP_TUNING, 0)], cpu, profile) == 256

    def test_worker_core_count_is_last_field(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        # TuningProfile field 5 is worker_core_count (4 in fixture).
        assert _eval([Op(OP_TUNING, 5)], cpu, profile) == 4


class TestArithmeticEval:
    """Multi-op expressions: MUL, DIV_LIT, ALIGN_UP under the VM."""

    def test_lit_mul_lit(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        # 256 * 2048 = 524_288
        ops = [Op(OP_LIT, 256), Op(OP_LIT, 2048), Op(OP_MUL)]
        assert _eval(ops, cpu, profile) == 524_288

    def test_cpu_mul_lit_align_up(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        # cores_sharing_l3 (4) * 1000, aligned up to 4096
        ops = [
            Op(OP_CPU, 6),       # cores_sharing_l3 = 4
            Op(OP_LIT, 1000),    # * 1000 → 4000
            Op(OP_MUL),
            Op(OP_LIT, 4096),    # align up to 4096
            Op(OP_ALIGN_UP),
        ]
        assert _eval(ops, cpu, profile) == 4096

    def test_div_lit(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        ops = [Op(OP_LIT, 4096), Op(OP_DIV_LIT, 64)]
        assert _eval(ops, cpu, profile) == 64

    def test_tuning_mul_tuning(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        # rx_queue_depth (256) * rx_buffer_bytes_hint (2048) = 524_288
        ops = [
            Op(OP_TUNING, 0),     # rx_queue_depth
            Op(OP_TUNING, 2),     # rx_buffer_bytes_hint
            Op(OP_MUL),
        ]
        assert _eval(ops, cpu, profile) == 524_288


class TestSchemaToVmRoundTrip:  # pylint: disable=missing-class-docstring
    """YAML schema → encoder Op → bytecode → VM result.

    This is the end-to-end chain a real `regions.yaml` will exercise.
    Verifying it here means the YAML-author's mental model of an
    expression matches what the kernel-side allocator computes.
    """

    def test_lit_in_yaml_form(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        decl = RegionDecl.model_validate({
            "name": "x", "tier": "cold", "lifetime": "steady_state",
            "owner": 0, "writable": True, "align": 4096,
            "size": [{"kind": "lit", "value": 524_288}],
        })
        assert isinstance(decl.size, list)
        ops = [to_op(op_model) for op_model in decl.size]
        assert _eval(ops, cpu, profile) == 524_288

    def test_cpu_mul_lit_in_yaml_form(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        # 64-byte L1D line × 1024 = 65_536
        decl = RegionDecl.model_validate({
            "name": "x", "tier": "cold", "lifetime": "steady_state",
            "owner": 0, "writable": True, "align": 4096,
            "size": [
                {"kind": "cpu", "field": "l1d_line_bytes"},
                {"kind": "lit", "value": 1024},
                {"kind": "mul"},
            ],
        })
        assert isinstance(decl.size, list)
        ops = [to_op(op_model) for op_model in decl.size]
        assert _eval(ops, cpu, profile) == 65_536

    def test_tuning_alignup_in_yaml_form(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        # rx_queue_depth (256) aligned up to 1024 = 1024
        # (256 + 1024 - 1 = 1279, & ~1023 = 1024)
        decl = RegionDecl.model_validate({
            "name": "x", "tier": "cold", "lifetime": "steady_state",
            "owner": 0, "writable": True, "size": 4096,
            "align": [
                {"kind": "tuning", "field": "rx_queue_depth"},
                {"kind": "lit", "value": 1024},
                {"kind": "align_up"},
            ],
        })
        assert isinstance(decl.align, list)
        ops = [to_op(op_model) for op_model in decl.align]
        assert _eval(ops, cpu, profile) == 1024

    def test_div_lit_in_yaml_form(
        self, cpu: CpuCharacteristics, profile: TuningProfile,
    ) -> None:
        # 4096 / 64 = 64. Exercises the DivLitOp → to_op handler.
        decl = RegionDecl.model_validate({
            "name": "x", "tier": "cold", "lifetime": "steady_state",
            "owner": 0, "writable": True, "align": 4,
            "size": [
                {"kind": "lit", "value": 4096},
                {"kind": "div_lit", "divisor": 64},
            ],
        })
        assert isinstance(decl.size, list)
        ops = [to_op(op_model) for op_model in decl.size]
        assert _eval(ops, cpu, profile) == 64

    def test_call_thunk_in_yaml_form_encodes(self) -> None:
        # CallThunk doesn't have a usable thunk to evaluate against
        # in this test (the kernel-side thunk table lives elsewhere);
        # cover the to_op → wire-bytes path without evaluating, so
        # the dispatch entry has coverage and a future thunk-id
        # binding is straightforward.
        decl = RegionDecl.model_validate({
            "name": "x", "tier": "cold", "lifetime": "steady_state",
            "owner": 0, "writable": True, "align": 4096,
            "size": [
                {"kind": "call_thunk", "thunk_id": 0xABCDEF12},
            ],
        })
        assert isinstance(decl.size, list)
        ops = [to_op(op_model) for op_model in decl.size]
        blob = encode_bytecode(ops, SIZE_BYTECODE_BYTES)
        # CALL_THUNK + LE u32(0xABCDEF12) + END.
        assert blob[:6] == bytes([
            OP_CALL_THUNK, 0x12, 0xEF, 0xCD, 0xAB, 0x00,
        ])
