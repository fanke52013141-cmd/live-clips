"""生成素材渲染集成测试（P3：B-roll/生视频前置/mock 禁入/AIGC 角标，真实 lavfi 媒体）。"""
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.engines.base import ClipPlan, EngineContext, RenderPlan, Word
from studio.engines.registry import EngineRegistry
from studio.engines.render_ffmpeg import FfmpegRenderer
from studio.media import ffmpeg_available, make_lavfi_fixture
from studio.models import (
    Base,
    StudioArtifact,
    StudioMaterial,
    StudioProject,
)
from studio.pipeline.orchestrator import Pipeline
from studio.review_service import adopt_candidate
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
pytestmark_media = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")


def words_2_to_8():
    text = "价格三十九块九拍一发三"
    out = []
    t = 2000
    i = 0
    while t < 8000:
        out.append(Word(text[i % len(text)], t, t + 300))
        t += 300
        i += 1
    return tuple(out)


def make_png(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=red:s=640x360", "-frames:v", "1", str(path)],
        capture_output=True, check=True,
    )
    return path


def plan(tmp_path, src, **kw):
    defaults = dict(
        clip=ClipPlan("c1", 2000, 8000, "标题", "价格"),
        platform="douyin", portrait=False, subtitles=True,
        out_path=str(tmp_path / "out.mp4"), source_path=str(src), words=words_2_to_8(),
    )
    defaults.update(kw)
    return RenderPlan(**defaults)


@pytestmark_media
def test_broll_image_prepend(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    img = make_png(tmp_path / "broll.png")
    art = FfmpegRenderer().render(
        plan(tmp_path, src, broll_image=str(img), aigc_badge=True), lambda ev: None
    )
    # 6s 切片 + 2s B-roll = 8s
    assert abs(art.meta["selfcheck"]["duration_s"] - 8.0) <= 0.3
    assert art.meta["hooks"]["broll_image"] == str(img)
    assert art.meta["hooks"]["prepend_dur_s"] == 2.0
    assert art.meta["hooks"]["prepend_audio"] == "silence"  # 生图无音轨→静音
    assert art.meta["hooks"]["aigc_labeled"] is True
    ass = Path(art.file_path).with_name("subs_c1.ass").read_text(encoding="utf-8")
    assert "Style: AIGC," in ass and "AI生成" in ass
    # 字幕整体偏移 2s（首词事件 0:00:02.00）
    assert "Dialogue: 0,0:00:02.00," in ass


@pytestmark_media
def test_hook_video_prepend(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    hook = make_lavfi_fixture(tmp_path / "hook.mp4", seconds=2)
    art = FfmpegRenderer().render(
        plan(tmp_path, src, hook_video=str(hook), aigc_badge=True), lambda ev: None
    )
    assert abs(art.meta["selfcheck"]["duration_s"] - 8.0) <= 0.3
    assert art.meta["hooks"]["hook_video"] == str(hook)
    assert art.meta["hooks"]["prepend_audio"] == "source"  # lavfi 生视频带音轨→原声（W-3）


@pytestmark_media
def test_broll_with_cuts_combined(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    img = make_png(tmp_path / "broll.png")
    art = FfmpegRenderer().render(
        plan(tmp_path, src, broll_image=str(img), cuts=((4000, 5000),), aigc_badge=True),
        lambda ev: None,
    )
    # (6-1)s + 2s = 7s：段式剪辑 + 前置段组合
    assert abs(art.meta["selfcheck"]["duration_s"] - 7.0) <= 0.3


@pytestmark_media
def test_badge_without_subtitles(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    img = make_png(tmp_path / "broll.png")
    art = FfmpegRenderer().render(
        plan(tmp_path, src, broll_image=str(img), subtitles=False, aigc_badge=True),
        lambda ev: None,
    )
    ass = Path(art.file_path).with_name("subs_c1.ass").read_text(encoding="utf-8")
    assert "AI生成" in ass  # 无字幕也要烧角标


@pytestmark_media
def test_mock_asset_rejected(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    fake = tmp_path / "mock.mp4"
    fake.write_bytes(b"MOCK_VIDEO_HOOK placeholder")
    with pytest.raises(RuntimeError, match="mock"):
        FfmpegRenderer().render(plan(tmp_path, src, hook_video=str(fake)), lambda ev: None)


# ---------- 编排器级：mock 禁入 + L4 替代原生冷开场 ----------

@pytestmark_media
def test_orchestrator_gen_hooks(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path / "ms-empty"))
    engine = create_engine(f"sqlite:///{(tmp_path / 'gen.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=25)
    hook = make_lavfi_fixture(tmp_path / "hook.mp4", seconds=2)
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri=str(src),
                           file_path=str(src), fingerprint="fp-gen", duration_ms=25000)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="生成集成", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.commit()
        pid = p.id
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    pipe = Pipeline(settings, ctx, sf, registry=EngineRegistry(settings, ctx))
    result = pipe.run(pid)
    assert result.status == "candidates_ready"
    c1 = adopt_candidate(sf, result.candidate_ids[0], user="t")
    # 一条真实生视频 + 一条 mock 生图
    with sf() as s:
        s.add(StudioArtifact(project_id=pid, candidate_id=c1, kind="hook_video",
                             file_path=str(hook), meta={"mock": False}))
        s.add(StudioArtifact(project_id=pid, candidate_id=c1, kind="hook_image",
                             file_path=str(tmp_path / "m.png"), meta={"mock": True}))
        s.commit()
    rr = pipe.render_project(pid, portrait=False, use_gen_hooks=True)
    assert rr.status == "rendered", rr.error
    # mock 生图被拒（显式降级）
    assert any(d["engine"] == "gen_hook" and "mock" in d["reason"] for d in rr.degraded)
    with sf() as s:
        mp4 = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "mp4")).scalars().one()
        assert mp4.meta["hooks"]["hook_video"] == str(hook)
        assert mp4.meta["hooks"]["broll_image"] is None
        assert mp4.meta["hooks"]["aigc_badge"] is True
        # 16s 切片 + 2s 生视频（原生冷开场被替代，不叠加）
        assert abs(mp4.meta["selfcheck"]["duration_s"] - 18.0) <= 0.4
        assert mp4.meta["editlist"]["cold_open"] is None
    engine.dispose()


@pytestmark_media
def test_orchestrator_gen_asset_selection(tmp_path, monkeypatch):
    """显式勾选 gen_asset_ids：只接入选中的素材（前端工坊勾选语义）。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path / "ms-empty"))
    engine = create_engine(f"sqlite:///{(tmp_path / 'sel.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=25)
    hook_a = make_lavfi_fixture(tmp_path / "hook_a.mp4", seconds=2)
    hook_b = make_lavfi_fixture(tmp_path / "hook_b.mp4", seconds=3)
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri=str(src),
                           file_path=str(src), fingerprint="fp-sel", duration_ms=25000)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="选择", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.commit()
        pid = p.id
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    pipe = Pipeline(settings, ctx, sf, registry=EngineRegistry(settings, ctx))
    result = pipe.run(pid)
    c1 = adopt_candidate(sf, result.candidate_ids[0], user="t")
    with sf() as s:
        s.add(StudioArtifact(project_id=pid, candidate_id=c1, kind="hook_video",
                             file_path=str(hook_a), meta={"mock": False}))
        b = StudioArtifact(project_id=pid, candidate_id=c1, kind="hook_video",
                           file_path=str(hook_b), meta={"mock": False})
        s.add(b)
        s.commit()
        bid = b.id
    rr = pipe.render_project(pid, portrait=False, use_gen_hooks=True, gen_asset_ids=[bid])
    assert rr.status == "rendered", rr.error
    with sf() as s:
        mp4 = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "mp4")).scalars().one()
        assert mp4.meta["hooks"]["hook_video"] == str(hook_b)  # 只接勾选的 B
        assert abs(mp4.meta["selfcheck"]["duration_s"] - 19.0) <= 0.4  # 16+3
    engine.dispose()