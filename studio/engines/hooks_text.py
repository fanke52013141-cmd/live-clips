"""L1 文字钩子贴片引擎（04 号方案 §10.7，P2-4）。

产物为贴片描述（text/duration/位置），由渲染器烧 ASS Hook 样式：
开头 2s、上 1/3、大字描边、淡入淡出。v1 固定安全区（人脸检测避让留后续批）。
写不出悬念句 → 返回 None，上层自动跳过（宁缺毋滥）。
"""
from __future__ import annotations

from studio.engines.base import (
    ClipPlan,
    EngineContext,
    HookAsset,
    ProgressFn,
)
from studio.pipeline.hook_decision import clamp_hook_text

OVERLAY_DURATION_S = 2.0


class TextOverlayHook:
    name = "text_overlay"
    level = 1

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        return True, ""  # 纯描述生成，渲染器负责烧录

    def generate(
        self, clip: ClipPlan, ctx: EngineContext, progress: ProgressFn
    ) -> HookAsset | None:
        plan = clip.hook_plan or {}
        text = (plan.get("l1_text") or {}).get("text") or clip.title or clip.quote
        clamped = clamp_hook_text(text)
        if clamped is None:
            return None  # 写不出 → 不加
        return HookAsset(
            level=1,
            kind="text_overlay",
            text=clamped,
            meta={
                "duration_s": OVERLAY_DURATION_S,
                "position": "top_third_safe_zone_v1",
                "style": "big_word_outline_fade",
            },
        )