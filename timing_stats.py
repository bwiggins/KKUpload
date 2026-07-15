from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


TIMING_STATS_FILE_NAME = "timing_stats.json"
@dataclass(frozen=True)
class UploadTimingSample:
    completed_at: str
    file_count: int
    source_bytes: int
    uploaded_bytes: int
    upload_seconds: float
    total_seconds: float


@dataclass(frozen=True)
class UploadTimeEstimate:
    seconds: float | None
    sample_count: int
    upload_bytes_per_second: float | None
    overhead_seconds_per_file: float | None
    used_fallback: bool = False


def default_timing_stats_path(app_path: Path) -> Path:
    return app_path.with_name(TIMING_STATS_FILE_NAME)


def new_upload_timing_sample(
    *,
    file_count: int,
    source_bytes: int,
    uploaded_bytes: int,
    upload_seconds: float,
    total_seconds: float,
) -> UploadTimingSample:
    return UploadTimingSample(
        completed_at=datetime.now(timezone.utc).isoformat(),
        file_count=max(file_count, 0),
        source_bytes=max(source_bytes, 0),
        uploaded_bytes=max(uploaded_bytes, 0),
        upload_seconds=max(upload_seconds, 0.0),
        total_seconds=max(total_seconds, 0.0),
    )


def record_upload_timing(path: Path, sample: UploadTimingSample) -> None:
    path.write_text(
        json.dumps(
            {"samples": [_sample_to_json(sample)]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def estimate_upload_time(
    path: Path,
    *,
    file_count: int,
    source_bytes: int,
) -> UploadTimeEstimate:
    samples = load_upload_timing_samples(path)
    if not samples:
        return UploadTimeEstimate(
            seconds=None,
            sample_count=0,
            upload_bytes_per_second=None,
            overhead_seconds_per_file=None,
        )

    sample = samples[-1]

    upload_bytes_per_second = (
        sample.uploaded_bytes / sample.upload_seconds
        if sample.uploaded_bytes > 0 and sample.upload_seconds > 0
        else None
    )
    overhead_seconds_per_file = (
        max(sample.total_seconds - sample.upload_seconds, 0.0)
        / sample.file_count
        if sample.file_count > 0
        else None
    )

    estimated_seconds = 0.0
    if upload_bytes_per_second is not None:
        estimated_seconds += source_bytes / upload_bytes_per_second
    if overhead_seconds_per_file is not None:
        estimated_seconds += file_count * overhead_seconds_per_file

    if estimated_seconds <= 0:
        return UploadTimeEstimate(
            seconds=None,
            sample_count=1,
            upload_bytes_per_second=upload_bytes_per_second,
            overhead_seconds_per_file=overhead_seconds_per_file,
        )

    return UploadTimeEstimate(
        seconds=estimated_seconds,
        sample_count=1,
        upload_bytes_per_second=upload_bytes_per_second,
        overhead_seconds_per_file=overhead_seconds_per_file,
    )


def load_upload_timing_samples(path: Path) -> tuple[UploadTimingSample, ...]:
    if not path.exists():
        return ()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()

    if not isinstance(raw, dict):
        return ()

    samples_raw = raw.get("samples", [])
    if not isinstance(samples_raw, list):
        return ()

    samples: list[UploadTimingSample] = []
    for item in samples_raw:
        sample = _sample_from_json(item)
        if sample is not None:
            samples.append(sample)

    return tuple(samples)


def _sample_from_json(raw: Any) -> UploadTimingSample | None:
    if not isinstance(raw, dict):
        return None

    try:
        sample = UploadTimingSample(
            completed_at=str(raw["completed_at"]),
            file_count=int(raw["file_count"]),
            source_bytes=int(raw["source_bytes"]),
            uploaded_bytes=int(raw["uploaded_bytes"]),
            upload_seconds=float(raw["upload_seconds"]),
            total_seconds=float(raw["total_seconds"]),
        )
    except (KeyError, TypeError, ValueError):
        return None

    if (
        sample.file_count <= 0
        or sample.source_bytes < 0
        or sample.uploaded_bytes < 0
        or sample.upload_seconds < 0
        or sample.total_seconds <= 0
    ):
        return None

    return sample


def _sample_to_json(sample: UploadTimingSample) -> dict[str, object]:
    return {
        "completed_at": sample.completed_at,
        "file_count": sample.file_count,
        "source_bytes": sample.source_bytes,
        "uploaded_bytes": sample.uploaded_bytes,
        "upload_seconds": sample.upload_seconds,
        "total_seconds": sample.total_seconds,
    }
