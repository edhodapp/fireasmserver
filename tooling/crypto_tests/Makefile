# SPDX-License-Identifier: AGPL-3.0-or-later
# Host-side test drivers for the per-arch crypto assembly primitives.
#
# Targets:
#   build/x86_64/crc32_test       native ELF64
#   build/aarch64/crc32_test      cross-built, runs under qemu-aarch64
#   build/x86_64/sha256_test      native ELF64
#   build/aarch64/sha256_test     cross-built, runs under qemu-aarch64
#   build/x86_64/aes128_test      native ELF64 (AES-NI)
#   build/aarch64/aes128_test     cross-built, runs under qemu-aarch64
#   build/x86_64/aes128_gcm_test  native ELF64 (AES-NI + PCLMULQDQ gate)
#   build/aarch64/aes128_gcm_test cross-built, runs under qemu-aarch64
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
X86_AES_OBJ   := $(REPO_ROOT)/arch/x86_64/crypto/build/aes128.o
AARCH_AES_OBJ := $(REPO_ROOT)/arch/aarch64/crypto/build/aes128.o
X86_GCM_OBJ   := $(REPO_ROOT)/arch/x86_64/crypto/build/aes128_gcm.o
AARCH_GCM_OBJ := $(REPO_ROOT)/arch/aarch64/crypto/build/aes128_gcm.o

BUILD_DIR := build
X86_CRC_BIN   := $(BUILD_DIR)/x86_64/crc32_test
AARCH_CRC_BIN := $(BUILD_DIR)/aarch64/crc32_test
X86_SHA_BIN   := $(BUILD_DIR)/x86_64/sha256_test
AARCH_SHA_BIN := $(BUILD_DIR)/aarch64/sha256_test
X86_AES_BIN   := $(BUILD_DIR)/x86_64/aes128_test
AARCH_AES_BIN := $(BUILD_DIR)/aarch64/aes128_test
X86_GCM_BIN   := $(BUILD_DIR)/x86_64/aes128_gcm_test
AARCH_GCM_BIN := $(BUILD_DIR)/aarch64/aes128_gcm_test

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
AES_SRCS := aes128_test.c
AES_HDRS := aes128_vectors.h
GCM_SRCS := aes128_gcm_test.c
GCM_HDRS := aes128_gcm_vectors.h

# -- Lint stack ----------------------------------------------------
# Four independent static-analysis layers per D-series discipline
# (automation cost is bounded; bug cost is not). Each catches a
# different class of bug:
#   gcc compile-as-lint   — strict warnings + -Werror
#   clang compile-as-lint — same flags, different compiler;
#                           divergences between the two ARE the
#                           signal
#   clang-tidy            — semantic checks via .clang-tidy config
#   cppcheck              — complementary engine (UB detection,
#                           buffer bounds, leaks)
#   scan-build            — clang static analyzer with full
#                           symbolic execution, deeper than
#                           clang-tidy's clang-analyzer-* subset
GCC_HOST      ?= gcc
CLANG_HOST    ?= clang
CLANG_TIDY    ?= clang-tidy
CPPCHECK      ?= cppcheck
SCAN_BUILD    ?= scan-build

LINT_CFLAGS   := -Wall -Wextra -Wpedantic -Werror -Wshadow \
                 -Wstrict-prototypes -Wmissing-prototypes \
                 -Wdouble-promotion -Wformat=2 \
                 -Wcast-align -Wconversion \
                 -std=c11

LINT_SRCS     := $(CRC_SRCS) $(SHA_SRCS) $(AES_SRCS) $(GCM_SRCS)

.PHONY: all \
        build-x86_64 build-aarch64 \
        build-x86_64-crc32 build-aarch64-crc32 \
        build-x86_64-sha256 build-aarch64-sha256 \
        build-x86_64-aes128 build-aarch64-aes128 \
        build-x86_64-aes128-gcm build-aarch64-aes128-gcm \
        test test-x86_64 test-aarch64 \
        test-x86_64-crc32 test-aarch64-crc32 \
        test-x86_64-sha256 test-x86_64-sha256-native \
        test-x86_64-sha256-shani test-x86_64-sha256-shani-max \
        test-aarch64-sha256 \
        test-x86_64-aes128 test-x86_64-aes128-native \
        test-x86_64-aes128-aesni test-x86_64-aes128-aesni-max \
        test-aarch64-aes128 \
        test-x86_64-aes128-gcm test-x86_64-aes128-gcm-native \
        test-x86_64-aes128-gcm-pclmul test-x86_64-aes128-gcm-pclmul-max \
        test-aarch64-aes128-gcm \
        lint lint-gcc lint-clang lint-clang-tidy \
        lint-cppcheck lint-scan-build \
        clean

all: $(X86_CRC_BIN) $(AARCH_CRC_BIN) \
     $(X86_SHA_BIN) $(AARCH_SHA_BIN) \
     $(X86_AES_BIN) $(AARCH_AES_BIN) \
     $(X86_GCM_BIN) $(AARCH_GCM_BIN)

build-x86_64:  $(X86_CRC_BIN)  $(X86_SHA_BIN)  $(X86_AES_BIN)  $(X86_GCM_BIN)
build-aarch64: $(AARCH_CRC_BIN) $(AARCH_SHA_BIN) $(AARCH_AES_BIN) $(AARCH_GCM_BIN)

build-x86_64-crc32:       $(X86_CRC_BIN)
build-aarch64-crc32:      $(AARCH_CRC_BIN)
build-x86_64-sha256:      $(X86_SHA_BIN)
build-aarch64-sha256:     $(AARCH_SHA_BIN)
build-x86_64-aes128:      $(X86_AES_BIN)
build-aarch64-aes128:     $(AARCH_AES_BIN)
build-x86_64-aes128-gcm:  $(X86_GCM_BIN)
build-aarch64-aes128-gcm: $(AARCH_GCM_BIN)

$(X86_CRC_OBJ) $(X86_SHA_OBJ) $(X86_AES_OBJ) $(X86_GCM_OBJ):
	$(MAKE) -C $(REPO_ROOT)/arch/x86_64/crypto

$(AARCH_CRC_OBJ) $(AARCH_SHA_OBJ) $(AARCH_AES_OBJ) $(AARCH_GCM_OBJ):
	$(MAKE) -C $(REPO_ROOT)/arch/aarch64/crypto

$(X86_CRC_BIN): $(CRC_SRCS) $(CRC_HDRS) $(X86_CRC_OBJ) | $(BUILD_DIR)/x86_64
	$(CC_X86) $(CFLAGS) -o $@ $(CRC_SRCS) $(X86_CRC_OBJ)

$(AARCH_CRC_BIN): $(CRC_SRCS) $(CRC_HDRS) $(AARCH_CRC_OBJ) | $(BUILD_DIR)/aarch64
	$(CC_AARCH) $(CFLAGS) $(LDFLAGS_AARCH) -o $@ $(CRC_SRCS) $(AARCH_CRC_OBJ)

$(X86_SHA_BIN): $(SHA_SRCS) $(SHA_HDRS) $(X86_SHA_OBJ) | $(BUILD_DIR)/x86_64
	$(CC_X86) $(CFLAGS) $(LDFLAGS_X86) -o $@ $(SHA_SRCS) $(X86_SHA_OBJ)

$(AARCH_SHA_BIN): $(SHA_SRCS) $(SHA_HDRS) $(AARCH_SHA_OBJ) | $(BUILD_DIR)/aarch64
	$(CC_AARCH) $(CFLAGS) $(LDFLAGS_AARCH) -o $@ $(SHA_SRCS) $(AARCH_SHA_OBJ)

$(X86_AES_BIN): $(AES_SRCS) $(AES_HDRS) $(X86_AES_OBJ) | $(BUILD_DIR)/x86_64
	$(CC_X86) $(CFLAGS) $(LDFLAGS_X86) -o $@ $(AES_SRCS) $(X86_AES_OBJ)

$(AARCH_AES_BIN): $(AES_SRCS) $(AES_HDRS) $(AARCH_AES_OBJ) | $(BUILD_DIR)/aarch64
	$(CC_AARCH) $(CFLAGS) $(LDFLAGS_AARCH) -o $@ $(AES_SRCS) $(AARCH_AES_OBJ)

# AES-128-GCM drivers link against BOTH aes128.o (block primitive,
# extern-referenced from aes128_gcm.S) AND aes128_gcm.o (the mode
# composition + tag computation). Order matters for the linker
# only insofar as it needs to resolve all externs; we pass the
# .o's in dependency order for readability.
$(X86_GCM_BIN): $(GCM_SRCS) $(GCM_HDRS) $(X86_AES_OBJ) $(X86_GCM_OBJ) \
                | $(BUILD_DIR)/x86_64
	$(CC_X86) $(CFLAGS) $(LDFLAGS_X86) -o $@ \
	    $(GCM_SRCS) $(X86_AES_OBJ) $(X86_GCM_OBJ)

$(AARCH_GCM_BIN): $(GCM_SRCS) $(GCM_HDRS) $(AARCH_AES_OBJ) $(AARCH_GCM_OBJ) \
                  | $(BUILD_DIR)/aarch64
	$(CC_AARCH) $(CFLAGS) $(LDFLAGS_AARCH) -o $@ \
	    $(GCM_SRCS) $(AARCH_AES_OBJ) $(AARCH_GCM_OBJ)

$(BUILD_DIR)/x86_64 $(BUILD_DIR)/aarch64:
	mkdir -p $@

test: test-x86_64 test-aarch64

test-x86_64:  test-x86_64-crc32  test-x86_64-sha256  test-x86_64-aes128 \
              test-x86_64-aes128-gcm
test-aarch64: test-aarch64-crc32 test-aarch64-sha256 test-aarch64-aes128 \
              test-aarch64-aes128-gcm

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

# Full x86_64 AES-128 gate: native host path (skips cleanly when
# host CPU lacks AES-NI) + two fork-qemu cells that force the
# AES-NI path under emulated CPU models that advertise
# CPUID.(EAX=1):ECX[bit 25]. Both qemu cells must exit 0.
test-x86_64-aes128: test-x86_64-aes128-native \
                    test-x86_64-aes128-aesni \
                    test-x86_64-aes128-aesni-max

test-x86_64-aes128-native: $(X86_AES_BIN)
	$(X86_AES_BIN)

test-x86_64-aes128-aesni: $(X86_AES_BIN)
	$(QEMU_X86_FORK) -cpu $(QEMU_X86_CPU) $(X86_AES_BIN)

test-x86_64-aes128-aesni-max: $(X86_AES_BIN)
	$(QEMU_X86_FORK) -cpu max $(X86_AES_BIN)

test-aarch64-aes128: $(AARCH_AES_BIN)
	$(QEMU_AARCH64) $(AARCH_AES_BIN)

# AES-128-GCM gate: native host path (skips cleanly when AES-NI or
# PCLMULQDQ missing) + two fork-qemu cells that force the
# accelerated path (both advertise CPUID.(EAX=1):ECX bits 1 and
# 25). Denverton and max both qualify per D054.
test-x86_64-aes128-gcm: test-x86_64-aes128-gcm-native \
                        test-x86_64-aes128-gcm-pclmul \
                        test-x86_64-aes128-gcm-pclmul-max

test-x86_64-aes128-gcm-native: $(X86_GCM_BIN)
	$(X86_GCM_BIN)

test-x86_64-aes128-gcm-pclmul: $(X86_GCM_BIN)
	$(QEMU_X86_FORK) -cpu $(QEMU_X86_CPU) $(X86_GCM_BIN)

test-x86_64-aes128-gcm-pclmul-max: $(X86_GCM_BIN)
	$(QEMU_X86_FORK) -cpu max $(X86_GCM_BIN)

test-aarch64-aes128-gcm: $(AARCH_GCM_BIN)
	$(QEMU_AARCH64) $(AARCH_GCM_BIN)

lint: lint-gcc lint-clang lint-clang-tidy lint-cppcheck lint-scan-build

lint-gcc:
	@echo "=== gcc compile-as-lint ==="
	@for src in $(LINT_SRCS); do \
	    echo "  [gcc] $$src"; \
	    $(GCC_HOST) $(LINT_CFLAGS) -c -o /dev/null $$src || exit 1; \
	done

lint-clang:
	@echo "=== clang compile-as-lint ==="
	@for src in $(LINT_SRCS); do \
	    echo "  [clang] $$src"; \
	    $(CLANG_HOST) $(LINT_CFLAGS) -c -o /dev/null $$src || exit 1; \
	done

lint-clang-tidy:
	@echo "=== clang-tidy ==="
	$(CLANG_TIDY) $(LINT_SRCS) -- $(LINT_CFLAGS)

lint-cppcheck:
	@echo "=== cppcheck ==="
	$(CPPCHECK) --enable=warning,performance,portability,style \
	    --error-exitcode=1 --inline-suppr \
	    --suppress=missingIncludeSystem \
	    --std=c11 -I. $(LINT_SRCS)

lint-scan-build:
	@echo "=== scan-build (clang static analyzer) ==="
	@# -disable-checker matches the .clang-tidy disable: C11 Annex K
	@# `_s`-suffixed functions aren't universally available, so plain
	@# memcpy/memset with known bounds is correct C, not "insecure."
	@for src in $(LINT_SRCS); do \
	    echo "  [scan-build] $$src"; \
	    $(SCAN_BUILD) --status-bugs \
	        -enable-checker security -enable-checker unix \
	        -enable-checker nullability \
	        -disable-checker security.insecureAPI.DeprecatedOrUnsafeBufferHandling \
	        $(CLANG_HOST) $(LINT_CFLAGS) -c -o /dev/null $$src \
	            || exit 1; \
	done

clean:
	rm -rf $(BUILD_DIR)
	# Cascade into the arch-side crypto build dirs. Without this,
	# a `make clean && make test` cycle in this directory leaves
	# the asm .o files (arch/<isa>/crypto/build/*.o) stale —
	# they get rebuilt only when the .S file is newer. Symptom
	# observed 2026-04-23 while working on pt-zero-on-mismatch:
	# the test harness ran against a pre-edit binary and passed
	# with outdated semantics. Cascading the clean here makes
	# `make clean && make test` rebuild everything end-to-end.
	$(MAKE) -C $(REPO_ROOT)/arch/x86_64/crypto clean
	$(MAKE) -C $(REPO_ROOT)/arch/aarch64/crypto clean
