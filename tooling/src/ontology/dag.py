"""DAG persistence and snapshot utilities for the ontology.

Forked from python_agent.dag_utils on 2026-04-19. This fork
intentionally drops the HMAC integrity signing and the LLM
prompt-injection scan: our ontology is produced from a trusted
in-repo builder (tooling/build_qemu_harness_ontology.py and
future peers), not from external input, so the security machinery
python_agent needs for agent-mediated loads is dead weight here.

If fireasmserver ever starts loading ontologies from a less-trusted
source, revisit this decision and port the integrity checks back.

O2 additions (D049):

- ``ontology_content_hash`` — stable hash over a serialized
  ``Ontology`` so the builder can decide whether a new snapshot
  actually changes anything or is a no-op re-run.
- ``git_snapshot_label`` — build a snapshot label that embeds the
  current git HEAD SHA and a ``+dirty`` marker when the working
  tree has uncommitted changes, per D049's source-level + DAG
  cross-reference contract.
- ``snapshot_if_changed`` — idempotent wrapper around
  ``save_snapshot``: adds a node only when the new ontology's
  content hash differs from the current node's.

O2b additions (D049, concurrent-process safety):

- ``dag_transaction`` — context manager that acquires an
  exclusive ``fcntl.flock`` on a sidecar lock file, loads the
  DAG, yields it for modification, and saves on normal exit.
  Serializes the entire load-modify-save cycle across processes,
  so concurrent builders can't lose each other's updates.
- ``save_dag`` now uses ``os.replace`` (cross-platform atomic
  rename), creates missing parent directories, and suppresses
  ``FileNotFoundError`` during cleanup.
- ``snapshot_if_changed`` signature tightened:
  ``tuple[str, bool]`` (no longer falsely advertises ``None``).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from ontology.models import (
    DAGEdge,
    DAGNode,
    Decision,
    Ontology,
    OntologyDAG,
)


def make_node_id() -> str:
    """Generate a unique node ID using uuid4."""
    return str(uuid.uuid4())


def load_dag(path: str, project_name: str) -> OntologyDAG:
    """Load an OntologyDAG from a JSON file.

    Returns a new empty DAG if the file is not present or fails
    to validate. A validation failure is treated as a hard error
    (raise) rather than silently creating an empty DAG — the
    builder should never be reading a corrupted DAG without
    knowing, especially given the ontology's role as the project's
    formal-requirements artifact.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return OntologyDAG(project_name=project_name)
    return OntologyDAG.from_json(text)


def save_dag(dag: OntologyDAG, path: str) -> None:
    """Save an OntologyDAG to a JSON file.

    Uses atomic rename (temp file + ``os.replace``) so an
    interrupted write cannot corrupt the existing artifact.
    Creates the parent directory if missing. On partial-write
    failure, the temp file is cleaned up with
    ``FileNotFoundError`` suppressed in case the write never
    reached disk at all.
    """
    parent_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent_dir, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=parent_dir,
        suffix=".tmp", delete=False,
    )
    try:
        handle.write(dag.to_json())
        handle.close()
        # os.replace is atomic on Linux AND Windows; os.rename
        # fails on Windows if the destination exists.
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(handle.name)
        raise


def save_snapshot(
    dag: OntologyDAG, ontology: Ontology,
    label: str, decision: Decision | None = None,
) -> str:
    """Create a new DAG node from the current ontology.

    Links it as a child of the current node if one exists. If
    decision is None, a default decision is recorded for the edge.
    Returns the new node id.
    """
    now = datetime.now(timezone.utc).isoformat()
    node_id = make_node_id()
    node = DAGNode(
        id=node_id,
        ontology=ontology.model_copy(deep=True),
        created_at=now,
        label=label,
    )
    dag.nodes.append(node)
    if dag.current_node_id:
        if decision is None:
            decision = Decision(
                question="save",
                options=["continue"],
                chosen="continue",
                rationale=label,
            )
        edge = DAGEdge(
            parent_id=dag.current_node_id,
            child_id=node_id,
            decision=decision,
            created_at=now,
        )
        dag.edges.append(edge)
    dag.current_node_id = node_id
    return node_id


def ontology_content_hash(ontology: Ontology) -> str:
    """SHA-256 hex digest over a stable JSON serialization.

    ``sort_keys=True`` means key order can't drift between Python
    versions; ``separators=(',', ':')`` removes whitespace so
    formatting tweaks don't churn the hash. Output is the full
    hex digest — not truncated — so an auditor can paste it
    directly into grep against DAG labels or cross-check tools.
    """
    payload = json.dumps(
        ontology.model_dump(),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_head_sha(short: bool = True) -> str | None:
    """Return the current HEAD SHA, or None if git is unavailable
    or the current directory is not in a git work tree.

    ``short=True`` uses ``git rev-parse --short`` for the
    human-friendly 7-ish-char form embedded in snapshot labels;
    ``short=False`` gives the full 40-char SHA for hash-level
    cross-referencing.
    """
    args = ["git", "rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    try:
        result = subprocess.run(
            args, capture_output=True,
            text=True, encoding="utf-8",
            check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_is_dirty() -> bool:
    """True iff the working tree has uncommitted changes.

    Uses ``git status --porcelain`` which prints one line per
    modified / untracked file and is empty when the tree is clean.
    Treats any non-zero git exit as clean — we'd rather under-flag
    than crash the build on a git hiccup.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True,
            encoding="utf-8", check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def git_snapshot_label(prefix: str = "") -> str:
    """Produce a snapshot label embedding git source context.

    Format: ``<prefix> <ISO-UTC-timestamp> @<short-sha>[+dirty]``,
    whitespace-separated for readability. Prefix can carry a
    human-supplied tag (``"perf-baseline"``, ``"D049-rollout"``,
    etc.); missing git context falls back to just the timestamp.

    Keep the resulting label short enough to eyeball in
    ``git log`` adjacent output — full SHA + full dirty-status
    ceremony goes in node properties we can extend later if
    needed, not in the label.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha = _git_head_sha(short=True)
    parts = [prefix, now] if prefix else [now]
    if sha:
        dirty = "+dirty" if _git_is_dirty() else ""
        parts.append(f"@{sha}{dirty}")
    return " ".join(parts)


def snapshot_if_changed(
    dag: OntologyDAG, ontology: Ontology,
    label: str, decision: Decision | None = None,
) -> tuple[str, bool]:
    """Append a snapshot only when the ontology's content differs
    from the currently-selected parent node's.

    Returns ``(node_id, created)`` where ``created`` is True when a
    new node was appended and False when this call was a no-op
    because the content hash already matched the current node.

    When ``created`` is False, the returned ``node_id`` is the
    current node's ID — lets the caller still reference "the node
    that holds this ontology" without needing a second lookup.

    When the DAG is empty (no current node) the new snapshot always
    lands as the root. This is the bootstrap case for a fresh repo.
    The return tuple's first element is always a string — there's
    no code path that yields ``None``.
    """
    new_hash = ontology_content_hash(ontology)
    current = dag.get_current_node()
    if current is not None:
        current_hash = ontology_content_hash(current.ontology)
        if current_hash == new_hash:
            return current.id, False
    node_id = save_snapshot(dag, ontology, label, decision)
    return node_id, True


@contextmanager
def dag_transaction(
    path: str, project_name: str,
) -> Iterator[OntologyDAG]:
    """Process-safe load-modify-save transaction on a DAG file.

    Acquires an exclusive advisory lock (``fcntl.flock``
    ``LOCK_EX``) on a sidecar lock file before loading the DAG;
    holds the lock for the duration of the yielded block; saves
    the DAG and releases the lock on normal exit.

    Usage::

        with dag_transaction(path, project_name) as dag:
            snapshot_if_changed(dag, ontology, label)
        # DAG saved here; lock released.

    Concurrency contract: two processes contending for the same
    DAG file serialize — the second blocks on ``flock`` until the
    first's ``save_dag`` completes and the with-block exits. No
    lost updates, no torn reads.

    On an exception raised inside the yielded block the DAG is
    NOT saved — the in-memory state may be inconsistent — but the
    lock is still released via the ``with open`` exit. Callers
    that want to commit a partial state must save explicitly
    before the exception escapes.

    Concurrency limitations:

    - Lock is advisory (``flock``), not mandatory. A cooperating
      process that doesn't use this wrapper can still trample
      the file. Every builder must use ``dag_transaction`` for
      the guarantee to hold.
    - Lock is per-file-description on Linux; this function
      acquires a fresh descriptor each call, so nested
      ``dag_transaction`` calls in the same process on the same
      path will self-deadlock. Don't nest.
    - Advisory locks don't survive ``os.unlink`` of the lock
      file itself. Don't delete the sidecar.

    The sidecar lock file lives at ``path + ".lock"`` and
    persists across runs deliberately — creating and deleting the
    lock file on every transaction would open a TOCTOU race of
    its own. The file is empty; we never write content to it.
    """
    lock_path = path + ".lock"
    parent_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent_dir, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        dag = load_dag(path, project_name)
        yield dag
        # Only reached on normal exit; exceptions propagate
        # without saving.
        save_dag(dag, path)
