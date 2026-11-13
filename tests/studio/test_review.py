"""复审环测试（P1-2 验收：judge→repair→rejudge、边界代码级校验、回执、开关）。"""
from studio.engines.base import Sentence, TranscriptResult, Word
from studio.pipeline.review import review_candidates


def make_tr():
    """3 句：0-5000 / 5400-9900 / 10300-18300。"""
    texts = ["今天价格三十九块九。", "拍一发三送运费险。", "只有今天这个机制下播恢复原价啊。"]
    words, sentences = [], []
    t = 0
    for text in texts:
        start = t
        for ch in text:
            words.append(Word(ch, t, t + 500))
            t += 500
        sentences.append(Sentence(text, start, t))
        t += 400
    return TranscriptResult(
        words=tuple(words), sentences=tuple(sentences),
        engine="t", model="t", params_hash="t", word_level=True,
    )


def cand(start=0, end=9900):
    return {
        "start_ms": start, "end_ms": end,
        "quote": "今天价格三十九块九", "title": "复审候选",
        "scores": {"hook": 9, "flow": 8, "value": 9, "trend": 7},
    }


OK = {"standalone": True, "score": 0.9, "reason": "结构完整"}
BAD = {"standalone": False, "score": 0.3, "reason": "半截话，缺收尾"}


class ScriptedLLM:
    name = "scripted"

    def __init__(self, judge_results, repair=None):
        self.judge_results = list(judge_results)
        self.repair = repair
        self.calls = []

    def available(self, ctx):
        return True, ""

    def chat_json(self, system, user, schema):
        self.calls.append(system)
        if "切片复审官" in system:
            return self.judge_results.pop(0)
        if "边界修复器" in system:
            assert self.repair is not None, "不应触发 repair"
            return self.repair
        raise AssertionError(f"未知 system: {system}")


DR = (1000, 60000)  # 放宽时长，聚焦复审逻辑


def test_judge_pass_keeps_and_logs():
    llm = ScriptedLLM([OK])
    kept, disc, log = review_candidates([cand()], make_tr(), llm, live_type="ecommerce", duration_range=DR)
    assert len(kept) == 1 and disc == []
    assert log[0]["verdict"] == "pass"
    assert log[0]["rounds"][0]["action"] == "judge"
    assert len(llm.calls) == 1


def test_repair_then_rejudge_pass():
    llm = ScriptedLLM([BAD, OK], repair={"start_ms": 0, "end_ms": 18300, "reason": "补全收尾"})
    kept, disc, log = review_candidates([cand()], make_tr(), llm, live_type="ecommerce", duration_range=DR)
    assert len(kept) == 1 and disc == []
    c = kept[0]
    assert c["end_ms"] == 18300
    assert c["repair"] == {"old_start_ms": 0, "old_end_ms": 9900}
    assert log[0]["verdict"] == "repaired"
    assert [r["action"] for r in log[0]["rounds"]] == ["judge", "repair", "rejudge"]


def test_repair_non_sentence_boundary_rejected():
    llm = ScriptedLLM([BAD], repair={"start_ms": 0, "end_ms": 12345, "reason": "x"})
    kept, disc, log = review_candidates([cand()], make_tr(), llm, live_type="ecommerce", duration_range=DR)
    assert kept == []
    assert "未落在句界" in disc[0]["reason"]
    assert log[0]["verdict"] == "repair_rejected"


def test_repair_beyond_adjacent_sentence_rejected():
    # 原 start=0（句0），修复 start=10300（句2）距离 2 > 1 → 拒绝
    llm = ScriptedLLM([BAD], repair={"start_ms": 10300, "end_ms": 18300, "reason": "x"})
    kept, disc, _ = review_candidates([cand()], make_tr(), llm, live_type="ecommerce", duration_range=DR)
    assert kept == [] and "相邻句" in disc[0]["reason"]


def test_repair_duration_violation_rejected():
    llm = ScriptedLLM([BAD], repair={"start_ms": 0, "end_ms": 18300, "reason": "x"})
    kept, disc, _ = review_candidates(
        [cand()], make_tr(), llm, live_type="ecommerce", duration_range=(1000, 10000)
    )
    assert kept == [] and "时长不符" in disc[0]["reason"]


def test_rejudge_still_fails_discarded():
    llm = ScriptedLLM([BAD, BAD], repair={"start_ms": 0, "end_ms": 18300, "reason": "x"})
    kept, disc, log = review_candidates([cand()], make_tr(), llm, live_type="ecommerce", duration_range=DR)
    assert kept == []
    assert "复审仍不合格" in disc[0]["reason"]
    assert log[0]["verdict"] == "fail"


def test_disabled_zero_llm_calls():
    llm = ScriptedLLM([])
    kept, disc, log = review_candidates(
        [cand()], make_tr(), llm, live_type="ecommerce", enabled=False
    )
    assert len(kept) == 1 and disc == [] and log == []
    assert llm.calls == []


def test_low_score_standalone_true_still_fails():
    verdict = {"standalone": True, "score": 0.5, "reason": "勉强"}
    llm = ScriptedLLM([verdict, OK], repair={"start_ms": 0, "end_ms": 18300, "reason": "x"})
    kept, _, log = review_candidates([cand()], make_tr(), llm, live_type="ecommerce", duration_range=DR)
    assert len(kept) == 1 and log[0]["verdict"] == "repaired"