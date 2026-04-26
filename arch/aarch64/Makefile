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

ASFLAGS :=
LDFLAGS :=

SRC_DIR       := platform/$(PLATFORM)
BUILD_DIR     := build/$(PLATFORM)
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

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.S | $(BUILD_DIR)
	$(AS) $(ASFLAGS) -o $@ $<

$(BUILD_DIR)/memory/%.o: memory/%.S | $(BUILD_DIR)/memory
	$(AS) $(ASFLAGS) -o $@ $<

$(ELF): $(OBJECTS) $(LINKER_SCRIPT)
	$(LD) $(LDFLAGS) -T $(LINKER_SCRIPT) -o $@ $(OBJECTS)

$(BINARY): $(ELF)
	$(OBJCOPY) -O binary $< $@

$(BUILD_DIR) $(BUILD_DIR)/memory:
	mkdir -p $@

clean:
	rm -rf build/
