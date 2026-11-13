"""合规报告与留证（P2-7）。全部规则代码化，命中可定位、可审计。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.models import (
    StudioArtifact,
    StudioCandidate,
    StudioLicense,
    StudioMaterial,
    StudioProject,
    StudioTask,
)

# 保守内置词表 v1：广告法绝对化用语 + 站外导流敏感词。
# 词表可项目级扩展；误报由人工审核台处置（lint 只报告不阻断）。
BANNED_WORDS: tuple[str, ...] = (
    "最好", "最佳", "最优", "最低价", "全网最低", "史上最低", "第一品牌",
    "国家级", "顶级", "绝无仅有", "包治", "根治", "药到病除",
    "加微信", "加微信", "加V", "私下交易", "绕过平台",
)


def lint_text(text: str, words: tuple[str, ...] = BANNED_WORDS) -> list[dict]:
    hits = []
    for w in words:
        idx = text.find(w)
        while idx != -1:
            hits.append({"word": w, "pos": idx, "context": text[max(0, idx - 8):idx + len(w) + 8]})
            idx = text.find(w, idx + len(w))
    return hits


def lint_project_texts(
    title: str | None, caption: str | None, sentences: list[dict],
    words: tuple[str, ...] = BANNED_WORDS,
) -> list[dict]:
    """标题/文案/逐句字幕扫描；字幕命中带时间码。"""
    out: list[dict] = []
    for field, text in (("title", title or ""), ("caption", caption or "")):
        for h in lint_text(text, words):
            out.append({"field": field, **h})
    for sen in sentences:
        for h in lint_text(str(sen.get("text", "")), words):
            out.append({
                "field": "subtitle", "start_ms": sen.get("start_ms"),
                "end_ms": sen.get("end_ms"), **h,
            })
    return out


def transform_score(meta: dict) -> dict:
    """变形度自评 v1（启发式，非平台判定）：二创要素加权 0-100。

    要素：剪辑改变( cuts/cold_open/flash )、钩子( L1/L2 )、竖屏重构、逐词字幕。
    分数只表达「我们做了多少增量加工」，平台搬运判定以其自身规则为准。
    """
    el = meta.get("editlist") or {}
    hooks = meta.get("hooks") or {}
    factors: list[dict] = []
    if el.get("cuts"):
        factors.append({"factor": "去气口/剪除", "weight": 15})
    if el.get("cold_open"):
        factors.append({"factor": "冷开场重排", "weight": 20})
    if el.get("flash_forward"):
        factors.append({"factor": "高能闪现", "weight": 10})
    if hooks.get("l1_overlay"):
        factors.append({"factor": "L1 文字钩子", "weight": 15})
    if hooks.get("l2_audio"):
        factors.append({"factor": "L2 TTS 钩子", "weight": 15})
    if meta.get("portrait"):
        factors.append({"factor": "竖屏重构", "weight": 15})
    if meta.get("subtitles"):
        factors.append({"factor": "逐词字幕", "weight": 10})
    score = min(100, sum(f["weight"] for f in factors))
    return {
        "score": score, "factors": factors,
        "method": "heuristic_self_assessment_v1",
        "disclaimer": "启发式自评，非平台搬运判定；纯切片无加工=0 分",
    }


def aigc_check(meta: dict) -> dict:
    """AIGC 标识核对：TTS/B-roll/生视频属 AI 合成内容，须显式标识（角标+元数据）。"""
    hooks = meta.get("hooks") or {}
    required = bool(
        hooks.get("l2_audio") or hooks.get("broll_image") or hooks.get("hook_video")
        or hooks.get("l3_broll") or hooks.get("l4_gen")
    )
    labeled = bool(hooks.get("aigc_labeled"))
    badge = bool(hooks.get("aigc_badge"))
    return {
        "required": required,
        "labeled": labeled,
        "badge_burned": badge,
        "ok": (not required) or (labeled and badge),
        "note": "AI 内容须元数据标识+显式角标双到位；角标由渲染器烧录（P3）",
    }


def license_check(
    sf: sessionmaker[Session], material: StudioMaterial, platform: str | None = None,
    today: date | None = None,
) -> dict:
    """授权台账核对：无授权→需声明；过期→阻断；范围不含平台→警告。"""
    today = today or date.today()
    if not material.license_id:
        return {"status": "declaration_required",
                "note": "素材未关联授权台账：自有素材需书面声明，他人素材需授权"}
    with sf() as s:
        lic = s.get(StudioLicense, material.license_id)
    if lic is None:
        return {"status": "broken_reference", "note": "授权台账引用不存在"}
    if lic.expires_at is not None:
        exp = lic.expires_at if isinstance(lic.expires_at, date) else lic.expires_at.date()
        if exp < today:
            return {"status": "expired", "note": f"授权已过期: {exp}", "license_id": lic.id}
    scope = lic.scope or {}
    platforms = scope.get("platforms")
    if platform and platforms and platform not in platforms:
        return {"status": "scope_mismatch",
                "note": f"授权范围不含平台 {platform}", "license_id": lic.id}
    return {"status": "ok", "license_id": lic.id, "licensor": lic.licensor}


def _file_sha256(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_compliance_report(
    sf: sessionmaker[Session], project_id: str, *, platform: str | None = None,
) -> dict:
    with sf() as s:
        project = s.get(StudioProject, project_id)
        if project is None:
            raise ValueError(f"项目不存在: {project_id}")
        material = s.get(StudioMaterial, project.material_id)
        candidates = s.execute(
            select(StudioCandidate).where(StudioCandidate.project_id == project_id)
        ).scalars().all()
        artifacts = s.execute(
            select(StudioArtifact).where(
                StudioArtifact.project_id == project_id,
                StudioArtifact.kind == "mp4",
            )
        ).scalars().all()
        tr = s.execute(
            select(StudioTask).where(
                StudioTask.project_id == project_id, StudioTask.kind == "analyze",
            ).order_by(StudioTask.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        sentences = []
        from studio.models import StudioTranscript
        trow = s.execute(
            select(StudioTranscript)
            .where(StudioTranscript.material_id == material.id)
            .order_by(StudioTranscript.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        if trow:
            sentences = trow.sentences
        adopted = [c for c in candidates if c.layer == "clips1" and c.adopted]
        text_hits = []
        for c in adopted:
            text_hits.extend(lint_project_texts(c.title, c.caption, sentences))
        lic = license_check(sf, material, platform)
        per_artifact = []
        for a in artifacts:
            meta = a.meta or {}
            per_artifact.append({
                "artifact_id": a.id, "candidate_id": a.candidate_id,
                "transform": transform_score(meta),
                "aigc": aigc_check(meta),
            })
        lineage = [
            {"id": c.id, "layer": c.layer, "parent_id": c.parent_id,
             "created_by": c.created_by, "adopted": c.adopted,
             "rejected_reason": c.rejected_reason}
            for c in candidates
        ]
        report = {
            "project_id": project_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "banned_words": {"hits": text_hits, "wordlist_version": "v1_conservative"},
            "license": lic,
            "artifacts": per_artifact,
            "lineage": lineage,
            "analyze_receipt_ref": tr.id if tr else None,
            "blockers": (
                (["授权过期"] if lic["status"] == "expired" else [])
                + [f"AIGC 未标识: {x['artifact_id']}" for x in per_artifact if not x["aigc"]["ok"]]
            ),
        }
        return report


def write_evidence(
    sf: sessionmaker[Session], project_id: str, out_dir: str | Path,
    *, platform: str | None = None,
) -> dict:
    """留证包：合规报告+血缘+决策回执+产物指纹 → evidence.json，落 artifact。"""
    report = build_compliance_report(sf, project_id, platform=platform)
    with sf() as s:
        artifacts = s.execute(
            select(StudioArtifact).where(StudioArtifact.project_id == project_id)
        ).scalars().all()
        tasks = s.execute(
            select(StudioTask).where(StudioTask.project_id == project_id)
        ).scalars().all()
        material = s.get(StudioMaterial, s.get(StudioProject, project_id).material_id)
        report["artifact_hashes"] = [
            {"id": a.id, "kind": a.kind, "file_path": a.file_path,
             "sha256": _file_sha256(a.file_path)}
            for a in artifacts
        ]
        report["task_receipts"] = [
            {"id": t.id, "kind": t.kind, "status": t.status, "receipt": t.receipt}
            for t in tasks
        ]
        report["material_fingerprint"] = material.fingerprint
    out = Path(out_dir) / "evidence.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with sf() as s:
        row = StudioArtifact(
            project_id=project_id, kind="evidence", file_path=str(out),
            size_bytes=out.stat().st_size, meta={"platform": platform},
        )
        s.add(row)
        s.commit()
        report["artifact_id"] = row.id
    report["evidence_path"] = str(out)
    return report