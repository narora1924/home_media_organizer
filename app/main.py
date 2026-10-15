"""Local web UI for the home media organizer."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.convert import tool_status
from app.pipeline import (
    job_snapshot,
    resolve_duplicates,
    start_compare,
    start_plan,
    start_run,
    start_scan,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

app = FastAPI(title="Home media organizer")


class BrowseRequest(BaseModel):
    kind: str = "input"
    initial: str = ""


class ScanRequest(BaseModel):
    input_dir: str


class Decision(BaseModel):
    path: str
    action: str


class ResolveRequest(BaseModel):
    decisions: list[Decision] = Field(default_factory=list)
    confirm_delete: bool = False


class CompareRequest(BaseModel):
    output_dir: str


def _value_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/api/tools")
def tools() -> dict:
    return tool_status()


@app.post("/api/browse")
def browse(body: BrowseRequest) -> dict:
    if body.kind not in {"input", "output"}:
        raise HTTPException(status_code=400, detail="Browse kind must be input or output.")
    try:
        selected = _choose_directory(body.initial.strip() or None)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not open the folder dialog: {exc}") from exc
    return {"path": selected}


@app.post("/api/scan")
def scan(body: ScanRequest) -> dict:
    try:
        return start_scan(body.input_dir)
    except ValueError as exc:
        raise _value_error(exc) from exc


@app.post("/api/duplicates/resolve")
def resolve(body: ResolveRequest) -> dict:
    try:
        return resolve_duplicates([item.model_dump() for item in body.decisions], body.confirm_delete)
    except ValueError as exc:
        raise _value_error(exc) from exc


@app.post("/api/compare")
def compare(body: CompareRequest) -> dict:
    try:
        return start_compare(body.output_dir)
    except ValueError as exc:
        raise _value_error(exc) from exc


@app.post("/api/plan")
def plan() -> dict:
    try:
        return start_plan()
    except ValueError as exc:
        raise _value_error(exc) from exc


@app.post("/api/run")
def run() -> dict:
    try:
        return start_run()
    except ValueError as exc:
        raise _value_error(exc) from exc


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    try:
        return job_snapshot(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="That task was not found.") from None


def _choose_directory(initial: str | None) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    try:
        selected = filedialog.askdirectory(initialdir=initial or None, mustexist=True)
    finally:
        root.destroy()
    return selected or ""


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
