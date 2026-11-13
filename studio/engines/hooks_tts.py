"""L2 TTS 钩子引擎（04 号方案 P2-5）。

主引擎 edge-tts（微软官方服务，SDK 直连）；本机已知 NoAudioReceived 故障
（P0-4b 记录）→ Windows SAPI 离线兜底（zh-CN 语音，完全本地）。
两路都失败 → 返回 None，上层跳过该级钩子（宁缺毋滥，绝不静默假音频）。
合成引擎与真实来源落 meta，可审计。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from studio.engines.base import (
    ClipPlan,
    EngineContext,
    HookAsset,
    ProgressFn,
)
from studio.pipeline.hook_decision import clamp_hook_text

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
_SAPI_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sapi_tts.ps1"


def _edge_tts(text: str, out_path: Path, voice: str) -> None:
    import edge_tts

    async def _run() -> None:
        await edge_tts.Communicate(text, voice).save(str(out_path))

    asyncio.run(_run())


def _sapi_tts(text: str, out_path: Path) -> None:
    """Windows SAPI 离线合成。中文文本写 UTF-8 文件传入（PS 脚本纯 ASCII 防 GBK）。"""
    txt = out_path.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    r = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(_SAPI_SCRIPT), "-TextFile", str(txt), "-OutWav", str(out_path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"SAPI 合成失败: {(r.stderr or r.stdout)[-300:]}")


class TtsHook:
    name = "edge_tts"
    level = 2

    def __init__(self, synth=None) -> None:
        # synth(text, out_path) 注入点（测试用）；None=真实双路
        self._synth = synth

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        try:
            import edge_tts  # noqa: F401
            return True, ""
        except ImportError:
            pass
        if sys.platform == "win32" and _SAPI_SCRIPT.is_file():
            return True, ""  # SAPI 离线兜底可用
        return False, "edge-tts 未安装且无 SAPI 兜底（非 Windows）"

    def generate(
        self, clip: ClipPlan, ctx: EngineContext, progress: ProgressFn
    ) -> HookAsset | None:
        plan = clip.hook_plan or {}
        text = (
            (plan.get("l2_tts") or {}).get("text")
            or (plan.get("l1_text") or {}).get("text")
            or clip.title
        )
        text = clamp_hook_text(text, max_chars=60)
        if text is None:
            return None
        voice = (plan.get("l2_tts") or {}).get("voice") or DEFAULT_VOICE
        out_dir = ctx.data_dir / "hooks"
        out_dir.mkdir(parents=True, exist_ok=True)

        engine_used = "injected"
        if self._synth is not None:
            out = out_dir / f"{clip.candidate_id}_tts.wav"
            self._synth(text, out)
        else:
            out = out_dir / f"{clip.candidate_id}_tts.mp3"
            try:
                _edge_tts(text, out, voice)
                engine_used = "edge-tts"
            except Exception:
                # 主路失败 → SAPI 离线兜底（仅 Windows）；非 Windows 直接放弃
                if sys.platform != "win32":
                    return None
                out = out_dir / f"{clip.candidate_id}_tts.wav"
                try:
                    _sapi_tts(text, out)
                    engine_used = "sapi_offline"
                except Exception:
                    return None
        if not out.is_file() or out.stat().st_size < 100:
            return None  # 产物为空 → 不冒充成功
        return HookAsset(
            level=2,
            kind="tts_audio",
            file_path=str(out),
            text=text,
            meta={"engine": engine_used, "voice": voice, "size_bytes": out.stat().st_size},
        )