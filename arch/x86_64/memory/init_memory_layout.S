; SPDX-License-Identifier: AGPL-3.0-or-later
;
; Init-time memory layout driver (D059 step 4.2). Builds the
; memlayout_call_t (88 bytes, layout per allocator.S) on the
; bootstrap stack, hands it to memlayout_run_allocator, and
; returns on success — at which point every .memreq record has
; its `assigned_addr` and `assigned_size` populated.
;
; Pre-D066 step 5d this function was a stub (`ret`) — the
; symbol existed and boot.S called it, but the actual allocator
; never ran, so every region's assigned_addr stayed at the
; link-time 0 placeholder. Step 5b/5c "passed" the tracer-bullet
; only because every consumer dereferenced 0 and the kernel +
; Firecracker coincidentally agreed on the (wrong) 0-based
; addresses. Step 5d's stack-switch broke the spell because
; `mov rsp, 0` then `call` faults. This wires the real path.
;
; Layout assumptions:
;   - No CPU intrinsics or TUNING references in the bytecode
;     (D066 Q-F: literal-only LIT through step 5c; non-literal
;     deferred per task #28). cpu_values / cpu_count /
;     tun_values / tun_count are zeroed.
;   - LIT-only bytecode never touches those tables; if a future
;     region adds CPU/TUNING references without first populating
;     these fields, the bytecode VM will return
;     BCVM_ERR_CPU_FIELD_OOR or _TUNING_FIELD_OOR and this
;     function halts via .alloc_failed.
;
; Halt-on-error: the bootstrap stack is not yet replaced by the
; kernel stack at this point in the boot flow (step 5d's SP
; switch happens after init_memory_layout returns), so we can't
; safely call into emit_bytes — boot.S owns that helper and
; relies on a stable stack discipline. Instead, on non-zero
; rc_out we hlt-loop. The tracer-bullet observable is "READY
; observed but LAYOUT-OK missing," which the parser already
; reports as "init_memory_layout did not return cleanly?"
;
; Bits-mode shim: x86_64 firecracker boots via PVH (elf64);
; x86_64 qemu boots via Multiboot1 (elf32). The PVH path runs
; the real allocator; the Multiboot1 path keeps the legacy stub
; behavior (the qemu cell doesn't have a virtio device today
; and isn't part of D066's step-5 rollout).

%ifidn __OUTPUT_FORMAT__, elf64
[bits 64]

section .text

extern memlayout_run_allocator
extern __memreq_start
extern __memreq_end
extern __heap_start
extern __ram_top

global init_memory_layout
init_memory_layout:
    ; memlayout_call_t is 88 bytes. Entry rsp%16==8 (just past
    ; the caller's call-pushed return addr); sub 88 → rsp%16==0,
    ; matching the SysV pre-call alignment requirement for the
    ; downstream `call memlayout_run_allocator`.
    sub     rsp, 88

    mov     rax, __memreq_start
    mov     [rsp + 0],  rax
    mov     rax, __memreq_end
    mov     [rsp + 8],  rax
    mov     qword [rsp + 16], 0          ; cpu_values  (unused)
    mov     qword [rsp + 24], 0          ; cpu_count
    mov     qword [rsp + 32], 0          ; tun_values  (unused)
    mov     qword [rsp + 40], 0          ; tun_count
    mov     rax, __heap_start
    mov     [rsp + 48], rax
    mov     rax, __ram_top
    mov     [rsp + 56], rax
    ; Output fields (rc_out, forward_out, reverse_out at +64/+72/+80)
    ; are pure write surfaces — the allocator never reads them before
    ; writing. Zeroing them anyway is defensive: a future allocator
    ; version that reads an output before write won't see stack junk.
    mov     qword [rsp + 64], 0          ; rc_out
    mov     qword [rsp + 72], 0          ; forward_out
    mov     qword [rsp + 80], 0          ; reverse_out

    mov     rdi, rsp
    call    memlayout_run_allocator

    ; rc_out is a u32 stored in the low 4 bytes of an 8-byte
    ; slot; we zeroed the slot above, so testing the low 32 bits
    ; suffices.
    mov     eax, [rsp + 64]
    test    eax, eax
    jnz     .alloc_failed

    add     rsp, 88
    ret

.alloc_failed:
    ; Pre-stack-switch fatal. Tracer-bullet sees READY but not
    ; LAYOUT-OK — sufficient diagnostic at this layer.
    add     rsp, 88
.halt:
    hlt
    jmp     .halt

%else
; Multiboot1 [bits 32] path — stub kept for the legacy qemu cell.
; D066 step 5 doesn't touch this path; if a future qemu virtio
; lands, this branch grows the same wire-up.
[bits 32]

section .text

global init_memory_layout
init_memory_layout:
    ret
%endif

; PT_NOTE for non-executable stack — match boot.asm's convention.
section .note.GNU-stack noalloc noexec nowrite progbits
