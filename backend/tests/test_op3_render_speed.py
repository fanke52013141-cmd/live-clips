"""OP-3 渲染提速：草稿档双路径选择 + 源编码探测。

对应《docs/11-优化包实施方案》OP-3.1。流复制草稿只允许 H.264 源；
HEVC/AV1/VP9 源必须回退轻量重编码，保证预览在应用内 WebView2 可播。
编码器探针与逐条回退（services/video_encoder.py）与上游合并的本机验证
为既有能力，不在本测试范围。
"""

from pathlib import Path

from backend.utils.video_processor import VideoProcessor


def _run_draft(monkeypatch, tmp_path: Path, codecs: set[str]):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"fake")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("backend.utils.video_processor.subprocess.run", fake_run)
    monkeypatch.setattr(
        "backend.utils.video_processor.VideoProcessor.get_video_info",
        staticmethod(lambda video_path: {'streams': [{'codec_name': c} for c in codecs]}),
    )
    monkeypatch.setattr("backend.utils.video_processor.get_ffmpeg_path", lambda: "ffmpeg")
    output = tmp_path / "draft.mp4"
    ok = VideoProcessor.extract_clip_draft(tmp_path / "src.mp4", output, "00:00:01,000", "00:00:11,000")
    return ok, calls


def test_draft_uses_stream_copy_for_h264_source(monkeypatch, tmp_path: Path):
    ok, calls = _run_draft(monkeypatch, tmp_path, {'h264', 'aac'})
    assert ok and len(calls) == 1
    cmd = calls[0]
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "libx264" not in cmd


def test_draft_reencodes_for_hevc_source(monkeypatch, tmp_path: Path):
    ok, calls = _run_draft(monkeypatch, tmp_path, {'hevc', 'aac'})
    assert ok and len(calls) == 1
    cmd = calls[0]
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "-preset" in cmd and cmd[cmd.index("-preset") + 1] == "ultrafast"


def test_draft_reencodes_for_av1_source(monkeypatch, tmp_path: Path):
    ok, calls = _run_draft(monkeypatch, tmp_path, {'av1', 'opus'})
    assert ok
    assert cmd_has_libx264(calls[0])


def cmd_has_libx264(cmd: list) -> bool:
    return "libx264" in cmd


def test_draft_rejects_invalid_range(monkeypatch, tmp_path: Path):
    output = tmp_path / "draft.mp4"
    assert VideoProcessor.extract_clip_draft(tmp_path / "s.mp4", output, "00:00:10,000", "00:00:01,000") is False


def test_draft_commands_both_safe_for_browser():
    copy_cmd = VideoProcessor.build_draft_extract_command(
        "ffmpeg", Path("in.mp4"), Path("o.mp4"), "00:00:01.000", 5.0, reencode=False)
    re_cmd = VideoProcessor.build_draft_extract_command(
        "ffmpeg", Path("in.mp4"), Path("o.mp4"), "00:00:01.000", 5.0, reencode=True)
    for cmd in (copy_cmd, re_cmd):
        assert "negative_cts_offsets" in cmd[cmd.index("-movflags") + 1]
        assert cmd.index("-ss") < cmd.index("-i")
    assert cmd_has_libx264(re_cmd)
    assert not cmd_has_libx264(copy_cmd)
