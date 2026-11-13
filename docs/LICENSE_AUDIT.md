# 第三方许可证审计（切片工作台）

> 纪律：只认官方仓库一手 LICENSE 文件；二手文章/聚合站信息仅作线索，冲突时以官方为准。
> 每次核实记录：日期、官方 URL、结论、集成义务。

## capcut-mate（P2-6，2026-11-13 核实）

- 官方仓库：https://github.com/Hommy-master/capcut-mate
- LICENSE 原文：https://raw.githubusercontent.com/Hommy-master/capcut-mate/main/LICENSE
- **结论：Apache-2.0**（官方 LICENSE 全文抓取确认）
- 冲突记录：掘金/头条等二手文章称「MIT License」，与官方文件不符，**不采信**；
  开源榜元数据（2026-10-15 检查）SPDX=Apache-2.0 与官方一致
- 集成方式：独立部署的 REST 服务对接（studio/export/capcut_mate.py 仅为 HTTP 客户端），
  不复制其源码进本仓库 → 我们的代码不构成 Apache-2.0 衍生作品
- 义务（若随产品分发该服务）：随附 LICENSE 副本与 NOTICE（如有）、修改文件显著声明
- 风险登记：草稿格式由 capcut-mate 维护，剪映版本升级可能破坏兼容；
  适配层独立 + ZIP 成片包兜底（02 号方案既定策略）
- 【未核实】API 响应具体 schema 与草稿格式版本：需部署服务后实测（本机无 Docker，
  测试用注入 fake transport 验证请求构造，不冒充真实服务验收）

## 已核实（前批）

| 项目 | 许可证 | 集成级别 | 备注 |
|---|---|---|---|
| AutoClip (zhouxiaoka/autoclip) | MIT | 基底 fork | 02 号选型论证 |
| FunClip (modelscope/FunClip) | Apache-2.0（P0 核实） | L1 思路/模型 | 模型权重商用条款另查 |
| hotclip | AGPL | **仅思路，不抄代码** | 污染风险隔离 |
| SeACo-Paraformer 权重 | ModelScope 官方源下载 | 本地 ASR | 商用条款待最终确认（04 §15） |

## 待核实（后续批触发）

- **social-auto-upload（P4 真实发布前置，未核实）**：发布泵真实上传已用 assert_real_publish_allowed
  显式 gated（dry_run=False → 403）。启用真实发布须先核实其许可证 + 配置平台账号授权 + 用户显式授权，
  三者缺一不可。当前工厂核心仅 dry-run 排期，无任何真实上传。
- VideoLingo（出海阶段前）
- FunASR 模型权重商用条款（商业发布前）
- biliup（开播检测，P4 后续若启用）