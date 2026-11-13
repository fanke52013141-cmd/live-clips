"""渲染剪辑路径测试（P2-2b/P2-3a：cuts/cold-open/flash-forward，真实 lavfi 媒体）。"""
from pathlib import Path

import pytest

from studio.engines.base import ClipPlan, RenderPlan, Word
from studio.engines.render_ffmpeg import FfmpegRenderer, build_ass_segments
from studio.media import ffmpeg_available, make_lavfi_fixture
from studio.pipeline.editlist import Segment

pytestmark_media = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")


def words_2_to_8():
    text = "价格三十九块九拍一发三"
    out = []
    t = 2000
    i = 0
    while t < 8000:
        out.append(Word(text[i % len(text)], t, t + 300))
        t += 300
        i += 1
    return tuple(out)


def plan(tmp_path, src, **kw):
    defaults = dict(
        clip=ClipPlan("c1", 2000, 8000, "标题", "价格"),
        platform="douyin", portrait=False, subtitles=True,
        out_path=str(tmp_path / "out.mp4"),
        source_path=str(src), words=words_2_to_8(),
    )
    defaults.update(kw)
    return RenderPlan(**defaults)


# ---------- 单元：段式字幕 ----------

def test_ass_segments_remap_and_copy():
    ws = [Word("你", 4000, 4300), Word("好", 4300, 4600), Word("世", 6000, 6300)]
    segs = [Segment(4000, 4600, "cold_open"), Segment(2000, 8000, "body")]
    ass = build_ass_segments(segs, ws, portrait=False)
    # 钩子副本在片头 0:00:00.00；正文「你」(源4000ms) 重映射到 0.6+(4.0-2.0)=2.6s
    assert "Dialogue: 0,0:00:00.00," in ass
    assert "Dialogue: 0,0:00:02.60," in ass
    # 「世」在正文 6s 处 → 输出 0.6+4=4.6s
    assert "Dialogue: 0,0:00:04.60," in ass


def test_ass_segments_cut_words_dropped():
    ws = [Word("你", 2000, 2300), Word("好", 4000, 4300)]
    segs = [Segment(2000, 3000, "body"), Segment(5000, 8000, "body")]
    ass = build_ass_segments(segs, ws, portrait=False)
    assert "你" in ass and "好" not in ass  # 被剪区间的词落选


# ---------- 集成：真实媒体 ----------

@pytestmark_media
def test_render_with_breath_cut(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    p = plan(tmp_path, src, cuts=((4000, 5000),))
    art = FfmpegRenderer().render(p, lambda ev: None)
    # 6s 切片剪掉 1s → 5s
    assert abs(art.meta["selfcheck"]["duration_s"] - 5.0) <= 0.25
    el = art.meta["editlist"]
    assert el["cuts"] == [{"start_ms": 4000, "end_ms": 5000}]
    assert [s["kind"] for s in el["segments"]] == ["body", "body"]


@pytestmark_media
def test_render_cold_open_keeps_original(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    p = plan(tmp_path, src, cold_open=(6000, 7000))
    art = FfmpegRenderer().render(p, lambda ev: None)
    # 钩子复制到片头，原位保留 → 7s
    assert abs(art.meta["selfcheck"]["duration_s"] - 7.0) <= 0.25
    el = art.meta["editlist"]
    assert el["segments"][0] == {"start_ms": 6000, "end_ms": 7000, "kind": "cold_open"}
    # 字幕：钩子词在片头出现，正文同词后移 1s（事件数多于无钩子版）
    ass = Path(art.file_path).with_name("subs_c1.ass").read_text(encoding="utf-8")
    assert "Dialogue: 0,0:00:00.00," in ass


@pytestmark_media
def test_render_cold_open_jumpcut(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    p = plan(tmp_path, src, cold_open=(6000, 7000), cold_open_jumpcut=True)
    art = FfmpegRenderer().render(p, lambda ev: None)
    # 原位挖掉 → 总时长不变 6s
    assert abs(art.meta["selfcheck"]["duration_s"] - 6.0) <= 0.25
    el = art.meta["editlist"]
    assert el["cold_open_jumpcut"] is True
    body = [s for s in el["segments"] if s["kind"] == "body"]
    assert all(not (s["start_ms"] < 7000 and s["end_ms"] > 6000) for s in body)


@pytestmark_media
def test_render_flash_forward_with_whoosh(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    p = plan(tmp_path, src, flash_forward=(7000, 7500))
    art = FfmpegRenderer().render(p, lambda ev: None)
    # 0.5s 闪现前置 → 6.5s
    assert abs(art.meta["selfcheck"]["duration_s"] - 6.5) <= 0.25
    el = art.meta["editlist"]
    assert el["segments"][0]["kind"] == "flash"
    assert el["whoosh"] is True


@pytestmark_media
def test_render_invalid_hook_raises(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    p = plan(tmp_path, src, cold_open=(7000, 9000))  # 越界
    with pytest.raises(ValueError, match="越界"):
        FfmpegRenderer().render(p, lambda ev: None)