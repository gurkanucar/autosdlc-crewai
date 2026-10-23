"""FastAPI interface for creating and inspecting runs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .intake import questions, validate_decisions
from .runner import run_project
from .storage import History


WORKSPACE = Path.cwd()
app = FastAPI(title="AutoSDLC Management API", version="0.1.0")


class RunRequest(BaseModel):
    idea: str = Field(min_length=1, max_length=1000)
    model: str | None = None
    decisions: dict[str, str] | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/questions")
def architecture_questions() -> dict:
    return questions()


@app.post("/runs")
async def create_run(request: RunRequest) -> dict:
    try:
        decisions = validate_decisions(request.decisions)
        result = await asyncio.to_thread(
            run_project,
            request.idea,
            workspace=WORKSPACE,
            model=request.model,
            decisions=decisions,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": result.id,
        "status": result.status,
        "attempts": result.attempts,
        "output_dir": str(result.output_dir),
        "project_dir": str(result.project_dir),
        "error": result.error,
    }


@app.get("/runs")
def list_runs() -> list[dict]:
    return History(WORKSPACE / "runs" / "history.sqlite3").list()


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    result = History(WORKSPACE / "runs" / "history.sqlite3").get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return result

