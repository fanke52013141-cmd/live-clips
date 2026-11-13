"""CAM++ 说话人分离适配（04 号方案 P1-7，可选开关，默认关）。

- 模型：iic/speech_campplus_sv_zh-cn_16k-common（funasr 官方名映射表核对）。
- 懒加载 funasr；模型未下载显式报原因，不静默下载（下载需用户批准）。
- 【未核实】FunASR cam++ 的 generate 输出段格式（start/end/spk 字段名与单位）
  以官方文档/实测为准；解析按「秒或毫秒自适应 + 显式报错」实现，真实验收待批准后补。
- 分离结果按时间重叠映射到词/句，不改时间戳。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from studio.engines.base import (
    EngineContext,
    ProgressEvent,
    ProgressFn,
    Sentence,
    TranscriptResult,
    Word,
)

_MODEL_ID = "iic/speech_campplus_sv_zh-cn_16k-common"


def _to_ms(v: float | int) -> int:
    """秒/毫秒自适应：>100000 视为毫秒，否则视为秒（2h 直播=7.2e6 ms）。"""
    return int(v) if v > 100_000 else int(float(v) * 1000)


class CamppDiarizer:
    def __init__(
        self,
        name: str = "campp_cpu",
        device: str = "cpu",
        model_factory: Callable | None = None,  # 测试注入
        cache_dir: str | Path | None = None,
    ) -> None:
        self.name = name
        self.device = device
        self._model_factory = model_factory
        self._cache_dir = cache_dir

    def _cache_root(self) -> Path:
        if self._cache_dir is not None:
            return Path(self._cache_dir)
        env = os.environ.get("MODELSCOPE_CACHE")
        if env:
            return Path(env)
        return Path.home() / ".cache" / "modelscope"

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        if self._model_factory is not None:
            return True, ""
        try:
            import funasr  # noqa: F401
        except ImportError:
            return False, "funasr 未安装"
        model_dir = self._cache_root() / "models" / _MODEL_ID.replace("/", "--") / "snapshots" / "master"
        if not model_dir.exists():
            return False, f"模型未下载: {model_dir}（需用户批准后下载）"
        return True, ""

    def assign_speakers(
        self, tr: TranscriptResult, audio_path: str, progress: ProgressFn
    ) -> TranscriptResult:
        if self._model_factory is not None:
            model = self._model_factory()
        else:
            os.environ["MODELSCOPE_CACHE"] = str(self._cache_root())
            from funasr import AutoModel

            model = AutoModel(model=_MODEL_ID, device=self.device, disable_update=True)
        progress(ProgressEvent("diarizing", 0, 1, "段", message="CAM++ 说话人分离中"))
        res = model.generate(input=audio_path)
        segments = self._parse_segments(res)
        if not segments:
            raise RuntimeError("CAM++ 未返回说话人段（不伪造标签）")

        def label(start_ms: int, end_ms: int) -> str | None:
            best, best_overlap = None, 0
            for s0, s1, spk in segments:
                overlap = min(end_ms, s1) - max(start_ms, s0)
                if overlap > best_overlap:
                    best, best_overlap = spk, overlap
            return best

        words = tuple(
            Word(w.text, w.start_ms, w.end_ms, speaker=label(w.start_ms, w.end_ms) or w.speaker)
            for w in tr.words
        )
        sentences = tuple(
            Sentence(s.text, s.start_ms, s.end_ms, speaker=label(s.start_ms, s.end_ms) or s.speaker)
            for s in tr.sentences
        )
        progress(ProgressEvent("diarizing", 1, 1, "段", message="说话人分离完成"))
        return TranscriptResult(
            words=words, sentences=sentences, engine=tr.engine, model=tr.model,
            params_hash=tr.params_hash, word_level=tr.word_level,
            degraded_from=tr.degraded_from,
        )

    @staticmethod
    def _parse_segments(res) -> list[tuple[int, int, str]]:
        """【未核实】按常见格式解析：[{start,end,spk}]；不符显式报错。"""
        item = res[0] if isinstance(res, (list, tuple)) and res else res
        if isinstance(item, dict):
            item = item.get("segments", item.get("sentences", []))
        if not isinstance(item, (list, tuple)):
            raise RuntimeError(f"CAM++ 输出格式不符（待实测核对）: {type(item)}")
        out: list[tuple[int, int, str]] = []
        for seg in item:
            if not isinstance(seg, dict):
                raise RuntimeError(f"CAM++ 段格式不符: {seg!r}")
            spk = seg.get("spk", seg.get("speaker"))
            if "start_ms" in seg or "end_ms" in seg:
                # 显式毫秒字段：直接按毫秒，不走启发式
                start, end = seg.get("start_ms"), seg.get("end_ms")
                if start is None or end is None or spk is None:
                    raise RuntimeError(f"CAM++ 段缺字段: {seg!r}")
                out.append((int(start), int(end), str(spk)))
                continue
            start = seg.get("start")
            end = seg.get("end")
            if start is None or end is None or spk is None:
                raise RuntimeError(f"CAM++ 段缺字段: {seg!r}")
            out.append((_to_ms(start), _to_ms(end), str(spk)))
        return out