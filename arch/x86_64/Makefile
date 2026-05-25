# SPDX-License-Identifier: AGPL-3.0-or-later
# fireasmserver x86_64 build
#
# Usage:
#   make PLATFORM=qemu        (default)
#   make PLATFORM=firecracker
#
# Assembler: NASM per D048. Linker stays at binutils' ld.

PLATFORM ?= qemu
# Fail-path stub scenario — set when PLATFORM=failpath. The stub
# at arch/x86_64/platform/failpath/boot.S uses %ifdef to pick
# which malformed virtqueue state to inject before calling
# l2_dispatch. Currently only BAD_ID is implemented.
SCENARIO ?= BAD_ID

AS := nasm
LD := x86_64-linux-gnu-ld

# qemu boots via Multiboot1 on -machine pc, which requires a 32-bit
# ELF. firecracker AND failpath boot via PVH, which requires a
# 64-bit ELF wrapping the same `[bits 32]` entry stub. ELF class
# is a function of (arch, platform).
# -i paths for NASM `%include`s:
#   - tooling/memlayout_diffharness/ : `bcvm_errors.nasm.inc` from
#     arch/x86_64/memory/bytecode_vm.S — shared contract with the
#     diff harness driver.
#   - build/$(PLATFORM)/ : `memreq_records.inc` and
#     `memreq_pin_hot.inc` emitted by the memreq_codegen tool from
#     platform/$(PLATFORM)/regions.yaml (D066 step 5a).
# Trailing slash is required by NASM's path-handling.
NASM_INCLUDE := -i ../../tooling/memlayout_diffharness/ \
                -i build/$(PLATFORM)/

ifneq ($(filter $(PLATFORM),firecracker failpath),)
ASFLAGS := -f elf64 $(NASM_INCLUDE)
LDFLAGS :=
else
ASFLAGS := -f elf32 $(NASM_INCLUDE)
LDFLAGS := -m elf_i386
endif

# The failpath stub picks its scenario at assemble time via
# -DFAILPATH_SCENARIO_<NAME>=1. Pass through SCENARIO=X to
# select; default BAD_ID (the only one implemented today).
ifeq ($(PLATFORM),failpath)
ASFLAGS += -DFAILPATH_SCENARIO_$(SCENARIO)=1
endif

# TX API end-to-end test pre-bake (phase b.4). When TXAPI_PREBAKE=1
# is set, the firecracker boot.S enqueues one TX API request before
# entering the dispatch loop. Builds into a separate BUILD_DIR so
# the production binary at build/firecracker/ is not overwritten;
# the test pytest at tooling/tests/integration/test_l2_tx_api.py
# invokes this build explicitly. OFF by default — Pi deploys + the
# normal L2 integration suite use the unflagged production binary.
TXAPI_PREBAKE ?= 0
ifeq ($(TXAPI_PREBAKE),1)
ifneq ($(PLATFORM),firecracker)
$(error TXAPI_PREBAKE=1 only meaningful with PLATFORM=firecracker)
endif
ASFLAGS += -DTXAPI_PREBAKE=1
BUILD_SUFFIX := _txapi
else
BUILD_SUFFIX :=
endif

SRC_DIR := platform/$(PLATFORM)
BUILD_DIR := build/$(PLATFORM)$(BUILD_SUFFIX)

# Re-point NASM include path to BUILD_DIR so the memreq incs are
# searched in the same dir where the codegen rule below writes them.
# Late re-assignment + ASFLAGS rebuild because the initial
# NASM_INCLUDE definition above pinned the non-suffixed path.
NASM_INCLUDE := -i ../../tooling/memlayout_diffharness/ -i $(BUILD_DIR)/
ifneq ($(filter $(PLATFORM),firecracker failpath),)
ASFLAGS := -f elf64 $(NASM_INCLUDE)
else
ASFLAGS := -f elf32 $(NASM_INCLUDE)
endif
ifeq ($(PLATFORM),failpath)
ASFLAGS += -DFAILPATH_SCENARIO_$(SCENARIO)=1
endif
ifeq ($(TXAPI_PREBAKE),1)
ASFLAGS += -DTXAPI_PREBAKE=1
endif

# Codegen for `.memreq` records and hot-tier register pins.
# Triggered by any change to the YAML source-of-truth or the
# tool's Python sources. Outputs land alongside object files in
# build/$(PLATFORM)/ so the `-i` flag above finds them.
REGIONS_YAML := $(SRC_DIR)/regions.yaml
MEMREQ_INCS := $(BUILD_DIR)/memreq_records.inc \
               $(BUILD_DIR)/memreq_pin_hot.inc
MEMREQ_CODEGEN_SRC := $(wildcard ../../tooling/src/memreq_codegen/*.py)

PLAT_SOURCES := $(sort $(wildcard $(SRC_DIR)/*.S))
PLAT_OBJECTS := $(patsubst $(SRC_DIR)/%.S,$(BUILD_DIR)/%.o,$(PLAT_SOURCES))
# Arch-shared sources live one level up under arch/x86_64/<subdir>/.
# `memory/` holds the init-time static memory layout machinery
# (D059 + D060 — bump allocator, bytecode VM, .memreq macros).
# `l2/` holds the FSA dispatcher + transitions + buffer-pool sizing
# (D012 + D043 — FSA runtime). Crypto stays out: those .S files
# are exercised by the host-side test harness in
# tooling/crypto_tests/, not linked into the guest kernel today.
MEM_SOURCES  := $(sort $(wildcard memory/*.S))
MEM_OBJECTS  := $(patsubst memory/%.S,$(BUILD_DIR)/memory/%.o,$(MEM_SOURCES))
# L2 dispatcher pulls in virtio-MMIO + emit_bytes/emit_hex32 + the
# canary TX frame; firecracker and failpath both link it.
# The qemu platform's boot.S is a minimal Multiboot1 stub without
# virtio. Conditional inclusion avoids unresolved external refs at
# link time on the qemu target.
ifneq ($(filter $(PLATFORM),firecracker failpath),)
L2_SOURCES   := $(sort $(wildcard l2/*.S))
L2_OBJECTS   := $(patsubst l2/%.S,$(BUILD_DIR)/l2/%.o,$(L2_SOURCES))
# L3 stub: the dispatcher's hand-off symbol for accepted
# non-ARP RX frames. D068 working order item 5 — see
# arch/x86_64/l3/stub.S for the contract. Future production
# L3 replaces stub.S; gated on the same firecracker/failpath
# filter so the qemu platform doesn't pull in unresolved
# emit_bytes / emit_hex32 refs.
L3_SOURCES   := $(sort $(wildcard l3/*.S))
L3_OBJECTS   := $(patsubst l3/%.S,$(BUILD_DIR)/l3/%.o,$(L3_SOURCES))
else
L2_SOURCES   :=
L2_OBJECTS   :=
L3_SOURCES   :=
L3_OBJECTS   :=
endif
ARCH_OBJECTS := $(MEM_OBJECTS) $(L2_OBJECTS) $(L3_OBJECTS)
OBJECTS      := $(PLAT_OBJECTS) $(ARCH_OBJECTS)
LINKER_SCRIPT := $(SRC_DIR)/linker.ld
BINARY := $(BUILD_DIR)/guest.elf

.PHONY: all clean

all: $(BINARY)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.S $(MEMREQ_INCS) | $(BUILD_DIR)
	$(AS) $(ASFLAGS) -o $@ $<

$(BUILD_DIR)/memory/%.o: memory/%.S | $(BUILD_DIR)/memory
	$(AS) $(ASFLAGS) -o $@ $<

$(BUILD_DIR)/l2/%.o: l2/%.S | $(BUILD_DIR)/l2
	$(AS) $(ASFLAGS) -o $@ $<

$(BUILD_DIR)/l3/%.o: l3/%.S | $(BUILD_DIR)/l3
	$(AS) $(ASFLAGS) -o $@ $<

# Both .inc files are produced in one tool invocation; the second
# target rule for memreq_pin_hot.inc just points at the same recipe.
$(BUILD_DIR)/memreq_records.inc \
$(BUILD_DIR)/memreq_pin_hot.inc &: \
        $(REGIONS_YAML) $(MEMREQ_CODEGEN_SRC) | $(BUILD_DIR)
	../../.venv/bin/memreq-codegen $(REGIONS_YAML) \
	    --arch x86_64 \
	    --out-records $(BUILD_DIR)/memreq_records.inc \
	    --out-pins $(BUILD_DIR)/memreq_pin_hot.inc

$(BINARY): $(OBJECTS) $(LINKER_SCRIPT)
	$(LD) $(LDFLAGS) -T $(LINKER_SCRIPT) -o $@ $(OBJECTS)

$(BUILD_DIR) $(BUILD_DIR)/memory $(BUILD_DIR)/l2 $(BUILD_DIR)/l3:
	mkdir -p $@

clean:
	rm -rf build/
