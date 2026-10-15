"""Wizard state: duplicates, output compare, copy plan, and the confirmed run."""

from __future__ import annotations

import os
import threading
import traceback
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from app.convert import (
    action_label,
    classify_action,
    clear_probe_cache,
    conversion_workers,
    needs_ffmpeg,
    tool_status,
    write_output,
)
from app.dates import allocate_destination, chosen_datetime
from app.scan import (
    FileInfo,
    HashCache,
    display_path,
    duplicate_groups,
    file_kind,
    hash_file,
    long_path,
    match_saved,
    scan_for_duplicates,
    separate_folders,
)

_LOCK = threading.RLock()
_JOBS: dict[str, "Job"] = {}
_SESSION = None


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.state = "running"
        self.done = 0
        self.total = 0
        self.message = "Starting"
        self.result = None
        self.error = ""

    def progress(self, done: int, total: int, message: str) -> None:
        self.done = done
        self.total = total
        self.message = message

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "message": self.message,
            "result": self.result,
            "error": self.error,
        }


class Session:
    def __init__(self):
        self.input_dir: Path | None = None
        self.output_dir: Path | None = None
        self.files: list[FileInfo] = []
        self.groups: list[dict] = []
        self.resolved = False
        self.resolve_result: dict | None = None
        self.continuing: list[FileInfo] = []
        self.compared = False
        self.remaining: list[FileInfo] = []
        self.planned = False
        self.plan_items: list[dict] = []
        self.skipped: list[dict] = []
        self.ran = False

    def reset(self) -> None:
        self.__init__()


def session() -> Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = Session()
    return _SESSION


def _require_dir(raw: str, label: str) -> Path:
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"Choose an {label} folder.")
    path = Path(text).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{label.capitalize()} folder does not exist: {path}")
    return path


def _prepare_output(raw: str) -> Path:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Choose an output folder.")
    path = Path(text).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f"Output path is not a folder: {path}")
    if not path.exists() and not path.parent.is_dir():
        raise ValueError(f"Output folder does not exist, and its parent is missing: {path}")
    return path


def _running_job() -> Job | None:
    for job in _JOBS.values():
        if job.state == "running":
            return job
    return None


def _start(kind: str, work) -> dict:
    with _LOCK:
        if _running_job() is not None:
            raise ValueError("Another task is still running.")
        job = Job(kind)
        _JOBS[job.id] = job

    def runner() -> None:
        try:
            job.result = work(job)
            job.state = "done"
            job.message = "Finished"
        except Exception as exc:
            job.state = "error"
            job.error = str(exc)
            job.message = "Failed"
            traceback.print_exc()

    threading.Thread(target=runner, name=f"media-{kind}", daemon=True).start()
    return {"job_id": job.id}


def job_snapshot(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if job is None:
        raise KeyError(job_id)
    return job.to_dict()


def type_counts(rows: list[tuple[str, str]]) -> list[dict]:
    counts = Counter(rows)
    summary = [
        {"extension": extension or "(no extension)", "kind": kind, "count": count}
        for (extension, kind), count in counts.items()
    ]
    summary.sort(key=lambda item: (-item["count"], item["extension"]))
    return summary


def start_scan(input_dir: str) -> dict:
    root = _require_dir(input_dir, "input")

    def work(job: Job) -> dict:
        clear_probe_cache()
        infos, errors, others = scan_for_duplicates(root, job.progress)
        groups = duplicate_groups(infos)
        extra = sum(group["count"] - 1 for group in groups)
        counted = [(info.extension, info.kind) for info in infos]
        counted.extend((item["extension"], item["kind"]) for item in others)
        with _LOCK:
            current = session()
            current.reset()
            current.input_dir = root
            current.files = infos
            current.groups = groups
        return {
            "input_dir": display_path(root),
            "file_count": len(infos) + len(others),
            "media_count": len(infos),
            "other_count": len(others),
            "type_counts": type_counts(counted),
            "other_files": others,
            "duplicate_group_count": len(groups),
            "extra_copy_count": extra,
            "groups": groups,
            "errors": errors,
        }

    return _start("scan", work)


def resolve_duplicates(decisions: list[dict], confirm_delete: bool) -> dict:
    with _LOCK:
        current = session()
        if current.input_dir is None:
            raise ValueError("Scan the input folder first.")
        if current.resolved and current.resolve_result is not None:
            return current.resolve_result
        groups = current.groups
        files = list(current.files)

    decision_map: dict[str, str] = {}
    for item in decisions:
        path = str(item.get("path") or "")
        action = str(item.get("action") or "")
        if path:
            decision_map[path] = action

    delete_paths: list[str] = []
    ignore_paths: list[str] = []
    keep_paths: list[str] = []
    grouped: set[str] = set()
    for group in groups:
        paths = [item["path"] for item in group["files"]]
        grouped.update(paths)
        keeps = [path for path in paths if decision_map.get(path) == "keep"]
        if len(keeps) != 1:
            raise ValueError("Each duplicate group needs exactly one file to keep.")
        keeper = keeps[0]
        for path in paths:
            action = decision_map.get(path)
            if action not in {"keep", "ignore", "delete"}:
                raise ValueError(f"Choose keep, ignore, or delete for {path}")
            if path == keeper:
                keep_paths.append(path)
                continue
            if action == "keep":
                raise ValueError(f"More than one keep was selected for {path}")
            if action == "ignore":
                ignore_paths.append(path)
            else:
                delete_paths.append(path)

    unknown = sorted(set(decision_map) - grouped)
    if unknown:
        raise ValueError("A choice was sent for a file that is not in a duplicate group.")
    if groups and len(decision_map) != len(grouped):
        raise ValueError("Every duplicate file needs a choice.")

    if delete_paths and not confirm_delete:
        return {
            "needs_confirm": True,
            "delete_paths": delete_paths,
            "delete_count": len(delete_paths),
        }

    deleted: list[str] = []
    delete_errors: list[dict] = []
    for path in delete_paths:
        try:
            os.remove(long_path(path))
            deleted.append(path)
        except OSError as exc:
            delete_errors.append({"path": path, "error": str(exc)})

    excluded = set(ignore_paths) | set(delete_paths)
    continuing = [info for info in files if info.path not in excluded]
    result = {
        "needs_confirm": False,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "delete_errors": delete_errors,
        "ignored": ignore_paths,
        "ignored_count": len(ignore_paths),
        "kept_count": len(keep_paths),
        "continuing_count": len(continuing),
    }
    with _LOCK:
        current = session()
        current.resolved = True
        current.resolve_result = result
        current.continuing = continuing
        current.compared = False
        current.planned = False
        current.ran = False
    return result


def start_compare(output_dir: str) -> dict:
    output = _prepare_output(output_dir)
    with _LOCK:
        current = session()
        if not current.resolved or current.input_dir is None:
            raise ValueError("Finish the duplicate review first.")
        separate_folders(current.input_dir, output)
        continuing = list(current.continuing)
        current.output_dir = output

    def work(job: Job) -> dict:
        cache = None
        errors: list[dict] = []
        known: set[str] = set()
        output_file_count = 0
        try:
            if output.exists():
                try:
                    cache = HashCache(output)
                except OSError:
                    cache = None
            known, output_file_count, errors = match_saved(continuing, output, cache, job.progress)
        finally:
            if cache is not None:
                cache.close()
        ignored = [info for info in continuing if info.content_hashed and info.sha256 in known]
        ignored_paths = {info.path for info in ignored}
        remaining = [info for info in continuing if info.path not in ignored_paths]
        with _LOCK:
            current = session()
            current.output_dir = output
            current.compared = True
            current.remaining = remaining
            current.planned = False
            current.plan_items = []
            current.skipped = []
        return {
            "output_dir": display_path(output),
            "file_count": len(continuing),
            "type_counts": type_counts([(info.extension, info.kind) for info in continuing]),
            "ignored_count": len(ignored),
            "ignored_type_counts": type_counts([(info.extension, info.kind) for info in ignored]),
            "ignored": [info.to_dict() for info in ignored],
            "remaining_count": len(remaining),
            "remaining_type_counts": type_counts([(info.extension, info.kind) for info in remaining]),
            "output_file_count": output_file_count,
            "errors": errors,
        }

    return _start("compare", work)


def start_plan() -> dict:
    with _LOCK:
        current = session()
        if not current.compared or current.output_dir is None:
            raise ValueError("Compare with the output folder first.")
        remaining = list(current.remaining)
        output = current.output_dir

    def work(job: Job) -> dict:
        items: list[dict] = []
        skipped: list[dict] = []
        total = len(remaining)
        job.progress(0, total, "Reading dates inside media files and planning year and month folders. Nothing is copied yet.")
        for index, info in enumerate(remaining, start=1):
            source = Path(info.path)
            job.progress(
                index,
                total,
                f"Reading the date in {source.name} and choosing its year and month folder. Nothing is copied yet.",
            )
            action = classify_action(source)
            if action in {"skip", "unreadable"}:
                if action == "unreadable":
                    if file_kind(info.extension or "") == "video":
                        reason = "Named as a video, but the file contains no video data."
                    else:
                        reason = "Named as a picture, but the file contains no image data."
                else:
                    suffix = info.extension or "no extension"
                    reason = f"Unsupported type {suffix}"
                skipped.append({"path": info.path, "reason": reason})
                continue
            kind = "image" if action in {"copy_jpeg", "convert_image"} else "video"
            when, date_source = chosen_datetime(source, kind)
            extension = ".jpg" if kind == "image" else ".mp4"
            destination = allocate_destination(output, when, source, extension)
            items.append(
                {
                    "source": info.path,
                    "destination": display_path(destination),
                    "action": action,
                    "action_label": action_label(action),
                    "date": when.isoformat(timespec="seconds"),
                    "date_source": date_source,
                    "size": info.size,
                    "sha256": info.sha256,
                    "extension": info.extension or "(no extension)",
                }
            )
        tools = tool_status()
        result = {
            "count": len(items),
            "file_count": len(items),
            "type_counts": type_counts(
                [(item["extension"], file_kind("" if item["extension"] == "(no extension)" else item["extension"])) for item in items]
            ),
            "action_counts": _action_counts(items),
            "items": items,
            "skipped_count": len(skipped),
            "skipped": skipped,
            "needs_ffmpeg": any(needs_ffmpeg(item["action"]) for item in items),
            **tools,
        }
        with _LOCK:
            current = session()
            current.planned = True
            current.plan_items = items
            current.skipped = skipped
            current.ran = False
        return result

    return _start("plan", work)


def start_run() -> dict:
    tools = tool_status()
    with _LOCK:
        current = session()
        if not current.planned or current.output_dir is None:
            raise ValueError("Review the copy plan first.")
        if current.ran:
            raise ValueError("This plan already ran. Scan again to start over.")
        items = list(current.plan_items)
        skipped = list(current.skipped)
        output = current.output_dir
        if any(needs_ffmpeg(item["action"]) for item in items) and not tools["ffmpeg_found"]:
            raise ValueError("ffmpeg was not found on PATH. Install it, then start the copy again.")

    def work(job: Job) -> dict:
        output.mkdir(exist_ok=True)
        written = []
        failed = []
        warnings = []
        total = len(items)
        cache = None
        try:
            try:
                cache = HashCache(output)
            except OSError:
                cache = None
            workers = min(conversion_workers(), total or 1)

            def convert_item(item: dict) -> dict:
                source = Path(item["source"])
                dest = Path(item["destination"])
                try:
                    when = datetime.fromisoformat(item["date"])
                    warning = write_output(source, dest, item["action"], when, item["date_source"])
                    stat = dest.stat()
                    digest = hash_file(dest)
                except Exception as exc:
                    return {
                        "ok": False,
                        "source": item["source"],
                        "error": str(exc),
                        "name": source.name,
                        "action_label": item["action_label"],
                    }
                return {
                    "ok": True,
                    "source": item["source"],
                    "destination": display_path(dest),
                    "action": item["action"],
                    "action_label": item["action_label"],
                    "date_source": item["date_source"],
                    "warning": warning,
                    "sha256": item["sha256"],
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "digest": digest,
                    "name": source.name,
                }

            groups: dict[str, list[dict]] = {}
            for item in items:
                groups.setdefault(os.path.normcase(item["destination"]), []).append(item)
            ordered_groups = []
            for group in groups.values():
                group.sort(key=lambda item: 0 if item["action"] in {"copy_jpeg", "copy_mp4"} else 1)
                ordered_groups.append(group)

            def convert_group(group: list[dict]) -> list[dict]:
                return [convert_item(item) for item in group]

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(convert_group, group) for group in ordered_groups]
                done = 0
                for future in as_completed(futures):
                    for result in future.result():
                        done += 1
                        if result["ok"]:
                            written.append(
                                {
                                    "source": result["source"],
                                    "destination": result["destination"],
                                    "action": result["action"],
                                    "action_label": result["action_label"],
                                    "date_source": result["date_source"],
                                }
                            )
                            if result["warning"]:
                                warnings.append({"path": result["source"], "warning": result["warning"]})
                            if cache is not None:
                                cache.put(result["destination"], result["size"], result["mtime_ns"], result["digest"])
                                cache.remember_import(result["sha256"], result["destination"])
                                if done % 50 == 0:
                                    cache.commit()
                        else:
                            failed.append({"path": result["source"], "error": result["error"]})
                        label = result.get("action_label") or "Working on a file"
                        name = result.get("name") or Path(result["source"]).name
                        job.progress(done, total, f"{workers} files at a time · {label} · {name}")
            if cache is not None:
                cache.commit()
        finally:
            if cache is not None:
                cache.close()
        with _LOCK:
            session().ran = True
        return {
            "written_count": len(written),
            "file_count": len(written),
            "type_counts": type_counts([(Path(item["destination"]).suffix.lower(), item["action"]) for item in written]),
            "action_counts": _action_counts(written),
            "failed_count": len(failed),
            "warning_count": len(warnings),
            "skipped_count": len(skipped),
            "written": written,
            "failed": failed,
            "warnings": warnings,
            "skipped": skipped,
            "output_dir": display_path(output),
        }

    return _start("run", work)


def _action_counts(items: list[dict]) -> list[dict]:
    counts = Counter(item.get("action_label") or item.get("action") or "Unknown" for item in items)
    rows = [{"action": action, "count": count} for action, count in counts.items()]
    rows.sort(key=lambda item: (-item["count"], item["action"]))
    return rows
