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
    // memlayout_call_t is 88 bytes; one extra qword keeps SP
    // 16-byte aligned per AAPCS64.
    sub     sp, sp, #96
    // Preserve LR so we can return cleanly.
    str     x30, [sp, #88]

    // Populate the struct.
    ldr     x0, =__memreq_start
    str     x0, [sp, #0]
    ldr     x0, =__memreq_end
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
    ldr     w0, [sp, #64]
    cbnz    w0, .Lalloc_failed

    ldr     x30, [sp, #88]
    add     sp, sp, #96
    ret

.Lalloc_failed:
    // Pre-stack-switch fatal. Tracer-bullet sees READY but not
    // LAYOUT-OK — sufficient diagnostic at this layer. Halt
    // loudly via WFI loop.
.Lhalt:
    wfi
    b       .Lhalt

    .size   init_memory_layout, . - init_memory_layout

    .section .note.GNU-stack, "", %progbits
