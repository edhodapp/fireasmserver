// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Init-time memory layout driver (D059 step 4.2), aarch64.
// Mirrors arch/x86_64/memory/init_memory_layout.S (commit
// a661828) — builds the 88-byte memlayout_call_t on the
// bootstrap stack, hands it to memlayout_run_allocator, and
// returns on success once every .memreq record has its
// assigned_addr / assigned_size populated.
//
// Layout assumptions: D066 Q-F constrains the early-step
// rollout to LIT-only bytecode (CPU / TUNING references
// deferred per task #28), so cpu_values / cpu_count /
// tun_values / tun_count are zeroed.
//
// Halt-on-error: tracer-bullet observes READY but not LAYOUT-OK,
// which the parser already reports as "init_memory_layout did
// not return cleanly?" Pre-stack-switch context can't safely
// branch into boot.S's emit_bytes without a stable stack
// discipline.

    .text
    .globl  init_memory_layout
    .type   init_memory_layout, %function
    .balign 4
init_memory_layout:
    // The caller (boot.S) pre-loads x0 with UART_BASE so the
    // emit_bytes/emit_hex32 helpers after the bl can fire without
    // re-deriving it. AAPCS64 doesn't promise x0 preservation
    // across a call (x0-x7 are caller-saved volatile), but the
    // pre-existing stub `ret` honored that informal contract by
    // touching no registers, and boot.S relies on it. Preserve
    // x0 explicitly on the frame so the wire-up doesn't break
    // the contract a `ret`-stub silently maintained.
    //
    // Frame: 112 bytes. memlayout_call_t at [sp, #0..#87];
    // saved x0 (UART_BASE) at [sp, #88]; saved x30 (LR) at
    // [sp, #96]; 8 bytes of padding at [sp, #104..#111] keep
    // the frame 16-aligned per AAPCS64.
    sub     sp, sp, #112
    stp     x0, x30, [sp, #88]

    // Populate the struct. x0 is scratch from here through the
    // call; it's restored from the saved slot before ret.
    //
    // PC-relative addressing (adrp + add) for __memreq_start /
    // __memreq_end is load-bearing: the firecracker linker
    // script uses `. = 0x0` convention so these labels carry a
    // file-offset value, not a runtime address. `ldr x0, =sym`
    // would put the file offset (e.g. 0xd90) into x0; the
    // allocator would then bus-fault dereferencing low physical
    // memory. `adrp + add` computes the runtime address from
    // PC, correct regardless of the link-time convention.
    //
    // __heap_start and __ram_top come from explicit linker
    // expressions (linker.ld lines 87, 111-112) that already
    // include the RAM_BASE + text_offset adjustment, so `ldr
    // =sym` is correct for those — the symbol's value IS the
    // runtime address.
    adrp    x0, __memreq_start
    add     x0, x0, :lo12:__memreq_start
    str     x0, [sp, #0]
    adrp    x0, __memreq_end
    add     x0, x0, :lo12:__memreq_end
    str     x0, [sp, #8]
    str     xzr, [sp, #16]              // cpu_values  (unused)
    str     xzr, [sp, #24]              // cpu_count
    str     xzr, [sp, #32]              // tun_values  (unused)
    str     xzr, [sp, #40]              // tun_count
    ldr     x0, =__heap_start
    str     x0, [sp, #48]
    ldr     x0, =__ram_top
    str     x0, [sp, #56]
    str     xzr, [sp, #64]              // rc_out  (output)
    str     xzr, [sp, #72]              // forward_out
    str     xzr, [sp, #80]              // reverse_out

    // Call: x0 = &memlayout_call_t (AAPCS64 first arg).
    mov     x0, sp
    bl      memlayout_run_allocator

    // rc_out is a u32 in the low 4 bytes of the 8-byte slot.
    // w9 is caller-saved (x9-x17 in AAPCS64), no save needed.
    ldr     w9, [sp, #64]
    cbnz    w9, .Lalloc_failed

    // Restore x0 (UART_BASE) + LR, then return.
    ldp     x0, x30, [sp, #88]
    add     sp, sp, #112
    ret

.Lalloc_failed:
    // Pre-stack-switch fatal. Tracer-bullet sees READY but not
    // LAYOUT-OK — sufficient diagnostic at this layer. Halt
    // loudly via WFI loop. Don't bother restoring frame; we
    // never return from this path.
.Lhalt:
    wfi
    b       .Lhalt

    .size   init_memory_layout, . - init_memory_layout

    .section .note.GNU-stack, "", %progbits
