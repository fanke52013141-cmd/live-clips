"""P0-4b 本地 ASR 真实验收。

样本：Windows 本地官方 SAPI（Microsoft Huihui Desktop，zh-CN）合成的中文带货口播
（真实音频信号；非真实直播录音，结论中如实标注。真实直播样本待用户提供后补验）。
注：edge-tts 7.3.1 本机 NoAudioReceived（环境问题记录），改用完全离线的本地 SAPI。
模型：SeACo-Paraformer（ModelScope 官方源，已批准下载）。
断言：词级时间戳单调、词数充足、字符召回率>=0.85；热词命中情况如实打印。
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / ".data-dev" / "acceptance"
OUT.mkdir(parents=True, exist_ok=True)

SCRIPT = (
    "今天这个价格直接给你干到三十九块九。"
    "拍一发三，还送运费险。"
    "很多人问这个面料透不透，我现场给你看。"
    "只有今天这个机制，下播就恢复原价。"
    "欢迎新进直播间的宝宝们，点点关注不迷路。"
)
HOTWORDS = ["三十九块九", "运费险", "拍一发三"]


def synth() -> Path:
    """本地 Windows SAPI 合成（完全离线，官方本地语音）。"""
    import subprocess

    wav = OUT / "sample_zh.wav"
    ps1 = ROOT / "scripts" / "make_sample_sapi.ps1"
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert wav.exists() and wav.stat().st_size > 1000, "SAPI 合成失败"
    return wav


def norm(s: str) -> str:
    return re.sub(r"[^\w]", "", s)


def main() -> int:
    mp3 = synth()
    print(f"audio: {mp3.name} {mp3.stat().st_size} bytes", flush=True)

    from studio.engines.asr_paraformer import ParaformerASR
    from studio.engines.base import EngineContext

    ctx = EngineContext(
        profile="cpu-cloud", data_dir=OUT, models_dir=OUT / "models", gpu_available=False
    )
    asr = ParaformerASR()
    ok, reason = asr.available(ctx)
    print(f"available: {ok} {reason}", flush=True)
    assert ok, reason

    events = []
    t0 = time.time()
    tr = asr.transcribe(str(mp3), hotwords=HOTWORDS, language="zh", progress=events.append)
    dt = time.time() - t0
    text = "".join(s.text for s in tr.sentences)
    print(
        f"transcribe: {dt:.1f}s words={len(tr.words)} sentences={len(tr.sentences)} "
        f"word_level={tr.word_level} progress_events={len(events)}",
        flush=True,
    )
    print(f"text: {text}", flush=True)

    assert tr.word_level is True
    assert len(tr.words) > 20, "词数不足"
    ts = [w.start_ms for w in tr.words]
    assert ts == sorted(ts), "时间戳非单调"
    assert all(w.end_ms > w.start_ms for w in tr.words)

    ref, hyp = norm(SCRIPT), norm(text)
    hits = sum(1 for c in set(ref) if c in hyp)
    recall = hits / len(set(ref))
    print(f"char recall: {hits}/{len(set(ref))} = {recall:.2%}", flush=True)
    # FunASR 热词模式输出空格分词文本（"三 十 九 块 九"），匹配前归一化空白；
    # 识别内容本身正确，此为匹配口径修正，非放宽断言。
    text_norm = norm(text)
    hotword_hits = {kw: (norm(kw) in text_norm) for kw in HOTWORDS}
    for kw, hit in hotword_hits.items():
        print(f"hotword '{kw}': {'HIT' if hit else 'miss'}", flush=True)
    assert recall >= 0.85, f"字符召回率不足: {recall:.2%}"

    # UTF-8 JSON 落盘：控制台 GBK 乱码不影响结果核对
    import json

    result = {
        "sample": "Windows SAPI Microsoft Huihui zh-CN (TTS, not real livestream)",
        "model": tr.model,
        "engine": tr.engine,
        "word_level": tr.word_level,
        "words": len(tr.words),
        "sentences": len(tr.sentences),
        "transcribe_seconds": round(dt, 1),
        "char_recall": round(recall, 4),
        "hotword_hits": hotword_hits,
        "text": text,
        "script": SCRIPT,
        "first_words": [
            {"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms}
            for w in tr.words[:10]
        ],
    }
    (OUT / "acceptance_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("ACCEPT_OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())