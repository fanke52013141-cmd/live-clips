"""剪映草稿导出服务（P2-6）。

流程：只读 adopted clips1 的成片产物 → capcut-mate create_draft/add_videos/
add_captions/save_draft → 草稿回执落 studio_artifact(kind=draft)。

铁律：
- 无采纳候选/无成片产物 → 显式报错，不导出空草稿
- 字幕时间按成片实际时间轴重映射（有剪辑段用段映射，否则减切点偏移）
- video_url 回退本地路径时回执标注「本地路径支持度未核实」，不冒充已验证
- 服务不可达 → DraftError 带原因；ZIP 成片包兜底走 P2-8 发布包
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.export.capcut_mate import CapCutMateClient, DraftError
from studio.models import (
    StudioArtifact,
    StudioCandidate,
    StudioProject,
    StudioTranscript,
)
from studio.pipeline.editlist import Segment, remap_ms


def _remap_sentence_times(meta: dict, start_ms: int, end_ms: int) -> tuple[int, int] | None:
    """源时间轴句区间 → 成片时间轴；被剪掉返回 None。"""
    segs_raw = (meta.get("editlist") or {}).get("segments")
    if segs_raw:
        segs = [Segment(s["start_ms"], s["end_ms"], s["kind"]) for s in segs_raw]
        rs, re_ = remap_ms(start_ms, segs), remap_ms(max(start_ms, end_ms - 1), segs)
        if rs is None or re_ is None:
            return None
        return rs, re_ + 1
    base = meta.get("start_ms", 0)
    return max(0, start_ms - base), max(0, end_ms - base)


def export_draft(
    sf: sessionmaker[Session],
    project_id: str,
    *,
    client: CapCutMateClient,
    video_urls: dict[str, str] | None = None,
    portrait: bool = True,
) -> dict:
    with sf() as s:
        project = s.get(StudioProject, project_id)
        if project is None:
            raise DraftError(f"项目不存在: {project_id}")
        adopted = s.execute(
            select(StudioCandidate).where(
                StudioCandidate.project_id == project_id,
                StudioCandidate.layer == "clips1",
                StudioCandidate.adopted.is_(True),
            )
        ).scalars().all()
        if not adopted:
            raise DraftError("无可导出候选：请先采纳 clips1 并渲染成片")
        clips = [(c.id, c.start_ms, c.end_ms) for c in adopted]
        mp4s = {
            a.candidate_id: a
            for a in s.execute(
                select(StudioArtifact).where(
                    StudioArtifact.project_id == project_id,
                    StudioArtifact.kind == "mp4",
                )
            ).scalars().all()
        }
        tr = s.execute(
            select(StudioTranscript)
            .where(StudioTranscript.material_id == project.material_id)
            .order_by(StudioTranscript.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        sentences = tr.sentences if tr else []

    width, height = (1080, 1920) if portrait else (1920, 1080)
    entries: list[dict] = []
    for cid, c_start, c_end in clips:
        art = mp4s.get(cid)
        if art is None:
            raise DraftError(f"候选 {cid} 无成片产物：请先渲染")
        meta = art.meta or {}
        created = client.create_draft(width, height)
        draft_id = created.get("draft_id")
        draft_url = created.get("draft_url") or (
            f"{client._base}/openapi/capcut-mate/v1/get_draft?draft_id={draft_id}"
            if draft_id else None
        )
        if not draft_url:
            raise DraftError(f"create_draft 未返回 draft_url/draft_id: {created}")
        dur_s = (meta.get("selfcheck") or {}).get("duration_s") or (c_end - c_start) / 1000
        url = (video_urls or {}).get(cid)
        url_note = None
        if not url:
            url = art.file_path
            url_note = "本地路径回退：capcut-mate 对本地路径支持度未核实，建议传可访问 URL"
        client.add_videos(draft_url, [{
            "video_url": url, "start": 0, "end": int(dur_s * 1_000_000),
        }])
        captions: list[dict] = []
        for sen in sentences:
            ss, se = int(sen["start_ms"]), int(sen["end_ms"])
            if se <= c_start or ss >= c_end:
                continue
            mapped = _remap_sentence_times(meta, max(ss, c_start), min(se, c_end))
            if mapped is None:
                continue
            ms, me = mapped
            if me <= ms:
                continue
            captions.append({
                "text": sen["text"], "start": ms * 1000, "end": me * 1000,
            })
        if captions:
            client.add_captions(draft_url, captions)
        client.save_draft(draft_url)
        entries.append({
            "candidate_id": cid, "draft_id": draft_id, "draft_url": draft_url,
            "captions": len(captions), "video_url": url, "url_note": url_note,
            "format_version_note": "草稿格式版本由 capcut-mate 维护，剪映打开验收待实测",
        })

    receipt = {"service": "capcut-mate", "license": "Apache-2.0", "portrait": portrait,
               "drafts": entries}
    with sf() as s:
        row = StudioArtifact(
            project_id=project_id, kind="draft", platform=None,
            file_path=draft_url or "", size_bytes=0, meta=receipt,
        )
        s.add(row)
        s.commit()
        receipt["artifact_id"] = row.id
    return receipt