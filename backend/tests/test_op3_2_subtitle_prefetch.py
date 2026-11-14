"""OP-3.2 字幕预取：灰度开关、预取线程成功/失败/竞态、等待消费与回退。

对应《docs/11-优化包实施方案》OP-3.2。生产侧消费点是
`process_project_sync` 的 R3 断点（raw/input.srt 存在即跳过转写），
本文件覆盖预取模块自身的行为与等待语义。
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest

import backend.services.studio.jobs  # noqa: F401  # 线程内 from-import 需要 jobs 已加载
from backend.services import subtitle_prefetch


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv('AUTOCLIP_SEGMENTED_IMPORT', '1')


def _fake_transcribe(monkeypatch, tmp_path: Path, *, srt_text: str = "1\n00:00:01,000 --> 00:00:03,000\n内容\n",
                     fail: bool = False):
    """替换下载/转码/转写三步为可断言的假实现。"""
    calls = {}

    def fake_download(url, options):
        calls['url'] = url
        calls['format'] = options.get('format')
        (tmp_path / 'prefetch_audio.m4a').write_bytes(b'fake-audio')

    def fake_convert(cmd, **kwargs):
        calls['ffmpeg'] = cmd
        Path(cmd[-1]).write_bytes(b'fake-wav')

        class Result:
            returncode = 0
            stdout = ''
            stderr = ''

        return Result()

    def fake_asr(video_path, output_path=None, method='auto', language='auto', **kwargs):
        calls['asr_input'] = Path(video_path)
        if fail:
            raise RuntimeError('asr boom')
        Path(output_path).write_text(srt_text, encoding='utf-8')
        return Path(output_path)

    monkeypatch.setattr('backend.utils.download_recovery.download_with_recovery', fake_download)
    monkeypatch.setattr('subprocess.run', fake_convert)  # worker 内局部 import subprocess，全局替换
    monkeypatch.setattr('backend.utils.speech_recognizer.generate_subtitle_for_video', fake_asr)
    return calls


def _wait_marker(folder: Path, want: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = subtitle_prefetch.read_status(folder)
        if status and status.get('status') == want:
            return status
        time.sleep(0.05)
    raise AssertionError(f'marker 未达到 {want}: {subtitle_prefetch.read_status(folder)}')


def test_flag_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.delenv('AUTOCLIP_SEGMENTED_IMPORT', raising=False)
    assert subtitle_prefetch.flag_enabled() is False
    assert subtitle_prefetch.maybe_start('p1', tmp_path, 'https://www.bilibili.com/video/BV1xx411c7mD', None) is None
    assert subtitle_prefetch.read_status(tmp_path) is None


def test_prefetch_lands_input_srt_and_stamps_origin(monkeypatch, tmp_path):
    calls = _fake_transcribe(monkeypatch, tmp_path)
    (tmp_path / 'input.mp4').write_bytes(b'v')
    thread = subtitle_prefetch.maybe_start('p1', tmp_path, 'https://www.bilibili.com/video/BV1xx411c7mD', None)
    assert thread is not None
    thread.join(timeout=10)
    status = _wait_marker(tmp_path, 'done')
    assert status['used'] is True
    assert (tmp_path / 'input.srt').exists()
    assert (tmp_path / '.prefetch_audio.wav').exists() is False  # 中间音频已清理
    assert 'bestaudio' in calls['format']  # 只拉音频小流
    assert calls['asr_input'].suffix == '.wav'
    # origin 盖章与平台字幕路径同构（record_srt_origin 在 pipeline_resume 里）
    stamp = json.loads((tmp_path.parent / 'metadata' / 'pipeline_stamp.json').read_text(encoding='utf-8')) \
        if (tmp_path.parent / 'metadata' / 'pipeline_stamp.json').exists() else None
    assert stamp is None or stamp.get('srt_origin') == 'asr'


def test_prefetch_never_starts_when_input_srt_already_exists(monkeypatch, tmp_path):
    _fake_transcribe(monkeypatch, tmp_path)
    (tmp_path / 'input.srt').write_text('attached', encoding='utf-8')  # 用户附带字幕已就位
    thread = subtitle_prefetch.maybe_start('p1', tmp_path, 'https://www.bilibili.com/video/BV1xx411c7mD', None)
    assert thread is None  # 不预取、不覆盖


def test_write_prefetch_srt_discards_when_target_appeared(tmp_path):
    """竞态保护：预取运行期间平台字幕先落盘 → 预取结果丢弃，不覆盖。"""
    (tmp_path / 'input.srt').write_text('platform', encoding='utf-8')
    produced = tmp_path / '.prefetch_input.srt'
    produced.write_text('1\n00:00:01,000 --> 00:00:02,000\n预取\n', encoding='utf-8')
    assert subtitle_prefetch._write_prefetch_srt(tmp_path, produced, None) is False
    assert (tmp_path / 'input.srt').read_text(encoding='utf-8') == 'platform'
    assert not produced.exists()  # 已丢弃

    # 目标不存在时正常落盘并清理 produced
    other = tmp_path / 'raw2'
    other.mkdir()
    produced2 = other / '.prefetch_input.srt'
    produced2.write_text('1\n00:00:01,000 --> 00:00:02,000\n预取\n', encoding='utf-8')
    assert subtitle_prefetch._write_prefetch_srt(other, produced2, None) is True
    assert (other / 'input.srt').exists() and not produced2.exists()


def test_prefetch_failure_marks_failed_and_never_raises(monkeypatch, tmp_path):
    _fake_transcribe(monkeypatch, tmp_path, fail=True)
    thread = subtitle_prefetch.maybe_start('p1', tmp_path, 'https://www.bilibili.com/video/BV1xx411c7mD', None)
    thread.join(timeout=10)
    status = _wait_marker(tmp_path, 'failed')
    assert 'asr boom' in status['error']
    assert not (tmp_path / 'input.srt').exists()


def test_wait_for_prefetch_returns_srt_when_done(tmp_path):
    (tmp_path / 'input.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\nx\n', encoding='utf-8')
    subtitle_prefetch._write_status(tmp_path, status='done', used=True, finished_at=time.time())
    assert subtitle_prefetch.wait_for_prefetch(tmp_path, timeout=1) == tmp_path / 'input.srt'


def test_wait_for_prefetch_dead_pid_returns_none(tmp_path):
    subtitle_prefetch._write_status(tmp_path, status='running', pid=999999999, started_at=time.time())
    assert subtitle_prefetch.wait_for_prefetch(tmp_path, timeout=1, poll=0.1) is None
    assert subtitle_prefetch.read_status(tmp_path)['status'] == 'failed'  # 标记为失败供诊断


def test_wait_for_prefetch_timeout_falls_back(tmp_path):
    started = threading.Event()

    def stuck_runner(project_id, folder, url, browser):
        subtitle_prefetch._write_status(folder, status='running', pid=os.getpid(), started_at=time.time())
        started.set()
        time.sleep(5)
        subtitle_prefetch._write_status(folder, status='done', used=True, finished_at=time.time())

    thread = threading.Thread(target=stuck_runner, args=('p', tmp_path, 'u', None), daemon=True)
    thread.start()
    started.wait(timeout=5)
    assert subtitle_prefetch.wait_for_prefetch(tmp_path, timeout=0.5, poll=0.1) is None  # 超时回退
    thread.join(timeout=1)


def test_wait_for_prefetch_without_marker_is_none(tmp_path):
    assert subtitle_prefetch.wait_for_prefetch(tmp_path, timeout=0.5) is None


def test_prefetch_skips_when_platform_subtitle_available(monkeypatch, tmp_path):
    """probe 发现创作者字幕：预取让位（marker=skipped），不下载音频。"""
    downloads = []
    _fake_transcribe(monkeypatch, tmp_path)  # 万一 probe 判定继续，下载也已被 mock
    monkeypatch.setattr('backend.utils.download_recovery.download_with_recovery',
                        lambda url, options: downloads.append(url))
    monkeypatch.setattr('backend.services.studio.jobs.inspect_url',
                        lambda url, browser=None: {'language': 'zh', 'subtitles': {'zh-Hans': []}})
    thread = subtitle_prefetch.maybe_start('p1', tmp_path, 'https://www.bilibili.com/video/BV1xx411c7mD', None)
    thread.join(timeout=10)
    status = _wait_marker(tmp_path, 'skipped')
    assert status['reason'] == 'platform_subtitle_available'
    assert downloads == []
