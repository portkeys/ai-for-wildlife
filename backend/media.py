"""ffmpeg/ffprobe helpers: probe metadata, extract a thumbnail, sample frames."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path


def probe(path: Path) -> dict:
    """Return {duration, width, height, size_bytes} for a video file."""
    size = path.stat().st_size
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        duration = float(data.get("format", {}).get("duration") or 0.0)
        return {
            "duration": round(duration, 2),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "size_bytes": size,
        }
    except Exception:
        return {"duration": 0.0, "width": 0, "height": 0, "size_bytes": size}


def make_thumbnail(video: Path, dest: Path, at_seconds: float = 1.0) -> bool:
    """Grab a single frame as a JPEG thumbnail. Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(at_seconds), "-i", str(video),
                "-frames:v", "1", "-vf", "scale=320:-2",
                "-q:v", "4", str(dest),
            ],
            capture_output=True, timeout=60, check=True,
        )
        return dest.exists()
    except Exception:
        # Fall back to the very first frame if seeking past EOF on short clips.
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video), "-frames:v", "1",
                 "-vf", "scale=320:-2", "-q:v", "4", str(dest)],
                capture_output=True, timeout=60, check=True,
            )
            return dest.exists()
        except Exception:
            return False


def extract_frames(video: Path, out_dir: Path, count: int = 8,
                   max_dim: int = 768, duration: float | None = None) -> list[Path]:
    """Extract `count` evenly-spaced frames across the clip, scaled to <= max_dim.

    Returns the list of frame paths in chronological order.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()

    if duration is None:
        duration = probe(video)["duration"]
    duration = max(duration, 0.1)

    # Sample at the midpoints of `count` equal segments so we never grab the
    # exact last frame (which can be black / past EOF on some encoders).
    timestamps = [duration * (i + 0.5) / count for i in range(count)]

    paths: list[Path] = []
    for idx, ts in enumerate(timestamps):
        dest = out_dir / f"frame_{idx:02d}.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", str(video),
                    "-frames:v", "1",
                    "-vf", f"scale='min({max_dim},iw)':-2",
                    "-q:v", "3", str(dest),
                ],
                capture_output=True, timeout=60, check=True,
            )
            if dest.exists():
                paths.append(dest)
        except Exception:
            continue
    return paths


def frames_as_data_uris(frame_paths: list[Path]) -> list[str]:
    """Encode JPEG frames as base64 data URIs for multimodal LLM requests."""
    uris = []
    for p in frame_paths:
        b = p.read_bytes()
        uris.append("data:image/jpeg;base64," + base64.b64encode(b).decode("ascii"))
    return uris


def compress_for_upload(video: Path, dest: Path, height: int = 480,
                        fps: int = 10, crf: int = 28) -> Path | None:
    """Transcode to a small H.264 mp4 so the whole clip fits inline as base64.

    A 1080p 20s clip (~40MB) becomes a few hundred KB, sendable as native video
    input without hitting request-size limits — while keeping enough detail/motion
    for species + behavior. Audio is dropped (irrelevant + saves size).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(video),
                "-vf", f"scale=-2:{height},fps={fps}",
                "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
                "-movflags", "+faststart", "-an", str(dest),
            ],
            capture_output=True, timeout=300, check=True,
        )
        return dest if dest.exists() else None
    except Exception:
        return None


def video_as_data_uri(path: Path) -> str:
    """Encode an mp4 as a base64 data URI for native video input."""
    return "data:video/mp4;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
