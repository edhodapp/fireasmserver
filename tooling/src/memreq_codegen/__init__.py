"""Memreq codegen — emit .memreq records from a regions.yaml source.

Per D066 Q-D: the human-edited source-of-truth for memory regions is
`arch/<isa>/platform/<vmm>/regions.yaml`. This package validates the
YAML against the schema, computes the FNV-1a name hash, encodes
size/align expressions as D060 bytecode, and emits two .inc files
per (arch, platform):

- `memreq_records.inc` — the 48-byte .memreq record bytes, one per
  region, in the `.memreq` ELF section. The init-time allocator
  iterates this section and writes `assigned_addr` / `assigned_size`
  in place.
- `memreq_pin_hot.inc` — register pins for hot-tier regions,
  emitted at the kernel-main entry per Q-B's per-arch register
  budget.

D066 step 5a is literal-only (LIT bytecode); the schema accepts
integer size/align directly. Later steps (#28) will extend the
schema to non-literal expressions (CPU / TUNING references).
"""
