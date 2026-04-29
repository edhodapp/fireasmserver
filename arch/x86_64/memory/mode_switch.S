; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; x86_64 stage-1 mode-switch body — shared across PVH (Firecracker)
; and Multiboot1 (QEMU) per D062 + D063.
;
; ENTRY: mode_switch_to_long_mode
;   State on entry:
;     [bits 32] protected mode, paging off, IF=0
;     ESP set to a valid 32-bit stack by the caller (the per-platform
;       prologue, task #5 in D062's plan)
;     CS = flat 32-bit code segment from the loader
;     DF, segment registers in their loader-handed state
;
;   State on exit: control transferred to kernel_main_64 in [bits 64]
;     long mode, paging on, low 4 GiB identity-mapped at 2 MiB
;     granularity (D063), RSP at __bootstrap_stack_top (D060 step 4.1),
;     null data-segment selectors loaded.
;
;   This is a JUMP target, not a callable: control never returns to
;   the caller's [bits 32] stack frame because the mode switch
;   invalidates the 32-bit return address. Each platform's prologue
;   does `jmp mode_switch_to_long_mode`, not `call`.
;
; Sequence (D062 steps 2-11; D063 amends step 2's PD population):
;   2.  Zero the 24 KiB page-table reservation block, populate
;       __boot_pd's 2048 PD entries (4 GiB at 2 MiB), link
;       __boot_pdpt's 4 PDPT entries, link __boot_pml4's 1 PML4 entry.
;   3.  lgdt the boot-time GDT.
;   4.  CR4.PAE := 1.
;   5.  CR3 := __boot_pml4.
;   6.  EFER.LME := 1 (wrmsr to MSR 0xC0000080).
;   7.  CR0.PE | CR0.PG := 1.
;   8.  Far jump CS:label_64 to land in [bits 64].
;   9.  Reload DS/ES/SS/FS/GS to null selectors.
;   10. mov rsp, __bootstrap_stack_top.
;   11. jmp kernel_main_64.
;
; kernel_main_64 today calls init_memory_layout and halts. Task #7
; will inject LAYOUT-OK serial emission between the call and the halt.
; Task #14 will route past the halt into the [bits 64]-ported virtio
; init for firecracker.
;
; NASM per D048.

extern __boot_pml4
extern __boot_pdpt
extern __boot_pd
extern __bootstrap_stack_top
extern init_memory_layout

; --- Page-table entry flag bits (Intel SDM Vol 3A §4.5) ---
PAGE_PRESENT  equ 1 << 0
PAGE_WRITABLE equ 1 << 1
PAGE_PSE      equ 1 << 7   ; in PD entries: 1 = 2 MiB page

; --- Control-register / MSR bits ---
CR4_PAE       equ 1 << 5
CR0_PE        equ 1 << 0
CR0_PG        equ 1 << 31
EFER_MSR      equ 0xC0000080
EFER_LME      equ 1 << 8

; --- GDT segment selectors ---
;   index << 3 | TI=0 (GDT) | RPL=0 (kernel)
CODE64_SEL    equ 0x08
DATA64_SEL    equ 0x10

section .text

[bits 32]

global mode_switch_to_long_mode
mode_switch_to_long_mode:
    ; Defensive: PVH and Multiboot1 both deliver IF=0 / DF=0 per
    ; spec, but a buggy prologue or future caller could violate
    ; those assumptions. The two bytes here cost nothing and
    ; remove an entire class of "interrupt fires before IDT is
    ; installed" failure modes.
    cli
    cld

    ; ----- Step 2: page-table population -----

    ; 2a: zero the entire 24 KiB reservation block
    ;   (__boot_pml4 + __boot_pdpt + __boot_pd) in one rep stosd.
    ;   Layout from the linker script: pml4 (4 KiB) + pdpt (4 KiB)
    ;   + pd (16 KiB), contiguous. 24 KiB / 4 = 6144 dwords.
    mov edi, __boot_pml4
    xor eax, eax
    mov ecx, 6144
    rep stosd

    ; 2b: populate __boot_pd. 2048 entries, each 8 bytes; entry i
    ;   maps phys [i*2 MiB, (i+1)*2 MiB) with PRESENT|WRITABLE|PSE.
    ;   eax holds the running phys-address low-32-bits across the
    ;   loop; the high-32-bits are zero (low-4-GiB phys only).
    mov edi, __boot_pd
    mov ecx, 2048
    xor eax, eax            ; phys = 0
.fill_pd:
    mov edx, eax
    or  edx, PAGE_PRESENT | PAGE_WRITABLE | PAGE_PSE
    mov [edi], edx          ; lo: addr | flags
    mov dword [edi+4], 0    ; hi: 0 (zeroed by stosd above, redundant
                            ; but explicit — defends against a future
                            ; refactor that drops the wholesale zero)
    add eax, 1 << 21        ; advance phys by 2 MiB
    add edi, 8
    dec ecx
    jnz .fill_pd

    ; 2c: link __boot_pdpt[0..3] → __boot_pd[0..3]. One PDPTE per
    ;   GiB; eax tracks the running PD page address.
    mov edi, __boot_pdpt
    mov eax, __boot_pd
    or  eax, PAGE_PRESENT | PAGE_WRITABLE
    mov [edi+0],  eax       ; PDPTE[0] → __boot_pd + 0x0000 (0..1 GiB)
    add eax, 0x1000
    mov [edi+8],  eax       ; PDPTE[1] → __boot_pd + 0x1000 (1..2 GiB)
    add eax, 0x1000
    mov [edi+16], eax       ; PDPTE[2] → __boot_pd + 0x2000 (2..3 GiB)
    add eax, 0x1000
    mov [edi+24], eax       ; PDPTE[3] → __boot_pd + 0x3000 (3..4 GiB)
    ; PDPTE high dwords already zero from the wholesale stosd.

    ; 2d: link __boot_pml4[0] → __boot_pdpt.
    mov edi, __boot_pml4
    mov eax, __boot_pdpt
    or  eax, PAGE_PRESENT | PAGE_WRITABLE
    mov [edi], eax
    ; high dword already zero.

    ; ----- Step 3: load the boot-time GDT -----
    ; lgdt loads the 6-byte pseudo-descriptor (limit:16, base:32) at
    ; [boot_gdtr]. CS stays whatever the loader set; the new code
    ; segment doesn't take effect until the far jump in step 8.
    lgdt [boot_gdtr]

    ; ----- Step 4: CR4.PAE := 1 -----
    mov eax, cr4
    or  eax, CR4_PAE
    mov cr4, eax

    ; ----- Step 5: CR3 := __boot_pml4 -----
    mov eax, __boot_pml4
    mov cr3, eax

    ; ----- Step 6: EFER.LME := 1 -----
    ; wrmsr expects ECX = MSR index, EDX:EAX = value. rdmsr first to
    ; preserve any other EFER bits the loader may have set (NXE, etc.).
    mov ecx, EFER_MSR
    rdmsr
    or  eax, EFER_LME
    wrmsr

    ; ----- Step 7: CR0.PE | CR0.PG := 1 -----
    ; PE is already 1 in protected mode; the OR pattern preserves any
    ; CR0 bits the loader cared about (EM, MP, etc.) while flipping
    ; PG on. From this instruction's retirement onward the CPU is in
    ; long-mode-compat (paging on, EFER.LMA goes to 1 automatically),
    ; but CS is still the 32-bit segment, so the next instruction
    ; must be the far jump that loads a 64-bit code segment.
    mov eax, cr0
    or  eax, CR0_PE | CR0_PG
    mov cr0, eax

    ; ----- Step 8: far jump to [bits 64] code segment -----
    ; The 32-bit far jump encodes (offset:32, segment:16). NASM
    ; resolves .long_mode_entry to its link-time offset (< 4 GiB),
    ; so the jump fits the 32-bit operand-size form.
    jmp CODE64_SEL:.long_mode_entry


[bits 64]
.long_mode_entry:
    ; ----- Step 9: reload data segments -----
    ; DS/ES/FS/GS get null selectors — in long mode the CPU treats
    ; their base as 0 regardless, and nulling them is the canonical
    ; "I have no segmentation policy yet" shape.
    ;
    ; SS gets DATA64_SEL, NOT null. SS=null is legal at CPL 0 in
    ; long mode (Intel SDM Vol 3A §3.4.4) but creates a latent
    ; stack-fault hazard if/when control ever transfers to a lower
    ; CPL (any future IRET-to-userspace path). One descriptor
    ; reload now is cheaper than a debugging session later.
    xor eax, eax
    mov ds, ax
    mov es, ax
    mov fs, ax
    mov gs, ax
    mov ax, DATA64_SEL
    mov ss, ax

    ; ----- Step 10: RSP := __bootstrap_stack_top -----
    ; Use rsp (full 64-bit form, movabs encoding) rather than esp
    ; even though __bootstrap_stack_top fits in 32 bits today —
    ; removes the truncation hazard if a future linker change
    ; pushes the symbol past 4 GiB. Cost: 5 extra bytes on a
    ; one-shot boot path.
    mov rsp, qword __bootstrap_stack_top

    ; ----- Step 11: hand off to kernel_main_64 -----
    jmp kernel_main_64


; ================================================================
; kernel_main_64 — [bits 64] kernel entry, post-mode-switch.
; ================================================================
; Reached only via mode_switch_to_long_mode's tail jump. Calls the
; init-time static memory allocator (D060 step 4.2's wire-up) and
; halts. Successor tasks expand the body:
;   - Task #7: emit LAYOUT-OK on serial after init_memory_layout
;     returns; LAYOUT-FAIL on a non-zero rc (when the contract
;     starts returning rc).
;   - Task #14: jump past the halt into the [bits 64]-ported
;     firecracker virtio-net init. QEMU stays at halt.
;
; The current contract for init_memory_layout is "void f(void),
; halt-on-fail" — see arch/x86_64/memory/init_memory_layout.S. So
; no return-code check at the call site today.

[bits 64]
global kernel_main_64
kernel_main_64:
    call init_memory_layout
.halt:
    hlt
    jmp .halt


; ================================================================
; Boot-time GDT (3 entries: null + 64-bit code + 64-bit data)
; and its 6-byte pseudo-descriptor for lgdt.
; ================================================================
; Lives in .text rather than .rodata because the existing x86_64
; cells don't carve out .rodata in their linker scripts; data
; lives in .text by convention (see ready_msg in firecracker boot.S).
; Once the data is past the final code path's last instruction,
; the CPU never decodes it as instructions — both code paths above
; end in unconditional jumps before reaching this region.
;
; Descriptor encoding (Intel SDM Vol 3A §3.4.5):
;
;   64-bit code (CS): 0x00AF9A000000FFFF
;     base       = 0           (ignored in long mode for code)
;     limit      = 0xFFFFF     (ignored in long mode for code)
;     access     = 0x9A        (P=1, DPL=00, S=1, type=1010
;                              code/r-x/non-conforming/accessed)
;     flags      = 0xA         (G=1, D=0, L=1, AVL=0)
;     limit_hi   = 0xF
;
;   64-bit data (DS): 0x00CF92000000FFFF
;     access     = 0x92        (P=1, DPL=00, S=1, type=0010
;                              data/r-w/expand-up/accessed)
;     flags      = 0xC         (G=1, D=1, L=0)
;     (D and L are essentially ignored for data segs in long mode;
;     the 0x00CF... shape matches Linux head_64.S.)

align 8
boot_gdt:
    dq 0x0000000000000000        ; 0x00  null
    dq 0x00AF9A000000FFFF        ; 0x08  CODE64_SEL
    dq 0x00CF92000000FFFF        ; 0x10  DATA64_SEL
boot_gdt_end:

; lgdt operand layout in [bits 32]: limit (2 bytes) + base (4 bytes).
; In [bits 64] it would be limit (2) + base (8); since lgdt runs
; while we're still in [bits 32], the 6-byte form is correct.
boot_gdtr:
    dw boot_gdt_end - boot_gdt - 1
    dd boot_gdt
