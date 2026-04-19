"""Concurrent-process safety tests for ``dag_transaction``.

These spawn real OS processes via ``multiprocessing.Process``
because ``fcntl.flock`` is per-file-description on Linux — same-
process tests wouldn't exercise the inter-process contention that
the lock is designed to prevent.

The invariant under test: two concurrent writers each appending a
distinct snapshot must produce a final DAG containing BOTH
snapshots (plus whatever seed was there). Without ``flock`` the
second writer's save would clobber the first's in a
load-modify-save race; with ``flock`` the writes serialize and
both land.
"""
from __future__ import annotations

import multiprocessing
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ontology import Entity, Ontology
from ontology import dag as dag_module
from ontology.dag import (
    dag_transaction,
    load_dag,
    save_snapshot,
)


def _append_snapshot_worker(
    dag_path: str, label: str, hold_seconds: float,
) -> None:
    """Entry point for a concurrent worker process. Enters a
    ``dag_transaction``, sleeps to guarantee overlap with a peer
    worker, and appends a single-entity snapshot before exiting.
    """
    with dag_transaction(dag_path, "concurrent") as dag:
        time.sleep(hold_seconds)
        save_snapshot(
            dag,
            Ontology(entities=[Entity(id=label, name=label)]),
            label,
        )


def _raise_inside_transaction_worker(dag_path: str) -> None:
    """Worker that opens a transaction, appends a snapshot (in
    memory only), then raises. The with-exit should release the
    lock without calling save_dag — so the file on disk must NOT
    contain the snapshot after this worker runs."""
    with dag_transaction(dag_path, "concurrent") as dag:
        save_snapshot(
            dag,
            Ontology(entities=[Entity(id="phantom", name="phantom")]),
            "phantom",
        )
        raise RuntimeError("worker-chosen failure")


class TestDagTransactionConcurrency:
    """Cross-process serialization of DAG writes."""

    def test_concurrent_appends_lose_nothing(
        self, tmp_path: Path,
    ) -> None:
        """Two workers appending in parallel must both land."""
        dag_path = str(tmp_path / "concurrent.json")
        # Seed so both workers race on "append to an existing DAG"
        # rather than both bootstrapping a fresh one.
        with dag_transaction(dag_path, "concurrent") as dag:
            save_snapshot(
                dag,
                Ontology(entities=[Entity(id="seed", name="seed")]),
                "seed",
            )

        # 50 ms hold inside each worker guarantees they overlap
        # even on a fast machine — otherwise one process's full
        # transaction could complete before the other's fork returns.
        hold = 0.05
        first = multiprocessing.Process(
            target=_append_snapshot_worker,
            args=(dag_path, "first", hold),
        )
        second = multiprocessing.Process(
            target=_append_snapshot_worker,
            args=(dag_path, "second", hold),
        )
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        assert first.exitcode == 0, (
            f"worker 'first' exited with {first.exitcode}"
        )
        assert second.exitcode == 0, (
            f"worker 'second' exited with {second.exitcode}"
        )

        final = load_dag(dag_path, "concurrent")
        labels = {node.label for node in final.nodes}
        # All three nodes must be present. Without flock, one of
        # "first"/"second" would have been overwritten.
        assert labels == {"seed", "first", "second"}

    def test_transaction_exception_does_not_save(
        self, tmp_path: Path,
    ) -> None:
        """Exception inside the yielded block skips save_dag —
        the on-disk DAG must not reflect the in-memory change."""
        dag_path = str(tmp_path / "rollback.json")
        # Seed under a clean transaction.
        with dag_transaction(dag_path, "concurrent") as dag:
            save_snapshot(
                dag,
                Ontology(entities=[Entity(id="before", name="before")]),
                "before",
            )

        worker = multiprocessing.Process(
            target=_raise_inside_transaction_worker,
            args=(dag_path,),
        )
        worker.start()
        worker.join(timeout=10)
        # Worker should have exited non-zero (the RuntimeError
        # propagates out of the with-block).
        assert worker.exitcode != 0

        # On-disk state must still be just the seed — the
        # phantom snapshot was only in memory.
        final = load_dag(dag_path, "concurrent")
        labels = {node.label for node in final.nodes}
        assert labels == {"before"}
        assert len(final.nodes) == 1

    def test_lock_released_after_failed_worker(
        self, tmp_path: Path,
    ) -> None:
        """A worker that raises must release the lock so a
        subsequent worker can acquire it without hanging."""
        dag_path = str(tmp_path / "release.json")
        # First worker raises inside the transaction.
        worker_a = multiprocessing.Process(
            target=_raise_inside_transaction_worker,
            args=(dag_path,),
        )
        worker_a.start()
        worker_a.join(timeout=10)
        assert worker_a.exitcode != 0

        # Second worker should acquire the lock and complete
        # within a sane timeout. If the first worker had leaked
        # the lock, this second worker would hang and the join
        # timeout below would fire with exitcode None.
        worker_b = multiprocessing.Process(
            target=_append_snapshot_worker,
            args=(dag_path, "after", 0.01),
        )
        worker_b.start()
        worker_b.join(timeout=10)
        assert worker_b.exitcode == 0, (
            f"second worker exited {worker_b.exitcode} — "
            f"lock likely leaked from the failed first worker"
        )

        final = load_dag(dag_path, "concurrent")
        labels = {node.label for node in final.nodes}
        assert labels == {"after"}


@pytest.mark.parametrize("worker_count", [3, 5])
def test_many_concurrent_appends_all_land(
    tmp_path: Path, worker_count: int,
) -> None:
    """Stress-ish coverage: N workers in parallel must produce N
    snapshots in the final DAG. Catches regressions where flock
    succeeds but the DAG load-path reads stale state somewhere."""
    dag_path = str(tmp_path / "many.json")
    workers = [
        multiprocessing.Process(
            target=_append_snapshot_worker,
            args=(dag_path, f"w{i}", 0.02),
        )
        for i in range(worker_count)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    final = load_dag(dag_path, "concurrent")
    labels = {node.label for node in final.nodes}
    expected = {f"w{i}" for i in range(worker_count)}
    assert labels == expected


# ---- Explicit rollback + lock-release guarantees ----
#
# The tests in TestDagTransactionConcurrency assert the outcome
# (final DAG state after a failure); these same-process tests
# assert the mechanisms directly — save_dag is NOT called when
# the yielded block raises, and the same file descriptor is
# available for a subsequent flock immediately after.


class TestExplicitRollbackAndLockRelease:
    """Explicit-mechanism coverage of the dag_transaction
    rollback-and-release contract per the `finally`/`try/except`
    structure in the implementation."""

    def test_yielded_exception_skips_save_dag(
        self, tmp_path: Path,
    ) -> None:
        """An exception inside the yielded block must NOT call
        save_dag. Patches save_dag to count invocations."""
        dag_path = str(tmp_path / "noop-on-raise.json")
        # Seed the file so load_dag doesn't hit the bootstrap path.
        with dag_transaction(dag_path, "test") as dag:
            save_snapshot(
                dag,
                Ontology(entities=[Entity(id="seed", name="seed")]),
                "seed",
            )

        call_count = 0
        real_save_dag = dag_module.save_dag

        def counting_save(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            real_save_dag(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(dag_module, "save_dag", counting_save):
            with pytest.raises(RuntimeError, match="deliberate"):
                with dag_transaction(dag_path, "test") as dag:
                    save_snapshot(
                        dag,
                        Ontology(entities=[
                            Entity(id="phantom", name="phantom"),
                        ]),
                        "phantom",
                    )
                    raise RuntimeError("deliberate test failure")

        # save_dag must NOT have been called inside the failed
        # transaction. The seed transaction's save is outside the
        # patch context.
        assert call_count == 0, (
            "save_dag was called despite exception — rollback "
            "contract violated"
        )

    def test_noop_transaction_skips_save_dag(
        self, tmp_path: Path,
    ) -> None:
        """A transaction that doesn't modify the DAG must skip
        save_dag. Eliminates redundant writes + mtime churn on
        regeneration runs that find nothing changed."""
        dag_path = str(tmp_path / "noop.json")
        # Seed.
        with dag_transaction(dag_path, "test") as dag:
            save_snapshot(
                dag,
                Ontology(entities=[Entity(id="seed", name="seed")]),
                "seed",
            )

        call_count = 0
        real_save_dag = dag_module.save_dag

        def counting_save(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            real_save_dag(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(dag_module, "save_dag", counting_save):
            with dag_transaction(dag_path, "test"):
                # Do nothing — no modification.
                pass

        assert call_count == 0, (
            "save_dag was called on a no-op transaction — save-"
            "elision contract violated"
        )

    def test_modifying_transaction_calls_save_dag(
        self, tmp_path: Path,
    ) -> None:
        """Positive case of the save-elision contract: a
        transaction that DOES modify the DAG must call save_dag."""
        dag_path = str(tmp_path / "modifies.json")

        call_count = 0
        real_save_dag = dag_module.save_dag

        def counting_save(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            real_save_dag(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(dag_module, "save_dag", counting_save):
            with dag_transaction(dag_path, "test") as dag:
                save_snapshot(
                    dag,
                    Ontology(entities=[
                        Entity(id="modified", name="modified"),
                    ]),
                    "modified",
                )
        assert call_count == 1, (
            "save_dag wasn't called on a modifying transaction — "
            "save-elision over-eager"
        )

    def test_exception_releases_lock_same_process(
        self, tmp_path: Path,
    ) -> None:
        """After an exception, the next transaction in the SAME
        process must be able to acquire the lock immediately. If
        the flock UN leaked we'd see a hang or OSError; if the
        DAG state is corrupted we'd see a load failure.

        Same-process coverage complements the multiprocessing
        `test_lock_released_after_failed_worker` test above —
        this one exercises the finally-clause release path in the
        current interpreter rather than relying on fd-close-on-
        process-exit to do the work."""
        dag_path = str(tmp_path / "release-same-proc.json")
        with pytest.raises(RuntimeError, match="planned"):
            with dag_transaction(dag_path, "test") as dag:
                save_snapshot(
                    dag,
                    Ontology(entities=[
                        Entity(id="x", name="x"),
                    ]),
                    "x",
                )
                raise RuntimeError("planned failure")

        # Must not hang; must succeed.
        with dag_transaction(dag_path, "test") as dag:
            save_snapshot(
                dag,
                Ontology(entities=[
                    Entity(id="after", name="after"),
                ]),
                "after",
            )

        final = load_dag(dag_path, "test")
        labels = {n.label for n in final.nodes}
        # "x" was rolled back; only "after" survives.
        assert labels == {"after"}
