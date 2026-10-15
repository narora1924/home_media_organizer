"""Convert media with ffmpeg, with Pillow as the HEIC fallback."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image

from app.dates import (
    build_exif_tiff,
    creation_time_metadata,
    register_heif,
)
from app.scan import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, long_path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    _LPFILETIME = ctypes.POINTER(_FILETIME)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.SetFileTime.argtypes = [wintypes.HANDLE, _LPFILETIME, _LPFILETIME, _LPFILETIME]
    _kernel32.SetFileTime.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

JPEG_EXTENSIONS = {".jpg", ".jpeg"}
H264_CODECS = {"h264"}
AAC_CODECS = {"aac"}
DECODABLE_AUDIO = {
    "aac",
    "mp3",
    "ac3",
    "eac3",
    "mp2",
    "flac",
    "vorbis",
    "opus",
    "alac",
    "pcm_s16le",
    "pcm_s16be",
    "pcm_s24le",
    "pcm_s32le",
    "wmav1",
    "wmav2",
    "amr_nb",
    "amr_wb",
}


def conversion_workers() -> int:
    """How many files to convert at once. Each video encode also uses several CPU cores."""
    cpu = os.cpu_count() or 4
    return max(2, min(4, max(2, cpu // 2)))


def encoder_threads() -> str:
    cpu = os.cpu_count() or 4
    return str(max(1, cpu // conversion_workers()))


def tool_status() -> dict:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    return {
        "ffmpeg": ffmpeg or "",
        "ffprobe": ffprobe or "",
        "ffmpeg_found": bool(ffmpeg),
        "ffprobe_found": bool(ffprobe),
    }


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
        check=False,
    )


_PROBE_CACHE: dict[str, dict | None] = {}
_PROBE_LOCK = threading.Lock()


def clear_probe_cache() -> None:
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()


def probe(path: Path) -> dict | None:
    from app.scan import display_path

    key = display_path(path)
    with _PROBE_LOCK:
        if key in _PROBE_CACHE:
            return _PROBE_CACHE[key]
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = None
        return None
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            _ffmpeg_path(path),
        ]
    )
    payload = None
    if result.returncode == 0 and result.stdout.strip():
        import json

        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = payload
    return payload


def is_jpeg(path: Path) -> bool:
    try:
        with open(long_path(path), "rb") as handle:
            return handle.read(3) == b"\xff\xd8\xff"
    except OSError:
        return False


def looks_like_image(path: Path) -> bool:
    """True when the file starts with a picture format this app can convert."""
    try:
        with open(long_path(path), "rb") as handle:
            header = handle.read(16)
    except OSError:
        return False
    if header.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*")):
        return True
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return True
    return len(header) >= 8 and header[4:8] == b"ftyp"


def stream_codecs(path: Path) -> tuple[str | None, list[tuple[int, str]], list[str]]:
    """Return the video codec, decodable audio tracks, and audio codecs ffmpeg cannot read."""
    payload = probe(path) or {}
    video = None
    usable: list[tuple[int, str]] = []
    skipped: list[str] = []
    audio_ordinal = 0
    for stream in payload.get("streams") or []:
        codec = str(stream.get("codec_name") or "").lower()
        kind = stream.get("codec_type")
        if kind == "video" and video is None and codec:
            video = codec
        elif kind == "audio":
            if codec in DECODABLE_AUDIO:
                usable.append((audio_ordinal, codec))
            elif codec:
                skipped.append(codec)
            audio_ordinal += 1
    return video, usable, skipped


def video_codecs(path: Path) -> tuple[str | None, list[str]]:
    video, usable, _skipped = stream_codecs(path)
    return video, [codec for _index, codec in usable]


def classify_action(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in JPEG_EXTENSIONS and is_jpeg(path):
        return "copy_jpeg"
    if extension in IMAGE_EXTENSIONS:
        if not looks_like_image(path):
            return "unreadable"
        return "convert_image"
    if extension in VIDEO_EXTENSIONS:
        try:
            if path.stat().st_size == 0:
                return "unreadable"
        except OSError:
            return "unreadable"
        video, audio = video_codecs(path)
        if not video:
            return "unreadable"
        universal = video in H264_CODECS and all(item in AAC_CODECS for item in audio)
        if universal and extension == ".mp4":
            return "copy_mp4"
        if universal:
            return "remux"
        return "transcode"
    return "skip"


def action_label(action: str) -> str:
    return {
        "copy_jpeg": "Copy JPG",
        "convert_image": "Convert to JPG",
        "copy_mp4": "Copy MP4",
        "remux": "Remux to MP4",
        "transcode": "Transcode to H.264/AAC MP4",
        "skip": "Skip",
        "unreadable": "Skip",
    }.get(action, action)


def needs_ffmpeg(action: str) -> bool:
    return action in {"convert_image", "remux", "transcode"}


def _failure_text(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "").strip()
    if len(text) > 1200:
        text = text[-1200:]
    return text or f"ffmpeg exited with code {result.returncode}"


def extract_exif_bytes(path: Path) -> bytes | None:
    register_heif()
    try:
        with Image.open(long_path(path)) as image:
            exif = image.getexif()
            raw = exif.tobytes() if exif else b""
            if raw and len(raw) > 16:
                return raw
            info = image.info.get("exif")
            if isinstance(info, bytes) and len(info) > 16:
                return info
    except (OSError, ValueError, SyntaxError):
        return None
    return None


def embed_exif(jpeg_path: Path, exif_payload: bytes) -> bool:
    data = jpeg_path.read_bytes()
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return False
    payload = exif_payload
    if payload.startswith(b"Exif\x00\x00"):
        payload = payload[6:]
    if not payload or len(payload) + 8 > 65533:
        return False
    body = b"Exif\x00\x00" + payload
    app1 = b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body
    index = 2
    kept = bytearray()
    while index + 1 < len(data) and data[index] == 0xFF:
        marker = data[index + 1]
        if marker == 0xDA:
            break
        if marker in (0xD8, 0xD9) or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            kept += data[index : index + 2]
            index += 2
            continue
        if index + 4 > len(data):
            break
        segment_length = int.from_bytes(data[index + 2 : index + 4], "big")
        if segment_length < 2 or index + 2 + segment_length > len(data):
            break
        chunk = data[index : index + 2 + segment_length]
        is_exif = marker == 0xE1 and len(chunk) >= 10 and chunk[4:10] == b"Exif\x00\x00"
        if not is_exif:
            kept += chunk
        index += 2 + segment_length
    jpeg_path.write_bytes(bytes(b"\xff\xd8" + app1 + kept + data[index:]))
    return True


def _ffmpeg_to_jpeg(source: Path, dest: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found on PATH.")
    result = _run(
        [
            ffmpeg,
            "-y",
            "-i",
            _ffmpeg_path(source),
            "-frames:v",
            "1",
            "-map_metadata",
            "0",
            "-q:v",
            "2",
            _ffmpeg_path(dest),
        ]
    )
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(_failure_text(result))


def _pillow_to_jpeg(source: Path, dest: Path, exif_payload: bytes | None) -> None:
    register_heif()
    with Image.open(long_path(source)) as image:
        frame = image.convert("RGB")
        save_kwargs = {"format": "JPEG", "quality": 92}
        if exif_payload:
            payload = exif_payload[6:] if exif_payload.startswith(b"Exif\x00\x00") else exif_payload
            save_kwargs["exif"] = payload
        frame.save(long_path(dest), **save_kwargs)


def _convert_image(source: Path, dest: Path, when: datetime, date_source: str) -> str | None:
    if not looks_like_image(source):
        raise RuntimeError("Named as a picture, but the file contains no image data.")
    exif_payload = extract_exif_bytes(source)
    warning = None
    try:
        _ffmpeg_to_jpeg(source, dest)
    except RuntimeError as ffmpeg_error:
        try:
            _pillow_to_jpeg(source, dest, exif_payload)
            warning = "ffmpeg could not read this image. Pillow wrote the JPG instead."
        except Exception as exc:
            raise RuntimeError(f"{ffmpeg_error}; Pillow fallback failed: {exc}") from exc
    embedded = False
    if exif_payload:
        embedded = embed_exif(dest, exif_payload)
    if not embedded and date_source != "file_created":
        if exif_payload:
            warning = warning or "The original EXIF block was too large to copy. The date was written into the JPG instead."
        embedded = embed_exif(dest, build_exif_tiff(when))
    if date_source != "file_created" and not embedded:
        warning = warning or "The image was converted, but its date could not be written into the JPG."
    return warning


def _ffmpeg_path(path: Path) -> str:
    """Use a normal Windows path unless the file needs the long-path prefix."""
    text = os.path.normpath(str(path))
    if os.name != "nt" or len(text) >= 240 or text.startswith("\\\\"):
        return long_path(path)
    return text


def _convert_video(source: Path, dest: Path, when: datetime, remux: bool) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found on PATH.")
    _video, usable_audio, skipped_audio = stream_codecs(source)
    if not _video:
        raise RuntimeError("Named as a video, but the file contains no video data.")
    chosen = None
    for index, codec in usable_audio:
        if codec in AAC_CODECS:
            chosen = (index, codec)
            break
    if chosen is None and usable_audio:
        chosen = usable_audio[0]
    created = creation_time_metadata(when)
    command = [
        ffmpeg,
        "-y",
        "-i",
        _ffmpeg_path(source),
        "-map",
        "0:v:0",
        "-dn",
        "-map_metadata",
        "0",
        "-metadata",
        f"creation_time={created}",
        "-movflags",
        "+faststart",
    ]
    if chosen is not None:
        command.extend(["-map", f"0:a:{chosen[0]}"])
    if remux:
        command.extend(["-c", "copy"])
    else:
        command.extend(
            [
                "-c:v",
                "libx264",
                "-threads",
                encoder_threads(),
                "-crf",
                "18",
                "-preset",
                "medium",
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if chosen is not None:
            if chosen[1] in AAC_CODECS:
                command.extend(["-c:a", "copy"])
            else:
                command.extend(["-c:a", "aac", "-b:a", "192k", "-ac", "2"])
    command.append(_ffmpeg_path(dest))
    result = _run(command)
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        if dest.exists():
            dest.unlink()
        detail = _failure_text(result)
        if remux:
            note = _convert_video(source, dest, when, remux=False)
            message = "Stream copy into MP4 failed, so the video was transcoded to H.264/AAC."
            return f"{message} {note}" if note else message
        undecodable = any(token in detail.lower() for token in ("apple_apac", "no decoder", "decoder found"))
        if chosen is not None and undecodable:
            return _convert_video_without_audio(source, dest, when, skipped_audio)
        raise RuntimeError(detail)
    if skipped_audio:
        names = ", ".join(sorted(set(skipped_audio)))
        return f"Kept the playable audio and ignored an unsupported track ({names})."
    return None


def _convert_video_without_audio(source: Path, dest: Path, when: datetime, skipped_audio: list[str]) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found on PATH.")
    created = creation_time_metadata(when)
    result = _run(
        [
            ffmpeg,
            "-y",
            "-i",
            _ffmpeg_path(source),
            "-map",
            "0:v:0",
            "-dn",
            "-map_metadata",
            "0",
            "-metadata",
            f"creation_time={created}",
            "-movflags",
            "+faststart",
            "-c:v",
            "libx264",
            "-threads",
            encoder_threads(),
            "-crf",
            "18",
            "-preset",
            "medium",
            "-pix_fmt",
            "yuv420p",
            "-an",
            _ffmpeg_path(dest),
        ]
    )
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        if dest.exists():
            dest.unlink()
        raise RuntimeError(_failure_text(result))
    names = ", ".join(sorted(set(skipped_audio))) or "the original audio"
    return f"The audio track ({names}) could not be converted, so the MP4 has picture only."


def set_file_times(path: Path, when: datetime) -> None:
    try:
        timestamp = when.timestamp()
    except (OSError, OverflowError, ValueError):
        return
    if os.name == "nt":
        if _set_windows_times(path, timestamp):
            return
    os.utime(path, (timestamp, timestamp))


def _set_windows_times(path: Path, timestamp: float) -> bool:
    ticks = int((timestamp + 11644473600) * 10_000_000)
    if ticks < 0:
        return False

    filetime = _FILETIME(ticks & 0xFFFFFFFF, (ticks >> 32) & 0xFFFFFFFF)
    created = ctypes.pointer(filetime)
    FILE_WRITE_ATTRIBUTES = 0x0100
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    handle = _kernel32.CreateFileW(
        long_path(path),
        FILE_WRITE_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        return False
    try:
        return bool(_kernel32.SetFileTime(handle, created, created, created))
    finally:
        _kernel32.CloseHandle(handle)


def write_output(source: Path, dest: Path, action: str, when: datetime, date_source: str) -> str | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(f"{dest.stem}.converting{dest.suffix}")
    if partial.exists():
        partial.unlink()
    warning = None
    try:
        if action in {"copy_jpeg", "copy_mp4"}:
            shutil.copyfile(long_path(source), long_path(partial))
        elif action == "convert_image":
            warning = _convert_image(source, partial, when, date_source)
        elif action == "remux":
            warning = _convert_video(source, partial, when, remux=True)
        elif action == "transcode":
            warning = _convert_video(source, partial, when, remux=False)
        else:
            raise RuntimeError(f"Unsupported action {action}")
        if not partial.exists() or partial.stat().st_size == 0:
            raise RuntimeError("Conversion produced an empty file.")
        partial.replace(dest)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    set_file_times(dest, when)
    suffix = dest.suffix.lower()
    if suffix not in {".jpg", ".mp4"}:
        dest.unlink(missing_ok=True)
        raise RuntimeError("Refused to keep a file that is not JPG or MP4.")
    return warning
