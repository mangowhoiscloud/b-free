"""Request models shared by REST and MCP. IDs and choices only; never secrets or approvers."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class EnvRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    role: Literal["source", "target"]
    engine: Literal["postgres"] = "postgres"
    secret_ref: str = Field(min_length=10, max_length=256, description="Secret store reference: tenants/<tenant_id>/...")


class PlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workload: str = Field(pattern=NAME)
    source_environment_id: UUID
    target_environment_id: UUID


class ApprovalDecision(BaseModel):
    # extra="ignore": an "approver" (or anything else) in the body is ignored; the approver
    # is always the console session principal (README A3).
    model_config = ConfigDict(extra="ignore")
    stage: Literal["change", "cutover", "failback"] = "cutover"
    approval_request_id: UUID
    revision: int = Field(ge=1)
    gate_verdict_id: UUID | None = None
    change_id: UUID | None = Field(default=None, description="Required for stage change: the prepared change shown")


class ShiftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["cutover", "failback"]
    write_pause_budget_ms: int = Field(default=5000, ge=100, le=60000)


TASK = r"^[a-z0-9][a-z0-9-]{0,62}$"


class RehearsalStart(BaseModel):
    """Start one rehearsal attempt (§8.9 ``POST /v1/plans/{id}/rehearsals``): task ID only."""

    model_config = ConfigDict(extra="forbid")
    task: str = Field(pattern=TASK, description="Rehearsal task template ID, e.g. fix-bucket-policy")
    role: Literal["governance", "app_migration", "ci"] = Field(
        description="Agent role for this attempt; must be one the PROPOSE stage may invoke and match the task."
    )
