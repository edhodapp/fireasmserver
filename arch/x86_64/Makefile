# SPDX-License-Identifier: AGPL-3.0-or-later
# fireasmserver x86_64 build
#
# Usage:
#   make PLATFORM=qemu        (default)
#   make PLATFORM=firecracker

PLATFORM ?= qemu

AS := x86_64-linux-gnu-as
LD := x86_64-linux-gnu-ld

# qemu boots via Multiboot1 on -machine pc, which requires a
# 32-bit ELF (--32 / -m elf_i386). firecracker boots via PVH,
# which requires a 64-bit ELF wrapping the same .code32 entry
# stub. ELF class is a function of (arch, platform).
ifeq ($(PLATFORM),firecracker)
ASFLAGS :=
LDFLAGS :=
else
ASFLAGS := --32
LDFLAGS := -m elf_i386
endif

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
