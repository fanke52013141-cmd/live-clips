"""平台发布包构建（P2-8）。

每平台一个目录：成片 mp4 + 封面 + title.txt + caption.txt + safe_area.json + meta.json，
整体打 ZIP。铁律：
- 画幅不匹配 → 显式警告「需按该平台画幅重渲染」，绝不静默拉伸
- 文案超限 → 截断版+溢出标记+原文保留，人工决定
- 只打包 adopted clips1 的成片产物
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.export.platforms import fit_text, get_platform
from studio.models import StudioArtifact, StudioCandidate, StudioProject


class PackError(RuntimeError):
    pass


def build_pack(
    sf: sessionmaker[Session],
    project_id: str,
    platforms: list[str],
    out_dir: str | Path,
) -> dict:
    presets = {p: get_platform(p) for p in platforms}  # 未知平台先报错
    with sf() as s:
        project = s.get(StudioProject, project_id)
        if project is None:
            raise PackError(f"项目不存在: {project_id}")
        adopted = s.execute(
            select(StudioCandidate).where(
                StudioCandidate.project_id == project_id,
                StudioCandidate.layer == "clips1",
                StudioCandidate.adopted.is_(True),
            )
        ).scalars().all()
        if not adopted:
            raise PackError("无可打包候选：请先采纳 clips1 并渲染成片")
        clips = {c.id: c for c in adopted}
        mp4s = s.execute(
            select(StudioArtifact).where(
                StudioArtifact.project_id == project_id,
                StudioArtifact.kind == "mp4",
            )
        ).scalars().all()
        covers = s.execute(
            select(StudioArtifact).where(
                StudioArtifact.project_id == project_id,
                StudioArtifact.kind == "cover",
            )
        ).scalars().all()
        cover_by_cid = {c.candidate_id: c for c in covers}
        items = []
        for a in mp4s:
            c = clips.get(a.candidate_id)
            if c is None:
                continue  # 未采纳的产物不打包
            items.append((a, c, cover_by_cid.get(a.candidate_id)))
    if not items:
        raise PackError("无成片产物：请先渲染")

    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    pack_dirs: list[str] = []
    for pname, preset in presets.items():
        pdir = root / pname
        pdir.mkdir(parents=True, exist_ok=True)
        pack_dirs.append(str(pdir))
        (pdir / "safe_area.json").write_text(
            json.dumps({
                "platform": pname, "ratios": preset["safe_area"],
                "note": "经验值，须真机校准；比例为画面宽高占比",
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for a, c, cover in items:
            meta = a.meta or {}
            cid = c.id
            sub = pdir / cid
            sub.mkdir(exist_ok=True)
            # 画幅核对：竖屏成片 vs 平台画幅
            portrait = bool(meta.get("portrait"))
            want_portrait = preset["height"] > preset["width"]
            aspect_ok = portrait == want_portrait or preset["aspect"] == "3:4"
            if not aspect_ok:
                warnings.append(
                    f"{pname}/{cid}: 成片画幅({'竖' if portrait else '横'})与平台 "
                    f"{preset['aspect']} 不匹配，需重渲染（不静默拉伸）"
                )
            src = Path(a.file_path)
            if src.is_file():
                shutil.copy2(src, sub / "video.mp4")
            else:
                warnings.append(f"{pname}/{cid}: 成片文件缺失 {a.file_path}")
            if cover is not None and Path(cover.file_path).is_file():
                shutil.copy2(cover.file_path, sub / "cover.jpg")
            title, t_over = fit_text(c.title or "", preset["title_max"])
            caption, c_over = fit_text(c.caption or "", preset["caption_max"])
            (sub / "title.txt").write_text(title, encoding="utf-8")
            (sub / "caption.txt").write_text(caption, encoding="utf-8")
            if t_over or c_over:
                (sub / "caption_full.txt").write_text(c.caption or "", encoding="utf-8")
                warnings.append(f"{pname}/{cid}: 文案超限已截断（原文保留 caption_full.txt）")
            (sub / "meta.json").write_text(
                json.dumps({
                    "platform": pname, "candidate_id": cid,
                    "content_id": a.content_id,
                    "aspect_expected": preset["aspect"], "aspect_ok": aspect_ok,
                    "cover_ratio_expected": preset["cover_ratio"],
                    "title_overflow": t_over, "caption_overflow": c_over,
                    "source_artifact_id": a.id,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    zip_path = root / f"publish_pack_{project_id[:8]}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in pack_dirs:
            for f in Path(d).rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(root))
    receipt = {
        "platforms": list(presets), "clips": len(items),
        "zip_path": str(zip_path), "zip_bytes": zip_path.stat().st_size,
        "warnings": warnings,
        "safe_area_note": "安全区为经验值，须真机校准",
    }
    with sf() as s:
        row = StudioArtifact(
            project_id=project_id, kind="publish_pack",
            file_path=str(zip_path), size_bytes=zip_path.stat().st_size, meta=receipt,
        )
        s.add(row)
        s.commit()
        receipt["artifact_id"] = row.id
    return receipt