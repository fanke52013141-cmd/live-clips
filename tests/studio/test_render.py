"""渲染引擎测试（P2-1a 验收：词边界吸附/逐词 ASS/两遍 loudnorm/自检，真实媒体）。"""
import json
import subprocess
from pathlib import Path

import pytest

from studio.engines.base import ClipPlan, EngineContext, RenderPlan, Word
from studio.engines.render_ffmpeg import (
    FfmpegRenderer,
    _ass_filter_path,
    _ass_time,
    build_ass,
    group_lines,
    measure_loudness,
    snap_to_words,
)
from studio.media import ffmpeg_available, make_lavfi_fixture

CTX = EngineContext(profile="cpu-cloud", data_dir=Path("."), models_dir=Path("."), gpu_available=False)
pytestmark_media = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")


def words_2_to_8():
    """2-8s 词级时间戳，300ms/字，文本循环。"""
    text = "价格三十九块九拍一发三"
    out = []
    t = 2000
    i = 0
    while t < 8000:
        out.append(Word(text[i % len(text)], t, t + 300))
        t += 300
        i += 1
    return tuple(out)


def plan(tmp_path, **kw):
    defaults = dict(
        clip=ClipPlan("c1", 2000, 8000, "标题", "价格"),
        platform="douyin", portrait=False, subtitles=True,
        out_path=str(tmp_path / "out.mp4"),
        source_path=kw.pop("source_path", ""),
        words=kw.pop("words", words_2_to_8()),
    )
    defaults.update(kw)
    return RenderPlan(**defaults)


# ---------- 单元：吸附 ----------

def test_snap_nearest_word_boundary():
    ws = (Word("a", 1000, 1500), Word("b", 1500, 2000))
    s, e, meta = snap_to_words(1100, 1900, ws)
    assert (s, e) == (1000, 2000)
    assert meta["snapped"] and meta["start_offset_ms"] == 100


def test_snap_no_words_passthrough():
    s, e, meta = snap_to_words(123, 456, ())
    assert (s, e) == (123, 456) and not meta["snapped"]


def test_snap_empty_interval_keeps_original():
    # start=1900 吸附到后词起点 2000，end=1020 吸附到前词终点 1010
    # → end<=start 空区间 → 放弃吸附保留原切点（回执记录原因）
    ws = (Word("a", 1000, 1010), Word("b", 2000, 2010))
    s, e, meta = snap_to_words(1900, 1020, ws)
    assert (s, e) == (1900, 1020)
    assert not meta["snapped"] and "空" in meta["reason"]


# ---------- 单元：断行与 ASS ----------

def test_group_lines_punctuation():
    ws = [Word(c, i * 100, i * 100 + 100) for i, c in enumerate("你好，世界。")]
    lines = group_lines(ws)
    assert ["".join(w.text for w in ln) for ln in lines] == ["你好，", "世界。"]


def test_group_lines_max_chars():
    ws = [Word("字", i * 100, i * 100 + 100) for i in range(30)]
    lines = group_lines(ws)
    assert all(len(ln) <= 12 for ln in lines)
    assert sum(len(ln) for ln in lines) == 30


def test_ass_time_format():
    assert _ass_time(0) == "0:00:00.00"
    assert _ass_time(61230) == "0:01:01.23"
    assert _ass_time(3600000) == "1:00:00.00"


def test_build_ass_word_highlight():
    ws = [Word("你", 2000, 2300), Word("好", 2300, 2600), Word("，", 2600, 2900)]
    ass = build_ass(ws, 2000, 2900, portrait=False)
    assert ass.count("Dialogue:") == 3  # 每词一个事件
    assert r"{\c&H00FFFF&}你{\c&HFFFFFF&}" in ass  # 第一事件高亮「你」
    assert "PlayResX: 1920" in ass
    # 时间相对切片：首事件 0:00:00.00 开始
    assert "Dialogue: 0,0:00:00.00," in ass


def test_build_ass_portrait_res():
    ass = build_ass([Word("你", 0, 300)], 0, 300, portrait=True)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass


def test_ass_path_escape():
    p = Path("D:\\videos\\口播\\f.ass")
    escaped = _ass_filter_path(p)
    # 双反斜杠：滤镜图与 ass 选项两层解析各消耗一个
    assert escaped == "D\\\\:/videos/口播/f.ass"


# ---------- 集成：真实媒体 ----------

@pytestmark_media
def test_render_full_pipeline(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    p = plan(tmp_path, source_path=str(src))
    events = []
    art = FfmpegRenderer().render(p, events.append)

    assert Path(art.file_path).is_file() and art.size_bytes > 1000
    assert art.meta["snap"]["snapped"] is True
    assert art.meta["subtitles"] == "ass_word_highlight"
    assert art.meta["selfcheck"]["streams"] == ["audio", "video"]
    assert abs(art.meta["selfcheck"]["duration_s"] - 6.0) <= 0.25
    assert len(events) == 4  # 数字进度 1-4 步

    # 输出响度实测应接近 -14 LUFS（±2）
    out_loud = measure_loudness(art.file_path, 0, 6.0, -14.0)
    assert -16.0 <= float(out_loud["input_i"]) <= -12.0

    # ASS 文件真实生成且含逐词事件
    ass = Path(art.file_path).with_name("subs_c1.ass")
    assert ass.is_file() and ass.read_text(encoding="utf-8").count("Dialogue:") >= 15


@pytestmark_media
def test_render_portrait_dimensions(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    p = plan(tmp_path, source_path=str(src), portrait=True,
             out_path=str(tmp_path / "portrait.mp4"))
    art = FfmpegRenderer().render(p, lambda ev: None)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", art.file_path],
        capture_output=True, text=True,
    )
    v = next(s for s in json.loads(probe.stdout)["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert art.meta["portrait_mode"] == "center_crop_v1"


@pytestmark_media
def test_render_no_subtitles_no_words(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    p = plan(tmp_path, source_path=str(src), subtitles=False, words=(),
             out_path=str(tmp_path / "plain.mp4"))
    art = FfmpegRenderer().render(p, lambda ev: None)
    assert art.meta["subtitles"] is None
    assert art.meta["snap"]["snapped"] is False
    assert abs(art.meta["selfcheck"]["duration_s"] - 6.0) <= 0.25


def test_render_missing_source_raises(tmp_path):
    p = plan(tmp_path, source_path=str(tmp_path / "no.mp4"))
    with pytest.raises(RuntimeError, match="源文件不存在"):
        FfmpegRenderer().render(p, lambda ev: None)


def test_available():
    ok, reason = FfmpegRenderer().available(CTX)
    assert ok == ffmpeg_available()