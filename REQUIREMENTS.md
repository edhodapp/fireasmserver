# fireasmserver — Requirements

This file records requirements derived from the project's design
decisions in `DECISIONS.md`. Each requirement is one verifiable
statement linked to its source decision(s) and to the
implementation and verification artifacts that satisfy it.

## Conventions

**Keyword usage** (IETF RFC 2119 / RFC 8174, ISO/IEC/IEEE 29148):

- `shall` / `shall not` — mandatory; absolute requirement or
  prohibition.
- `should` / `should not` — strongly preferred or discouraged;
  exceptions permitted with documented rationale.
- `may` — truly optional; permitted but not required.
- `must` and `will` are not used as requirement verbs.

**Sentence pattern** (INCOSE Guide for Writing Requirements V4):

> When [condition], the [named entity] shall [active verb]
> [object] [measurable criterion].

One thought per requirement. Active voice naming the responsible
entity. No subjective terms ("adequate", "reasonable"), escape
clauses ("where possible", "if necessary"), or open-ended phrases
("etc.", "including but not limited to").

**Numbering** — category-prefixed, sequential within each category:

- `MR-NNN`  memory regions / record format / ownership
- `AL-NNN`  allocator behavior / halt codes / phases
- `BC-NNN`  bytecode VM opcode definitions and evaluation
- `PR-NNN`  build-time profile selection
- `CP-NNN`  CPU characteristic detection
- `BS-NNN`  boot stage / mode-switch invariants

**Immutability** — each entry is immutable. Changes are made by
adding a new requirement that supersedes the old, with a
bidirectional annotation:

- New entry opens with `**Supersedes:** MR-NNN (deprecated
  YYYY-MM-DD HH:MM UTC). [reason]`
- Superseded entry gets `**DEPRECATED YYYY-MM-DD HH:MM UTC —
  superseded by MR-MMM. [reason]` prepended to its body, with
  the original body intact below.

**Status field values:**

- `implemented` — code and tests both exist; audit gate verifies
  refs resolve.
- `partial` — code exists; tests are missing or incomplete.
- `gap` — neither code nor tests yet.
- `spec-only` — requirement is established but implementation is
  not yet planned (intentional).

---

## Memory regions / record format / ownership (MR-NNN)

### MR-001: Owner identification on every memreq record

Every `.memreq` record shall declare exactly one `owner_id` value
at record offset 28 (uint16, little-endian).

The `owner_id` field shall hold one of three value classes:

- `0`           — the boot core.
- `1..N-1`      — a worker core, where `N` is the deployment-
  tuning `worker_core_count` value.
- `0xFFFF`      — a shared-readonly region accessible by all
  cores.

**Derived from:** D058 (actor model — no mutable state crosses a
core or VM boundary), D059 (audit invariants).

**Implementation refs:**

- `tooling/src/memlayout/models.py:MemoryRegion.owner_id`
- `tooling/src/memlayout/models.py:OWNER_BOOT_CORE`
- `tooling/src/memlayout/models.py:OWNER_SHARED_RO`
- `arch/x86_64/memory/memreq.inc` (macro arg `%5`)
- `arch/aarch64/memory/memreq.inc` (macro arg `\owner`)

**Verification refs:**

- `tooling/tests/test_memlayout_properties.py` (Hypothesis
  strategy `_region_strategy` exercises `owner_id` over the full
  `0..0xFFFF` range; valid construction confirms Pydantic field
  bounds)

**Status:** implemented


### MR-002: Lifetime tag values and range enforcement

Every `.memreq` record shall set the `lifetime` byte at record
offset 30 to one of four values:

- `0`  `STEADY_STATE`         — region lives the lifetime of
  the VM; assigned once at init, not freed.
- `1`  `INIT_ONLY`             — region is consumed only during
  init; may be reclaimed after `init_complete`.
- `2`  `IMMUTABLE_AFTER_INIT`  — region is written during init
  and must not be modified after `init_complete`. Carries
  `writable=0` invariant per MR-005.
- `3`  `STACK`                 — region is stack-shaped;
  reverse-bumped from `__ram_top`.

The init-time allocator shall halt with the error code
`MEMLAYOUT_ERR_BAD_LIFETIME` (=102) when the lifetime byte of
any record exceeds 3.

**Derived from:** D059.

**Implementation refs:**

- `tooling/src/memlayout/types.py:49` — `Lifetime(IntEnum)` with
  4 values: `STEADY_STATE=0`, `INIT_ONLY=1`,
  `IMMUTABLE_AFTER_INIT=2`, `STACK=3`
- `arch/x86_64/memory/allocator.S:84-87` — `LT_STEADY=0`,
  `LT_INIT_ONLY=1`, `LT_IMMUT=2`, `LT_STACK=3`; range halt at
  `arch/x86_64/memory/allocator.S:140-143` (`cmp eax, LT_STACK`
  / `ja .Lalloc_bad_lifetime`); halt body at line 297 stores
  `MEMLAYOUT_ERR_BAD_LIFETIME`=102 into `rc_out`
- `arch/aarch64/memory/allocator.S:88-91` — same constants;
  range halt at `arch/aarch64/memory/allocator.S:142-145`
  (`cmp w0, #LT_STACK` / `b.hi .Lalloc_bad_lifetime`); halt
  body at line 296 sets `MEMLAYOUT_ERR_BAD_LIFETIME`=102

**Verification refs:**

- `tooling/tests/test_memlayout_alloc_diff.py`
- `tooling/memlayout_diffharness/`

**Status:** implemented


### MR-003: Bootstrap stack is not a memreq region

The bootstrap stack reservation (linker symbol
`__bootstrap_stack_top`) shall be allocated by the linker
script of each cell, not declared as a `.memreq` record.

The init-time allocator shall not iterate the bootstrap stack
reservation.

**Derived from:** D059 ("Pre-stack bootstrap" paragraph).

**Implementation refs:**

- `arch/x86_64/platform/firecracker/linker.ld`
  (`__bootstrap_stack_top`, `__bootstrap_stack_size`)
- `arch/x86_64/platform/qemu/linker.ld`
- `arch/aarch64/platform/firecracker/linker.ld`
- `arch/aarch64/platform/qemu/linker.ld`

**Verification refs:**

- Tracer-bullet observes `READY` (cell boots through pre-allocator
  SP setup): `tooling/tracer_bullet/run_local.sh`,
  `tooling/tracer_bullet/pi_aarch64_firecracker.sh`

**Status:** implemented


### MR-004: Per-core kernel stacks are memreq regions

Each per-core kernel stack shall be declared as a `.memreq`
record with `lifetime=STACK` and the appropriate `owner_id`
(`0` for the boot core; `1..N-1` for worker cores).

The boot path on each core shall install its assigned per-core
stack address into `SP` after `init_memory_layout` returns and
before the actor loop entry on that core.

**Derived from:** D058, D059 ("the real stack region(s) are
declared in the memreq table and their addresses are installed
into SP/ESP after the allocator completes").

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** gap (D059 step-5 deliverable; not yet implemented)


### MR-005: Per-region writability flag

Every `.memreq` record shall set the `writable` byte at record
offset 31 to either `0` (read-only after `init_complete`) or
`1` (read/write at steady state).

When the lifetime tag is `IMMUTABLE_AFTER_INIT`, the writable
byte shall be `0`.

**Derived from:** D059 (audit invariants: `immutable_after_init`
regions are not marked `writable=1`).

**Implementation refs:**

- `tooling/src/memlayout/models.py:MemoryRegion.writable`
- `arch/x86_64/memory/memreq.inc` (macro arg `%7`)
- `arch/aarch64/memory/memreq.inc` (macro arg `\writable`)

**Verification refs:**

- `tooling/tests/test_memlayout_properties.py` (Hypothesis
  strategy `_region_strategy` exercises `writable` over
  `st.booleans()`; valid construction confirms Pydantic field
  type)
- (gap) audit invariant cross-check between lifetime and
  writable

**Status:** partial (field present and modeled; cross-field audit
invariant not yet implemented)


### MR-006: Record name identification by FNV-1a hash

Every `.memreq` record shall declare a `name_hash` value at
record offset 0 (uint32, little-endian) computed as the FNV-1a
hash of the human-readable region name.

The audit tooling shall verify that no two records in the same
build share a `name_hash`.

**Derived from:** D059.

**Implementation refs:**

- `tooling/src/memlayout/models.py:MemoryRegion.name_hash`
- `arch/x86_64/memory/memreq.inc` (macro arg `%2`)
- `arch/aarch64/memory/memreq.inc` (macro arg `\name_hash`)

**Verification refs:**

- `tooling/tests/test_memlayout_properties.py` (Hypothesis
  strategy `_region_strategy` exercises `name_hash` over the full
  `0..0xFFFFFFFF` range)
- (gap) hash-collision detection across the .memreq set

**Status:** partial (field present; collision audit not yet
implemented)


### MR-007: Record fixed layout — 48 bytes, 8-byte aligned

Every `.memreq` record shall occupy exactly 48 bytes with
the following fixed-offset field layout:

| Offset | Size | Field |
|--------|------|-------|
| 0      | 4    | `name_hash` (uint32) |
| 4      | 16   | `size_bytecode` (END-terminated) |
| 20     | 8    | `align_bytecode` (END-terminated) |
| 28     | 2    | `owner_id` (uint16) |
| 30     | 1    | `lifetime` (uint8) |
| 31     | 1    | `writable` (uint8) |
| 32     | 8    | `assigned_addr` (uint64) |
| 40     | 8    | `assigned_size` (uint64) |

The `.memreq` ELF section shall be 8-byte aligned.

**Derived from:** D059, D060.

**Implementation refs:**

- `arch/x86_64/memory/memreq.inc:15-32` — layout commentary
  block; macro definition at line 37 (`%macro memreq 7`)
- `arch/aarch64/memory/memreq.inc` (mirror of the x86_64
  layout)
- `arch/x86_64/memory/allocator.S:54-60` — `MR_OFF_NAME_HASH`,
  `MR_OFF_SIZE_BC`, `MR_OFF_ALIGN_BC`, `MR_OFF_LIFETIME`,
  `MR_OFF_ASSIGNED_ADDR`, `MR_OFF_ASSIGNED_SIZE`,
  `MR_RECORD_BYTES=48`
- `arch/aarch64/memory/allocator.S:66-72` — same constant set

**Verification refs:**

- `tooling/memlayout_diffharness/` — Python reference + per-arch
  assembly interpreter agree on record consumption byte-for-byte

**Status:** implemented


### MR-008: Allocator output fields written in place

The init-time allocator shall write the assigned absolute
address of each placed region into the `assigned_addr` field
at record offset 32 (uint64, little-endian).

The init-time allocator shall write the bytecode-evaluated size
of each placed region into the `assigned_size` field at record
offset 40 (uint64, little-endian).

These two fields shall be the only mutations the allocator
makes to a record after the linker has placed it.

**Derived from:** D059, D060.

**Implementation refs:**

- `arch/x86_64/memory/allocator.S` (writes to `MR_OFF_ASSIGNED_*`)
- `arch/aarch64/memory/allocator.S`

**Verification refs:**

- `tooling/memlayout_diffharness/` (allocator differential test)

**Status:** implemented

---

## Allocator behavior (AL-NNN)

### AL-001: Init-time allocation runs once

The init-time allocator shall run exactly once per VM lifetime
during boot, before any region's assigned address is consumed
by code other than the allocator itself.

After `init_complete`, no allocator code shall execute.

**Derived from:** D059, D060.

**Implementation refs:**

- `arch/x86_64/memory/init_memory_layout.S:42-43` —
  `init_memory_layout` entry; calls `memlayout_run_allocator`
- `arch/aarch64/memory/init_memory_layout.S:28-31` — same role

**Verification refs:**

- Tracer-bullet `LAYOUT-OK` marker (allocator returned
  successfully): `tooling/tracer_bullet/run_local.sh`,
  `tooling/tracer_bullet/pi_aarch64_firecracker.sh`

**Status:** partial (allocator wires up at boot; `init_complete`
fence not yet asserted)


### AL-002: Two-pass allocator — forward heap, reverse stack

The init-time allocator shall run two passes over the `.memreq`
record table:

- Pass 1 (forward) — for every record whose lifetime is not
  `STACK`, the allocator shall align-up the bump pointer to the
  record's alignment, assign that address, and advance the bump
  pointer by the record's size. The forward pass starts at
  `__heap_start` and proceeds toward `__ram_top`.
- Pass 2 (reverse) — for every record whose lifetime is `STACK`,
  the allocator shall align-down a reverse bump pointer (starting
  at `__ram_top`) by the record's size, then assign that address.
  The reverse pass proceeds toward `__heap_start`.

The allocator shall halt with `LAYOUT-OVERFLOW` when the forward
pass crosses the reverse pass.

**Derived from:** D059, D060.

**Implementation refs:**

- `arch/x86_64/memory/allocator.S:95-96` —
  `memlayout_run_allocator` entry; pass-1 forward loop at lines
  135-195 (`.Lalloc_pass1_loop` / `.Lalloc_pass1_skip` /
  `.Lalloc_pass2_init`); pass-2 reverse loop at lines 197-251
  (`.Lalloc_pass2_loop` / `.Lalloc_pass2_skip` / `.Lalloc_done`);
  forward/reverse-cross overflow halt at line 184 (`ja
  .Lalloc_overflow`)
- `arch/aarch64/memory/allocator.S:98` —
  `memlayout_run_allocator` entry; pass-1 at lines 137-190; pass-2
  at lines 192-242
- `tooling/src/memlayout/reference.py:98` — `_pass_forward`;
  `tooling/src/memlayout/reference.py:121` — `_pass_reverse`;
  `tooling/src/memlayout/reference.py:149` — `allocate` entry
  driving both passes

**Verification refs:**

- `tooling/memlayout_diffharness/` differential test
- `tooling/tests/test_memlayout_alloc_diff.py`

**Status:** implemented


### AL-003: Three-phase init sequence

The allocator shall execute three init phases in order:

1. **Phase 0** — populate the `cpu_characteristics` table from
   per-arch CPU registers. On unknown CPU model, the allocator
   shall load conservative defaults and emit `UNKNOWN-CPU
   model=<...>` on serial.
2. **Phase 1** — copy the selected build-time tuning profile
   from `.rodata` to the allocator-readable `tuning_profile`
   table and validate every field against its declared range.
   On range violation, the allocator shall halt with
   `PROFILE-INVALID field=X value=Y`.
3. **Phase 2** — walk the `.memreq` records, evaluate each
   record's `size_bytecode` and `align_bytecode` against the
   frozen Layer-1 + Layer-2 tables, and assign addresses per
   AL-002. On bytecode error, the allocator shall halt with
   `LAYOUT-INVALID`.

After phase 2, the allocator shall execute a memory barrier and
set the `init_complete` flag (phase 3).

**Derived from:** D060.

**Implementation refs:**

- `arch/x86_64/memory/allocator.S:95-253` — phase-2 walk over
  `.memreq` records (forward + reverse passes per AL-002);
  phase 0 (CPU detection) and phase 1 (profile copy/validate)
  are not yet implemented in this file
- `arch/aarch64/memory/allocator.S:98-242` — same role
- `tooling/src/memlayout/reference.py:149` — `allocate`
  reference driving phase 2

**Verification refs:**

- `tooling/memlayout_diffharness/` (covers phase 2)

**Status:** partial (phase 2 implemented; phases 0/1 and the
`init_complete` fence are gaps)


### AL-004: Allocator pre-stack discipline

**DEPRECATED 2026-05-02 03:52 UTC — superseded by AL-005.** The
implementation_refs below name the wrong registers; the same
defect that produced BC-002. The shall-clause itself (no memory
stack required during allocator phases) is correct and is
restated in AL-005 with corrected refs.

The allocator shall not require a memory stack during phase 0
or phase 2. The bytecode VM (per BC-NNN) shall execute on a
register-only stack (4-deep) so that no `.memreq` region's
size or alignment expression depends on a pre-existing stack.

**Derived from:** D059, D060.

**Implementation refs:**

- `arch/x86_64/memory/bytecode_vm.S` (uses rax/rcx/rdx/r8 only)
- `arch/aarch64/memory/bytecode_vm.S` (uses x0/x1/x2/x3 only)

**Verification refs:**

- `tooling/memlayout_diffharness/` (bytecode differential)

**Status:** implemented


### AL-005: Allocator pre-stack discipline (corrected)

**Supersedes:** AL-004 (deprecated 2026-05-02 03:52 UTC). The
shall-clause itself was correct; only the implementation_refs
needed correction (the registers named did not match the actual
code, propagated from the same defect in BC-002).

The allocator shall not require a memory stack during phase 0
or phase 2. The bytecode VM (per BC-005) shall execute on a
4-deep register-only stack so that no `.memreq` region's
size or alignment expression depends on a pre-existing stack.

**Derived from:** D059, D060.

**Implementation refs:**

- `arch/x86_64/memory/bytecode_vm.S` (4-deep stack in
  `r11`/`r10`/`r9`/`r15`; `bcvm_push`/`bcvm_pop` macros at
  lines 69, 79; entry/exit at `memlayout_run_bytecode`)
- `arch/aarch64/memory/bytecode_vm.S` (4-deep stack in
  `x10`/`x11`/`x12`/`x13`)

**Verification refs:**

- `tooling/memlayout_diffharness/` (bytecode differential —
  Python reference and per-arch interpreter agree on every
  operation; would diverge if either side touched a memory
  stack)
- `tooling/tests/test_memlayout_alloc_diff.py`

**Status:** implemented

---

## Bytecode VM (BC-NNN)

### BC-001: Opcode set and wire encoding

The bytecode VM shall recognize exactly the following opcodes
at the byte values listed:

| Opcode       | Byte | Payload    | Effect |
|--------------|------|------------|--------|
| `END`        | 0x00 | none       | Result is the value at top of stack |
| `LIT`        | 0x01 | u32 (LE)   | Push literal |
| `TUNING`     | 0x02 | u8 id      | Push `tuning_profile[id]` |
| `CPU`        | 0x03 | u8 id      | Push `cpu_characteristics[id]` |
| `MUL`        | 0x04 | none       | Pop b, pop a, push a*b |
| `DIV_LIT`    | 0x05 | u8 div     | Pop a, push a / div |
| `ALIGN_UP`   | 0x06 | none       | Pop align, pop value, push align_up(value, align) |
| `CALL_THUNK` | 0x07 | u32 id     | Call named thunk; push return value |

The VM shall not recognize any byte value as an opcode that is
not listed above; encountering an unrecognized opcode byte shall
halt the allocator with a bytecode error.

**Derived from:** D060.

**Implementation refs:**

- `tooling/src/memlayout/types.py:30` — `Opcode(IntEnum)`
  defining `END=0`, `LIT=1`, `TUNING=2`, `CPU=3`, `MUL=4`,
  `DIV_LIT=5`, `ALIGN_UP=6`, `CALL_THUNK=7`
- `arch/x86_64/memory/bytecode_vm.S:92-99` — `OP_*` constants;
  dispatch at lines 148-162 (`cmp eax, OP_*` + jump table)
- `arch/aarch64/memory/bytecode_vm.S:86-93` — same constants;
  dispatch at lines 141-155

**Verification refs:**

- `tooling/memlayout_diffharness/` (Python reference + per-arch
  interpreter agree on every opcode)

**Status:** implemented


### BC-002: Stack-machine depth and register residency

**DEPRECATED 2026-05-02 03:52 UTC — superseded by BC-005.** The
register names listed below are wrong: on x86_64 the
implementation must keep `rax` and `rdx` out of the stack because
the `mul` and `div` instructions clobber them implicitly via
ISA-mandated operand placement. The pre-bugfix design that
matched this requirement actually corrupted state on every
multiply. The original body is preserved below; the corrected
shall-clause is in BC-005.

The bytecode VM shall execute on a 4-deep stack machine whose
slots reside in registers, not memory.

On x86_64 the VM shall use rax, rcx, rdx, r8 as the four stack
slots.

On aarch64 the VM shall use x0, x1, x2, x3 as the four stack
slots.

**Derived from:** D060.

**Implementation refs:**

- `tooling/src/memlayout/types.py:STACK_DEPTH`
- `arch/x86_64/memory/bytecode_vm.S`
- `arch/aarch64/memory/bytecode_vm.S`

**Verification refs:**

- `tooling/memlayout_diffharness/` exercises stack-overflow and
  stack-underflow paths

**Status:** implemented


### BC-003: Bytecode budgets per record

Every `.memreq` record's `size_bytecode` field shall fit in 16
bytes including the trailing `END` opcode.

Every `.memreq` record's `align_bytecode` field shall fit in 8
bytes including the trailing `END` opcode.

The codegen tool shall reject any region whose bytecode exceeds
these budgets at codegen time.

**Derived from:** D060.

**Implementation refs:**

- `tooling/src/memlayout/types.py:SIZE_BYTECODE_BYTES,
  ALIGN_BYTECODE_BYTES`

**Verification refs:**

- `tooling/tests/test_memlayout_properties.py` (`_small_size_bytecode`
  generator caps at 16 bytes including `END`; `_power_of_two_bytecode`
  caps at 8 bytes; positive coverage of valid bytecode acceptance)
- (gap) negative test for oversized-bytecode rejection at the
  Pydantic layer
- (gap) negative test for codegen-time rejection (pending the
  codegen tool)

**Status:** partial (positive-path field-length validation via
properties test; negative tests and codegen-time rejection are gaps)


### BC-004: Bytecode field IDs are positional

The `tuning_profile` and `cpu_characteristics` tables shall be
positionally addressed: the byte payload of `TUNING` and `CPU`
opcodes is the index of the field within its struct.

Reordering fields in either struct shall require coordinated
update of every emitted bytecode that references those fields,
or the bytecode-vs-table version shall be detected and rejected
at audit time.

**Derived from:** D060.

**Implementation refs:**

- `tooling/src/memlayout/models.py:CpuCharacteristics`
- `tooling/src/memlayout/models.py:TuningProfile`

**Verification refs:**

- `tooling/memlayout_diffharness/` (differential ensures both
  ends agree on field-id mapping)

**Status:** partial (positional addressing is implemented;
reordering-detection audit is a gap)


### BC-005: Bytecode VM register stack allocation (corrected)

**Supersedes:** BC-002 (deprecated 2026-05-02 03:52 UTC). The
original named the wrong registers; the implementation actively
avoids `rax`/`rdx` because `mul`/`div` clobber them via
ISA-mandated operand placement, and the pre-bugfix design that
matched BC-002 corrupted state on every multiply.

The bytecode VM shall execute on a 4-deep stack machine whose
slots reside in registers, not memory.

On x86_64, the VM stack slots shall not occupy `rax` or `rdx`,
both of which the `mul` and `div` instructions clobber
implicitly via ISA-mandated operand placement. The current
allocation is `r11` (top), `r10`, `r9`, `r15` (bottom).

On aarch64, the VM stack slots shall not collide with `x0`,
which carries the call-struct pointer at function entry. The
current allocation is `x10` (top), `x11`, `x12`, `x13`
(bottom).

Internal register choice within the VM body is otherwise
unconstrained: the kernel runs without preemptive context
switching (D058 actor model — one actor per core, no
scheduler), so SysV (x86_64) and AAPCS64 (aarch64)
callee-saved register discipline applies only at the
external function-call boundary (preserved by the
function-prologue push and function-epilogue pop), not
internally.

**Derived from:** D058, D060.

**Implementation refs:**

- `arch/x86_64/memory/bytecode_vm.S` (header at lines 24-28
  documents the register map: `r11` top, `r10`, `r9`, `r15`
  bottom; rationale at lines 38-46 explains the `mul`/`rdx`
  clobber that drove the choice)
- `arch/x86_64/memory/bytecode_vm.S:69-89` (`bcvm_push` /
  `bcvm_pop` macros operate on the named registers)
- `arch/aarch64/memory/bytecode_vm.S` (header at lines 22-25
  documents the register map: `x10` top through `x13` bottom)

**Verification refs:**

- `tooling/memlayout_diffharness/` (per-arch interpreter +
  Python reference agree on every opcode; a wrong register
  allocation that corrupted `rdx` mid-stack would diverge)
- `tooling/tests/test_memlayout_bytecode.py`
- `tooling/tests/test_memlayout_diff.py`

**Status:** implemented

---

## Build-time profile selection (PR-NNN)

### PR-001: Single profile per build

A build of fireasmserver shall bake exactly one tuning profile
into the kernel artifact. The profile shall be selected at
build time via `make PROFILE=<name>`.

The kernel artifact filename shall include the profile name:
`kernel-<arch>-<platform>-<profile>.elf`.

The kernel shall not select a profile at runtime via command
line, configuration file, or any other dynamic mechanism.

**Derived from:** D059, D060.

**Implementation refs:** (none — gap; PROFILE makefile var not
yet wired)

**Verification refs:** (none — gap)

**Status:** gap (D060 deliverable; profile system not yet built)


### PR-002: Profile field range validation at boot

The init-time allocator's phase 1 shall validate every field of
the loaded tuning profile against its declared valid range.

On range violation, the allocator shall halt with
`PROFILE-INVALID field=<id> value=<v>` on serial.

**Derived from:** D060.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** gap

---

## CPU characteristic detection (CP-NNN)

### CP-001: Per-arch CPU detection registers

The init-time allocator's phase 0 on x86_64 shall populate
`cpu_characteristics` from `CPUID` leaves: `EAX=1` (family/model),
`EAX=4 ECX=0..N` (Intel cache topology) or
`EAX=0x80000005,0x80000006` (AMD cache topology), and `EAX=0xB`
(topology).

The init-time allocator's phase 0 on aarch64 shall populate
`cpu_characteristics` from system registers: `CTR_EL0` (D-cache
line size), `MIDR_EL1` (implementer/part identification),
`CLIDR_EL1` + `CCSIDR_EL1` (per-level cache sizes and
associativity), and `MPIDR_EL1` (cluster membership).

**Derived from:** D060.

**Implementation refs:** (none — gap; phase 0 stub only)

**Verification refs:** (none — gap)

**Status:** gap


### CP-002: Unknown-CPU fallback defaults

When the per-arch detection in phase 0 does not identify the
running CPU model, the allocator shall load the following
conservative defaults into `cpu_characteristics` and continue
boot:

- `l1d_line_bytes` = 64
- `l1d_bytes` = 32768
- `l1i_bytes` = 32768
- `l2_bytes` = 262144
- `l3_bytes_per_cluster` = 0
- `cores_sharing_l2` = 1
- `cores_sharing_l3` = 1
- `hw_prefetcher_stride_lines` = 0
- `detected_model_id` = 0

The allocator shall emit `UNKNOWN-CPU model=<XX:YY:Z>` on serial
where the bracketed value is the per-arch detection-source value
that did not match the known-model table.

**Derived from:** D059, D060.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** gap

---

## Boot stage / mode-switch invariants (BS-NNN)

### BS-001: x86_64 stage-1 identity-map coverage

The x86_64 stage-1 boot identity map shall cover the low 4 GiB
of physical address space using 2 MiB pages.

The page-table reservations (`__boot_pml4`, `__boot_pdpt`,
`__boot_pd`) shall be 4 KiB-aligned linker reservations, not
`.memreq` regions.

**Derived from:** D062, D063.

**Implementation refs:**

- `arch/x86_64/platform/firecracker/linker.ld:88-101` —
  `__boot_pml4`, `__boot_pdpt`, `__boot_pd` reservations
  (4 KiB + 4 KiB + 16 KiB, contiguous, 4 KiB-aligned); ASSERT
  guards at lines 134-138 enforce alignment, contiguity, and
  RAM-fit
- `arch/x86_64/platform/qemu/linker.ld:59-68` — same
  reservations; ASSERT guards at lines 83-87
- `arch/x86_64/memory/mode_switch.S` consumes the
  reservations (PD population fills 2048 entries × 2 MiB =
  4 GiB)

**Verification refs:**

- Tracer-bullet observes `LAYOUT-OK` and downstream markers
  after mode switch:
  `tooling/tracer_bullet/run_local.sh`

**Status:** implemented


### BS-002: x86_64 mode-switch sequence

The x86_64 stage-1 mode-switch shall execute the following
sequence in order before transferring control to
`kernel_main_64`:

1. Zero the page-table reservation block.
2. Populate `__boot_pd` with 2048 entries identity-mapping the
   low 4 GiB at 2 MiB granularity.
3. Link `__boot_pdpt[0..3]` to the four PD pages.
4. Link `__boot_pml4[0]` to `__boot_pdpt`.
5. `lgdt` the boot-time GDT.
6. Set `CR4.PAE`.
7. Load `CR3` with `__boot_pml4`.
8. Set `EFER.LME` and `EFER.NXE` simultaneously.
9. Set `CR0.PE | CR0.PG`.
10. Far-jump through the 64-bit code-segment selector.
11. Reload data segments (DS/ES/FS/GS to null; SS to DATA64_SEL).
12. Set `RSP` to `__bootstrap_stack_top`.
13. Jump to `kernel_main_64`.

**Derived from:** D062, D063, D064.

**Implementation refs:**

- `arch/x86_64/memory/mode_switch.S:81` —
  `mode_switch_to_long_mode` entry. The 13-step sequence:
  CR-bit constants at lines 65-70 (`CR4_PAE`, `CR0_PE`,
  `CR0_PG`, `EFER_LME`, `EFER_NXE`); CR4.PAE set at line 150
  (step 4 in the source, step 6 in BS-002 numbering — the
  source orders zero-tables / PML4-link / lgdt before
  CR4/CR3/EFER); EFER.LME|NXE set at line 170;
  CR0.PE|CR0.PG set at line 181

**Verification refs:**

- Tracer-bullet observes `LAYOUT-OK` and downstream virtio
  markers on x86_64/firecracker

**Status:** implemented


### BS-003: EFER.NXE enabled at stage 1

The x86_64 stage-1 mode-switch shall enable `EFER.NXE` (bit 11)
in step 6 of the mode-switch sequence (BS-002), simultaneously
with `EFER.LME`.

**Derived from:** D064.

**Implementation refs:**

- `arch/x86_64/memory/mode_switch.S` (step 6: `or eax,
  EFER_LME | EFER_NXE`)

**Verification refs:**

- x86_64/firecracker tracer-bullet (12 markers green confirms
  stage-1 mode switch and downstream virtio init)

**Status:** implemented

---

## Engineering authoring rules (ENG-NNN)

### ENG-001: Production runtime is 100% assembly

All code in `arch/<isa>/` that is linked into a guest artifact
shall be authored in arch-specific assembly per the project's
ISA toolchain (NASM on x86_64 per D048; GNU as on aarch64 per
D006). The guest artifact shall contain no compiled C, no
runtime linkage against libc or any other C standard library,
and no machine code originating from a high-level-language
compiler.

Tooling, codegen, and test harnesses outside `arch/` are
explicitly out of scope — Python, shell, and host C drivers
are permitted in `tooling/` per their established roles.

**Derived from:** D003.

**Implementation refs:**

- `arch/x86_64/` — NASM source tree.
- `arch/aarch64/` — GNU as source tree.

**Verification refs:**

- Pre-commit gates reject non-asm production sources by virtue
  of the build targeting these directories exclusively.

**Status:** implemented


---

## Astier FSA / VMIO automaton runtime (FSA-NNN)

### FSA-001: Single cooperative dispatcher per actor

Each per-core actor shall run a single cooperative dispatcher
loop. The dispatcher shall pull the next pending event from the
actor's priority wait queues and invoke the matching transition
handler. Preemption, kernel-thread scheduling, and operating-
system context switches shall not appear in the dispatcher path.

**Derived from:** D012 (VMIO automaton engine per Astier), D043
(FSA runtime model — statically-allocated per-type pools,
cooperative dispatch).

**Implementation refs:** (none — gap; dispatcher to land in a
later commit)

**Verification refs:** (none — gap)

**Status:** spec-only


### FSA-002: Priority wait queues per automaton

Each automaton instance shall expose a priority-ordered wait
queue. Pending events shall be enqueued by their priority class
and dequeued by the dispatcher in priority order.

**Derived from:** D012.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### FSA-003: Transition atomicity

A transition handler shall either complete fully or shall be
treated as if it never started. On any fault inside a
transition, the affected slot shall be rolled back to its pre-
transition state before control returns to the dispatcher.

**Derived from:** D012, D043.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### FSA-004: Per-type static slot pools

Each FSA species (L2 connection, L3 ARP entry, L4 TCB, HTTP
request, TLS context, future timers, etc.) shall reserve its
slot pool as a contiguous static array sized by a build-time
`.equ` constant. The full runtime memory footprint shall be
the sum across pool types and shall be known at link time.

**Derived from:** D043.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### FSA-005: No heap allocation in the steady state

After the init-complete fence (per D059), the dispatcher and
all transition handlers shall perform no heap allocation, no
heap deallocation, and no slot recycling that requires an
allocator decision. Slot reuse shall be drawn exclusively from
the per-type static pools (FSA-004) and shall zero the
reclaimed slot before returning it to a subsequent caller.

**Derived from:** D003, D043.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap; future static-analysis gate
shall reject `malloc`/`free` symbol references in linked output)

**Status:** spec-only


### FSA-006: Bounded-work transitions

Each transition handler shall complete within
`FSA_TRANSITION_BUDGET_NS` wall-clock nanoseconds. Handlers
whose worst-case bound cannot be proven to fit shall be split
into multiple transitions with interleaved dispatcher passes.

**Derived from:** D043.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap; perf-ratchet baseline shall
include the transition-time budget per D040)

**Status:** spec-only


### FSA-007: Per-layer pool-exhaustion behavior

Each FSA layer shall declare its response to a "pool full"
condition at design time. The dispatcher shall not produce
silent drops outside of explicitly-named layers (ARP,
reassembly per D043's backpressure table).

**Derived from:** D043.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


---

## Foundational abstractions (ABS-NNN)

### ABS-001: Bottom-up layered abstractions

The project shall build foundational abstractions (event
formats, queue structures, transition-table layouts, context
struct shapes) in the lowest applicable layer before any
higher-layer code consumes them. Higher-layer designs shall not
reach across an abstraction boundary to inspect or mutate
lower-layer state.

**Derived from:** D013 (foundational abstractions, Lextrait).

**Implementation refs:** (cross-cutting — applies to every new
module)

**Verification refs:** (none — non-mechanical; enforced through
review)

**Status:** spec-only


---

## Filesystem and content storage (FS-NNN)

### FS-001: FAT32 content filesystem

Web content served by the guest shall be stored on a FAT32
filesystem on the Firecracker virtio-block device. The guest
shall not parse any other filesystem format for content
delivery.

**Derived from:** D019 (FAT32 read-only for virtio-block content
filesystem).

**Implementation refs:** (none — gap; FAT32 reader to land
post-L2)

**Verification refs:** (none — gap)

**Status:** spec-only


### FS-002: Content filesystem is read-only

The guest's FAT32 driver shall implement read-only access. The
driver shall not implement any write, allocation, journaling,
or directory-mutation path.

**Derived from:** D019.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


---

## TLS protocol stack (TLS-NNN)

### TLS-001: TLS 1.3 and TLS 1.2 are both supported

The TLS implementation shall support TLS 1.3 (RFC 8446) and
TLS 1.2 (RFC 5246) negotiation. The implementation shall not
negotiate SSL 3.0, TLS 1.0, or TLS 1.1.

**Derived from:** D031.

**Implementation refs:** (none — gap; primitives only today,
no protocol layer yet)

**Verification refs:** (none — gap; eventual interop:
`openssl s_client` matrix, `gnutls-cli`, `testssl.sh`)

**Status:** spec-only


### TLS-002: Key-exchange algorithm set

The TLS implementation shall support the following key-exchange
algorithms and shall not negotiate any algorithm outside this
set:

- ECDHE over x25519, secp256r1, secp384r1.
- FFDHE over ffdhe2048, ffdhe3072, ffdhe4096 (RFC 7919).
- RSA key-exchange in TLS 1.2 only (required for 2013-onwards
  client compatibility).

**Derived from:** D031.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### TLS-003: Cipher-suite set

The TLS implementation shall negotiate cipher suites only from
the following set:

- TLS 1.3: `TLS_AES_128_GCM_SHA256`, `TLS_AES_256_GCM_SHA384`,
  `TLS_CHACHA20_POLY1305_SHA256`, `TLS_AES_128_CCM_SHA256`,
  `TLS_AES_128_CCM_8_SHA256`.
- TLS 1.2: ECDHE-ECDSA / ECDHE-RSA / DHE-RSA with AES-GCM,
  AES-CBC-HMAC-SHA, or ChaCha20-Poly1305.

The implementation shall not negotiate RC4, 3DES, EXPORT,
anonymous, or static-DH suites.

**Derived from:** D031.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### TLS-004: Signature-algorithm set

The TLS implementation shall support the following signature
algorithms: ECDSA over P-256 / P-384 / P-521, RSA-PSS,
RSA-PKCS1 v1.5 (TLS 1.2 compatibility only), and Ed25519.

**Derived from:** D031.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### TLS-005: SNI, ALPN, OCSP stapling, secure renegotiation

The TLS implementation shall support: Server Name Indication
(RFC 6066), Application-Layer Protocol Negotiation (RFC 7301),
OCSP stapling, secure renegotiation (RFC 5746), and TLS 1.3
key update.

**Derived from:** D031.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### TLS-006: Certificate lifecycle

The TLS implementation shall include an ACMEv2 client
(RFC 8555) for certificate issuance against Let's Encrypt and
other ACME-compatible CAs, a private-CA injection path for OEM
provisioning, full chain validation against configurable trust
anchors, and hostname matching per RFC 6125.

**Derived from:** D031.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### TLS-007: Mutual TLS (client certificate authentication)

The TLS implementation shall support client-certificate
authentication (mTLS), including validation of the client
chain against configurable trust anchors and signature
verification against the negotiated signature-algorithm set
(TLS-004).

**Derived from:** D031.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### TLS-008: Session resumption

The TLS implementation shall support session resumption: PSK
resumption in TLS 1.3, session ID and session tickets in
TLS 1.2.

**Derived from:** D031.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### TLS-009: 0-RTT early data with replay protection

The TLS 1.3 implementation shall support 0-RTT early data,
shall implement single-use ticket replay protection, and shall
enforce a per-deployment replay window.

**Derived from:** D031.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### TLS-010: Extended Master Secret and Encrypt-then-MAC

The TLS 1.2 implementation shall support Extended Master Secret
(RFC 7627) and shall negotiate Encrypt-then-MAC (RFC 7366) for
CBC cipher suites when the peer advertises support.

**Derived from:** D031.

**Implementation refs:** (none — gap)

**Verification refs:** (none — gap)

**Status:** spec-only


### TLS-011: Heartbeat extension explicitly excluded

The TLS implementation shall not implement the Heartbeat
extension (RFC 6520) and shall not negotiate it under any
configuration.

**Derived from:** D031.

**Implementation refs:** (Heartbeat code is absent by
construction; nothing to point at)

**Verification refs:** (eventual `testssl.sh` interop sweep
shall confirm no `heartbeat_enabled`)

**Status:** spec-only


---

## Crypto math implementation (CRYPTO-NNN)

### CRYPTO-001: ISA-idiomatic primitives

Each crypto primitive shall be implemented to use its target
arch's native instruction set. Implementations shall not write
to a least-common-denominator interface across arches.

**Derived from:** D031, D032.

**Implementation refs:**

- `arch/x86_64/crypto/aes128.S` — AES-NI path.
- `arch/x86_64/crypto/sha256.S` — SHA-NI / AVX2 fallback.
- `arch/x86_64/crypto/aes128_gcm.S` — PCLMULQDQ for GHASH.
- `arch/x86_64/crypto/crc32_ieee.S` — PCLMULQDQ fold-by-4.
- `arch/aarch64/crypto/aes128.S` — ARMv8 AES.
- `arch/aarch64/crypto/sha256.S` — ARMv8 SHA-256.
- `arch/aarch64/crypto/aes128_gcm.S` — PMULL for GHASH.
- `arch/aarch64/crypto/crc32_ieee.S` — PMULL fold path.

**Verification refs:**

- `tooling/tests/test_aes128.py`, `tooling/tests/test_sha256.py`,
  `tooling/tests/test_aes128_gcm.py`,
  `tooling/tests/test_crc32_ieee.py`

**Status:** implemented


### CRYPTO-002: Macros-first implementation

Performance-critical bignum and crypto routines shall be
authored as assembler macros that inline at each call site,
not as subroutines that take an ABI cost. The default policy
shall be macros; subroutine refactoring shall be deferred until
a specific instruction-cache or deployment-footprint constraint
is identified.

**Derived from:** D032.

**Implementation refs:** (cross-cutting — every primitive)

**Verification refs:** (verified by review against D032)

**Status:** partial


### CRYPTO-003: Constant-time discipline

Crypto primitives shall not branch on data values derived from
secret inputs and shall not access memory addresses derived
from secret inputs. T-table-based AES is explicitly forbidden;
implementations shall use the hardware AES instructions
instead.

**Derived from:** D032, D057.

**Implementation refs:**

- AES implementations use hardware AES instructions; no
  S-box tables.

**Verification refs:**

- Manual review during commit; constant-time checks against
  Wycheproof corpus during interop tier.

**Status:** partial


### CRYPTO-004: Cache-aware data layout

Hot crypto state shall be aligned to a cache line boundary
(`.balign 64`). Hot/cold field separation shall keep
per-connection working sets minimized relative to L1D capacity
in the active hardware profile.

**Derived from:** D032.

**Implementation refs:**

- `.balign 64` on AES round keys and SHA state in the existing
  crypto sources.

**Verification refs:** (manual review against D032 + D034)

**Status:** partial


### CRYPTO-005: PCLMULQDQ fold-by-4 constant set

The x86_64 PCLMULQDQ fold-by-4 path shall reuse the fold-by-1
reduction constants. The on-chip constants table shall contain
exactly four exponents: `x^576 mod P`, `x^512 mod P`,
`x^192 mod P`, and `x^128 mod P`.

**Derived from:** D050.

**Implementation refs:**

- `arch/x86_64/crypto/crc32_ieee.S` — fold-by-4 main loop and
  reduction, constants in `.rodata.crc32_pclmul_consts`.
- `tooling/crypto_tests/derive_fold_constants.py` — derivation
  + verification against `zlib.crc32`.

**Verification refs:**

- `tooling/tests/test_derive_fold_constants.py` (361 tests).
- `tooling/crypto_tests/crc32_test.c` (264 lengths × 3 code
  paths per arch under pre-push integration gate).

**Status:** implemented


### CRYPTO-006: AES-NI required at runtime on x86_64

The x86_64 AES primitive shall refuse to run on a host CPU
that does not advertise `CPUID.(EAX=1):ECX[bit 25]` (AES-NI).
The implementation shall not provide a scalar fallback path.

**Derived from:** D057.

**Implementation refs:**

- `arch/x86_64/crypto/aes128.S:aes128_has_aesni` — CPUID probe.
- `arch/x86_64/crypto/aes128.S:aes128_encrypt_block` and
  `aes128_decrypt_block` — AES-NI-only.

**Verification refs:**

- `tooling/tests/test_aes128.py` (covers AES128-001).

**Status:** implemented


---

## Hardware platform profiles (PROFILE-NNN)

### PROFILE-001: Build-time profile selection

Each guest artifact shall be built against exactly one hardware
profile. The selected profile shall be embedded in `.rodata` and
shall determine the values exposed in `cpu_characteristics`
(per D034). Runtime CPU-dispatch shall not appear in the initial
implementation; profile selection shall happen at
`make ARCH=<arch> PROFILE=<name>` time.

**Derived from:** D034.

**Implementation refs:** (gap — profile tables not yet
declared as `.S` includes; `make PROFILE=…` not yet wired)

**Verification refs:** (gap)

**Status:** spec-only


### PROFILE-002: Crypto-extension profile conformance floor

Every aarch64 production profile shall expose `HAS_ARMV8_AES`,
`HAS_ARMV8_SHA256`, and `HAS_PMULL`. Every x86_64 production
profile shall expose `HAS_AES_NI` and `HAS_PCLMULQDQ`. Profiles
without these features shall not be considered production
profiles.

**Derived from:** D034.

**Implementation refs:** (gap — flags not yet declared)

**Verification refs:** (gap)

**Status:** spec-only


---

## VirtIO MVP scope (VIO-MVP-NNN)

### VIO-MVP-001: Single-queue MVP

The first-release L2 driver shall negotiate one RX queue
(queue 0) and one TX queue (queue 1). The driver shall not
negotiate `VIRTIO_NET_F_MQ` (bit 22). The RX path shall hard-
code queue 0 and the TX path shall hard-code queue 1.

**Derived from:** D053.

**Implementation refs:**

- `arch/x86_64/platform/firecracker/boot.S` and
  `arch/aarch64/platform/firecracker/boot.S` — `program_queue
  0, …` and `program_queue 1, …`.

**Verification refs:**

- `tooling/tracer_bullet/run_local.sh` x86_64/firecracker cell
  observes `QUEUES:RX=0x100 TX=0x100 / QUEUES:READY`.

**Status:** implemented


---

## L2 architectural accommodations (L2-NNN)

The L2-specific conformance requirement table at
`docs/l2/REQUIREMENTS.md` carries the per-standard rows
(ETH-NNN, VLAN-NNN, VIO-NNN, etc.). The L2-NNN entries below
are the architectural-shape requirements that D045 codified —
the requirements that say "the hot-path data layout shall
accommodate feature X even when MVP runtime defaults X off."

### L2-001: VLAN-tagged RX frames accepted

The L2 RX parser shall accept tagged frames (EtherType
`0x8100` or `0x88A8`), shall strip the tag, and shall expose
the VID to the L3 handoff metadata. No filter enforcement
shall run by default at MVP runtime.

**Derived from:** D045 (supersedes D044's VLAN-out-of-scope
clause).

**Implementation refs:** (gap — L2 RX parser not yet
implemented)

**Verification refs:** (gap; eventual: `docs/l2/REQUIREMENTS.md`
`VLAN-001..VLAN-005`)

**Status:** spec-only


### L2-002: Multi-queue framework designed in

The L2 dispatcher shall index pools by queue, with RX/TX
pools represented as per-queue arrays. The MVP runtime shall
build with `NUM_QUEUES = 1` and shall expose the per-queue
indirection to retrofit-free MQ enablement.

**Derived from:** D045.

**Implementation refs:** (gap)

**Verification refs:** (gap)

**Status:** spec-only


### L2-003: Checksum and GSO metadata passthrough

The L2 parser shall populate `virtio_net_hdr.flags`,
`csum_start`, `csum_offset`, `gso_type`, `hdr_len`, and
`gso_size` on RX and shall pass the populated header struct
intact to the L4 handoff metadata. The L2 layer shall not
gate on metadata values; gating shall happen at L4.

**Derived from:** D045.

**Implementation refs:** (gap)

**Verification refs:** (gap)

**Status:** spec-only


---

## External-input boundary discipline (BOUNDARY-NNN)

### BOUNDARY-001: Length clamp at the public crypto API boundary

The trusted-`len` contract for every public crypto primitive
shall live with the caller, at the trust boundary where the
length first crosses from external input into the guest.
Public crypto primitives shall not re-validate `len` at every
invocation. The project-wide bound shall be
`FIREASMSERVER_MAX_HASH_LEN = 1 << 40` bytes (1 TiB).

**Derived from:** D055.

**Implementation refs:**

- `arch/<isa>/crypto/sha256.S` documents the trusted-`len`
  contract.

**Verification refs:** (gap; protocol-layer callers will
verify the clamp in their respective test suites)

**Status:** spec-only


### BOUNDARY-002: ISA overflow detection on external-input arithmetic

Every arithmetic step that operates on external-input-derived
values (length math, offset computation, index multiplication,
size accumulation from network or untrusted input) shall use
the ISA's overflow-detection primitives (`jc` / `jo` on
x86_64; `adds` / `subs` + `b.cs` / `b.vs` and `umulh` /
`smulh` on aarch64). Silent-truncate arithmetic shall not be
used at external-input boundaries.

**Derived from:** D055, D056.

**Implementation refs:**

- `arch/x86_64/crypto/sha256.S` and the SHA-NI siblings carry
  the pattern; new primitives inherit the discipline by review.

**Verification refs:** (manual review during commit;
constant-time + overflow-discipline review of every new
primitive)

**Status:** partial
