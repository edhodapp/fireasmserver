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
SOURCES       := $(sort $(wildcard $(SRC_DIR)/*.S))
OBJECTS       := $(patsubst $(SRC_DIR)/%.S,$(BUILD_DIR)/%.o,$(SOURCES))
LINKER_SCRIPT := $(SRC_DIR)/linker.ld
ELF           := $(BUILD_DIR)/guest.elf
BINARY        := $(BUILD_DIR)/guest.bin

.PHONY: all clean

all: $(BINARY)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.S | $(BUILD_DIR)
	$(AS) $(ASFLAGS) -o $@ $<

$(ELF): $(OBJECTS) $(LINKER_SCRIPT)
	$(LD) $(LDFLAGS) -T $(LINKER_SCRIPT) -o $@ $(OBJECTS)

$(BINARY): $(ELF)
	$(OBJCOPY) -O binary $< $@

$(BUILD_DIR):
	mkdir -p $@

clean:
	rm -rf build/
