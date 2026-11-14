"""字幕预取（OP-3.2）：下载视频的同时，后台先用"音频小流"转出字幕。

机制（《11-优化包实施方案》OP-3.2，来源 StreamCap 分段并行 + bili-shadowreplay
"归档即回放源"的免等待思想）：

1. `maybe_start` 在视频下载开始前拉起一个守护线程——先 probe 平台字幕
   （有创作者字幕就不预取，避免白花一次 ASR），再用 yt-dlp 只拉音频轨
   （几百 MB 的视频 → 几十 MB 的 m4a），转 16 kHz mono wav 后交给现有
   转写服务（本地 Whisper / SenseVoice / 云端百炼均可），产出 `raw/input.srt`。
2. 生产阶段（run_content → process_project_sync 的 R3 断点）本来就会在
   `raw/input.srt` 存在时跳过转写——预取完成即"免费跳过"；未完成时由
   `wait_for_prefetch` 等它收尾，失败则回退到对整段视频的普通转写。

灰度与回退：`AUTOCLIP_SEGMENTED_IMPORT` 未开启时一切按原路径走；
预取线程任何失败只写 marker、绝不影响下载与生产主流程。
状态以 `raw/.subtitle_prefetch.json` 落盘（进程重启后 pid 失活按失败处理）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MARKER_FILE = '.subtitle_prefetch.json'
FLAG_ENV = 'AUTOCLIP_SEGMENTED_IMPORT'
_WAIT_POLL_SEC = 2.0
_WAIT_TIMEOUT_SEC = 3600.0  # 预取仍在跑的兜底上限：超过则回退整段转写（罕见）

_TRUTHY = frozenset({'1', 'on', 'true', 'yes'})


def flag_enabled() -> bool:
    """灰度开关：AUTOCLIP_SEGMENTED_IMPORT=1/on/true/yes 时启用，默认关闭。"""
    return os.getenv(FLAG_ENV, '').strip().lower() in _TRUTHY


def marker_path(folder: Path) -> Path:
    return Path(folder) / MARKER_FILE


def read_status(folder: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(marker_path(folder).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_status(folder: Path, **fields: Any) -> None:
    try:
        data = read_status(folder) or {}
        data.update(fields)
        marker_path(folder).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass  # 状态落盘失败不阻塞主流程


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover - psutil 是 requirements 固定依赖
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _write_prefetch_srt(folder: Path, produced: Path, video: Optional[Path]) -> bool:
    """预取结果原子落盘：input.srt 已存在（用户附带/平台字幕先到）则丢弃预取结果。"""
    target = folder / 'input.srt'
    if target.exists():
        produced.unlink(missing_ok=True)
        return False
    produced.replace(target)
    try:
        from backend.services.studio import pipeline_resume
        metadata_dir = folder.parent / 'metadata'
        pipeline_resume.record_srt_origin(metadata_dir, 'asr', video=video)
    except Exception:  # noqa: BLE001 - 盖章失败不改变字幕有效性
        logger.debug('prefetch srt_origin stamp skipped', exc_info=True)
    return True


def _prefetch_worker(project_id: str, folder: Path, url: str, browser: Optional[str]) -> None:
    """守护线程主体：probe → 音频小流 → wav → ASR → 原子落盘。任何失败只写 marker。"""
    _write_status(folder, status='running', project_id=project_id, pid=os.getpid(),
                  started_at=time.time())
    try:
        from backend.utils.download_recovery import download_with_recovery
        from backend.utils.ffmpeg_utils import get_ffmpeg_path, ytdlp_js_runtimes
        import subprocess

        # 1. 平台字幕 probe：创作者字幕存在时平台路径更优，预取让位（也避免竞态覆盖）
        try:
            from backend.services.studio.jobs import _platform_subtitle_code, inspect_url
            info = inspect_url(url, browser)
            if _platform_subtitle_code(info):
                _write_status(folder, status='skipped', reason='platform_subtitle_available',
                              finished_at=time.time())
                return
        except Exception as error:  # noqa: BLE001 - probe 失败继续预取（宁可多花一次 ASR）
            logger.debug('prefetch subtitle probe failed: %s', type(error).__name__)

        # 2. 只拉音频轨（体积约为视频的 1/10，先到先转）
        audio_options = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': str(folder / 'prefetch_audio.%(ext)s'),
            'noplaylist': True, 'quiet': True, 'no_warnings': True,
            'socket_timeout': 30, 'retries': 2,
            **ytdlp_js_runtimes(),
        }
        if browser:
            audio_options['cookiesfrombrowser'] = (browser,)
        download_with_recovery(url, audio_options)
        audio = next((p for p in folder.iterdir() if p.name.startswith('prefetch_audio.')
                      and p.suffix.lower() in ('.m4a', '.mp3', '.webm', '.ogg', '.aac')), None)
        if audio is None:
            raise FileNotFoundError('prefetch audio download produced no file')

        # 3. 转 16 kHz mono wav——与生产路径同一 ASR 输入规格（T2）
        wav = folder / '.prefetch_audio.wav'
        convert = subprocess.run(
            [get_ffmpeg_path(), '-y', '-i', str(audio), '-vn',
             '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', str(wav)],
            capture_output=True, text=True, timeout=1800,
        )
        if convert.returncode != 0 or not wav.is_file():
            raise RuntimeError(f'prefetch audio convert failed: {(convert.stderr or "")[-300:]}')

        # 4. 复用现有转写服务（本地/云端路由不变），进度沿用 SUBTITLE 的进度通道
        from backend.utils.speech_recognizer import generate_subtitle_for_video
        from backend.utils.transcription_progress import reporting
        from backend.services.simple_pipeline_adapter import TranscriptionProgress
        produced = folder / '.prefetch_input.srt'
        try:
            with reporting(TranscriptionProgress(project_id)):
                generate_subtitle_for_video(wav, output_path=produced, method='auto', language='auto')
        finally:
            TranscriptionProgress.finish_studio(project_id)
        if not produced.exists():
            raise RuntimeError('prefetch transcription produced no srt')

        # 5. 原子落盘 + 盖章 + 清理中间音频
        video = next((p for p in folder.iterdir()
                      if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.m4v')
                      and p.name.startswith('input')), None)
        landed = _write_prefetch_srt(folder, produced, video)
        wav.unlink(missing_ok=True)
        audio.unlink(missing_ok=True)
        _write_status(folder, status='done', used=landed, finished_at=time.time())
        logger.info('字幕预取完成 project=%s landed=%s', project_id, landed)
    except Exception as error:  # noqa: BLE001 - 预取失败绝不外溢：生产路径会回退整段转写
        logger.warning('字幕预取失败（将回退普通转写）: %s %s', type(error).__name__, str(error)[:300])
        _write_status(folder, status='failed', error=f'{type(error).__name__}: {str(error)[:300]}',
                      finished_at=time.time())


def maybe_start(project_id: str, folder: Path, url: str, browser: Optional[str]) -> Optional[threading.Thread]:
    """视频下载开始前调用：灰度开启、无既有字幕、无重复预取时拉起守护线程。

    返回线程对象（测试可 join）；未启动返回 None。download() 不等待它。
    """
    if not flag_enabled():
        return None
    folder = Path(folder)
    if (folder / 'input.srt').exists():
        return None  # 用户附带字幕已就位，无需预取
    status = read_status(folder)
    if status and status.get('status') == 'running' and _pid_alive(status.get('pid')):
        return None  # 已有预取在跑（重试/重复导入），不叠加
    thread = threading.Thread(
        target=_prefetch_worker, name=f'subtitle-prefetch-{project_id}',
        args=(project_id, folder, url, browser), daemon=True,
    )
    thread.start()
    logger.info('字幕预取已启动 project=%s', project_id)
    return thread


def wait_for_prefetch(folder: Path, *, timeout: float = _WAIT_TIMEOUT_SEC,
                      poll: float = _WAIT_POLL_SEC) -> Optional[Path]:
    """生产阶段调用：预取完成 → 返回 `input.srt`；失败/未启动/超时 → None（回退普通转写）。

    进程重启后 marker 里的 pid 已失活时按失败处理，不会无限等。
    """
    status = read_status(Path(folder))
    if status is None:
        return None
    deadline = time.monotonic() + max(1.0, timeout)
    while True:
        status = read_status(Path(folder)) or {}
        state = status.get('status')
        if state == 'running':
            if not _pid_alive(status.get('pid')):
                logger.warning('字幕预取进程已消失（重启/崩溃），回退普通转写')
                _write_status(folder, status='failed', error='prefetch pid gone', finished_at=time.time())
                return None
            if time.monotonic() >= deadline:
                logger.warning('字幕预取等待超时（%.0fs），回退普通转写', timeout)
                return None
            time.sleep(poll)
            continue
        if state == 'done' and status.get('used') and (Path(folder) / 'input.srt').is_file():
            return Path(folder) / 'input.srt'
        if state == 'done' and not status.get('used'):
            return None  # 预取结果被平台字幕/附带字幕抢先落盘：沿用那条路
        return None  # failed / skipped / 未知状态
