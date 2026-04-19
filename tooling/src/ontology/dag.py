"""DAG persistence and snapshot utilities for the ontology.

Forked from python_agent.dag_utils on 2026-04-19. This fork
intentionally drops the HMAC integrity signing and the LLM
prompt-injection scan: our ontology is produced from a trusted
in-repo builder (tooling/build_qemu_harness_ontology.py and
future peers), not from external input, so the security machinery
python_agent needs for agent-mediated loads is dead weight here.

If fireasmserver ever starts loading ontologies from a less-trusted
source, revisit this decision and port the integrity checks back.
"""

from __future__ import annotations

import os
import tempfile
import uuid
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

    Uses atomic rename (temp file + os.rename) so an interrupted
    write cannot corrupt the existing artifact.
    """
    parent_dir = os.path.dirname(os.path.abspath(path))
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=parent_dir,
        suffix=".tmp", delete=False,
    )
    try:
        handle.write(dag.to_json())
        handle.close()
        os.rename(handle.name, path)
    except BaseException:
        handle.close()
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
