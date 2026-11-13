"""回流对账（P3-5）：CSV 导入 + content_id 保守匹配 + A/B 报告。

铁律：
- 匹配保守：content_id 精确 > 发布 URL 精确；多命中=歧义，**显式列出不自动指派**
- 未匹配行显式列出，不静默丢弃
- A/B 门槛：同平台、发布≥72h、播放≥500 才入组；样本不足只报方向性结论
- 指标只报均值与样本数，不做统计显著性宣称（样本量不支持）
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.models import (
    StudioArtifact,
    StudioFeedback,
    StudioProject,
    StudioPublication,
)

HEADER_ALIASES: dict[str, str] = {
    "platform": "platform", "平台": "platform",
    "account": "account", "账号": "account",
    "url": "url", "链接": "url", "视频链接": "url", "作品链接": "url",
    "content_id": "content_id", "内容id": "content_id", "内容ID": "content_id",
    "title": "title", "标题": "title",
    "views": "views", "播放": "views", "播放量": "views", "观看": "views",
    "likes": "likes", "点赞": "likes", "点赞数": "likes",
    "comments": "comments", "评论": "comments", "评论数": "comments",
    "shares": "shares", "转发": "shares", "分享": "shares", "分享数": "shares",
    "orders": "orders", "订单": "orders", "成交": "orders", "订单数": "orders",
    "published_at": "published_at", "发布时间": "published_at",
}
_METRIC_FIELDS = ("views", "likes", "comments", "shares", "orders")


class FeedbackError(RuntimeError):
    pass


def _num(v: str) -> float:
    v = (v or "").strip().replace(",", "")
    if not v:
        return 0.0
    m = re.match(r"^([\d.]+)\s*[万wW]$", v)
    if m:
        return float(m.group(1)) * 10000
    try:
        return float(v)
    except ValueError:
        return 0.0


def _parse_dt(v: str) -> datetime | None:
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def parse_csv(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        raise FeedbackError(f"CSV 文件不存在: {path}")
    rows: list[dict] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise FeedbackError("CSV 无表头")
        norm = {h: HEADER_ALIASES.get((h or "").strip(), (h or "").strip().lower())
                for h in reader.fieldnames}
        for i, raw in enumerate(reader):
            row = {norm[k]: (v or "").strip() for k, v in raw.items() if k is not None}
            row["_line"] = i + 2
            for k in _METRIC_FIELDS:
                row[k] = _num(row.get(k, ""))
            row["published_at"] = _parse_dt(row.get("published_at", ""))
            rows.append(row)
    return rows


def import_feedback(sf: sessionmaker[Session], csv_path: str | Path) -> dict:
    rows = parse_csv(csv_path)
    matched: list[dict] = []
    ambiguous: list[dict] = []
    unmatched: list[dict] = []
    with sf() as s:
        for row in rows:
            cid = row.get("content_id", "")
            url = row.get("url", "")
            art: StudioArtifact | None = None
            if cid:
                hits = s.execute(
                    select(StudioArtifact).where(
                        StudioArtifact.content_id == cid, StudioArtifact.kind == "mp4",
                    )
                ).scalars().all()
                if len(hits) > 1:
                    ambiguous.append({"line": row["_line"], "content_id": cid,
                                      "artifact_ids": [h.id for h in hits],
                                      "reason": "content_id 多命中，需人工指定"})
                    continue
                if len(hits) == 1:
                    art = hits[0]
            if art is None and url:
                pubs = s.execute(
                    select(StudioPublication).where(StudioPublication.url == url)
                ).scalars().all()
                if len(pubs) > 1:
                    ambiguous.append({"line": row["_line"], "url": url,
                                      "publication_ids": [p.id for p in pubs],
                                      "reason": "URL 多命中，需人工指定"})
                    continue
                if len(pubs) == 1:
                    art = s.get(StudioArtifact, pubs[0].artifact_id)
            if art is None:
                unmatched.append({"line": row["_line"], "content_id": cid or None,
                                  "url": url or None, "title": row.get("title"),
                                  "reason": "无 content_id/URL 精确匹配"})
                continue
            pub = s.execute(
                select(StudioPublication).where(
                    StudioPublication.artifact_id == art.id,
                    StudioPublication.platform == (row.get("platform") or art.platform),
                )
            ).scalars().first()
            if pub is None:
                pub = StudioPublication(
                    artifact_id=art.id, platform=row.get("platform") or art.platform,
                    account=row.get("account"), url=url or None,
                    published_at=row.get("published_at"), status="published",
                )
                s.add(pub)
                s.flush()
            elif row.get("published_at") and pub.published_at is None:
                pub.published_at = row["published_at"]
            fb = StudioFeedback(
                publication_id=pub.id,
                metrics={k: row[k] for k in _METRIC_FIELDS},
                source_file=str(csv_path),
            )
            s.add(fb)
            matched.append({"line": row["_line"], "artifact_id": art.id,
                            "content_id": art.content_id, "publication_id": pub.id})
        s.commit()
    return {
        "rows_total": len(rows), "imported": len(matched),
        "matched": matched, "ambiguous": ambiguous, "unmatched": unmatched,
        "note": "歧义/未匹配行显式列出，不自动指派不静默丢弃",
    }


def _hook_strategy(meta: dict) -> str:
    hooks = meta.get("hooks") or {}
    el = meta.get("editlist") or {}
    if hooks.get("l2_audio"):
        return "L2 TTS"
    if hooks.get("l1_overlay"):
        return "L1 文字"
    if el.get("cold_open"):
        return "原生冷开场"
    return "无钩子"


def build_ab_report(
    sf: sessionmaker[Session], project_id: str, *,
    min_views: int = 500, min_age_hours: int = 72, now: datetime | None = None,
) -> dict:
    now = now or datetime.now()
    with sf() as s:
        if s.get(StudioProject, project_id) is None:
            raise FeedbackError(f"项目不存在: {project_id}")
        arts = s.execute(
            select(StudioArtifact).where(
                StudioArtifact.project_id == project_id, StudioArtifact.kind == "mp4",
            )
        ).scalars().all()
        groups: dict[tuple[str, str], list[dict]] = {}
        excluded: list[dict] = []
        for a in arts:
            pubs = s.execute(
                select(StudioPublication).where(StudioPublication.artifact_id == a.id)
            ).scalars().all()
            for pub in pubs:
                fb = s.execute(
                    select(StudioFeedback).where(StudioFeedback.publication_id == pub.id)
                    .order_by(StudioFeedback.imported_at.desc()).limit(1)
                ).scalar_one_or_none()
                if fb is None:
                    excluded.append({"artifact_id": a.id, "platform": pub.platform,
                                     "reason": "无回流数据"})
                    continue
                views = float(fb.metrics.get("views", 0))
                if views < min_views:
                    excluded.append({"artifact_id": a.id, "platform": pub.platform,
                                     "reason": f"播放 {views:.0f} < {min_views}"})
                    continue
                if pub.published_at is None:
                    excluded.append({"artifact_id": a.id, "platform": pub.platform,
                                     "reason": "无发布时间，无法核验 72h"})
                    continue
                age_h = (now - pub.published_at).total_seconds() / 3600
                if age_h < min_age_hours:
                    excluded.append({"artifact_id": a.id, "platform": pub.platform,
                                     "reason": f"发布 {age_h:.0f}h < {min_age_hours}h"})
                    continue
                key = (pub.platform or "unknown", _hook_strategy(a.meta or {}))
                groups.setdefault(key, []).append(fb.metrics)
    out_groups = []
    for (platform, strategy), metrics in sorted(groups.items()):
        n = len(metrics)
        avg = {k: round(sum(float(m.get(k, 0)) for m in metrics) / n, 1) for k in _METRIC_FIELDS}
        eng = round((avg["likes"] + avg["comments"] + avg["shares"]) / avg["views"], 4) if avg["views"] else 0.0
        out_groups.append({
            "platform": platform, "hook_strategy": strategy, "n": n,
            "avg": avg, "engagement_rate": eng,
        })
    # 方向性结论：仅当存在 ≥2 组且每组 n≥2 时给方向，否则明示样本不足
    conclusion = "样本不足，仅方向性参考，不做显著性宣称"
    eligible = [g for g in out_groups if g["n"] >= 2]
    if len(eligible) >= 2:
        best = max(eligible, key=lambda g: g["engagement_rate"])
        worst = min(eligible, key=lambda g: g["engagement_rate"])
        conclusion = (
            f"方向性：{best['platform']}/{best['hook_strategy']} 互动率 "
            f"{best['engagement_rate']:.2%} 高于 {worst['platform']}/{worst['hook_strategy']} "
            f"{worst['engagement_rate']:.2%}（样本小，需更多数据验证）"
        )
    return {
        "project_id": project_id,
        "gates": {"min_views": min_views, "min_age_hours": min_age_hours, "same_platform": True},
        "groups": out_groups, "excluded": excluded, "conclusion": conclusion,
        "disclaimer": "均值对比仅方向性参考；平台/账号差异未控制，不构成因果结论",
    }