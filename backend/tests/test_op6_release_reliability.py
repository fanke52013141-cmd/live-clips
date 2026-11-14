"""OP-6 发布可靠性：磁盘熔断（下载/合集输出前）+ MCP 入参路径校验。

对应《docs/11-优化包实施方案》OP-6。通知防跳过原因：tasks/notification.py
当前是纯日志桩实现，真实通道是 WebSocket 推送，防抖没有实际挂载点（见 HANDOFF）。
"""

import pytest

from backend.utils import disk_guard


def test_free_mb_returns_none_for_bogus_path():
    assert disk_guard.free_mb(None) is None  # type: ignore[arg-type]


def test_ensure_disk_space_passes_when_plenty(monkeypatch, tmp_path):
    monkeypatch.setattr(disk_guard.shutil, "disk_usage",
                        lambda root: type("D", (), {"free": 10 * 1024 * 1024 * 1024})())
    disk_guard.ensure_disk_space(tmp_path, min_free_mb=2048)  # 不抛即通过


def test_ensure_disk_space_fails_open_on_probe_error(monkeypatch, tmp_path):
    def boom(_root):
        raise OSError("no disk info")
    monkeypatch.setattr(disk_guard.shutil, "disk_usage", boom)
    disk_guard.ensure_disk_space(tmp_path, min_free_mb=2048)  # fail-open：不抛


def test_ensure_disk_space_raises_below_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(disk_guard.shutil, "disk_usage",
                        lambda root: type("D", (), {"free": 100 * 1024 * 1024})())
    with pytest.raises(RuntimeError, match="磁盘剩余空间不足"):
        disk_guard.ensure_disk_space(tmp_path, min_free_mb=2048)


def test_mcp_request_rejects_missing_video():
    from backend.mcp_server import _make_request
    with pytest.raises(ValueError, match="video_path 不存在"):
        _make_request("Z:/definitely/not/here.mp4", None, None, "default", None)


def test_mcp_request_rejects_missing_srt(tmp_path):
    from backend.mcp_server import _make_request
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(ValueError, match="srt_path 不存在"):
        _make_request(str(video), str(tmp_path / "missing.srt"), None, "default", None)


def test_mcp_request_accepts_relative_video(tmp_path, monkeypatch):
    from backend.mcp_server import _make_request
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    req = _make_request("v.mp4", None, None, "default", None)
    assert req.video == video.resolve()
