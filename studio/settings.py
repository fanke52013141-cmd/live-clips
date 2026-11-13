"""执行档案与全局配置（04 号方案 §13、03 号双环境方案）。

约定：
- Settings 为 frozen dataclass，切换 Profile 必须整体替换，不改单字段。
- 档案文件 profiles/<name>.yaml 声明每阶段引擎链（有序降级）。
- 引擎链中尚未实现的引擎名由 Registry 记为「未实现」降级，不报错（P0 阶段）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

STAGES = ("asr", "diarize", "signals", "llm", "hooks", "render")


@dataclass(frozen=True)
class EngineChains:
    asr: tuple[str, ...]
    diarize: tuple[str, ...]
    signals: tuple[str, ...]  # 注意：signals 是并行抽取器列表，非降级链
    llm: tuple[str, ...]
    hooks: tuple[str, ...]
    render: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    profile: str
    data_dir: Path
    models_dir: Path
    chains: EngineChains
    allow_paid_generation: bool = False  # L3/L4 生成钩子计费开关，默认关
    max_concurrency: int = 1
    review_loop: bool = True  # P1-2 复审环 judge→repair→rejudge，默认开
    diarize_enabled: bool = False  # P1-7 说话人分离，可选开关默认关


def load_settings(
    profiles_dir: Path,
    name: str,
    *,
    data_dir: Path | None = None,
    models_dir: Path | None = None,
) -> Settings:
    path = Path(profiles_dir) / f"{name}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw.get("profile") != name:
        raise ValueError(f"profile 名与文件名不一致: {path}")
    chains_raw = raw["chains"]
    missing = [s for s in STAGES if s not in chains_raw]
    if missing:
        raise ValueError(f"档案缺少阶段引擎链: {missing}")
    chains = EngineChains(**{s: tuple(chains_raw[s]) for s in STAGES})
    opts = raw.get("options", {})
    base = Path(data_dir) if data_dir else Path(raw.get("data_dir", "./.data-dev"))
    return Settings(
        profile=name,
        data_dir=base,
        models_dir=Path(models_dir) if models_dir else base / "models",
        chains=chains,
        allow_paid_generation=bool(opts.get("allow_paid_generation", False)),
        max_concurrency=int(opts.get("max_concurrency", 1)),
        review_loop=bool(opts.get("review_loop", True)),
        diarize_enabled=bool(opts.get("diarize", False)),
    )