# SPDX-License-Identifier: AGPL-3.0-or-later
# fireasmserver x86_64 build
#
# Usage:
#   make PLATFORM=qemu        (default)
#   make PLATFORM=firecracker

PLATFORM ?= qemu

AS := x86_64-linux-gnu-as
LD := x86_64-linux-gnu-ld
ASFLAGS := --32
LDFLAGS := -m elf_i386

SRC_DIR := platform/$(PLATFORM)
BUILD_DIR := build/$(PLATFORM)

SOURCES := $(sort $(wildcard $(SRC_DIR)/*.S))
OBJECTS := $(patsubst $(SRC_DIR)/%.S,$(BUILD_DIR)/%.o,$(SOURCES))
LINKER_SCRIPT := $(SRC_DIR)/linker.ld
BINARY := $(BUILD_DIR)/guest.elf

.PHONY: all clean

all: $(BINARY)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.S | $(BUILD_DIR)
	$(AS) $(ASFLAGS) -o $@ $<

$(BINARY): $(OBJECTS)
	$(LD) $(LDFLAGS) -T $(LINKER_SCRIPT) -o $@ $^

$(BUILD_DIR):
	mkdir -p $@

clean:
	rm -rf build/
