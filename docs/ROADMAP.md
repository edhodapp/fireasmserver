# fireasmserver roadmap

Scope-locking document. Captures the project's long-range
phase sequence, the cross-cutting architectural invariants
that shape every phase, and the explicit out-of-scope list
that keeps the work from sprawling.

Not a schedule. Phase ordering reflects technical dependencies,
not calendar commitments. Individual phase effort sizes are
rough approximations subject to re-estimation as each phase
opens.

This document is a living artifact. Supersedes any earlier
informal scope discussion; updates land as commits with
normal commit discipline.

## Foundational axiom

**"Software should be like air"** (Ed Hodapp's tagline,
codified project-wide 2026-04-23 via
`~/.claude/projects/-home-ed-fireasmserver/memory/user_tagline_software_as_air.md`).
Reliability is not a feature; it is the baseline the work
must embody. Scope decisions trade feature velocity for
reliability, not the reverse. MVP does not mean cutting
corners; it means the smallest thing that demonstrably meets
the axiom.

## Load-bearing architectural invariants

These invariants shape every phase and every data structure.
Violating any of them requires either a superseding DECISIONS
entry or removal from this list. They don't get quietly
relaxed.

- **D003 — 100% assembly.** Every line of runtime code is
  hand-written assembly (NASM on x86_64, GNU as on aarch64).
  Python / shell tooling sits alongside but never inside the
  runtime.

- **D032 — crypto-math is ISA-idiomatic, macros-first,
  constant-time.** No table-lookup scalar paths for
  cryptographic primitives.

- **D034 — hardware platform profiles with required ISA
  feature floors.** x86_64: Westmere+ server / Goldmont+
  Atom. AArch64: Cortex-A76+ / Neoverse N1+ / Apple cores.
  Features required: AES, SHA-2, PCLMULQDQ/PMULL, CRC32.

- **D049 — ontology as formal verifiable requirements.**
  Every requirement row in `docs/l2/REQUIREMENTS.md` (and
  future `docs/tls/REQUIREMENTS.md`) has a first-class
  representation in `tooling/qemu-harness.json`.

- **D051 — ontology audit as closing pre-push gate.**

- **D052 — side-session isolation via git worktree.**

- **D054 — QEMU fork sandbox for ISA-extension testing.**

- **D055 — external-input length clamp at trust boundary.**

- **D056 — ISA overflow-detection primitives on every
  external-input arithmetic step.**

- **D057 — AES-NI required at runtime on x86_64; no scalar
  fallback.** (The same posture extends to PCLMULQDQ /
  PMULL / other ISA-feature-floor crypto; a follow-on
  D-entry may codify this extension explicitly after the
  first consumer lands.)

- **D058 — no mutable state crosses a core or VM boundary.**
  Actor-model architecture. Cross-core is lockless queues;
  cross-VM is network. Mutable state is transported between
  processes only by copying it into a message. Each VM is
  internally a set of single-threaded actors (one per core)
  communicating through well-defined message-passing
  boundaries. See the full statement in DECISIONS.md D058.

## Architecture shape (cascade from D058)

Each VM runs:

- **LB core (core 0):** accept loop, TLS termination, HTTP
  request parsing, dispatch to workers via lockless queue.
  Owns TLS session state; mutable state never leaves this
  core.
- **Worker cores (cores 1..N-1):** each owns its own WASM
  engine, its own request-handler stack, its own
  request-scoped allocations. Workers dequeue plaintext
  requests from the LB, process them (WASM execution,
  backend calls, response generation), enqueue responses
  back to the LB.
- **Shared-read-only state:** config, certs, trust store,
  compiled WASM bytecode, CA root set. Frozen at init-
  complete fence; every core reads, no one writes after
  init.
- **Future-reserved:** one worker core may run a stateful
  engine (Redis-shape database). Queue semantics unchanged;
  the DB core's state stays inside the DB core.

Across VMs:

- **Fully stateless across VMs in MVP.** Customers who need
  cluster-wide mutable state externalize to their choice of
  coordinator (Postgres, Redis, etcd, S3, etc.).
- **Multi-VM scaling** via normal cloud load-balancing (ELB,
  DNS round-robin, HAProxy, whatever).
- **Multi-VM sharding** is a future option: one VM = one
  shard; routing above our layer. D058 reserves the
  architectural space without requiring it in MVP.

## Phase sequence

### Phase 0 — current

Finish the two active threads before committing TLA+ calories
to Phase 1. These are in progress and have no dependency on
the TLS work.

- Round-III AES-128-GCM (side session, dispatched
  2026-04-23).
- AArch64 L2 parity: VIO-001..009 init + VIO-R + VIO-T,
  mirroring the x86_64 work. First commit (VIRTIO probe)
  landed 2026-04-23; remaining ~6-8 commits follow the
  x86_64 pattern slice-by-slice.

No new architectural surface in Phase 0 — it closes out
existing commitments.

### Phase 1 — TLA+ concurrency specification

**Entry criterion:** Phase 0 closed out. Round-III merged,
aarch64 L2 at VIO-T parity.

**Deliverable:** `docs/concurrency/SPEC.tla` (TLA+ spec in
PlusCal notation, translated to plain TLA+ by the pcal
translator), `docs/concurrency/INVARIANTS.md` (natural-
language description of every invariant we assert), TLC
model-checking runs at N = 1, 2, 3, 4, 8 worker cores with
automated CI verification.

**What the spec models:**

- **Actor lifecycle:** LB core and worker cores as
  independent single-threaded processes, each with its own
  state machine.
- **Lockless queue protocol between LB and workers:** SPSC
  or MPMC ring buffer primitives, enqueue / dequeue
  semantics, backpressure handling, producer-starvation
  and consumer-starvation corner cases.
- **Connection-and-request lifecycle:** acceptance →
  TLS handshake → request parsing (on LB) → dispatch to
  worker → worker processing → response → teardown. Every
  message in this sequence has a TLA+-modeled step.
- **Graceful shutdown:** LB stops accepting; in-flight
  requests drain; workers complete queued requests; fence
  and halt.
- **Failure modes:** a core panics — what happens to its
  in-flight work? (Answer must be: only that core's work
  is lost; the rest of the VM keeps serving. The spec
  proves it.)
- **Init-complete fence:** the mutually-exclusive phase
  transition from "config mutable, one writer" to "config
  frozen, all readers." TLC verifies no core reads config
  before the fence and no write happens after.

**Invariants to check (safety):**

- No request is lost (enqueued-but-never-dequeued) under
  any scheduling.
- No request is double-processed.
- No connection holds TLS session state on more than one
  core.
- No mutable state is shared across cores (the D058
  invariant, formally encoded).
- No message is read from an uninitialized queue slot.

**Invariants to check (liveness):**

- Every accepted connection eventually either completes
  or is cleanly torn down (no hung connections).
- Graceful shutdown terminates in finite time if all
  in-flight requests terminate in finite time.
- If the LB has capacity and a worker has capacity, the
  LB's queue eventually drains (no livelock at the queue
  boundary).

**Why this comes before implementation:** shared-mutable-
state concurrency bugs are the class of CVE Ed explicitly
refuses to ship. Writing the spec first, running TLC on
it, finding the bugs in the model where they're cheap —
that is the only path consistent with the axiom for the
multiprocessing work. Retrofitting formal verification
after the code ships is much more expensive and often
impossible (the code's shape constrains the model, not
the other way around).

**Toolchain:** TLA+ Toolbox (or VS Code TLA+ extension) for
authoring; TLC for bounded model checking; Apalache for
symbolic model checking on larger state spaces.
Open-source; BSD-3 licensed; AGPL-compatible.

**Scope approximation:** 500-1500 lines of PlusCal for the
full model; separate invariant suite; CI workflow that
runs TLC on every push to the spec.

### Phase 2 — TLS single-core

**Entry criterion:** Phase 1 spec landed, all invariants
TLC-verified at N=1, initial N=2..8 runs passing.

**Deliverable:** TLS 1.2 + 1.3 server and client, single-
core, AEAD-only (AES-128-GCM, ChaCha20-Poly1305), X25519 +
secp256r1 key exchange, RSA-PSS + RSA-PKCS#1 v1.5 +
Ed25519 signatures, minimal-but-strict X.509 v3 DER parser
(boringssl-minimum + extKeyUsage/keyUsage), serious security
test matrix (NIST CAVS + Wycheproof + RFC 8448 traces +
x509-limbo + Frankencerts + Heartbleed/goto-fail/Lucky13
regression tests + OSS-Fuzz integration + dudect-style
constant-time verification).

Phase 2 validates the FSA framework on a realistic load and
produces a shippable single-core product. Multi-core
activation is Phase 3; nothing in Phase 2 precludes it.

**Scope approximation:** multi-month work. Per-primitive side
sessions continue in the established rhythm for ChaCha20-
Poly1305, X25519, secp256r1, RSA (modexp + Montgomery), and
Ed25519. Main session handles FSA runtime, TLS state
machines, DER parser, certificate validation, integration
glue.

### Phase 3 — multicore activation

**Entry criterion:** Phase 2 shipped. TLA+ spec updated for
any differences between the spec and the reality Phase 2
discovered.

**Deliverable:** LB core brings up N-1 worker cores via PSCI
CPU_ON (aarch64) or INIT-SIPI-SIPI (x86_64). Lockless queue
primitives wired per the verified TLA+ model. Per-core FSA
instances. Request dispatch policy (round-robin or
least-loaded; decision at Phase 3 entry). Integration tests
with N = 1, 2, 4, 8 worker cores; all invariants from the
TLA+ spec empirically reproduced.

Each Phase 3 commit re-runs the TLC model against the
current spec before landing. Any code change that adjusts
the spec also reruns TLC at the full N range.

**Scope approximation:** several weeks once Phase 1 + 2 land.
Most of the risky work is in Phase 1 (the spec); Phase 3
is refinement against a verified model.

### Phase 4 — WASM runtime

**Entry criterion:** Phase 3 landed; multicore stable.

**Deliverable:** WebAssembly 1.0 MVP subset interpreter in
hand-rolled assembly (i32/i64/f32/f64, control flow, linear
memory, minimal import/export) + WASI subset (fd_read,
fd_write, random_get, clock_time_get). One WASM engine
instance per worker core; WASM modules execute sandboxed
on worker cores. Module instantiation lifecycle (load,
instantiate, invoke, free) integrated with the
per-core actor model.

Stretch: AOT compilation of hot-path WASM bytecode to native
assembly (via an in-process assembler). Not MVP.

**Scope approximation:** ~3-4 months of focused work.
Orthogonal to Phase 3 in the sense that WASM can be started
before Phase 3 finishes (just deploy as a single-core-only
WASM for development); but full WASM-on-workers requires
Phase 3.

### Phase 5 — DB-on-a-core (architectural space, post-MVP)

**Status:** reserved by D058; not committed to a build date.

A customer-deployed fireasmserver VM might choose the layout
`{core 0: LB, core 1: WASM worker, core 2: DB}` to get
local-latency durable state. Serves workloads where a small
working set fits in one core's RAM and the persistence can
ride a block device. Redis-shape single-threaded event loop
on the DB core; queries via the same lockless queue mechanism
workers use.

D058 preserves the architectural space. No implementation
work planned until a concrete user demand surfaces.

## Multi-VM test matrix (threaded through phases)

Ed's requirement: tests must scale cleanly from 1 to 8 VM
instances. Applied per phase:

- **Phase 0:** N=1 VM. No multi-VM testing beyond the
  existing x86_64/aarch64 cells in the CD matrix.
- **Phase 1:** TLA+ spec itself models the multi-VM case
  (if we accept external coordination as out-of-scope,
  the multi-VM model reduces to "each VM is independent"
  — trivial). TLC doesn't need to run at multi-VM scale.
- **Phase 2:** single-VM TLS. Multi-VM testing adds a
  CI cell that spins up 1, 2, 4, 8 independent Firecracker
  VMs, a host-side load generator sends mixed-client
  traffic, all VMs serve independently, no cross-VM state
  leaks.
- **Phase 3:** multi-VM × multi-core. Same test but each
  VM has multi-core enabled. Verify per-VM core-count
  independence and per-VM per-core correctness.
- **Phase 4+:** WASM module per-VM isolation tests; DB
  partition tests if DB-on-a-core ships.

## Explicitly out of scope

These are not "later"; they are "no" until a concrete
user demand reopens them. Listing them here prevents
accidental scope creep and gives reviewers a clear "what
isn't here" answer.

- **TLS 1.0, TLS 1.1, SSL 3.0, SSLv2.** All deprecated;
  no fallback. Clients speaking these versions get a
  clean handshake refusal.
- **CBC+HMAC cipher suites, RC4, 3DES, export-grade
  ciphers.** AEAD-only posture (D057 extends to this).
- **TLS renegotiation.** Refused; handshake initiation
  is allowed only once per connection.
- **TLS compression.** CRIME attack class; refused.
- **TLS heartbeat.** Heartbleed attack class; refused.
- **RSA key exchange** (as distinct from RSA signatures
  on certs). ECDHE only; RSA certs sign, never establish
  session keys.
- **Server-side revocation checking (OCSP on client
  connections).** Modern-browser-matching posture: no
  real-time revocation checks, rely on short-lived certs
  + OCSP stapling from customer's origin.
- **Distributed consensus, leader election, shared
  caches across VMs.** D058 precludes; delegate to
  external coordinators.
- **Live connection migration during rolling upgrades.**
  D058 precludes; clients retry.
- **Cluster-wide rate-limiting.** Per-VM approximate
  (token-bucket); exact cluster-wide via external.
- **Full X.509 path validation (name constraints, policy
  mappings, CRL processing).** Boringssl-minimum parser
  only — SubjectPublicKeyInfo, validity, SAN, BasicConstraints,
  KeyUsage / ExtKeyUsage. Reject anything unknown-critical.
- **HTTP/2, HTTP/3, QUIC.** Not yet. HTTP/1.1 is the MVP
  application-layer protocol.
- **Client-side cert verification with CRL / OCSP-fetch.**
  Not yet; short-cert-lifetime posture.
- **Arbitrary-IV GCM (any IV length other than 12 bytes).**
  TLS 1.3 standardizes 12 bytes; arbitrary-IV J0 is out.
- **Non-Ed25519 / non-P-256 / non-RSA cert signatures.**
  ECDSA P-384, ECDSA P-521, Ed448 all deferred.
- **AES-256, SHA-384 (beyond primitives we already need).**
  No immediate customer use case; 128-bit AES + SHA-256
  meet all TLS 1.2/1.3 MTI.

## References

- `DECISIONS.md` — the immutable decision log. D003 through
  D058 are load-bearing for this roadmap.
- `docs/l2/REQUIREMENTS.md` — L2 networking requirements
  that have already shipped or are in Phase 0.
- `docs/side_sessions/` — per-dispatch briefings from prior
  crypto-primitive side sessions; pattern reference for
  future dispatches.
- `~/.claude/projects/-home-ed-fireasmserver/memory/user_tagline_software_as_air.md`
  — the axiom.
- `~/.claude/projects/-home-ed-fireasmserver/memory/project_architecture_actor_model.md`
  — the pointer memory for D058.
- Astier FSA reference document at
  `~/.claude/Finite_State_Automaton_for_Input_Output_Containers_1746783516.pdf`
  — the per-actor runtime pattern draws from this.
- Lextrait "Software Development or the Art of Abstraction
  Crafting" at `~/.claude/Software_Development_or_the_Art_of_Abstraction_Crafting_1765497213.pdf`
  — the abstraction posture the project embodies.

## Updating this document

ROADMAP.md is NOT immutable (unlike DECISIONS.md entries).
When a phase ships or a scope boundary moves, this doc gets
updated in the same commit that ships the change.
Out-of-scope items move into-scope only with a new
DECISIONS.md entry that explicitly reopens them.
