# SPDX-License-Identifier: AGPL-3.0-or-later
# Host-side test drivers for the per-arch CRC-32 IEEE 802.3 assembly.
#
# Two targets:
#   build/x86_64/crc32_test      native ELF64, runs directly on host
#   build/aarch64/crc32_test     cross-built, runs under qemu-aarch64
#
# Each target links its C driver against the per-arch
# arch/<arch>/crypto/build/crc32_ieee.o and runs it; a passing binary
# prints "PASS  all CRC-32 IEEE vectors match" and exits 0.
#
# The module's .S assembly is built by the per-arch crypto Makefile
# (arch/<arch>/crypto/Makefile); this Makefile invokes those as
# subordinate targets.

REPO_ROOT := $(abspath $(CURDIR)/../..)

X86_OBJ   := $(REPO_ROOT)/arch/x86_64/crypto/build/crc32_ieee.o
AARCH_OBJ := $(REPO_ROOT)/arch/aarch64/crypto/build/crc32_ieee.o

BUILD_DIR := build
X86_BIN   := $(BUILD_DIR)/x86_64/crc32_test
AARCH_BIN := $(BUILD_DIR)/aarch64/crc32_test

# Compilers and qemu wrapper are `?=` so CI can override per runner:
# a native x86_64 runner uses CC_X86=gcc; a native arm64 runner uses
# CC_AARCH=gcc and QEMU_AARCH64='' (empty — run the binary directly).
# The defaults below are the cross-compile / emulated-execution path
# used from Ed's x86_64 laptop.
CC_X86        ?= x86_64-linux-gnu-gcc
CC_AARCH      ?= aarch64-linux-gnu-gcc
QEMU_AARCH64  ?= qemu-aarch64-static
# -static simplifies running the aarch64 binary under qemu-aarch64
# without juggling a sysroot for the dynamic loader.
CFLAGS        := -O2 -Wall -Wextra -Wpedantic -std=c11
LDFLAGS_AARCH := -static

SRCS := crc32_test.c
HDRS := vectors.h

.PHONY: all build-x86_64 build-aarch64 test test-x86_64 test-aarch64 clean

all: $(X86_BIN) $(AARCH_BIN)

build-x86_64: $(X86_BIN)

build-aarch64: $(AARCH_BIN)

$(X86_OBJ):
	$(MAKE) -C $(REPO_ROOT)/arch/x86_64/crypto

$(AARCH_OBJ):
	$(MAKE) -C $(REPO_ROOT)/arch/aarch64/crypto

$(X86_BIN): $(SRCS) $(HDRS) $(X86_OBJ) | $(BUILD_DIR)/x86_64
	$(CC_X86) $(CFLAGS) -o $@ $(SRCS) $(X86_OBJ)

$(AARCH_BIN): $(SRCS) $(HDRS) $(AARCH_OBJ) | $(BUILD_DIR)/aarch64
	$(CC_AARCH) $(CFLAGS) $(LDFLAGS_AARCH) -o $@ $(SRCS) $(AARCH_OBJ)

$(BUILD_DIR)/x86_64 $(BUILD_DIR)/aarch64:
	mkdir -p $@

test: test-x86_64 test-aarch64

test-x86_64: $(X86_BIN)
	$(X86_BIN)

test-aarch64: $(AARCH_BIN)
	$(QEMU_AARCH64) $(AARCH_BIN)

clean:
	rm -rf $(BUILD_DIR)
