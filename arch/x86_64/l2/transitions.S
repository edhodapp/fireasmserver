; SPDX-License-Identifier: AGPL-3.0-or-later
;
; L2 transitions — per-event state-transition handlers (x86_64).
;
; FSA-1 scope: empty placeholder. In FSA-1 the dispatcher carries
; the per-event logic inline. FSA-2 will pull the RX-consume and
; TX-submit transitions out into named, callable functions here.
; See arch/aarch64/l2/transitions.S for the FSA-2 contract sketch.

section .note.GNU-stack noalloc noexec nowrite progbits
