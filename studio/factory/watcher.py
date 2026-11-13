"""录播目录监听（P4-a，04 号方案 §11）。

铁律：连续两轮大小不变且非空才算录完（防止把正在写入的文件入库）；
按指纹去重（已入库不重复）；状态落盘支持跨轮询/跨进程。
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.engines.cache import file_fingerprint
from studio.media import probe_media
from studio.models import StudioMaterial

MEDIA_EXTS = {".mp4", ".mkv", ".flv", ".ts", ".mov", ".m4v"}


def poll_stable(directory: str | Path, state_file: str | Path) -> dict:
    """一轮轮询：返回 {stable:[新稳定文件], pending:[仍在写入], state_path}。

    稳定判定：本轮大小 == 上轮大小 且 > 0。首轮只记录不判定。
    """
    d = Path(directory)
    sf_path = Path(state_file)
    prev: dict[str, int] = {}
    if sf_path.is_file():
        prev = json.loads(sf_path.read_text(encoding="utf-8"))
    current: dict[str, int] = {}
    stable: list[str] = []
    pending: list[str] = []
    if d.is_dir():
        for f in sorted(d.iterdir()):
            if not f.is_file() or f.suffix.lower() not in MEDIA_EXTS:
                continue
            size = f.stat().st_size
            current[str(f)] = size
            if size > 0 and prev.get(str(f)) == size:
                stable.append(str(f))
            else:
                pending.append(str(f))
    sf_path.parent.mkdir(parents=True, exist_ok=True)
    sf_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    return {"stable": stable, "pending": pending, "state_path": str(sf_path)}


def ingest_stable(
    sf: sessionmaker[Session], directory: str | Path, state_file: str | Path,
) -> dict:
    """轮询 + 把新稳定文件入库（指纹去重）。返回 {ingested, skipped, pending}。"""
    poll = poll_stable(directory, state_file)
    ingested: list[dict] = []
    skipped: list[dict] = []
    with sf() as s:
        for path in poll["stable"]:
            fp = file_fingerprint(path)
            exists = s.execute(
                select(StudioMaterial).where(StudioMaterial.fingerprint == fp)
            ).scalars().first()
            if exists is not None:
                skipped.append({"file_path": path, "reason": "指纹已入库", "material_id": exists.id})
                continue
            try:
                info = probe_media(path)
            except Exception as e:
                skipped.append({"file_path": path, "reason": f"探测失败: {e}"})
                continue
            m = StudioMaterial(
                source_type="recorded", source_uri=path, file_path=path,
                fingerprint=fp, duration_ms=info["duration_ms"],
                width=info["width"], height=info["height"], fps=info["fps"],
            )
            s.add(m)
            s.commit()
            ingested.append({"file_path": path, "material_id": m.id, "duration_ms": info["duration_ms"]})
    return {"ingested": ingested, "skipped": skipped, "pending": poll["pending"]}