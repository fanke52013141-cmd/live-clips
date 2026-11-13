"""提示词资源服务（04 号方案 P1-3）。

- CRUD + 版本号：body 变更 version+1，历史可审计（不物理删被引用资源）。
- 解析优先级：项目显式 prompt_id > live_type 最新 active > 内置默认。
- user-intent/background 已是项目字段，选段时注入（编排器负责）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


class PromptError(ValueError):
    pass


def create_prompt(
    sf: sessionmaker[Session],
    *,
    name: str,
    body: str,
    live_type: str | None = None,
) -> str:
    if not name.strip() or not body.strip():
        raise PromptError("name/body 不能为空")
    from studio.models import StudioPrompt

    with sf() as s:
        p = StudioPrompt(name=name, body=body, live_type=live_type, version=1, active=True)
        s.add(p)
        s.commit()
        return p.id


def update_prompt(
    sf: sessionmaker[Session],
    prompt_id: str,
    *,
    name: str | None = None,
    body: str | None = None,
    live_type: str | None = None,
    active: bool | None = None,
) -> int:
    """返回更新后的 version。body 变更才递增版本。"""
    from studio.models import StudioPrompt

    with sf() as s:
        p = s.get(StudioPrompt, prompt_id)
        if p is None:
            raise PromptError(f"提示词不存在: {prompt_id}")
        if body is not None and body != p.body:
            if not body.strip():
                raise PromptError("body 不能为空")
            p.body = body
            p.version += 1
        if name is not None:
            p.name = name
        if live_type is not None:
            p.live_type = live_type
        if active is not None:
            p.active = active
        s.commit()
        return p.version


def list_prompts(
    sf: sessionmaker[Session],
    *,
    live_type: str | None = None,
    active_only: bool = False,
) -> list[dict]:
    from studio.models import StudioPrompt

    with sf() as s:
        q = select(StudioPrompt)
        if live_type is not None:
            q = q.where(StudioPrompt.live_type == live_type)
        if active_only:
            q = q.where(StudioPrompt.active.is_(True))
        rows = s.execute(q.order_by(StudioPrompt.version.desc())).scalars().all()
        return [
            {"id": p.id, "name": p.name, "live_type": p.live_type,
             "version": p.version, "active": p.active, "body": p.body}
            for p in rows
        ]


def delete_prompt(sf: sessionmaker[Session], prompt_id: str) -> None:
    """被项目引用的提示词不允许删除（置 active=False 代替）。"""
    from studio.models import StudioProject, StudioPrompt

    with sf() as s:
        p = s.get(StudioPrompt, prompt_id)
        if p is None:
            raise PromptError(f"提示词不存在: {prompt_id}")
        used = s.execute(
            select(StudioProject.id).where(StudioProject.prompt_id == prompt_id).limit(1)
        ).first()
        if used:
            raise PromptError("提示词被项目引用，不能删除；可置为停用")
        s.delete(p)
        s.commit()


def resolve_prompt_body(
    sf: sessionmaker[Session],
    *,
    prompt_id: str | None,
    live_type: str,
) -> str | None:
    """项目显式指定 > live_type 最新 active > None（编排器用内置默认）。"""
    from studio.models import StudioPrompt

    with sf() as s:
        if prompt_id:
            p = s.get(StudioPrompt, prompt_id)
            if p is None:
                raise PromptError(f"项目引用的提示词不存在: {prompt_id}")
            return p.body
        p = s.execute(
            select(StudioPrompt)
            .where(StudioPrompt.live_type == live_type, StudioPrompt.active.is_(True))
            .order_by(StudioPrompt.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return p.body if p else None