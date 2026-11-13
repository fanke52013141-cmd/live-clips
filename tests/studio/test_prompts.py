"""提示词资源测试（P1-3 验收：CRUD/版本/引用保护/解析优先级/管线注入）。"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.models import Base, StudioMaterial, StudioProject, StudioPrompt
from studio.prompts import (
    PromptError,
    create_prompt,
    delete_prompt,
    list_prompts,
    resolve_prompt_body,
    update_prompt,
)


@pytest.fixture()
def sf(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'pr.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_create_and_list(sf):
    pid = create_prompt(sf, name="带货默认", body="挑福利爆点", live_type="ecommerce")
    rows = list_prompts(sf, live_type="ecommerce")
    assert len(rows) == 1 and rows[0]["id"] == pid and rows[0]["version"] == 1


def test_create_rejects_empty(sf):
    with pytest.raises(PromptError):
        create_prompt(sf, name="x", body="  ")


def test_update_body_bumps_version(sf):
    pid = create_prompt(sf, name="p", body="v1")
    assert update_prompt(sf, pid, body="v2") == 2
    assert update_prompt(sf, pid, body="v2") == 2  # 相同 body 不递增
    assert update_prompt(sf, pid, name="改名") == 2
    rows = list_prompts(sf)
    assert rows[0]["body"] == "v2" and rows[0]["name"] == "改名"


def test_delete_referenced_rejected(sf):
    pid = create_prompt(sf, name="p", body="b")
    with sf() as s:
        m = StudioMaterial(
            source_type="local_file", source_uri="x", fingerprint="fp", duration_ms=1000
        )
        s.add(m)
        s.flush()
        s.add(StudioProject(material_id=m.id, name="proj", live_type="ecommerce",
                            profile="cpu-cloud", prompt_id=pid))
        s.commit()
    with pytest.raises(PromptError, match="引用"):
        delete_prompt(sf, pid)
    # 未引用的可删
    pid2 = create_prompt(sf, name="p2", body="b")
    delete_prompt(sf, pid2)
    assert list_prompts(sf) and all(r["id"] != pid2 for r in list_prompts(sf))


def test_resolve_priority(sf):
    auto = create_prompt(sf, name="auto", body="按类型自动", live_type="ecommerce")
    explicit = create_prompt(sf, name="explicit", body="显式指定")
    # 显式 > 类型自动
    assert resolve_prompt_body(sf, prompt_id=explicit, live_type="ecommerce") == "显式指定"
    assert resolve_prompt_body(sf, prompt_id=None, live_type="ecommerce") == "按类型自动"
    assert resolve_prompt_body(sf, prompt_id=None, live_type="game") is None
    # 停用后不再自动命中
    update_prompt(sf, auto, active=False)
    assert resolve_prompt_body(sf, prompt_id=None, live_type="ecommerce") is None
    # 显式引用不存在 → 报错不静默
    with pytest.raises(PromptError):
        resolve_prompt_body(sf, prompt_id="no-such-id", live_type="ecommerce")


def test_pipeline_uses_custom_prompt(sf, tmp_path, monkeypatch):
    """管线集成：自定义提示词必须进入 LLM system，且铁律后缀保留。"""
    from pathlib import Path

    from studio.engines.base import EngineContext
    from studio.engines.mock import MockLLM
    from studio.engines.registry import EngineRegistry, default_factories
    from studio.pipeline.orchestrator import Pipeline
    from studio.settings import load_settings

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    captured = {}

    class CapturingLLM(MockLLM):
        def chat_json(self, system, user, schema):
            if "选段" in system or "高光" in system or "自定义" in system:
                captured["system"] = system
                captured["user"] = user
            return super().chat_json(system, user, schema)

    media = tmp_path / "m.mp4"
    media.write_bytes(b"fake")
    # 先独立会话建提示词，避免与外层写事务争 SQLite 单写锁
    pid = create_prompt(sf, name="自定义", body="自定义选段策略：只要价格爆点。",
                        live_type="ecommerce")
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri=str(media),
                           file_path=str(media), fingerprint="fp", duration_ms=60000)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="p", live_type="ecommerce",
                          profile="cpu-cloud", prompt_id=pid,
                          intent="只要讲价格的", background="主播卖耳机")
        s.add(p)
        s.commit()
        proj_id = p.id

    settings = load_settings(Path(__file__).resolve().parents[2] / "profiles",
                             "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    factories = default_factories()
    factories["mock_llm"] = CapturingLLM
    reg = EngineRegistry(settings, ctx, factories=factories)
    result = Pipeline(settings, ctx, sf, registry=reg).run(proj_id)

    assert result.status == "candidates_ready"
    assert "自定义选段策略" in captured["system"]
    assert "quote 必须是转写原文子串" in captured["system"]  # 铁律后缀保留
    assert "只要讲价格的" in captured["user"]  # intent 注入
    assert "主播卖耳机" in captured["user"]  # background 注入