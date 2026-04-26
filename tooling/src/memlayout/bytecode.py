"""Bytecode interpreter for D060 layer-3 size/align expressions.

The interpreter is a fixed-vocabulary stack calculator — 7
primary opcodes plus a CALL_THUNK escape. Each opcode consumes
its payload bytes from the input stream and produces or
consumes stack values. Termination is on END.

Errors raised by this module are BytecodeError; the allocator
turns them into a LAYOUT-INVALID halt. Errors are carefully
deterministic so differential testing against the assembly
interpreter compares behavior, not error spelling.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from memlayout.models import (
    CpuCharacteristics,
    ThunkFn,
    TuningProfile,
)
from memlayout.types import MAX_U64, Opcode, STACK_DEPTH


class BytecodeError(Exception):
    """Raised on any malformed-bytecode condition.

    Same exception for every error type because the assembly
    side has only one halt code (LAYOUT-INVALID) — matching
    granularity here keeps the differential test simple.
    """


@dataclass
class _Interpreter:
    """Per-evaluation state. Created fresh for each run.

    `cpu_fields` and `tuning_fields` are computed once at
    construction so the per-opcode hot path doesn't pay an
    O(N) tuple build on every CPU/TUNING execution. The audit
    backend (D051 extension) runs this allocator across many
    (profile, cpu_model) combinations; the cached lookup keeps
    that loop tight.
    """

    code: bytes
    cpu: CpuCharacteristics
    profile: TuningProfile
    thunks: Mapping[int, ThunkFn]
    stack: list[int]
    cpu_fields: tuple[str, ...] = ()
    tuning_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.cpu_fields = tuple(
            self.cpu.__class__.model_fields.keys()
        )
        self.tuning_fields = tuple(
            self.profile.__class__.model_fields.keys()
        )

    def push(self, value: int) -> None:
        if len(self.stack) >= STACK_DEPTH:
            raise BytecodeError("stack overflow")
        if value < 0 or value > MAX_U64:
            raise BytecodeError(
                f"value {value} out of u64 range"
            )
        self.stack.append(value)

    def pop(self) -> int:
        if not self.stack:
            raise BytecodeError("stack underflow")
        return self.stack.pop()

    def read_byte(self, ip: int) -> tuple[int, int]:
        if ip >= len(self.code):
            raise BytecodeError("truncated payload (1 byte)")
        return self.code[ip], ip + 1

    def read_u32(self, ip: int) -> tuple[int, int]:
        if ip + 4 > len(self.code):
            raise BytecodeError("truncated payload (4 bytes)")
        value = int.from_bytes(
            self.code[ip:ip + 4], "little", signed=False,
        )
        return value, ip + 4

    def cpu_field(self, idx: int) -> int:
        if idx >= len(self.cpu_fields):
            raise BytecodeError(
                f"cpu field id {idx} out of range"
            )
        value = getattr(self.cpu, self.cpu_fields[idx])
        if not isinstance(value, int):  # pragma: no cover
            # Defensive: pydantic enforces int on every
            # CpuCharacteristics field. Reachable only if the
            # struct is mutated to add a non-int field without
            # also extending the bytecode VM.
            raise BytecodeError(
                f"cpu field {idx} is not an int"
            )
        return value

    def tuning_field(self, idx: int) -> int:
        if idx >= len(self.tuning_fields):
            raise BytecodeError(
                f"tuning field id {idx} out of range"
            )
        value = getattr(self.profile, self.tuning_fields[idx])
        if not isinstance(value, int):  # pragma: no cover
            # Defensive: pydantic enforces int on every
            # TuningProfile field. Same reasoning as cpu_field.
            raise BytecodeError(
                f"tuning field {idx} is not an int"
            )
        return value

    def op_lit(self, ip: int) -> int:
        value, new_ip = self.read_u32(ip)
        self.push(value)
        return new_ip

    def op_tuning(self, ip: int) -> int:
        idx, new_ip = self.read_byte(ip)
        self.push(self.tuning_field(idx))
        return new_ip

    def op_cpu(self, ip: int) -> int:
        idx, new_ip = self.read_byte(ip)
        self.push(self.cpu_field(idx))
        return new_ip

    def op_mul(self, ip: int) -> int:
        b_val = self.pop()
        a_val = self.pop()
        self.push((a_val * b_val) & MAX_U64)
        return ip

    def op_div_lit(self, ip: int) -> int:
        divisor, new_ip = self.read_byte(ip)
        if divisor == 0:
            raise BytecodeError("DIV_LIT divisor is zero")
        a_val = self.pop()
        self.push(a_val // divisor)
        return new_ip

    def op_align_up(self, ip: int) -> int:
        align = self.pop()
        value = self.pop()
        if align == 0:
            raise BytecodeError("ALIGN_UP align is zero")
        if (align & (align - 1)) != 0:
            raise BytecodeError(
                f"ALIGN_UP align {align} is not a power of two"
            )
        aligned = (value + align - 1) & ~(align - 1)
        self.push(aligned & MAX_U64)
        return ip

    def op_call_thunk(self, ip: int) -> int:
        fn_id, new_ip = self.read_u32(ip)
        fn = self.thunks.get(fn_id)
        if fn is None:
            raise BytecodeError(
                f"unregistered thunk id {fn_id}"
            )
        result = fn(self.cpu, self.profile)
        self.push(result)
        return new_ip


_OpHandler = Callable[[_Interpreter, int], int]

_DISPATCH: dict[Opcode, _OpHandler] = {
    Opcode.LIT: _Interpreter.op_lit,
    Opcode.TUNING: _Interpreter.op_tuning,
    Opcode.CPU: _Interpreter.op_cpu,
    Opcode.MUL: _Interpreter.op_mul,
    Opcode.DIV_LIT: _Interpreter.op_div_lit,
    Opcode.ALIGN_UP: _Interpreter.op_align_up,
    Opcode.CALL_THUNK: _Interpreter.op_call_thunk,
}


def _step(interp: _Interpreter, ip: int) -> int:
    op_byte, ip = interp.read_byte(ip)
    try:
        op = Opcode(op_byte)
    except ValueError as exc:
        raise BytecodeError(
            f"unknown opcode 0x{op_byte:02x}"
        ) from exc
    handler = _DISPATCH.get(op)
    if handler is None:  # pragma: no cover
        # Defensive: every Opcode value except END has a
        # _DISPATCH entry; END is intercepted by run_bytecode
        # before _step is called. Reachable only if a new opcode
        # is added to the enum without a corresponding
        # _DISPATCH entry — message is intentionally generic
        # so that a future "added an opcode, forgot the
        # handler" failure surfaces without the misleading
        # "END not handled" wording.
        raise BytecodeError(f"unhandled opcode: {op}")
    return handler(interp, ip)


def run_bytecode(
    code: bytes,
    cpu: CpuCharacteristics,
    profile: TuningProfile,
    thunks: Mapping[int, ThunkFn] | None = None,
) -> int:
    """Evaluate a bytecode expression against (cpu, profile).

    Stops at the first END opcode and returns the top of stack.
    Raises BytecodeError on any malformed condition: truncated
    payload, unknown opcode, stack under/overflow, division by
    zero, non-power-of-two alignment, missing thunk.
    """
    if not code:
        raise BytecodeError("empty bytecode")
    interp = _Interpreter(
        code=code, cpu=cpu, profile=profile,
        thunks=thunks or {}, stack=[],
    )
    ip = 0
    while ip < len(code):
        if code[ip] == Opcode.END.value:
            return _finish(interp)
        ip = _step(interp, ip)
    raise BytecodeError("bytecode missing END terminator")


def _finish(interp: _Interpreter) -> int:
    """Validate and return the stack-of-one result.

    A well-formed expression leaves exactly one value on the
    stack at END. Stricter than just "non-empty" — catches
    silent residue from malformed expressions like
    `LIT 1; LIT 2; END` (would otherwise return 2 with 1
    leftover). Tightens the contract the assembly side has to
    match in step 3.
    """
    if not interp.stack:
        raise BytecodeError("END reached with empty stack")
    if len(interp.stack) != 1:
        raise BytecodeError(
            f"END reached with {len(interp.stack)} stack "
            f"elements; expected exactly 1"
        )
    return interp.stack[-1]
