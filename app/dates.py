"""Choose a media date and the year/month destination path."""

from __future__ import annotations

import os
import re
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from app.scan import long_path

EXIF_IFD = 0x8769
TAG_DATETIME = 0x0132
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004
_HEIF_READY = False

_TAGGED_DATE = re.compile(
    rb"(DateTimeOriginal|CreateDate|creation_time|DateTimeDigitized|DateTime)"
    rb"[^0-9]{0,80}(20\d{2}|19\d{2})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
)
_TEXT_DATE = re.compile(
    r"^(?P<y>\d{4})[:\-](?P<m>\d{2})[:\-](?P<d>\d{2})[ T]"
    r"(?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})"
    r"(?:\.\d+)?(?P<tz>|[+-]\d{2}:?\d{2})?$"
)


def register_heif() -> None:
    global _HEIF_READY
    if _HEIF_READY:
        return
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _HEIF_READY = True
    except ImportError:
        _HEIF_READY = False


def parse_datetime(value: str) -> datetime | None:
    text = value.strip().replace("\x00", "")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = _TEXT_DATE.match(text)
    if not match:
        return None
    parsed = _from_parts(
        int(match.group("y")),
        int(match.group("m")),
        int(match.group("d")),
        int(match.group("H")),
        int(match.group("M")),
        int(match.group("S")),
    )
    if parsed is None:
        return None
    zone = match.group("tz") or ""
    if not zone:
        return parsed
    sign = 1 if zone[0] == "+" else -1
    digits = zone[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:4]) if len(digits) >= 4 else 0
    parsed = parsed.replace(tzinfo=timezone(timedelta(hours=sign * hours, minutes=sign * minutes)))
    return _as_local_naive(parsed)


def _as_local_naive(value: datetime) -> datetime | None:
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return _valid(value)


def _valid(value: datetime) -> datetime | None:
    if value.year < 1970 or value.year > 2100:
        return None
    # Google Photos writes 2036-01-01 when it has no real creation time.
    if value > datetime.now() + timedelta(days=2):
        return None
    return value


def _from_parts(year: int, month: int, day: int, hour: int, minute: int, second: int) -> datetime | None:
    try:
        return _valid(datetime(year, month, day, hour, minute, second))
    except ValueError:
        return None


def file_created(path: Path) -> datetime:
    stat = path.stat()
    created = stat.st_ctime if os.name == "nt" else stat.st_mtime
    return datetime.fromtimestamp(created)


def _file_timestamp(path: Path) -> tuple[datetime, str]:
    """Prefer the older of created and modified, so a later copy does not replace the original date."""
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime)
    created = datetime.fromtimestamp(stat.st_ctime) if os.name == "nt" else modified
    if modified < created:
        return modified, "file_modified"
    return created, "file_created"


def _exif_map(path: Path) -> dict[str, datetime]:
    found: dict[str, datetime] = {}
    register_heif()
    try:
        with Image.open(long_path(path)) as image:
            exif = image.getexif()
            ifd = exif.get_ifd(EXIF_IFD) if exif else {}
            mapping = (
                ("datetime_original", TAG_DATETIME_ORIGINAL, ifd),
                ("datetime_digitized", TAG_DATETIME_DIGITIZED, ifd),
                ("datetime", TAG_DATETIME, exif),
            )
            for name, tag, source in mapping:
                raw = source.get(tag) if source else None
                if raw is None and exif:
                    raw = exif.get(tag)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                if isinstance(raw, str):
                    parsed = parse_datetime(raw)
                    if parsed:
                        found[name] = parsed
    except (OSError, ValueError, SyntaxError):
        return found
    return found


def _raw_named_dates(path: Path) -> dict[str, datetime]:
    found: dict[str, datetime] = {}
    try:
        with open(long_path(path), "rb") as handle:
            blob = handle.read(2 * 1024 * 1024)
    except OSError:
        return found
    labels = {
        b"DateTimeOriginal": "datetime_original",
        b"CreateDate": "create_date",
        b"creation_time": "create_date",
        b"DateTimeDigitized": "datetime_digitized",
        b"DateTime": "datetime",
    }
    for match in _TAGGED_DATE.finditer(blob):
        key = labels.get(match.group(1))
        if key is None or key in found:
            continue
        parsed = _from_parts(*(int(part) for part in match.groups()[1:]))
        if parsed:
            found[key] = parsed
    return found


def _probe_dates(path: Path) -> dict[str, datetime]:
    from app.convert import probe

    found: dict[str, datetime] = {}
    payload = probe(path)
    if not payload:
        return found
    tags = []
    format_tags = (payload.get("format") or {}).get("tags") or {}
    tags.append(format_tags)
    for stream in payload.get("streams") or []:
        tags.append(stream.get("tags") or {})
    interesting = {
        "com.apple.quicktime.creationdate": "create_date",
        "creation_time": "create_date",
        "date": "create_date",
        "CreateDate": "create_date",
        "DateTimeOriginal": "datetime_original",
    }
    for bag in tags:
        for key, label in interesting.items():
            raw = bag.get(key)
            if not isinstance(raw, str) or label in found:
                continue
            parsed = parse_datetime(raw)
            if parsed:
                found[label] = parsed
    return found


def chosen_datetime(path: Path, kind: str) -> tuple[datetime, str]:
    """A date stored in the file wins. Otherwise the older of created and modified is used."""
    exif = _exif_map(path) if kind == "image" else {}
    raw = _raw_named_dates(path) if kind == "image" else {}
    have_preferred = (
        "datetime_original" in exif
        or "datetime_original" in raw
        or "create_date" in raw
    )
    probed = {} if kind == "image" and have_preferred else _probe_dates(path)

    order = (
        ("datetime_original", "DateTimeOriginal", exif, raw, probed),
        ("create_date", "CreateDate", raw, probed, exif),
        ("datetime_digitized", "DateTimeDigitized", exif, raw, probed),
        ("datetime", "DateTime", exif, raw, probed),
    )
    for key, label, *sources in order:
        for source in sources:
            if key in source:
                return source[key], label
    return _file_timestamp(path)


def sanitize_stem(stem: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    cleaned = cleaned[:120] if cleaned else "file"
    return cleaned or "file"


def allocate_destination(
    output_dir: Path,
    when: datetime,
    source: Path,
    extension: str,
) -> Path:
    folder = output_dir / f"{when.year:04d}" / f"{when.month:02d}"
    stem = sanitize_stem(source.stem)
    return folder / f"{stem}{extension}"


def build_exif_tiff(when: datetime) -> bytes:
    """Small TIFF EXIF block that stores the chosen date three ways."""
    date = when.strftime("%Y:%m:%d %H:%M:%S\x00").encode("ascii")
    date_offset = 38
    exif_ifd_offset = 58
    ifd0 = struct.pack("<H", 2)
    ifd0 += struct.pack("<HHII", TAG_DATETIME, 2, 20, date_offset)
    ifd0 += struct.pack("<HHII", EXIF_IFD, 4, 1, exif_ifd_offset)
    ifd0 += struct.pack("<I", 0)
    exif_ifd = struct.pack("<H", 2)
    exif_ifd += struct.pack("<HHII", TAG_DATETIME_ORIGINAL, 2, 20, date_offset)
    exif_ifd += struct.pack("<HHII", TAG_DATETIME_DIGITIZED, 2, 20, date_offset)
    exif_ifd += struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", 8) + ifd0 + date + exif_ifd


def creation_time_metadata(when: datetime) -> str:
    """Local wall time, so the container tag matches the year/month folder."""
    return when.strftime("%Y-%m-%dT%H:%M:%S")
