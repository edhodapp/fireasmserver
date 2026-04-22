# SPDX-License-Identifier: AGPL-3.0-or-later
# Host-side test drivers for the per-arch crypto assembly primitives.
#
# Targets:
#   build/x86_64/crc32_test       native ELF64
#   build/aarch64/crc32_test      cross-built, runs under qemu-aarch64
#   build/x86_64/sha256_test      native ELF64
#   build/aarch64/sha256_test     cross-built, runs under qemu-aarch64
#
# Each test binary links a per-primitive C driver against the
# corresponding arch/<arch>/crypto/build/<primitive>.o produced by
# the per-arch crypto Makefile; a passing binary prints its
# primitive's "PASS" banner and exits 0.

REPO_ROOT := $(abspath $(CURDIR)/../..)

X86_CRC_OBJ   := $(REPO_ROOT)/arch/x86_64/crypto/build/crc32_ieee.o
AARCH_CRC_OBJ := $(REPO_ROOT)/arch/aarch64/crypto/build/crc32_ieee.o
X86_SHA_OBJ   := $(REPO_ROOT)/arch/x86_64/crypto/build/sha256.o
AARCH_SHA_OBJ := $(REPO_ROOT)/arch/aarch64/crypto/build/sha256.o

BUILD_DIR := build
X86_CRC_BIN   := $(BUILD_DIR)/x86_64/crc32_test
AARCH_CRC_BIN := $(BUILD_DIR)/aarch64/crc32_test
X86_SHA_BIN   := $(BUILD_DIR)/x86_64/sha256_test
AARCH_SHA_BIN := $(BUILD_DIR)/aarch64/sha256_test

# Compilers and qemu wrapper are `?=` so CI can override per runner:
# native x86_64 uses CC_X86=gcc; native arm64 uses CC_AARCH=gcc and
# QEMU_AARCH64='' (empty — run the binary directly). The defaults
# below are the cross-compile / emulated path used from Ed's x86_64
# laptop.
CC_X86        ?= x86_64-linux-gnu-gcc
CC_AARCH      ?= aarch64-linux-gnu-gcc
QEMU_AARCH64  ?= qemu-aarch64-static
# The x86_64 SHA-NI path needs the project's QEMU fork (≥ 11.0) because
# stock Ubuntu Noble qemu-user 8.2.2 SIGILLs on sha256rnds2 under
# -cpu Icelake-Server and SIGSEGVs mid-block under -cpu max (D054).
# Absolute-path invocation keeps this independent of whether
# tooling/qemu_build/env.sh has been sourced in the shell.
# `?=` still allows CI cells with native SHA-NI silicon to point the
# var at /bin/true or similar to skip emulation.
QEMU_X86_FORK ?= $(HOME)/opt/qemu-fork/bin/qemu-x86_64
# D054: -cpu Icelake-Server does NOT advertise SHA-NI in QEMU's CPU
# model tables; -cpu Denverton and -cpu max both do. We use Denverton
# by default because it's a narrower model (less unemulated-feature
# warning noise than -cpu max); -cpu max is exercised as a second
# cell below to catch CPU-model-specific regressions.
QEMU_X86_CPU  ?= Denverton
CFLAGS        := -O2 -Wall -Wextra -Wpedantic -std=c11
LDFLAGS_X86   := -static
LDFLAGS_AARCH := -static

CRC_SRCS := crc32_test.c
CRC_HDRS := vectors.h
SHA_SRCS := sha256_test.c
SHA_HDRS := sha256_vectors.h

.PHONY: all \
        build-x86_64 build-aarch64 \
        build-x86_64-crc32 build-aarch64-crc32 \
        build-x86_64-sha256 build-aarch64-sha256 \
        test test-x86_64 test-aarch64 \
        test-x86_64-crc32 test-aarch64-crc32 \
        test-x86_64-sha256 test-x86_64-sha256-native \
        test-x86_64-sha256-shani test-x86_64-sha256-shani-max \
        test-aarch64-sha256 \
        clean

all: $(X86_CRC_BIN) $(AARCH_CRC_BIN) $(X86_SHA_BIN) $(AARCH_SHA_BIN)

build-x86_64: $(X86_CRC_BIN) $(X86_SHA_BIN)
build-aarch64: $(AARCH_CRC_BIN) $(AARCH_SHA_BIN)

build-x86_64-crc32:   $(X86_CRC_BIN)
build-aarch64-crc32:  $(AARCH_CRC_BIN)
build-x86_64-sha256:  $(X86_SHA_BIN)
build-aarch64-sha256: $(AARCH_SHA_BIN)

$(X86_CRC_OBJ) $(X86_SHA_OBJ):
	$(MAKE) -C $(REPO_ROOT)/arch/x86_64/crypto

$(AARCH_CRC_OBJ) $(AARCH_SHA_OBJ):
	$(MAKE) -C $(REPO_ROOT)/arch/aarch64/crypto

$(X86_CRC_BIN): $(CRC_SRCS) $(CRC_HDRS) $(X86_CRC_OBJ) | $(BUILD_DIR)/x86_64
	$(CC_X86) $(CFLAGS) -o $@ $(CRC_SRCS) $(X86_CRC_OBJ)

$(AARCH_CRC_BIN): $(CRC_SRCS) $(CRC_HDRS) $(AARCH_CRC_OBJ) | $(BUILD_DIR)/aarch64
	$(CC_AARCH) $(CFLAGS) $(LDFLAGS_AARCH) -o $@ $(CRC_SRCS) $(AARCH_CRC_OBJ)

$(X86_SHA_BIN): $(SHA_SRCS) $(SHA_HDRS) $(X86_SHA_OBJ) | $(BUILD_DIR)/x86_64
	$(CC_X86) $(CFLAGS) $(LDFLAGS_X86) -o $@ $(SHA_SRCS) $(X86_SHA_OBJ)

$(AARCH_SHA_BIN): $(SHA_SRCS) $(SHA_HDRS) $(AARCH_SHA_OBJ) | $(BUILD_DIR)/aarch64
	$(CC_AARCH) $(CFLAGS) $(LDFLAGS_AARCH) -o $@ $(SHA_SRCS) $(AARCH_SHA_OBJ)

$(BUILD_DIR)/x86_64 $(BUILD_DIR)/aarch64:
	mkdir -p $@

test: test-x86_64 test-aarch64

test-x86_64:  test-x86_64-crc32  test-x86_64-sha256
test-aarch64: test-aarch64-crc32 test-aarch64-sha256

test-x86_64-crc32: $(X86_CRC_BIN)
	$(X86_CRC_BIN)

test-aarch64-crc32: $(AARCH_CRC_BIN)
	$(QEMU_AARCH64) $(AARCH_CRC_BIN)

# Full x86_64 SHA-256 gate: native host path (selects scalar via CPUID
# unless the laptop has SHA-NI silicon) + two fork-qemu cells that
# force the SHA-NI dispatcher. Both qemu cells must pass with
# byte-identical digests across the 7 named vectors + 257-length
# sweep + shani-vs-scalar cross-check.
test-x86_64-sha256: test-x86_64-sha256-native \
                    test-x86_64-sha256-shani \
                    test-x86_64-sha256-shani-max

test-x86_64-sha256-native: $(X86_SHA_BIN)
	$(X86_SHA_BIN)

test-x86_64-sha256-shani: $(X86_SHA_BIN)
	$(QEMU_X86_FORK) -cpu $(QEMU_X86_CPU) $(X86_SHA_BIN)

test-x86_64-sha256-shani-max: $(X86_SHA_BIN)
	$(QEMU_X86_FORK) -cpu max $(X86_SHA_BIN)

test-aarch64-sha256: $(AARCH_SHA_BIN)
	$(QEMU_AARCH64) $(AARCH_SHA_BIN)

clean:
	rm -rf $(BUILD_DIR)
