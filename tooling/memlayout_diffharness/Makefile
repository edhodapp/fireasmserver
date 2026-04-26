# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build the differential test driver for the bytecode VM
# under each arch. Each driver is a static binary that reads
# test cases from stdin, runs them through the per-arch
# memlayout_run_bytecode, and writes (rc, result) tuples to
# stdout. The Python harness in tooling/tests/ launches each
# driver under qemu-<arch>-static and asserts against the
# Python reference.

REPO_ROOT := $(realpath $(CURDIR)/../..)

X86_VM_OBJ   := $(REPO_ROOT)/arch/x86_64/memory/build/diffharness/bytecode_vm.o
AARCH_VM_OBJ := $(REPO_ROOT)/arch/aarch64/memory/build/diffharness/bytecode_vm.o

BUILD_DIR := build
X86_BIN   := $(BUILD_DIR)/x86_64/bcvm_driver
AARCH_BIN := $(BUILD_DIR)/aarch64/bcvm_driver

CC_X86      := gcc
CC_AARCH    := aarch64-linux-gnu-gcc
NASM        := nasm
GAS_AARCH64 := aarch64-linux-gnu-as

CFLAGS := -std=c11 -O2 -Wall -Wextra -Wpedantic \
          -I$(CURDIR)
LDFLAGS_X86   := -static
LDFLAGS_AARCH := -static

.PHONY: all clean test

all: $(X86_BIN) $(AARCH_BIN)

# Per-arch VM .o lives under arch/<isa>/memory/build/diffharness/
# rather than the platform-specific build dir, so the diffharness
# build doesn't collide with the kernel build.

$(X86_VM_OBJ): $(REPO_ROOT)/arch/x86_64/memory/bytecode_vm.S
	@mkdir -p $(dir $@)
	$(NASM) -f elf64 -i $(CURDIR)/ -o $@ $<

$(AARCH_VM_OBJ): $(REPO_ROOT)/arch/aarch64/memory/bytecode_vm.S
	@mkdir -p $(dir $@)
	$(GAS_AARCH64) -I $(CURDIR) -o $@ $<

$(X86_BIN): driver.c bcvm_abi.h $(X86_VM_OBJ) | $(BUILD_DIR)/x86_64
	$(CC_X86) $(CFLAGS) $(LDFLAGS_X86) -o $@ \
	    driver.c $(X86_VM_OBJ)

$(AARCH_BIN): driver.c bcvm_abi.h $(AARCH_VM_OBJ) | $(BUILD_DIR)/aarch64
	$(CC_AARCH) $(CFLAGS) $(LDFLAGS_AARCH) -o $@ \
	    driver.c $(AARCH_VM_OBJ)

$(BUILD_DIR)/x86_64 $(BUILD_DIR)/aarch64:
	mkdir -p $@

test: all
	@echo "(diff harness exercised via tooling/tests/test_memlayout_diff_*.py)"

clean:
	rm -rf $(BUILD_DIR)
	rm -rf $(REPO_ROOT)/arch/x86_64/memory/build/diffharness
	rm -rf $(REPO_ROOT)/arch/aarch64/memory/build/diffharness
