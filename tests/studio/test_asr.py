"""ASR 引擎适配测试（P0-4 验收：探测/降级原因/词级映射/云不伪造时间戳）。

真实模型转写验收（10min 中文样本）需要：
- 本地引擎：批准下载模型权重后实测
- 云引擎：用户提供 DASHSCOPE_API_KEY 后实测
本文件用注入 fake 验证适配逻辑，不依赖网络与大模型。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio.engines.asr_bailian import BailianASR
from studio.engines.asr_paraformer import ParaformerASR
from studio.engines.asr_whisper import WhisperASR
from studio.engines.base import EngineContext, noop_progress


def make_ctx(tmp_path: Path, env: dict | None = None) -> EngineContext:
    return EngineContext(
        profile="cpu-cloud",
        data_dir=tmp_path,
        models_dir=tmp_path / "models",
        gpu_available=False,
        env=env or {},
    )


# ---------- Bailian ----------

def test_bailian_unavailable_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    ok, reason = BailianASR().available(make_ctx(tmp_path))
    assert ok is False
    assert "DASHSCOPE_API_KEY" in reason


def test_bailian_available_with_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    ok, reason = BailianASR().available(make_ctx(tmp_path, {"DASHSCOPE_API_KEY": "sk-test"}))
    assert ok is True and reason == ""


def test_bailian_sentence_level_with_fake_call(tmp_path):
    fake_resp = {
        "sentences": [
            {"text": "今天三十九块九", "start_ms": 0, "end_ms": 2000},
            {"text": "拍一发三", "start_ms": 2400, "end_ms": 3600},
        ]
    }
    asr = BailianASR(call_fn=lambda path, model, key: fake_resp)
    tr = asr.transcribe("x.wav", hotwords=[], language="zh", progress=noop_progress)
    assert tr.word_level is False  # 云端仅句级（05 号笔记）
    assert tr.words == ()
    assert len(tr.sentences) == 2
    assert tr.sentences[1].start_ms == 2400


def test_bailian_refuses_to_fabricate_timestamps(tmp_path):
    asr = BailianASR(call_fn=lambda path, model, key: {"text": "没有时间戳"})
    with pytest.raises(ValueError, match="不伪造时间戳"):
        asr.transcribe("x.wav", hotwords=[], language="zh", progress=noop_progress)


# ---------- Whisper ----------

def test_whisper_unavailable_reports_reason(tmp_path):
    ok, reason = WhisperASR().available(make_ctx(tmp_path))
    # 本 venv 未装 faster-whisper；若环境已装则应报模型未下载。两者都必须给原因。
    if not ok:
        assert reason and ("未安装" in reason or "未下载" in reason)


class _FakeWord:
    def __init__(self, word, start, end):
        self.word, self.start, self.end = word, start, end


class _FakeSeg:
    def __init__(self, text, start, end, words):
        self.text, self.start, self.end, self.words = text, start, end, words


class _FakeWhisperModel:
    def transcribe(self, path, **kwargs):
        segs = [
            _FakeSeg("你好", 0.0, 1.0, [_FakeWord("你", 0.0, 0.5), _FakeWord("好", 0.5, 1.0)]),
            _FakeSeg("世界", 1.5, 2.5, [_FakeWord("世", 1.5, 2.0), _FakeWord("界", 2.0, 2.5)]),
        ]
        return iter(segs), SimpleNamespace(duration=10)


def test_whisper_word_level_mapping_and_progress(tmp_path):
    events = []
    asr = WhisperASR(model_factory=lambda: _FakeWhisperModel())
    ok, _ = asr.available(make_ctx(tmp_path))
    assert ok is True  # 注入 factory 时跳过包/模型探测
    tr = asr.transcribe("x.wav", hotwords=["测试"], language="zh", progress=events.append)
    assert tr.word_level is True
    assert [w.text for w in tr.words] == ["你", "好", "世", "界"]
    assert tr.words[1].start_ms == 500 and tr.words[1].end_ms == 1000
    assert tr.sentences[1].start_ms == 1500
    assert events[-1].done == 2 and events[-1].total == 10 and events[-1].unit == "秒"
    assert tr.params_hash  # 热词参与 params_hash


# ---------- Paraformer ----------

def test_paraformer_unavailable_reports_reason(tmp_path):
    ok, reason = ParaformerASR().available(make_ctx(tmp_path))
    if not ok:
        assert reason and ("未安装" in reason or "未下载" in reason)


class _FakeParaformerModel:
    def __init__(self):
        self.kwargs = None

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return [
            {
                "text": "你好世界",
                "timestamp": [[0, 200], [200, 400], [400, 600], [600, 800]],
            }
        ]


def test_paraformer_char_timestamps_and_hotwords(tmp_path):
    fake = _FakeParaformerModel()
    asr = ParaformerASR(model_factory=lambda: fake)
    tr = asr.transcribe("x.wav", hotwords=["切片", "福利"], language="zh", progress=noop_progress)
    assert tr.word_level is True
    assert [w.text for w in tr.words] == ["你", "好", "世", "界"]
    assert tr.words[-1].end_ms == 800
    assert tr.sentences[0].start_ms == 0 and tr.sentences[0].end_ms == 800
    assert fake.kwargs["hotword"] == "切片 福利"  # 热词注入 SeACo