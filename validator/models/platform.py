from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel


class Status(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class JobRun(BaseModel):
    id: int
    job_id: int
    validator_id: int
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

class MockJobRun(BaseModel):
    id: int
    job_id: int
    validator_id: int

class AgentCode(BaseModel):
    code: str

class AgentExecution(BaseModel):
    validator_id: int
    job_run_id: int
    project: str
    success: bool
    report: dict | None = None
    stdout: str | None = None
    stderr: str | None = None
    error: str | None = None
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

class AgentEvaluation(BaseModel):
    id: int | None = None
    agent_execution_id: int
    status: str
    project: str
    timestamp: datetime
    total_expected: int
    total_found: int
    true_positives: int
    false_negatives: int
    false_positives: int
    detection_rate: float
    precision: float
    f1_score: float

    matched_findings: list | None = None
    missed_findings: list | None = None
    extra_findings: list | None = None
    undecided_findings: list | None = None

class UserRole(StrEnum):
    MINER = "miner"
    VALIDATOR = "validator"

class User(BaseModel):
    id: int
    email: str
    name: str | None = None
    role: UserRole
    is_active: bool
    is_active_validator: bool
    hotkey: str | None = None
