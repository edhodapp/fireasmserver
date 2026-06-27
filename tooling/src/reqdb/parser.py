"""Parse the canonical per-requirement text into the reqdb model.

Canonical source of truth is the git-tracked text: ``authorities.yaml``
(a list of authority records) plus one YAML file per requirement under
``requirements/``. Each file maps field-for-field onto the Pydantic
records in :mod:`reqdb.model`, so parsing is load-then-validate; Pydantic
carries the type/enum contract.

Ordering is made deterministic here — authorities by ``authority_id``,
requirements by ``req_id`` — independent of filesystem glob order, so a
build is reproducible and the SQLite round-trip (which reads back in the
same key order) is an identity.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from reqdb.model import Authority, ReqDB, Requirement


def load_reqdb(src_dir: Path) -> ReqDB:
    """Load the authorities lookup plus every per-requirement file under
    ``src_dir`` into a :class:`ReqDB`."""
    authorities = _load_authorities(src_dir / "authorities.yaml")
    requirements = _load_requirements(src_dir / "requirements")
    return ReqDB(authorities=authorities, requirements=requirements)


def _load_authorities(path: Path) -> list[Authority]:
    """Parse the authorities lookup file, sorted by ``authority_id``."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    authorities = [Authority.model_validate(item) for item in raw]
    return sorted(authorities, key=lambda a: a.authority_id)


def _load_requirements(req_dir: Path) -> list[Requirement]:
    """Parse every ``*.yaml`` requirement file, sorted by ``req_id``."""
    requirements = [
        Requirement.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8")),
        )
        for path in sorted(req_dir.glob("*.yaml"))
    ]
    return sorted(requirements, key=lambda r: r.req_id)
