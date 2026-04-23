# SPDX-License-Identifier: AGPL-3.0-or-later
# fireasmserver AArch64 crypto module build.
#
# Produces build/crc32_ieee.o, a host-callable AAPCS64 leaf routine
# suitable for linking against a C test driver (see
# tooling/crypto_tests/Makefile) or into a bare-metal guest.

AS := aarch64-linux-gnu-as

ASFLAGS :=

BUILD_DIR := build
OBJECTS   := $(BUILD_DIR)/crc32_ieee.o \
             $(BUILD_DIR)/sha256.o \
             $(BUILD_DIR)/aes128.o

.PHONY: all clean

all: $(OBJECTS)

$(BUILD_DIR)/%.o: %.S | $(BUILD_DIR)
	$(AS) $(ASFLAGS) -o $@ $<

$(BUILD_DIR):
	mkdir -p $@

clean:
	rm -rf $(BUILD_DIR)
