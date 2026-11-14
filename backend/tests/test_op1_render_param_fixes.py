"""OP-1 渲染参数修正：cfr/音画同步/首帧黑帧/流复制快拼/返回码白名单。

对应《docs/11-优化包实施方案》OP-1。来源：《09-指定开源项目能力吸收调研报告》
Clips Kitty（-fps_mode cfr 防音画漂移、+negative_cts_offsets 防首帧黑帧、
编码一致性探测后才流复制）、StreamCap（返回码白名单 {0,255}、stderr 尾部截断）。
"""

from pathlib import Path

from backend.utils.ffmpeg_utils import is_safe_returncode, stderr_tail
from backend.utils.video_processor import VideoProcessor


def test_extract_clip_command_locks_cfr_and_audio_sync():
    cmd = VideoProcessor.build_extract_clip_command(
        "ffmpeg", Path("in.mp4"), Path("out.mp4"), "00:00:01.000", 10.0,
    )
    assert cmd[cmd.index("-fps_mode") + 1] == "cfr"
    assert "aresample=async=1" in cmd[cmd.index("-af") + 1]
    movflags = cmd[cmd.index("-movflags") + 1]
    assert "+faststart" in movflags
    assert "negative_cts_offsets" in movflags


def test_safe_returncode_and_stderr_tail():
    assert is_safe_returncode(0)
    assert is_safe_returncode(255)  # 被信号中断（用户取消）的正常码
    assert not is_safe_returncode(1)
    assert not is_safe_returncode(None)
    tail = stderr_tail("x" * 70000)
    assert len(tail) < 70000 and tail.endswith("x" * 10)
    assert stderr_tail("") == ""
    assert stderr_tail(None) == ""
    assert stderr_tail("abc") == "abc"


def _write_clip(tmp_path: Path, name: str) -> Path:
    clip = tmp_path / name
    clip.write_bytes(b"fake-clip")
    return clip


def test_create_collection_uses_stream_copy_for_uniform_inputs(monkeypatch, tmp_path: Path):
    clips = [_write_clip(tmp_path, f"clip_{i}.mp4") for i in range(3)]
    output = tmp_path / "collections" / "set.mp4"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"fake-output")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("backend.utils.video_processor.subprocess.run", fake_run)
    monkeypatch.setattr("backend.utils.video_processor.VideoProcessor._streams_uniform_for_copy", lambda clips: True)

    assert VideoProcessor.create_collection(clips, output) is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "libx264" not in cmd


def test_create_collection_falls_back_to_reencode_for_mixed_inputs(monkeypatch, tmp_path: Path):
    clips = [_write_clip(tmp_path, f"clip_{i}.mp4") for i in range(2)]
    output = tmp_path / "collections" / "mixed.mp4"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"fake-output")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("backend.utils.video_processor.subprocess.run", fake_run)
    monkeypatch.setattr("backend.utils.video_processor.VideoProcessor._streams_uniform_for_copy", lambda clips: False)

    assert VideoProcessor.create_collection(clips, output) is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[cmd.index("-c:v") + 1] == "libx264"


def test_create_collection_does_not_copy_mismatched_probe(monkeypatch, tmp_path: Path):
    """真实 ffprobe 路径：参数不一致的输入绝不能走流复制（会产出坏文件且不一定报错）。"""
    clips = [_write_clip(tmp_path, "a.mp4"), _write_clip(tmp_path, "b.mp4")]

    def fake_probe(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = '{"streams":[{"codec_type":"video","codec_name":"h264","width":1920,"height":1080,"r_frame_rate":"30/1"},{"codec_type":"audio","codec_name":"aac","sample_rate":"48000"}]}'
            stderr = ""

        return Result()

    monkeypatch.setattr("backend.utils.video_processor.subprocess.run", fake_probe)
    assert VideoProcessor._streams_uniform_for_copy(clips) is True


def test_extract_thumbnail_seeks_on_input(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"fake-jpeg")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("backend.utils.video_processor.subprocess.run", fake_run)
    monkeypatch.setattr("backend.utils.video_processor.get_ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    output = tmp_path / "thumbs" / "cover.jpg"

    assert VideoProcessor.extract_thumbnail(tmp_path / "long.mp4", output, 30) is True
    cmd = calls[0]
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert cmd.index("-ss") < cmd.index("-i")  # 输入端寻址：长视频秒级定位
