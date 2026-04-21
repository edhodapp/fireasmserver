"""Write a dispatched ``SideSessionTask`` to the project's DAG.

Single function: ``write_dispatch_node(repo_root, task)`` opens
the DAG under the existing ``dag_transaction`` lock, appends the
task to the ontology's ``side_session_tasks`` list, and snapshots
a new node. Re-validation on construction enforces every existing
ontology rule — including the new
``_check_side_session_task_uniqueness`` from the 2026-04-20 third
hygiene pass, so a duplicate (slug, date) raises
``ValidationError`` and ``dag_transaction`` rolls back without
touching disk.

The caller is the ``Bootstrapper.run()`` orchestration (lands in
C5). Tests exercise this module directly against a tmp DAG file.
"""

from __future__ import annotations

from pathlib import Path

from ontology import Ontology, SideSessionTask
from ontology.dag import dag_transaction, save_snapshot

_PROJECT_NAME = "fireasmserver"
_DAG_RELATIVE_PATH = Path("tooling") / "qemu-harness.json"


class OntologyWriteError(Exception):
    """Raised when the DAG mutation cannot proceed. Wraps the
    underlying Pydantic / IO failure so callers can map to a
    single ``BootstrapError`` without importing pydantic."""


def write_dispatch_node(repo_root: Path, task: SideSessionTask) -> str:
    """Add ``task`` to the DAG at ``repo_root/tooling/qemu-harness.json``.

    Returns the new node's id (uuid string).

    The transaction holds an advisory ``flock`` on the DAG file
    for the duration of the load → mutate → save cycle, so
    concurrent writers serialize cleanly. If the constructed
    ``Ontology`` fails validation (most commonly: duplicate
    (slug, date) collision against an already-dispatched task),
    the exception bubbles, ``dag_transaction`` skips the save,
    and the on-disk DAG is unchanged.
    """
    dag_path = str(repo_root / _DAG_RELATIVE_PATH)
    new_node_id_holder: list[str] = []
    try:
        with dag_transaction(dag_path, _PROJECT_NAME) as dag:
            current = dag.get_current_node()
            base_ontology = (
                current.ontology if current is not None else Ontology()
            )
            new_ontology = _ontology_with_appended_task(
                base_ontology, task,
            )
            label = f"dispatch:{task.slug}@{task.date}"
            new_node_id_holder.append(
                save_snapshot(dag, new_ontology, label=label),
            )
    except Exception as exc:
        raise OntologyWriteError(
            f"failed to write SideSessionTask "
            f"{(task.slug, task.date)!r} to DAG at {dag_path}: {exc}"
        ) from exc
    return new_node_id_holder[0]


def _ontology_with_appended_task(
    base: Ontology, task: SideSessionTask,
) -> Ontology:
    """Construct a new Ontology containing ``base``'s contents
    plus ``task`` appended to ``side_session_tasks``. Constructing
    via ``Ontology(...)`` re-runs every model_validator — so the
    uniqueness, RI, and cross-field checks all fire on the new
    composite, not on a pre-validated dict that bypasses them."""
    return Ontology(
        entities=list(base.entities),
        relationships=list(base.relationships),
        domain_constraints=list(base.domain_constraints),
        performance_constraints=list(base.performance_constraints),
        modules=list(base.modules),
        data_models=list(base.data_models),
        external_dependencies=list(base.external_dependencies),
        open_questions=list(base.open_questions),
        side_session_tasks=[*base.side_session_tasks, task],
    )
