"""封面提名选优（04 号方案 §10.8 的 v1 实现）。

流程：切片内响度峰提名（1s 窗 RMS top3）+ 均匀补位 2 帧 → 逐帧灰度抽取
→ 清晰度（拉普拉斯方差）+ 曝光（均值偏离中灰惩罚）评分 → 淘汰黑场/白场
→ 取优保存 JPEG。全部提名与评分落回执，可审计。

铁律：全部候选被淘汰时抛错，绝不输出黑场封面冒充成功。
依赖：ffmpeg/ffprobe + numpy（均为既有依赖）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

_GRAY_W = 160  # 评分用缩略宽度，够用且快
_BLACK_MEAN = 16.0
_WHITE_MEAN = 240.0


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def rms_peak_times(source: str, start_s: float, end_s: float, *, window_s: float = 1.0, top: int = 3) -> list[float]:
    """切片内按窗 RMS 取响度峰中心时刻（秒，绝对时间轴）。解码失败抛错不静默。"""
    r = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error",
            "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}", "-i", source,
            "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
        ],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"封面提名音频解码失败: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    pcm = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    win = max(1, int(16000 * window_s))
    if pcm.size < win:
        return [(start_s + end_s) / 2] if pcm.size else []
    n = pcm.size // win
    rms = np.sqrt((pcm[: n * win].reshape(n, win) ** 2).mean(axis=1))
    idx = np.argsort(rms)[::-1][:top]
    return sorted(start_s + (int(i) + 0.5) * window_s for i in idx)


def _gray_frame(source: str, t_s: float) -> np.ndarray | None:
    """抽取单帧灰度缩略图；失败返回 None（该提名淘汰，回执记录）。"""
    r = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error",
            "-ss", f"{t_s:.3f}", "-i", source,
            "-frames:v", "1", "-vf", f"format=gray,scale={_GRAY_W}:-1",
            "-f", "rawvideo", "-",
        ],
        capture_output=True,
    )
    if r.returncode != 0 or not r.stdout:
        return None
    # 高度偶数对齐（scale=-1 可能产生奇数行被 yuv 对齐），按宽度整除推断
    h = len(r.stdout) // _GRAY_W
    if h < 2 or len(r.stdout) != _GRAY_W * h:
        return None
    return np.frombuffer(r.stdout[: _GRAY_W * h], dtype=np.uint8).reshape(h, _GRAY_W).astype(np.float32)


def score_frame(gray: np.ndarray) -> dict:
    """清晰度=拉普拉斯方差；曝光=均值偏离 128 的惩罚。黑白场直接淘汰。"""
    mean = float(gray.mean())
    if mean < _BLACK_MEAN or mean > _WHITE_MEAN:
        return {"mean": round(mean, 1), "sharpness": 0.0, "score": 0.0, "rejected": "黑场/白场"}
    lap = (
        gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:]
        - 4 * gray[1:-1, 1:-1]
    )
    sharp = float(lap.var())
    exposure_penalty = abs(mean - 128.0) / 128.0  # 0..1
    score = sharp / (1.0 + sharp) * (1.0 - 0.5 * exposure_penalty)
    return {"mean": round(mean, 1), "sharpness": round(sharp, 1), "score": round(score, 4), "rejected": None}


def select_cover(source: str, start_ms: int, end_ms: int, out_path: str | Path) -> dict:
    """提名→评分→取优→存 JPEG。返回回执 meta（含全部提名评分）。

    全部候选被淘汰/抽帧失败 → RuntimeError（宁缺毋滥）。
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    start_s, end_s = start_ms / 1000, end_ms / 1000
    peaks = rms_peak_times(source, start_s, end_s)
    # 均匀补位 2 帧，避免响度峰全集中在一处
    extras = [start_s + (end_s - start_s) * f for f in (0.25, 0.75)]
    times = sorted(set(round(t, 3) for t in peaks + extras if start_s <= t < end_s))
    nominations: list[dict] = []
    best: tuple[float, float, dict] | None = None  # (score, t, info)
    for t in times:
        gray = _gray_frame(source, t)
        if gray is None:
            nominations.append({"t_s": t, "rejected": "抽帧失败"})
            continue
        info = score_frame(gray)
        nominations.append({"t_s": t, **info})
        if info["rejected"] is None and (best is None or info["score"] > best[0]):
            best = (info["score"], t, info)
    if best is None:
        raise RuntimeError(f"封面选优失败：{len(times)} 个提名全部被淘汰（黑场/白场/抽帧失败）")
    _, t, info = best
    r = _run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-ss", f"{t:.3f}", "-i", source,
        "-frames:v", "1", "-q:v", "2", str(out),
    ])
    if r.returncode != 0 or not out.is_file() or out.stat().st_size < 100:
        raise RuntimeError(f"封面保存失败: {r.stderr[-300:]}")
    return {
        "method": "loudness_peak_nomination_v1",
        "chosen_t_s": t,
        "chosen_score": info,
        "nominations": nominations,
        "file_path": str(out),
        "size_bytes": out.stat().st_size,
    }