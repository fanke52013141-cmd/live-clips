"""封面选优与成片缺陷扫描测试（P2-1c，真实 lavfi 媒体）。"""
import subprocess
from pathlib import Path

import numpy as np
import pytest

from studio.engines.cover import rms_peak_times, score_frame, select_cover
from studio.engines.render_ffmpeg import scan_defects
from studio.media import ffmpeg_available, make_lavfi_fixture

pytestmark_media = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")


def make_black_fixture(out: Path, seconds: int = 5) -> Path:
    """黑场+静音夹具：验证淘汰与缺陷扫描。"""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=black:s=320x240:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:duration={seconds}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(out),
        ],
        capture_output=True, text=True, check=True,
    )
    return out


# ---------- 单元：评分 ----------

def test_score_black_white_rejected():
    assert score_frame(np.zeros((90, 160), dtype=np.float32))["rejected"] == "黑场/白场"
    assert score_frame(np.full((90, 160), 255.0))["rejected"] == "黑场/白场"


def test_score_sharp_beats_flat():
    rng = np.random.default_rng(42)
    noisy = rng.uniform(40, 210, (90, 160)).astype(np.float32)
    flat = np.full((90, 160), 128.0)
    assert score_frame(noisy)["score"] > score_frame(flat)["score"]
    assert score_frame(flat)["rejected"] is None


# ---------- 集成：真实媒体 ----------

@pytestmark_media
def test_rms_peaks_within_range(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    peaks = rms_peak_times(str(src), 2.0, 8.0)
    assert peaks and all(2.0 <= t < 8.0 for t in peaks)


@pytestmark_media
def test_select_cover_real_media(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    out = tmp_path / "cover.jpg"
    meta = select_cover(str(src), 2000, 8000, out)
    assert out.is_file() and meta["size_bytes"] > 100
    assert meta["method"] == "loudness_peak_nomination_v1"
    assert 2.0 <= meta["chosen_t_s"] < 8.0
    assert len(meta["nominations"]) >= 3  # 响度峰+均匀补位，全提名落回执
    assert meta["chosen_score"]["rejected"] is None


@pytestmark_media
def test_select_cover_all_black_raises(tmp_path):
    src = make_black_fixture(tmp_path / "black.mp4")
    with pytest.raises(RuntimeError, match="全部被淘汰"):
        select_cover(str(src), 0, 4000, tmp_path / "cover.jpg")


@pytestmark_media
def test_scan_defects_clean(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=8)
    assert scan_defects(str(src)) == []


@pytestmark_media
def test_scan_defects_black_and_silence(tmp_path):
    src = make_black_fixture(tmp_path / "black.mp4", seconds=5)
    issues = scan_defects(str(src))
    kinds = {i["type"] for i in issues}
    assert "black" in kinds and "silence" in kinds