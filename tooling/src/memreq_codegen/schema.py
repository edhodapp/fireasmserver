"""Pydantic models for `regions.yaml`.

The YAML structure is intentionally narrow for D066 step 5a/5b/5c —
literal-only size and align integers, no expressions. Future
non-literal extensions (CPU / TUNING references per task #28) will
grow the `size` / `align` fields to a discriminated union; until
then, integers keep validation simple.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# Constrain identifier shape so a typo doesn't silently produce a
# label that collides with another symbol or that the assembler
# can't parse. Lower-case with digits and underscores; must start
# with a letter to avoid leading-digit identifiers.
_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class RegionDecl(BaseModel):
    """One region declaration in `regions.yaml`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=_NAME_PATTERN)
    tier: Literal["hot", "cold", "init"]
    lifetime: Literal[
        "steady_state",
        "init_only",
        "immutable_after_init",
        "stack",
    ]
    owner: int = Field(ge=0, le=0xFFFF)
    writable: bool
    # Step 5a/5b/5c: literal-only LIT-encodable u32 size/align.
    # Non-literal extensions deferred per task #28.
    size: int = Field(ge=1, le=0xFFFFFFFF)
    align: int = Field(ge=1, le=0xFFFFFFFF)
    doc: str = ""


class RegionFile(BaseModel):
    """Top-level shape of `regions.yaml`."""

    model_config = ConfigDict(extra="forbid")

    regions: list[RegionDecl]
