# SPDX-License-Identifier: AGPL-3.0-or-later
# fireasmserver x86_64 build
#
# Usage:
#   make PLATFORM=qemu        (default)
#   make PLATFORM=firecracker
#
# Assembler: NASM per D048. Linker stays at binutils' ld.

PLATFORM ?= qemu

AS := nasm
LD := x86_64-linux-gnu-ld

# qemu boots via Multiboot1 on -machine pc, which requires a 32-bit
# ELF. firecracker boots via PVH, which requires a 64-bit ELF wrapping
# the same `[bits 32]` entry stub. ELF class is a function of (arch,
# platform).
# -i paths for NASM `%include`s:
#   - tooling/memlayout_diffharness/ : `bcvm_errors.nasm.inc` from
#     arch/x86_64/memory/bytecode_vm.S — shared contract with the
#     diff harness driver.
#   - build/$(PLATFORM)/ : `memreq_records.inc` and
#     `memreq_pin_hot.inc` emitted by the memreq_codegen tool from
#     platform/$(PLATFORM)/regions.yaml (D066 step 5a).
# Trailing slash is required by NASM's path-handling.
NASM_INCLUDE := -i ../../tooling/memlayout_diffharness/ \
                -i build/$(PLATFORM)/

ifeq ($(PLATFORM),firecracker)
ASFLAGS := -f elf64 $(NASM_INCLUDE)
LDFLAGS :=
else
ASFLAGS := -f elf32 $(NASM_INCLUDE)
LDFLAGS := -m elf_i386
endif

SRC_DIR := platform/$(PLATFORM)
BUILD_DIR := build/$(PLATFORM)

# Codegen for `.memreq` records and hot-tier register pins.
# Triggered by any change to the YAML source-of-truth or the
# tool's Python sources. Outputs land alongside object files in
# build/$(PLATFORM)/ so the `-i` flag above finds them.
REGIONS_YAML := $(SRC_DIR)/regions.yaml
MEMREQ_INCS := $(BUILD_DIR)/memreq_records.inc \
               $(BUILD_DIR)/memreq_pin_hot.inc
MEMREQ_CODEGEN_SRC := $(wildcard ../../tooling/src/memreq_codegen/*.py)

PLAT_SOURCES := $(sort $(wildcard $(SRC_DIR)/*.S))
PLAT_OBJECTS := $(patsubst $(SRC_DIR)/%.S,$(BUILD_DIR)/%.o,$(PLAT_SOURCES))
# Arch-shared sources live one level up under arch/x86_64/<subdir>/.
# `memory/` holds the init-time static memory layout machinery
# (D059 + D060 — bump allocator, bytecode VM, .memreq macros).
# `l2/` holds the FSA dispatcher + transitions + buffer-pool sizing
# (D012 + D043 — FSA runtime). Crypto stays out: those .S files
# are exercised by the host-side test harness in
# tooling/crypto_tests/, not linked into the guest kernel today.
MEM_SOURCES  := $(sort $(wildcard memory/*.S))
MEM_OBJECTS  := $(patsubst memory/%.S,$(BUILD_DIR)/memory/%.o,$(MEM_SOURCES))
# L2 dispatcher pulls in virtio-MMIO + emit_bytes/emit_hex32 + the
# canary TX frame; only the firecracker platform wires those today.
# The qemu platform's boot.S is a minimal Multiboot1 stub without
# virtio. Conditional inclusion avoids unresolved external refs at
# link time on the qemu target.
ifeq ($(PLATFORM),firecracker)
L2_SOURCES   := $(sort $(wildcard l2/*.S))
L2_OBJECTS   := $(patsubst l2/%.S,$(BUILD_DIR)/l2/%.o,$(L2_SOURCES))
else
L2_SOURCES   :=
L2_OBJECTS   :=
endif
ARCH_OBJECTS := $(MEM_OBJECTS) $(L2_OBJECTS)
OBJECTS      := $(PLAT_OBJECTS) $(ARCH_OBJECTS)
LINKER_SCRIPT := $(SRC_DIR)/linker.ld
BINARY := $(BUILD_DIR)/guest.elf

.PHONY: all clean

all: $(BINARY)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.S $(MEMREQ_INCS) | $(BUILD_DIR)
	$(AS) $(ASFLAGS) -o $@ $<

$(BUILD_DIR)/memory/%.o: memory/%.S | $(BUILD_DIR)/memory
	$(AS) $(ASFLAGS) -o $@ $<

$(BUILD_DIR)/l2/%.o: l2/%.S | $(BUILD_DIR)/l2
	$(AS) $(ASFLAGS) -o $@ $<

# Both .inc files are produced in one tool invocation; the second
# target rule for memreq_pin_hot.inc just points at the same recipe.
$(BUILD_DIR)/memreq_records.inc \
$(BUILD_DIR)/memreq_pin_hot.inc &: \
        $(REGIONS_YAML) $(MEMREQ_CODEGEN_SRC) | $(BUILD_DIR)
	../../.venv/bin/memreq-codegen $(REGIONS_YAML) \
	    --arch x86_64 \
	    --out-records $(BUILD_DIR)/memreq_records.inc \
	    --out-pins $(BUILD_DIR)/memreq_pin_hot.inc

$(BINARY): $(OBJECTS) $(LINKER_SCRIPT)
	$(LD) $(LDFLAGS) -T $(LINKER_SCRIPT) -o $@ $(OBJECTS)

$(BUILD_DIR) $(BUILD_DIR)/memory $(BUILD_DIR)/l2:
	mkdir -p $@

clean:
	rm -rf build/
