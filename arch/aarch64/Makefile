# SPDX-License-Identifier: AGPL-3.0-or-later
# fireasmserver aarch64 build
#
# Usage:
#   make PLATFORM=firecracker   (default)
#
# Produces build/$(PLATFORM)/guest.bin, a raw Linux arm64 Image that
# Firecracker aarch64 loads directly as a kernel.

PLATFORM ?= firecracker
# Fail-path stub scenario — set when PLATFORM=failpath. The
# stub at arch/aarch64/platform/failpath/boot.S uses .ifdef
# to pick which malformed virtqueue state to inject before
# calling l2_dispatch.
SCENARIO ?= BAD_ID

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

# Pass scenario flag through to GNU as via --defsym for the
# failpath build. Mirrors x86_64's -DFAILPATH_SCENARIO_<NAME>=1.
ifeq ($(PLATFORM),failpath)
ASFLAGS += --defsym FAILPATH_SCENARIO_$(SCENARIO)=1
endif

# TX API end-to-end test pre-bake (phase b.4 aarch64 mirror).
# When TXAPI_PREBAKE=1, the firecracker boot.S enqueues one TX
# API request before entering the dispatch loop. Mirrors
# arch/x86_64/Makefile's flag exactly — separate BUILD_DIR so
# the production binary at build/firecracker/ isn't overwritten.
# OFF by default — Pi deploys + the L2 integration tests via
# the production binary unchanged.
TXAPI_PREBAKE ?= 0
ifeq ($(TXAPI_PREBAKE),1)
ifneq ($(PLATFORM),firecracker)
$(error TXAPI_PREBAKE=1 only meaningful with PLATFORM=firecracker)
endif
ASFLAGS += --defsym TXAPI_PREBAKE=1
BUILD_SUFFIX := _txapi
else
BUILD_SUFFIX :=
endif

SRC_DIR       := platform/$(PLATFORM)
BUILD_DIR     := build/$(PLATFORM)$(BUILD_SUFFIX)

# Re-point ASFLAGS include path to the suffixed BUILD_DIR so
# memreq incs are found in the right dir (initial ASFLAGS pinned
# the non-suffixed path).
ASFLAGS := -I ../../tooling/memlayout_diffharness -I $(BUILD_DIR)
ifeq ($(PLATFORM),failpath)
ASFLAGS += --defsym FAILPATH_SCENARIO_$(SCENARIO)=1
endif
ifeq ($(TXAPI_PREBAKE),1)
ASFLAGS += --defsym TXAPI_PREBAKE=1
endif

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
# `l2/` holds the FSA dispatcher + transitions + buffer-pool sizing
# (D012 + D043 — FSA runtime). Crypto stays out: those .S files
# are exercised by the host-side test harness in
# tooling/crypto_tests/, not linked into the guest kernel today.
MEM_SOURCES   := $(sort $(wildcard memory/*.S))
MEM_OBJECTS   := $(patsubst memory/%.S,$(BUILD_DIR)/memory/%.o,$(MEM_SOURCES))
# L2 dispatcher pulls in virtio-MMIO + emit_hex32 + the canary TX
# frame; firecracker AND failpath both link it. The qemu platform's
# boot.S is a minimal PL011-on-virt stub without virtio.
# Conditional inclusion avoids unresolved external refs at link
# time on the qemu target. Mirrors arch/x86_64/Makefile's gate.
ifneq ($(filter $(PLATFORM),firecracker failpath),)
L2_SOURCES    := $(sort $(wildcard l2/*.S))
L2_OBJECTS    := $(patsubst l2/%.S,$(BUILD_DIR)/l2/%.o,$(L2_SOURCES))
else
L2_SOURCES    :=
L2_OBJECTS    :=
endif
ARCH_OBJECTS  := $(MEM_OBJECTS) $(L2_OBJECTS)
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

$(BUILD_DIR)/l2/%.o: l2/%.S | $(BUILD_DIR)/l2
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

$(BUILD_DIR) $(BUILD_DIR)/memory $(BUILD_DIR)/l2:
	mkdir -p $@

clean:
	rm -rf build/
