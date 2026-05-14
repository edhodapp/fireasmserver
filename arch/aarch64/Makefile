# SPDX-License-Identifier: AGPL-3.0-or-later
# fireasmserver aarch64 build
#
# Usage:
#   make PLATFORM=firecracker   (default)
#
# Produces build/$(PLATFORM)/guest.bin, a raw Linux arm64 Image that
# Firecracker aarch64 loads directly as a kernel.

PLATFORM ?= firecracker

AS      := aarch64-linux-gnu-as
LD      := aarch64-linux-gnu-ld
OBJCOPY := aarch64-linux-gnu-objcopy

# -I paths for `.include` resolution:
#   - tooling/memlayout_diffharness/ : `bcvm_errors.inc` from
#     arch/aarch64/memory/bytecode_vm.S.
#   - build/$(PLATFORM)/ : `memreq_records.inc` and
#     `memreq_pin_hot.inc` emitted by the memreq_codegen tool
#     from platform/$(PLATFORM)/regions.yaml (D066 step 5,
#     aarch64 replication).
ASFLAGS := -I ../../tooling/memlayout_diffharness \
           -I build/$(PLATFORM)
LDFLAGS :=

SRC_DIR       := platform/$(PLATFORM)
BUILD_DIR     := build/$(PLATFORM)

# Codegen for `.memreq` records and hot-tier register pins
# (D066 step 5 — aarch64 replication of the x86_64 path).
REGIONS_YAML := $(SRC_DIR)/regions.yaml
MEMREQ_INCS := $(BUILD_DIR)/memreq_records.inc \
               $(BUILD_DIR)/memreq_pin_hot.inc
MEMREQ_CODEGEN_SRC := $(wildcard ../../tooling/src/memreq_codegen/*.py)
PLAT_SOURCES  := $(sort $(wildcard $(SRC_DIR)/*.S))
PLAT_OBJECTS  := $(patsubst $(SRC_DIR)/%.S,$(BUILD_DIR)/%.o,$(PLAT_SOURCES))
# Arch-shared sources live one level up under arch/aarch64/<subdir>/.
# `memory/` holds the init-time static memory layout machinery
# (D059 + D060 — bump allocator, bytecode VM, .memreq macros).
# Crypto stays out: those .S files are exercised by the host-side
# test harness in tooling/crypto_tests/, not linked into the guest
# kernel today.
ARCH_SOURCES  := $(sort $(wildcard memory/*.S))
ARCH_OBJECTS  := $(patsubst memory/%.S,$(BUILD_DIR)/memory/%.o,$(ARCH_SOURCES))
OBJECTS       := $(PLAT_OBJECTS) $(ARCH_OBJECTS)
LINKER_SCRIPT := $(SRC_DIR)/linker.ld
ELF           := $(BUILD_DIR)/guest.elf
BINARY        := $(BUILD_DIR)/guest.bin

.PHONY: all clean

all: $(BINARY)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.S $(MEMREQ_INCS) | $(BUILD_DIR)
	$(AS) $(ASFLAGS) -o $@ $<

$(BUILD_DIR)/memory/%.o: memory/%.S | $(BUILD_DIR)/memory
	$(AS) $(ASFLAGS) -o $@ $<

# Codegen produces both .inc files in one tool invocation; the
# grouped-target syntax (`&:`, GNU Make 4.3+) ties both outputs
# to the same recipe.
$(BUILD_DIR)/memreq_records.inc \
$(BUILD_DIR)/memreq_pin_hot.inc &: \
        $(REGIONS_YAML) $(MEMREQ_CODEGEN_SRC) | $(BUILD_DIR)
	../../.venv/bin/memreq-codegen $(REGIONS_YAML) \
	    --arch aarch64 \
	    --out-records $(BUILD_DIR)/memreq_records.inc \
	    --out-pins $(BUILD_DIR)/memreq_pin_hot.inc

$(ELF): $(OBJECTS) $(LINKER_SCRIPT)
	$(LD) $(LDFLAGS) -T $(LINKER_SCRIPT) -o $@ $(OBJECTS)

$(BINARY): $(ELF)
	$(OBJCOPY) -O binary $< $@

$(BUILD_DIR) $(BUILD_DIR)/memory:
	mkdir -p $@

clean:
	rm -rf build/
