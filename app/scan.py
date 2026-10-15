"""Walk folders, hash file contents, and group duplicates."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTENSIONS = {
    ".heic",
    ".jpeg",
    ".jpg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
}
VIDEO_EXTENSIONS = {
    ".mov",
    ".m4v",
    ".avi",
    ".mts",
    ".mpg",
    ".mpeg",
    ".mp4",
    ".mkv",
    ".wmv",
    ".3gp",
    ".flv",
    ".webm",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
CACHE_NAME = ".media_organizer_cache.sqlite"
HASH_CHUNK = 8 * 1024 * 1024
SAMPLE_BYTES = 1024 * 1024


@dataclass
class FileInfo:
    path: str
    size: int
    mtime_ns: int
    sha256: str
    extension: str
    kind: str
    content_hashed: bool = True

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "extension": self.extension,
            "kind": self.kind,
        }


def file_kind(extension: str) -> str:
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return "other"


def display_path(path: Path) -> str:
    return os.path.normpath(str(path))


def long_path(path: Path | str) -> str:
    text = os.path.normpath(str(path))
    if os.name != "nt":
        return text
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def worker_count() -> int:
    """Enough threads to keep an SSD busy without thrashing a spinning disk."""
    return max(2, min(8, os.cpu_count() or 4))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_sample(path: Path, size: int) -> str:
    """Hash the ends of a file. Same-size files with different samples are not duplicates."""
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as handle:
        digest.update(handle.read(min(SAMPLE_BYTES, size)))
        if size > SAMPLE_BYTES:
            handle.seek(max(size - SAMPLE_BYTES, SAMPLE_BYTES))
            digest.update(handle.read(SAMPLE_BYTES))
    digest.update(str(size).encode("ascii"))
    return digest.hexdigest()


def iter_files(root: Path, *, media_only: bool = False):
    skip_prefix = CACHE_NAME
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            if name.startswith("."):
                continue
            if name == skip_prefix or name.startswith(skip_prefix + "-"):
                continue
            if media_only and Path(name).suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            yield Path(dirpath) / name


class HashCache:
    """Remember output hashes so unchanged files are not hashed again."""

    def __init__(self, output_dir: Path):
        self.path = output_dir / CACHE_NAME
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_hash (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported (
                sha256 TEXT PRIMARY KEY,
                destination TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def get(self, path: str, size: int, mtime_ns: int) -> str | None:
        row = self.conn.execute(
            "SELECT size, mtime_ns, sha256 FROM file_hash WHERE path = ?",
            (path,),
        ).fetchone()
        if row and row[0] == size and row[1] == mtime_ns:
            return row[2]
        return None

    def put(self, path: str, size: int, mtime_ns: int, sha256: str) -> None:
        self.conn.execute(
            """
            INSERT INTO file_hash (path, size, mtime_ns, sha256)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                sha256 = excluded.sha256
            """,
            (path, size, mtime_ns, sha256),
        )

    def prune(self, keep_paths: set[str]) -> None:
        rows = self.conn.execute("SELECT path FROM file_hash").fetchall()
        stale = [row[0] for row in rows if row[0] not in keep_paths]
        if stale:
            self.conn.executemany("DELETE FROM file_hash WHERE path = ?", [(item,) for item in stale])

    def remember_import(self, sha256: str, destination: str) -> None:
        row = self.conn.execute(
            "SELECT destination FROM imported WHERE sha256 = ?",
            (sha256,),
        ).fetchone()
        if row:
            _remove_future_year_copy(Path(row[0]), Path(destination))
        self.conn.execute(
            """
            INSERT INTO imported (sha256, destination)
            VALUES (?, ?)
            ON CONFLICT(sha256) DO UPDATE SET destination = excluded.destination
            """,
            (sha256, destination),
        )

    def imported_hashes(self) -> set[str]:
        rows = self.conn.execute("SELECT sha256, destination FROM imported").fetchall()
        missing = [row[0] for row in rows if not Path(row[1]).exists()]
        if missing:
            self.conn.executemany("DELETE FROM imported WHERE sha256 = ?", [(item,) for item in missing])
        kept = self.conn.execute("SELECT sha256, destination FROM imported").fetchall()
        return {row[0] for row in kept if not in_future_year_folder(Path(row[1]))}

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _hash_paths(items: list[tuple[Path, int, str]], mode: str, progress, label: str) -> tuple[dict[str, str], list[dict]]:
    results: dict[str, str] = {}
    errors: list[dict] = []
    if not items:
        return results, errors
    lock = threading.Lock()
    done = 0
    total = len(items)
    workers = min(worker_count(), total)

    def task(item: tuple[Path, int, str]) -> None:
        nonlocal done
        path, size, stored = item
        try:
            digest = hash_file(path) if mode == "full" else hash_sample(path, size)
            error = None
        except OSError as exc:
            digest = ""
            error = str(exc)
        with lock:
            done += 1
            current = done
            if error:
                errors.append({"path": stored, "error": error})
            else:
                results[stored] = digest
        if current == total or current % 8 == 0:
            progress(current, total, f"{label} · {workers} threads · {path.name}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(task, items))
    return results, errors


def _stat_files(
    root: Path,
    progress,
    *,
    media_only: bool = False,
    activity: str = "Listing files",
) -> tuple[list[tuple[Path, str, os.stat_result]], list[dict]]:
    paths = list(iter_files(root, media_only=media_only))
    total = len(paths)
    progress(0, total, activity)
    stated: list[tuple[Path, str, os.stat_result]] = []
    errors: list[dict] = []
    for index, path in enumerate(paths, start=1):
        try:
            stat = path.stat()
            if path.is_file():
                stated.append((path, display_path(path), stat))
        except OSError as exc:
            errors.append({"path": display_path(path), "error": str(exc)})
        if index == total or index % 100 == 0:
            progress(index, total, f"{activity} · {index} of {total} · {path.name}")
    return stated, errors


def _info(path: Path, stored: str, stat: os.stat_result, digest: str, hashed: bool) -> FileInfo:
    extension = path.suffix.lower()
    return FileInfo(
        path=stored,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=digest,
        extension=extension,
        kind=file_kind(extension),
        content_hashed=hashed,
    )


def scan_tree(
    root: Path,
    cache: HashCache | None,
    progress,
    *,
    duplicates_only: bool = False,
) -> tuple[list[FileInfo], list[dict]]:
    stated, errors = _stat_files(root, progress, media_only=duplicates_only)
    seen = {stored for _path, stored, _stat in stated}
    if duplicates_only:
        infos = _hash_duplicate_candidates(stated, progress, errors)
    else:
        infos = _hash_all(stated, cache, progress, errors)
    if cache is not None:
        cache.prune(seen)
        cache.commit()
    return infos, errors


def scan_for_duplicates(root: Path, progress) -> tuple[list[FileInfo], list[dict], list[dict]]:
    """Count every file, then hash only media files that might be duplicates."""
    stated, errors = _stat_files(
        root,
        progress,
        activity="Counting every file by type. Names that start with a dot are skipped",
    )
    media_rows = []
    others: list[dict] = []
    for path, stored, stat in stated:
        extension = path.suffix.lower()
        if extension in MEDIA_EXTENSIONS:
            media_rows.append((path, stored, stat))
        else:
            others.append(
                {
                    "path": stored,
                    "size": stat.st_size,
                    "extension": extension or "(no extension)",
                    "kind": "other",
                }
            )
    progress(
        0,
        len(media_rows),
        f"Found {len(stated)} files: {len(media_rows)} media files will be checked for duplicates, {len(others)} other files will be listed and not copied",
    )
    infos = _hash_duplicate_candidates(media_rows, progress, errors)
    return infos, errors, others


def _hash_all(stated, cache: HashCache | None, progress, errors: list[dict]) -> list[FileInfo]:
    infos: list[FileInfo] = []
    pending: list[tuple[Path, int, str]] = []
    ready: list[tuple[Path, str, os.stat_result, str]] = []
    for path, stored, stat in stated:
        digest = cache.get(stored, stat.st_size, stat.st_mtime_ns) if cache is not None else None
        if digest is None:
            pending.append((path, stat.st_size, stored))
        else:
            ready.append((path, stored, stat, digest))
    hashed, hash_errors = _hash_paths(pending, "full", progress, "Hashing")
    errors.extend(hash_errors)
    by_path = {stored: (path, stat) for path, stored, stat in stated}
    for path, stored, stat, digest in ready:
        infos.append(_info(path, stored, stat, digest, True))
    for stored, digest in hashed.items():
        path, stat = by_path[stored]
        if cache is not None:
            cache.put(stored, stat.st_size, stat.st_mtime_ns, digest)
        infos.append(_info(path, stored, stat, digest, True))
    infos.sort(key=lambda item: item.path.lower())
    return infos


def _hash_duplicate_candidates(stated, progress, errors: list[dict]) -> list[FileInfo]:
    """Hash only files that share a size, then only the samples that still match."""
    by_size: dict[int, list[tuple[Path, str, os.stat_result]]] = defaultdict(list)
    for item in stated:
        by_size[item[2].st_size].append(item)
    infos: list[FileInfo] = []
    small: list[tuple[Path, int, str]] = []
    large: list[tuple[Path, int, str]] = []
    for group in by_size.values():
        if len(group) < 2:
            path, stored, stat = group[0]
            infos.append(_info(path, stored, stat, "", False))
            continue
        for path, stored, stat in group:
            item = (path, stat.st_size, stored)
            if stat.st_size <= SAMPLE_BYTES * 2:
                small.append(item)
            else:
                large.append(item)
    full, full_errors = _hash_paths(small, "full", progress, "Reading media files that share a size, to find exact duplicates")
    errors.extend(full_errors)
    samples, sample_errors = _hash_paths(large, "sample", progress, "Comparing the start and end of large media files")
    errors.extend(sample_errors)
    by_path = {stored: (path, stat) for path, stored, stat in stated}
    for stored, digest in full.items():
        path, stat = by_path[stored]
        infos.append(_info(path, stored, stat, digest, True))
    sample_groups: dict[str, list[str]] = defaultdict(list)
    for stored, digest in samples.items():
        sample_groups[digest].append(stored)
    confirm: list[tuple[Path, int, str]] = []
    for grouped in sample_groups.values():
        if len(grouped) < 2:
            path, stat = by_path[grouped[0]]
            infos.append(_info(path, grouped[0], stat, "", False))
            continue
        for stored in grouped:
            path, stat = by_path[stored]
            confirm.append((path, stat.st_size, stored))
    confirmed, confirm_errors = _hash_paths(confirm, "full", progress, "Reading large files whose start and end match, to confirm they are duplicates")
    errors.extend(confirm_errors)
    for stored, digest in confirmed.items():
        path, stat = by_path[stored]
        infos.append(_info(path, stored, stat, digest, True))
    infos.sort(key=lambda item: item.path.lower())
    return infos


def hash_unhashed(files: list[FileInfo], progress, sizes: set[int] | None) -> list[dict]:
    """Fill in hashes that the duplicate scan skipped. `sizes` limits the work."""
    pending: list[tuple[Path, int, str]] = []
    targets: list[FileInfo] = []
    for info in files:
        if info.content_hashed:
            continue
        if sizes is not None and info.size not in sizes:
            continue
        pending.append((Path(info.path), info.size, info.path))
        targets.append(info)
    if not pending:
        return []
    hashed, errors = _hash_paths(pending, "full", progress, "Reading input files to see if the same bytes are already saved")
    for info in targets:
        digest = hashed.get(info.path)
        if digest:
            info.sha256 = digest
            info.content_hashed = True
    return errors


def match_saved(
    continuing: list[FileInfo],
    output: Path,
    cache: HashCache | None,
    progress,
) -> tuple[set[str], int, list[dict]]:
    """Return hashes already stored in the output library."""
    if not output.exists():
        progress(0, 0, "Output folder is empty")
        return set(), 0, []
    stated, errors = _stat_files(
        output,
        progress,
        activity="Listing the output folder to find files that were already saved",
    )
    if cache is not None:
        cache.prune({stored for _path, stored, _stat in stated})
    stated = [item for item in stated if not in_future_year_folder(item[0])]
    output_sizes = {stat.st_size for _path, _stored, stat in stated}
    imported = cache.imported_hashes() if cache is not None else set()
    if imported:
        errors.extend(hash_unhashed(continuing, progress, None))
    else:
        errors.extend(hash_unhashed(continuing, progress, output_sizes))
    interesting = {info.size for info in continuing if info.content_hashed}
    found, hash_errors = _hashes_for_sizes(stated, cache, interesting, progress)
    errors.extend(hash_errors)
    if cache is not None:
        cache.commit()
    return found | imported, len(stated), errors


def _hashes_for_sizes(stated, cache: HashCache | None, sizes: set[int], progress) -> tuple[set[str], list[dict]]:
    if not sizes:
        return set(), []
    pending: list[tuple[Path, int, str]] = []
    found: set[str] = set()
    for path, stored, stat in stated:
        if stat.st_size not in sizes:
            continue
        digest = cache.get(stored, stat.st_size, stat.st_mtime_ns) if cache is not None else None
        if digest:
            found.add(digest)
        else:
            pending.append((path, stat.st_size, stored))
    if pending:
        hashed, errors = _hash_paths(pending, "full", progress, "Reading output files that are the same size as an input file")
    else:
        hashed, errors = {}, []
    by_path = {stored: stat for _path, stored, stat in stated}
    for stored, digest in hashed.items():
        found.add(digest)
        stat = by_path.get(stored)
        if cache is not None and stat is not None:
            cache.put(stored, stat.st_size, stat.st_mtime_ns, digest)
    return found, errors


def in_future_year_folder(path: Path) -> bool:
    """True for output files stored under a year that has not happened yet."""
    year_name = path.parent.parent.name
    month_name = path.parent.name
    if len(year_name) != 4 or not year_name.isdigit():
        return False
    if len(month_name) != 2 or not month_name.isdigit():
        return False
    return int(year_name) > datetime.now().year


def _remove_future_year_copy(old: Path, new: Path) -> None:
    if os.path.normcase(str(old)) == os.path.normcase(str(new)):
        return
    if not old.exists() or not in_future_year_folder(old):
        return
    old.unlink()
    for folder in (old.parent, old.parent.parent):
        try:
            folder.rmdir()
        except OSError:
            break


def duplicate_groups(files: list[FileInfo]) -> list[dict]:
    buckets: dict[str, list[FileInfo]] = defaultdict(list)
    for info in files:
        if info.content_hashed and info.sha256:
            buckets[info.sha256].append(info)
    groups = []
    for digest, items in buckets.items():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda item: item.path.lower())
        groups.append(
            {
                "hash": digest,
                "size": ordered[0].size,
                "count": len(ordered),
                "files": [item.to_dict() for item in ordered],
            }
        )
    groups.sort(key=lambda group: group["files"][0]["path"].lower())
    return groups


def separate_folders(input_dir: Path, output_dir: Path) -> None:
    left = input_dir.resolve()
    right = output_dir.resolve()
    if left == right or _is_inside(left, right) or _is_inside(right, left):
        raise ValueError("Input and output folders must be separate. One cannot be inside the other.")


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return child != parent
