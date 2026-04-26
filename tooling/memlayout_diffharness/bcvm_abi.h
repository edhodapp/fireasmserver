/* SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * ABI between the C driver and the per-arch bytecode VM
 * implementation. This is the shared contract — any change
 * here is binary-incompatible with both arches' .S files.
 */

#ifndef FIREASM_BCVM_ABI_H
#define FIREASM_BCVM_ABI_H

#include <stddef.h>
#include <stdint.h>

/* Error codes returned in rc_out. Match the Python
 * BytecodeError messages 1-to-1 so the differential test
 * harness can compare (rc, result) tuples. */
enum bcvm_err {
    BCVM_OK = 0,
    BCVM_ERR_EMPTY_BYTECODE = 1,
    BCVM_ERR_MISSING_END = 2,
    BCVM_ERR_END_EMPTY_STACK = 3,
    BCVM_ERR_END_STACK_MULTI = 4,
    BCVM_ERR_UNKNOWN_OPCODE = 5,
    BCVM_ERR_TRUNCATED_PAYLOAD = 6,
    BCVM_ERR_STACK_OVERFLOW = 7,
    BCVM_ERR_STACK_UNDERFLOW = 8,
    BCVM_ERR_VALUE_OUT_OF_U64 = 9,
    BCVM_ERR_CPU_FIELD_OOR = 10,
    BCVM_ERR_TUNING_FIELD_OOR = 11,
    BCVM_ERR_DIV_LIT_ZERO = 12,
    BCVM_ERR_ALIGN_ZERO = 13,
    BCVM_ERR_ALIGN_NOT_POW2 = 14,
    BCVM_ERR_MUL_OVERFLOW = 15,
    BCVM_ERR_ALIGN_UP_OVERFLOW = 16,
    BCVM_ERR_THUNK_UNREGISTERED = 17,
};

/* The asm side reads inputs from this struct (pointed to by the
 * single ABI argument) and writes (rc_out, result_out) before
 * returning. CALL_THUNK is intentionally not supported by the
 * asm side in step 3A — thunks are an escape hatch for
 * declaration-time unusual sizes; they don't need to be runnable
 * under user-mode QEMU because their bodies are just
 * arch-specific assembly anyway. The asm side returns
 * BCVM_ERR_THUNK_UNREGISTERED for any CALL_THUNK opcode. */
typedef struct bcvm_call {
    const uint8_t  *code;
    size_t          code_len;
    const uint64_t *cpu_values;
    size_t          cpu_count;
    const uint64_t *tun_values;
    size_t          tun_count;
    int32_t         rc_out;
    int32_t         _pad;        /* keep result_out 8-aligned */
    uint64_t        result_out;
} bcvm_call_t;

/* Defined in arch/<isa>/memory/bytecode_vm.S. The asm side does
 * NOT use the C stack as a memory stack — it keeps a 4-deep
 * scratch stack in registers and dispatches off a jump table.
 * Pre-stack-safe (will be reused inside the boot.S allocator
 * where SP isn't yet established). */
void memlayout_run_bytecode(bcvm_call_t *call);

#endif /* FIREASM_BCVM_ABI_H */
