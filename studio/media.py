"""媒体探测（ffprobe 封装）。素材入库与集成测试共用。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe_media(path: str | Path) -> dict:
    """返回 {duration_ms, width, height, fps}；失败抛 RuntimeError（不静默）。"""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    data = json.loads(out)
    duration_s = float(data.get("format", {}).get("duration", 0) or 0)
    vstream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    fps = 0.0
    rate = vstream.get("r_frame_rate", "0/1")
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration_ms": int(duration_s * 1000),
        "width": int(vstream.get("width", 0) or 0),
        "height": int(vstream.get("height", 0) or 0),
        "fps": round(fps, 3),
    }


def make_lavfi_fixture(out_path: str | Path, seconds: int = 10) -> Path:
    """测试夹具一律 ffmpeg lavfi 生成（仓库惯例），不提交二进制。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(out),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return out


def scan_silence(
    path: str | Path, start_s: float, end_s: float,
    *, noise_db: float = -40.0, min_d: float = 0.4,
) -> list[tuple[int, int]]:
    """silencedetect 实测静音区间（源绝对 ms）。去气口三重判定条件二。

    解码失败抛错不静默（调用方决定降级）。
    """
    import re

    r = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "info",
            "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}", "-i", str(path),
            "-vn", "-af", f"silencedetect=n={noise_db}dB:d={min_d}",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"静音扫描失败: {r.stderr[-300:]}")
    starts = [float(v) for v in re.findall(r"silence_start:\s*([\d.]+)", r.stderr)]
    ends = [float(v) for v in re.findall(r"silence_end:\s*([\d.]+)", r.stderr)]
    out: list[tuple[int, int]] = []
    for i, st in enumerate(starts):
        en = ends[i] if i < len(ends) else (end_s - start_s)
        out.append((int((start_s + st) * 1000), int((start_s + en) * 1000)))
    return out


def range_rms(path: str | Path, start_s: float, end_s: float) -> float:
    """区间音频 RMS（能量 VAD v1）。去气口三重判定条件三。"""
    import numpy as np

    r = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error",
            "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}", "-i", str(path),
            "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
        ],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"间隙能量解码失败: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    pcm = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if pcm.size == 0:
        return 0.0
    return float(np.sqrt((pcm ** 2).mean()))


def waveform_peaks(path: str | Path, buckets: int = 1000) -> list[float]:
    """音频振幅波形（W-1）：8kHz 单声道 PCM → 每桶绝对值峰值，归一化 0..1。

    解码失败抛错不静默；无音频流返回全零（调用方回执标注）。
    """
    import numpy as np

    r = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
            "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-",
        ],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"波形解码失败: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    pcm = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if pcm.size == 0:
        return [0.0] * buckets
    n = max(1, pcm.size // buckets)
    trimmed = pcm[: n * buckets].reshape(buckets, n)
    peaks = np.abs(trimmed).max(axis=1)
    mx = float(peaks.max()) or 1.0
    return [round(float(v) / mx, 4) for v in peaks]


def has_audio_stream(path: str | Path) -> bool:
    """探测音频流存在性（W-3 前置段原声判定）。"""
    r = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode == 0 and "audio" in r.stdout