"""响度信号抽取（04 号方案 P1-4，纯 CPU，双档案无差别）。

ffmpeg 解码为 8kHz 单声道 s16le PCM → 5s 分桶 RMS → 归一 0-1。
流式读取不占内存；ffmpeg 不可用时 available() 显式报原因。
"""
from __future__ import annotations

import array
import shutil
import subprocess

from studio.engines.base import (
    EngineContext,
    MaterialRef,
    ProgressFn,
    SignalTrack,
)

BUCKET_MS = 5_000
SAMPLE_RATE = 8_000
SAMPLES_PER_BUCKET = SAMPLE_RATE * BUCKET_MS // 1000


class LoudnessSignal:
    name = "audio"

    def __init__(self, ffmpeg_path: str | None = None) -> None:
        self._ffmpeg = ffmpeg_path or shutil.which("ffmpeg")

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        if self._ffmpeg is None:
            return False, "ffmpeg 不可用"
        return True, ""

    def extract(self, material: MaterialRef, progress: ProgressFn) -> SignalTrack:
        n = max(1, (material.duration_ms + BUCKET_MS - 1) // BUCKET_MS)
        proc = subprocess.Popen(
            [
                self._ffmpeg,
                "-nostdin", "-i", material.file_path,
                "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        sums = [0.0] * n
        nsamples = [0] * n
        idx = 0
        carry = array.array("h")
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf = carry + array.array("h", chunk[: len(chunk) // 2 * 2])
            carry = array.array("h", chunk[len(chunk) // 2 * 2 :])
            for sample in buf:
                b = idx // SAMPLES_PER_BUCKET
                if b >= n:
                    break
                sums[b] += float(sample) * sample
                nsamples[b] += 1
                idx += 1
        proc.stdout.close()
        proc.wait()
        if proc.returncode != 0:
            # 解码失败 → 空 series 走降维，不输出假零值
            return SignalTrack(bucket_ms=BUCKET_MS, series={})
        series = tuple(
            round((sums[i] / nsamples[i]) ** 0.5 / 32768.0, 4) if nsamples[i] else 0.0
            for i in range(n)
        )
        return SignalTrack(bucket_ms=BUCKET_MS, series={"loudness": series})