"""
job_store.py — In-memory job tracking for report generation tasks.

Jobs move through: pending -> running -> done | error
"""

import uuid
from typing import Dict, Optional, List
from models import ReportRow


class Job:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.status: str = "pending"   # pending | running | done | error
        self.step: Optional[str] = None
        self.progress: int = 0         # 0-100
        self.rows: Optional[List[ReportRow]] = None
        self.error: Optional[str] = None
        self.window_start: Optional[str] = None   # UTC ISO string of from_date
        self.window_end: Optional[str] = None     # UTC ISO string of to_date (exclusive)

    def update(self, step: str, progress: int) -> None:
        # Never overwrite a terminal state — done/error are final
        if self.status in ("done", "error"):
            return
        self.status = "running"
        self.step = step
        self.progress = progress

    def finish(self, rows: List[ReportRow],
               window_start: Optional[str] = None,
               window_end: Optional[str] = None) -> None:
        self.status = "done"
        self.step = "Complete"
        self.progress = 100
        self.rows = rows
        self.window_start = window_start
        self.window_end = window_end

    def fail(self, error: str) -> None:
        self.status = "error"
        self.error = error


# Module-level store — lives as long as the process runs
_jobs: Dict[str, Job] = {}


def create_job() -> Job:
    """Create a new job and register it. Returns the Job object."""
    job_id = f"rep_{uuid.uuid4().hex[:12]}"
    job = Job(job_id)
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> Optional[Job]:
    """Look up a job by ID. Returns None if not found."""
    return _jobs.get(job_id)
