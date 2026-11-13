# SliceStudio · 直播切片工作台

基于 [AutoClip](https://github.com/zhouxiaoka/autoclip)（MIT）fork 的直播切片生产系统。
核心理念：**AI 出候选，人终审**——AI 负责转写/选段/钩子/渲染，人工审核采纳后才进成片与发布。

> 本仓库 = 上游 AutoClip 完整代码 + 自研 `studio/` 包 + 工作台前端 + 项目文档。
> 上游代码改动仅 3 处缝合点，见 [docs/UPSTREAM_PATCHES.md](docs/UPSTREAM_PATCHES.md)。

---

## 核心循环

```
素材入库 → AI 转写+信号+选段（clips0 候选）→ 人工审核采纳（clips1）
        → 钩子工坊 → 渲染成片 → 合规检查 → 发布包 / 剪映草稿 → 数据回流
```

## 快速开始（本机开发，无需 GPU / 无需 Key）

```powershell
# 后端（数据落 .data-dev/studio.db，零 Redis 零 Key，mock 引擎兜底）
.\.venv\Scripts\python.exe -m uvicorn studio.api.app:dev_app --port 26761

# 前端（另开终端）
cd frontend; npm run dev
```

浏览器入口：
- `/#/studio/review` — 候选审核台 + 时间轴（信号曲线/音频波形/逐词切点拖拽/快捷键）
- `/#/studio/render` — 渲染审阅 + 钩子工坊（L1/L2/L3/L4）+ 安全区遮罩
- `/#/studio/compliance` — 合规报告 / 留证包 / 平台发布包 / 剪映草稿
- `/#/studio/transcript/:id` — 逐字稿

## 双执行档案（一套代码，GPU 只做加速器不做依赖）

| 档案 | 环境 | ASR | 生成钩子 | 渲染 |
|---|---|---|---|---|
| `cpu-cloud` | 无 GPU 本机 | 云百炼 / 本地 Paraformer | 云 API | x264 |
| `gpu-local` | RTX 5080 | 本地 faster-whisper/Paraformer | 本地 Wan（待实测） | NVENC |

引擎注册表按档案探测降级，降级显式记录；mock 引擎永远兜底，无 Key/无 GPU/断网可跑全链路。

## 自研代码结构（studio/）

```
studio/
├── engines/        # ASR/分离/信号/LLM/钩子/渲染/生成提供商 + 注册表 + 缓存
├── pipeline/       # 编排器 / 选段复审 / 信号融合 / 剪辑决策 / 钩子决策 / 生成服务
├── contracts/      # LLM 输出 JSON Schema + 防幻觉铁律
├── compliance/     # 违禁词 / 变形度 / AIGC 标识 / 授权核对 / 留证
├── feedback/       # 回流对账 / A/B 报告 / 偏好回流
├── factory/        # 录播监听 / 三档审核闸门 / 发布泵(dry-run) / 数据战报
├── export/         # 剪映草稿(capcut-mate) / 平台发布包
├── models/         # SQLAlchemy 实体（studio_ 前缀，与上游共存）
└── api/            # FastAPI 路由
```

## 当前状态

- **P0→P4 核心代码层完成**，自研测试 **258 全绿**（`pytest tests/studio`）
- **M2 整链装配验收通过**：真实服务全链 12 步（入库→管线→采纳→渲染→合规→留证→发布包），见 [docs/07-端到端验收报告.md](docs/07-端到端验收报告.md)
- 真实直播效果验收待真实样本（当前验收用 mock + 合成素材，只证明装配正确）

## 安全边界（重要）

- **真实发布 gated**：发布泵默认 dry-run；真实上传需 social-auto-upload 许可证核实 + 平台授权 + 显式启用
- **付费生成 gated**：L3/L4 生成须费用预估 + 人工确认；mock 产物强制标记，禁入真实渲染
- **AIGC 标识**：AI 内容（TTS/B-roll/生视频）强制元数据 + 显式角标
- 本地运行数据（`data/`、`.data-dev/`）、模型权重、密钥均不入仓

## 文档索引（docs/）

| 文档 | 内容 |
|---|---|
| 01-直播切片开源项目调研报告 | 开源方案调研（四条路线 + 钩子方法论） |
| 02-基底选型与能力吸收方案 | 为何选 AutoClip + 吸收清单 |
| 03-双环境执行方案-无GPU与有GPU | cpu-cloud / gpu-local 双档案 |
| 04-详细开发方案 | 模块/DDL/接口契约/任务卡 |
| 05-上游架构笔记 | AutoClip 架构核实 |
| 06-P0测试基线报告 | 逐批测试基线（§1-20） |
| 07-端到端验收报告 | M2 整链装配验收 |
| 08-系统缺口与待提供清单 | 全景缺口 + 待提供输入 |
| STUDIO_HANDOFF | 逐批交接（含边界与待办） |
| LICENSE_AUDIT | 第三方许可证审计 |
| UPSTREAM_PATCHES | 上游改动台账（3 处缝合点） |

## 许可证

- 上游 AutoClip：MIT
- capcut-mate（剪映草稿服务，独立对接）：Apache-2.0（已核实）
- 自研 studio/ 代码：随上游 MIT
- 第三方依赖许可证详见 [docs/LICENSE_AUDIT.md](docs/LICENSE_AUDIT.md)