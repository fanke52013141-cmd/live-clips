# STUDIO HANDOFF — P4 工厂化核心（2026-11-13）

> 自研包 studio/ 的批次交接。上游 AutoClip 代码改动仅三处缝合点，见 docs/UPSTREAM_PATCHES.md。
> 前置：04-详细开发方案（workspace 根目录）、05-上游架构笔记、06-P0测试基线报告、07-端到端验收报告。

## 00000000000. 本批交付（P4 工厂化核心，258 passed；真实发布 gated）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| P4-a 录播监听 | studio/factory/watcher.py：poll_stable（**连续两轮大小不变且非空才算录完**，防写入中文件入库）+ ingest_stable（指纹去重、source_type=recorded、状态落盘跨轮询） | test_factory 监听 3 例（两轮稳定/写入中不稳定/非媒体忽略/去重） |
| P4-b 审核闸门 | studio/factory/gate.py：三档 hard_reject（合规阻断/已否决）/ auto_pass（已采纳+rank≥85+非弱片+无阻断）/ **human 默认**（AI 出候选、人终审） | 闸门 5 例（三档分流+项目分桶） |
| P4-c 发布泵 | studio/factory/pump.py：schedule_times（匀速间隔+确定性抖动 seed 可复现）；plan_pump **dry-run 排期落 planned**；**真实上传 assert_real_publish_allowed 显式拒绝**（social-auto-upload 许可证未核实+需平台授权） | 泵 3 例（间隔/抖动确定性/dry-run/真实 403） |
| P4-d 数据战报 | studio/factory/report.py：回流指标聚合（totals/按平台/top 切片互动率），无数据明示空不伪造 | 战报 2 例 |
| API | POST /factory/watch、POST /projects/{pid}/gate、POST /projects/{pid}/pump（dry_run=False→403）、GET /battle-report | API 用例 |

### 安全边界（重要）

- **真实发布全程 gated**：本批只做本地可测的工厂核心，无任何真实上传。
  启用真实发布的前置：① 核实 social-auto-upload 许可证（04 §15 待核）② 配置平台账号授权
  ③ 用户显式授权。三者缺一不可，assert_real_publish_allowed 强制拦截。
- 开播检测（biliup）/桌面壳评估未做（依赖外部组件/许可证，留后续）
- 闸门 auto_pass 也只是「可进泵」，仍经 dry-run/授权后才真实发布

### 测试口径

- 后端：**258 passed**（244 + 工厂 14），零失败；前端本批未改动；上游代码零改动

### 路线图状态

- **P0→P1→P2→P3→P4 核心代码层全部完成**；自研测试 42→122→210→240→258
- M0-M3 代码层达成 + M2 整链装配验收通过（07 报告）
- 剩余均为「待外部输入」项：真实样本/各 Key/capcut-mate 部署/social-auto-upload 许可证+授权/5080 本地 Wan

## 0000000000. 前批交付（音频振幅波形 + 前置段原声混音，244 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| W-1 波形后端 | media.waveform_peaks（8kHz PCM→1000 桶绝对值峰值归一化）+ has_audio_stream；GET /projects/{pid}/waveform **按素材指纹磁盘缓存**（data_dir/waveform/{fp}.json，跨项目/重跑复用） | test_api 波形用例（lavfi 真实素材：1000 桶/归一化/非静音/缓存命中） |
| W-2 波形前端 | 时间轴新增波形 lane（16..52，选中区内高亮蓝）；各轨道重排（信号 66..90/词 96..104/候选 108..138/采纳 142）；H 148→168；图例补波形项；审核台并行加载 waveform（404 静默降级 null） | tsc/构建通过 |
| W-3 前置原声 | _prepend_audio_parts：生视频**有音轨→原声**（atrim 对齐格式 concat）、无音轨/生图→等长静音；测量链同步含前置输入（索引一致）；回执 hooks.prepend_audio=source/silence | test_render_gen 断言（lavfi 生视频 source/生图 silence） |

### 验证口径

- 后端：**244 passed**（243 + 波形 1），零失败
- 前端：tsc ✅ 生产构建 ✅ 827 测试 824 过/3 挂（已知 Node24 基线同位置，零新增）
- 上游代码零改动

### 遗留缺口清偿

- 上批标注「音频振幅波形留后续批」→ 本批清偿
- 上批标注「前置段 v1 无原声」→ 本批清偿（生视频原声混音）
- 仍待：浏览器走查（真实素材）、P4 工厂化（按商业需求）、本地 Wan（5080）

## 000000000. 前批交付（审核台核心交互 R-1~R-4，243 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| R-1 后端数据源 | 信号轨落 analyze 回执；GET /projects/{pid}/signals；GET /materials/{mid}/file（DB 校验路径）；**修复取消早退 UnboundLocalError**（tracks 初始化，既有用例回归暴露） | test_api.py +2 例（13 例套件绿） |
| R-2 时间轴面板 | components/StudioTimelinePanel.tsx：深色剪辑台（NLE 语境，等宽时间码）；canvas 绘制弹幕/响度曲线（归一化）+词刻度+候选区域+已采纳绿线+播放头（rAF 平滑）；源视频预览（素材文件服务）；DPR 适配高清绘制 | tsc/构建通过 |
| R-3 切点交互 | 白色手柄拖拽（window 级监听出画布不丢事件）；松手**吸附词界**（>1s 不吸附防远跳）；clips1 直接 patch 落库、clips0 采纳时生效；快捷键 空格/I/O/[/]（输入框聚焦时禁用）；弱片「不建议发布」标签；点击候选区选中/空白 seek | tsc/构建通过；浏览器走查待真实素材 |
| R-4 集成 | 审核台 refresh 并行加载词级/信号/素材；面板嵌入任务提示与候选表之间；头部加渲染审阅/合规发布导航 | 后端 243 绿 + 前端基线零新增 |

### 验证口径

- 后端：**243 passed**（241 + signals/素材文件 2），零失败
- 前端：tsc ✅ 生产构建 ✅ 827 测试 824 过/3 挂（已知 Node24 基线同位置，零新增）
- 上游代码零改动（组件/页面均为自研文件）

### 已知边界

- 浏览器交互走查待真实素材（拖拽/快捷键/曲线需真实数据验证手感）
- 波形为信号曲线（弹幕/响度）而非音频振幅波形——音频波形需解码 PCM，留后续批
- 多用户鉴权仍沿上游 JWT 缝合待办

## 00000000. 前批交付（前端增强：use_gen_hooks/工坊预览选择/AIGC 角标展示，241 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| 后端支撑 | aigc_check 识别 B-roll/生视频（此前只认 TTS 会漏判）+**元数据+角标双到位才 ok**；render_project/API 支持 gen_asset_ids 显式勾选（查询层项目过滤，跨项目 ID 不纳入） | test_compliance_pack 更新 + test_render_gen 选择用例（20 例定向绿） |
| 渲染页开关 | use_gen_hooks 开关入九宫格；勾选素材时 render 传 gen_asset_ids，未勾选自动取最新非 mock | tsc/构建通过 |
| 生成素材工坊 | 新卡片：L3/L4 级别+mock/cloud 提供商选择→**预估费用→Modal 费用确认→生成**；产物画廊：真实素材 img/video 预览（文件服务），**mock 橙色醒目标注「MOCK 占位·禁入真实渲染」+虚线占位框**（不尝试播放假媒体）；接入勾选（mock 禁选）；勾选状态 Alert 说明自动/显式语义 | tsc/构建通过；浏览器走查待真实素材 |
| 合规页 | AIGC 列双状态：元数据已标识/缺失 + 角标已烧录/缺失 + 不合规红标 | tsc/构建通过 |
| studioApi | RenderOptions.use_gen_hooks/gen_asset_ids；hookEstimate/hookGenerate；ComplianceReport.aigc.badge_burned | tsc 通过 |

### 验证口径

- 后端：**241 passed**（240 + 选择用例 1），零失败
- 前端：tsc ✅ 生产构建 ✅ 827 测试 824 过/3 挂（逐一核对=已知 Node24 基线同位置，零新增）
- 上游代码零改动（本批未新增缝合点，页面为自研文件扩展）

### 已知边界

- 浏览器交互走查待真实素材（mock 产物为占位文本，画廊已按 mock 分支渲染不播放）
- cloud 生成仍需配置+付费开关（UI 选 cloud 未配置时后端显式报错展示）

## 0000000. 前批交付（生成素材渲染集成 + AIGC 角标 + 偏好回流，240 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| P3-f 渲染集成 | render_ffmpeg 重构为「图模式/快路径」两分支：L3 生图→片头 2s 静帧 B-roll（loop+trim+分辨率/帧率/SAR 对齐 concat）；L4 生视频→片头冷开场（trim 对齐）；前置段等长静音 concat（v1 前置无原声，回执标注）；字幕时间整体偏移；测量链含前置静音保证响度准确；**渲染器层 mock 防线**（文件头 MOCK_ 直接拒绝） | test_render_gen.py 6 例（真实 lavfi：B-roll 8s/生视频 8s/组合剪辑 7s/无字幕也烧角标/mock 拒绝） |
| P3-g AIGC 角标 | ASS AIGC 样式（右上角小字半透明「AI生成」全程）；任何 AI 内容（TTS/B-roll/生视频）编排器强制 aigc_badge=True；元数据 comment 同步；合规层 aigc_labeled 核对闭环 | 角标用例 + meta hooks.aigc_labeled |
| P3-h 编排器/API | use_gen_hooks 开关（默认关）：生成素材快照查询→mock 禁入显式降级→**L4 生视频替代原生 cold_open（避免双钩子）**；API RenderBody.use_gen_hooks | 编排器级用例（mock 拒+真实用+替代决策+18s 时长） |
| P3-i 偏好回流 | studio/feedback/preference.py：clips1 采纳/否决→按 clip_type 采纳率（n 明示）+否决理由高频词；prompt 偏置块（n≥3 才给类型偏置，无记录不注入噪声）；注入 analyze user prompt（铁律后缀不受影响）；回执 preference_applied | test_preference.py 3 例（统计/门槛/管线注入） |

### 纪律与边界（如实标注）

- 本批零费用：生成素材测试用 lavfi 真实媒体（非 AI 生成），真实 AI 素材待 Key
- 前置段 v1 无原声（等长静音）：生视频自带音轨的混音留后续批
- 本地 Wan 引擎仍留 5080 实测批（GenProvider 接口同构）
- 偏好偏置仅方向性（小样本不夸大），不改变契约铁律；否决关键词仅统计不自动改规则
- 渲染器重构后全量回归 240 绿，P0-P2 既有行为零破坏

### 测试口径

- 自研 studio：**240 passed**（P3 主批 231 → +9：渲染集成 6/偏好 3），零失败
- 前端本批未改动（use_gen_hooks 开关 UI 在前端批补）；上游代码零改动

## 000000. 前批交付（P3 生成式钩子 L3/L4 + 回流对账，231 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| P3-a 提供商适配层 | studio/engines/gen_providers.py：mock_image/mock_video（零费用，产物 meta.mock=True 绝不冒充）；cloud_image/cloud_video **配置驱动**（GEN_*_BASE_URL/ENDPOINT/API_KEY/UNIT_PRICE 来自 ctx.env，**代码不预设任何猜测端点**，未配置→显式不可用原因）；transport 可注入（测试零网络）；响应解析宽容标注【未核实】待 Key 到位按官方文档收紧 | test_hook_gen.py 9 例 |
| P3-a 费用确认服务 | studio/pipeline/hook_gen_service.py：**预估先行→confirm=true 才生成（绝不静默计费）**；L4 仅 rank top3 / L3 top5（门槛回执明示）；cloud 需 allow_paid_generation 开关；max_cost 预算上限超限拒绝；prompt 优先 hook_plan.l3_broll/l4_gen；单条失败不阻断其余（宁缺毋滥）；产物 kind=hook_image/hook_video + aigc=True 强制属性 | 确认强制/门槛/预算/开关/mock 标记全用例 |
| P3-b API | POST /hooks/estimate、POST /hooks/generate（confirm=false→400） | API  flow 用例 |
| P3-c content_id | 渲染 mp4 产物稳定键 `{project[:8]}-{candidate[:8]}`（重渲染不变）；发布包 meta.json 带 content_id（CSV 回流可携带） | 回流匹配用例依赖 |
| P3-d 回流对账 | studio/feedback/reconcile.py：CSV 导入（中英文表头别名/万单位/多日期格式）；**保守匹配** content_id 精确>URL 精确，多命中=歧义显式列出，未匹配显式列出；A/B 报告（同平台/≥72h/≥500 播放门槛，按钩子策略分组，均值+互动率，**仅方向性结论+免责声明**，样本不足明示） | test_feedback.py 10 例 |
| P3-d API | POST /studio/feedback/import、GET /studio/projects/{pid}/ab-report | API 用例 |

### 纪律与边界（如实标注）

- **本批零费用**：全部用 mock 开发测试；真实生成等用户提供 Key+官方 API 文档后配置端点，
  届时先只发 1 次请求验证结构再批量（付费接口纪律）
- cloud 响应 schema【未核实】：直返 URL/b64/异步任务结构各异，Key 到位后按官方文档收紧
- **生成素材渲染集成未做**（下一子批）：hook_image/hook_video 目前生成+存储+预览（文件服务可下载），
  接入成片（B-roll 前置/视频冷开场）需渲染器第二输入 concat；**mock 素材禁止进真实渲染**（集成时强制校验 meta.mock）
- AIGC 显式角标烧录在渲染集成批实现（当前 aigc=True 属性+合规层核对）
- 本地 Wan 引擎留 5080 实测批（gpu-local 档案注册即用，GenProvider 接口同构）
- 偏好回流（P3-6 采纳/否决→prompt 偏置）未做，后续批
- 过程修正 1 处测试漏传 provider_name（产品逻辑正确）

### 测试口径

- 自研 studio：**231 passed**（前端批 212 → +19：生成钩子 9/回流 10），零失败
- 前端本批未改动；上游代码零改动

## 00000. 前批交付（前端批 F：审阅台/钩子工坊/合规发布 UI，212 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| F-1 媒体文件服务 | GET /studio/artifacts/{aid}/file：**路径仅从 DB 产物记录取**（不接受任意路径参数，杜绝目录穿越）；mp4/cover/zip/evidence 按 kind 给 MIME | test_api.py +2 例（正常下载/404/文件缺失/平台预设） |
| F-2 API 封装扩展 | studioApi.ts：render/artifacts/artifactFileUrl/compliance/evidence/pack/platforms/draft + 类型（RenderOptions/PlatformPreset/ComplianceReport/PackReceipt）；patchClips1 支持 hook_plan | tsc 通过 |
| F-3 成片审阅台 | StudioRenderPage：视频预览（本机文件服务）+ **五平台安全区遮罩切换**（红色半透明遮挡区，比例来自 /studio/platforms）+ 封面/时长/画幅/缺陷扫描展示 + 完整回执折叠查看 | tsc/构建通过；浏览器走查待真实素材 |
| F-4 渲染/钩子工坊 | 同页：九个剪辑/钩子开关 + L2 rank 门槛 + 平台选择；**L1 悬念句/L2 配音文本人工编辑**（失焦保存 hook_plan）；渲染数字进度轮询（done/total/unit）+ 降级 Alert + 取消 | 同上 |
| F-5 合规与发布页 | StudioCompliancePage：阻断项/授权状态/违禁词表（时间码定位）/变形度自评（明示启发式）/AIGC 标识状态；留证包生成；发布包多平台构建+警告展示+ZIP 下载；剪映草稿导出（服务地址输入+回执展示含未核实标注） | 同上 |
| F-6 验证 | 后端 **212 passed**；前端 tsc ✅ 生产构建 ✅ 827 测试 824 过/3 挂（逐一核对=已知 Node24 import.meta 基线，零新增）；路由缝合台账 #3 | 日志 logs/frontend_test_f.txt |

### 启动与入口

```powershell
.\.venv\Scripts\python.exe -m uvicorn studio.api.app:dev_app --port 26761
cd frontend; npm run dev
# 审核台 /#/studio/review → 逐字稿 /#/studio/transcript/:id
# 渲染/钩子工坊+成片审阅 /#/studio/render → 合规发布 /#/studio/compliance
```

### 已知边界（如实标注）

- 页面验证到 tsc/构建/测试层；**浏览器交互走查待真实素材**（mock 渲染产物非真实视频，预览需真实渲染后走查）
- 安全区遮罩比例为经验值（页面数据源 /studio/platforms），须真机校准
- 剪映草稿导出需 capcut-mate 服务已部署；页面回执保留「格式版本待实测」标注
- SSE 仍未接（1.5s 轮询 task 端点，数字进度可见）

## 0000. 前批交付（P2-6 剪映草稿 + P2-7 合规四件套 + P2-8 发布包，210 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| P2-6a 许可证核实 | **capcut-mate = Apache-2.0**（官方 LICENSE 原文抓取确认；二手文章称 MIT 不采信）；docs/LICENSE_AUDIT.md 审计记录+集成义务；销项 04 §15 未核实项 #2 | 官方 raw LICENSE 全文 |
| P2-6b 草稿导出 | studio/export/capcut_mate.py（REST 客户端，可注入 transport，响应缺关键字段显式报错不伪造）+ draft_service.py（只读 adopted clips1 成片；字幕按剪辑段重映射 remap_ms；微秒换算；本地路径回退标注「支持度未核实」）；API POST /draft（服务不可达 502/业务错 400） | test_draft.py 7 例（fake transport 零网络：请求构造/段映射/失败路径） |
| P2-7 合规四件套 | studio/compliance/report.py：①违禁词 lint（保守词表 v1：广告法绝对化+导流词，命中定位字幕时间码，只报告不阻断）②变形度自评（二创要素加权 0-100，**明示启发式非平台判定**）③AIGC 核对（TTS/生成钩子须标识；渲染器已写元数据 comment，L3/L4 角标留 P3）④授权台账核对（无授权需声明/过期阻断/范围不符警告）；留证包 evidence.json（血缘+决策回执+产物 SHA256+素材指纹） | test_compliance_pack.py 合规 8 例 |
| P2-8 发布包 | studio/export/platforms.py（五平台预设：画幅/封面比例/标题文案上限/安全区经验值**标注须真机校准**）+ pack.py（每平台目录：成片+封面+title/caption+safe_area.json+meta.json→ZIP；**画幅不匹配显式警告不静默拉伸**；文案超限截断+原文保留）；API POST /pack、GET /platforms、GET /compliance、POST /evidence | 发布包 4 例（抖音匹配/B站警告/截断/ZIP 内容） |

### 关键设计决策（如实标注）

- capcut-mate 集成=独立服务 REST 对接，不复制源码 → 我方代码不构成 Apache-2.0 衍生作品；
  分发服务时随附 LICENSE/NOTICE。**真实服务验收待部署**（本机无 Docker，fake transport 只验请求构造，不冒充端到端）
- 草稿格式版本由 capcut-mate 维护，剪映打开验收待实测（回执已标注）
- 变形度分数只表达「我方增量加工量」，平台搬运判定以其自身规则为准——不夸大
- 安全区比例为经验值，前端遮罩预览数据源 GET /studio/platforms，真机校准后更新
- 违禁词表保守 v1，误报由人工审核台处置；词表可项目级扩展

### 测试口径

- 自研 studio：**210 passed**（P2-4/5 190 → +20：草稿 7/合规与发布包 13），零失败
- 过程修正 1 处测试断言（同句多词命中应验集合而非首元素），lint 产品行为正确
- 前端本批未改动（遮罩预览/合规页 UI 在后续前端批接）；上游代码零改动

### P2 阶段总结（P2-1 ~ P2-8 全部完成）

渲染规范化 → 去气口 → 原生钩子 → L1/L2 制作钩子 → 剪映草稿 → 合规 → 发布包，
自研测试 42（P0）→ 122（P1）→ 210（P2）。M2 里程碑（成片可直接发+剪映草稿+合规报告）代码层达成，
**真实客户试用验收待：真实直播样本 + capcut-mate 部署 + 剪映真机打开**。

## 000. 前批交付（P2-4 L1 文字贴片 + P2-5 L2 TTS 钩子，190 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| P2-4a 决策树 | studio/pipeline/hook_decision.py：**决策代码做，LLM 只填内容**——原生钩子已用→不叠 L1；否则 L1 必做（写不出自动不加）；L2 按开关+rank 门槛（默认 80）；悬念句≤20字归一化截断 | test_hooks.py 决策树 4 例 |
| P2-4a L1 引擎 | studio/engines/hooks_text.py：hook_plan.l1_text > 标题 > 金句回退；空→None 宁缺毋滥；产物为贴片描述（2s/上1/3/大字描边/淡化） | L1 引擎 2 例 |
| P2-4b 渲染贴片 | ASS Hook 样式（Alignment 8 顶部居中、MarginV≈res_y/6 上 1/3、字号 96/80、描边 4、\fad(200,300)）；快路径与段式路径都支持；v1 固定安全区（人脸避让留后续批） | 真实媒体贴片用例 + ASS 样式单测 |
| P2-5a TTS 引擎 | studio/engines/hooks_tts.py：edge-tts 主（SDK 直连微软官方服务）→ **Windows SAPI 离线兜底**（scripts/sapi_tts.ps1 纯 ASCII+UTF-8 文本文件防 GBK）；双路失败→None；空产物不冒充成功；合成引擎落 meta 可审计 | 注入 3 例 + **SAPI 兜底真实合成**（模拟 edge 故障，离线） |
| P2-5b 渲染混音 | TTS 叠片头+原声闪避（volume enable between(t,0,d)）；**apad 有限填充 whole_len**（无限 apad+amix 实测可挂起，诊断 A 60s 超时证据）；测量链与成片链一致含 TTS，响度归一准确；快路径/段式路径都支持 | 真实媒体混音 2 例（plain+段式，时长不变/双流齐全） |
| P2-4/5c 接线 | 编排器：决策树驱动，LLM 写悬念句（失败回退标题），L1/L2 生成失败显式降级跳过；回执 hooks.decision/l1/l2/skipped；API 三开关（l1_overlay 默认开/l2_tts 默认关/l2_rank_threshold）；ClipPlan 加 rank_score | 管线级 L2 门槛全链路用例（rank90 混音成片/rank50 跳过） |

### 本批排障记录（诚实定性）

1. **TTS 混音「时长截断」疑案**：初报 2.3s/3.3s 截断，先假设 apad 竞态→有限填充后仍现→打印真实命令发现
   `-to 5.300`：**测试夹具词表只覆盖到 5300ms，词边界吸附合法前移出点**，非产品缺陷。修正夹具（词表铺满切片）。
   教训：吸附行为会改变实际切点，断言期望值必须按吸附后计算
2. **apad 无限填充加固保留**：诊断 A（无限 apad+amix）真实挂起 60s——独立成立的健壮性修复，
   TTS/whoosh 链统一 whole_len 有限填充
3. L1 贴片默认开但**原生钩子已用时自动不叠加**（决策树强制，避免双钩子稀释）

### 测试口径

- 自研 studio：**190 passed**（P2-2/3 175 → +15：决策树/L1/TTS/渲染混音/管线门槛），零失败
- SAPI 兜底为真实离线合成（Windows Huihui zh-CN）；edge-tts 真实网络路径本机已知 NoAudioReceived，
  测试以 monkeypatch 模拟故障验证兜底，不依赖网络
- 前端本批未改动；上游代码零改动

## 00. 前批交付（P2-2 去气口 + P2-3 原生钩子，175 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| P2-2a 剪辑决策纯逻辑 | studio/pipeline/editlist.py：三重判定去气口（词间隙>0.6s ∧ silencedetect≥80%覆盖 ∧ 能量VAD无人声，缺一不剪）；保留呼吸口（两端各留 keep/2）；保守口头禅（仅独立单字语气词 嗯/呃/唔+两侧停顿≥250ms，语流中不剪）；段列表构建（flash+cold_open+正文保留段）；源→输出时间重映射 | test_editlist.py 17 例（含笑声能量拒绝/语流口头禅不剪/重叠剪除/跳剪不重复） |
| P2-2b 渲染器剪辑路径 | render_ffmpeg：trim+setpts+concat 段式滤镜图（输出顺序=段顺序）；段式 ASS 字幕（被剪词落选、钩子副本片头再现）；段式 loudnorm 测量（对 concat 后音频）；期望时长按段求和自检；剪辑决策全量落 meta.editlist 回执 | test_render_edits.py 8 例（真实 lavfi：剪1s→5s/冷开场→7s/跳剪→6s/闪现→6.5s） |
| P2-3a 原生钩子 | cold-open：钩子句复制到片头（原位保留/跳剪可选）；flash-forward：0.5s 高能闪现+whoosh（粉噪扫频 0.5s 淡入淡出，amix 叠片头）；区间越界/空 → ValueError（上层捕获自动跳过） | 同上 + 管线级定位用例 |
| P2-3b 编排器/API 接线 | Pipeline._plan_edits：quote 确定性定位钩子句（归一化去空格，切片内句界即词界）；响度峰定位闪现（0.5s 窗 top1）；**定位失败自动跳过+显式记录不阻断**；media.scan_silence/range_rms 两个实测信号源；API RenderBody 六开关（debreath/filler_cut/keep_breath_ms/cold_open/cold_open_jumpcut/flash_forward） | test_render_hooks.py 3 例（真实「语音+静音间隙」夹具：长间隙被剪短间隙不剪/钩子定位 18s/定位失败跳过 12s） |

### 关键设计决策（如实标注）

- **能量 VAD 为 v1**（RMS 阈值 0.01）：神经 VAD（如 FSMN/silero）需模型下载批准，后续升级；回执 vad=energy_rms_v1 如实标注。笑声/尾音有能量 → 拒绝剪除（已测）
- **cold_open 默认开**（04 §7.3 原生钩子应做默认策略）：quote 定位成功即用，失败自动跳过；flash_forward/filler_cut/debreath 默认关（侵入性/保守原则）
- 口头禅词表极保守：仅 嗯/呃/唔 单字独立出现才剪，「那个/就是」等可能有语义的词不入选
- 剪辑路径全量解码（无输入 seek），正确性优先；stream-copy 快路径仍留后续批

### 测试口径

- 自研 studio：**175 passed**（P2-1 148 → +27：editlist 17/渲染剪辑 8/管线钩子 3，含既有回归），零失败
- 管线级钩子测试用 numpy+wave 合成「正弦 bursts+真实静音间隙」夹具（2-3.2s 长间隙/6-6.3s 短间隙），silencedetect/RMS 全真实测量
- 前端本批未改动；上游代码零改动

## 0. 前批交付（P2-1 渲染引擎规范化，148 passed）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| P2-1b 渲染核心 | studio/engines/render_ffmpeg.py（既有核心复核+修复）：词边界吸附（空区间保护）、30ms afade、两遍 loudnorm（I=-14 linear）、逐词高亮 ASS（语义断行≤12字、最后烧录）、竖屏 v1 中心裁切（回执如实记录模式） | test_render.py 14 例（真实 lavfi 媒体，输出响度实测 -14±2 LUFS） |
| P2-1c 封面选优 | studio/engines/cover.py：响度峰提名（1s 窗 RMS top3）+均匀补位 2 帧 → 灰度拉普拉斯方差清晰度+曝光评分 → 黑场/白场淘汰 → JPEG 取优；全提名评分落回执；全淘汰抛错宁缺毋滥 | test_cover.py 7 例（含黑场全淘汰拒绝） |
| P2-1c 扩展自检 | render_ffmpeg.scan_defects：blackdetect≥0.5s / silencedetect≥1.5s / freezedetect≥2s，问题写 selfcheck.defects 回执（警告级不阻断）；主体出框需人脸检测留后续批 | 干净夹具零缺陷 + 黑场静音夹具双检出 |
| P2-1d 渲染阶段 | Pipeline.render_project：**只读 adopted=True 的 clips1**（无采纳显式拒绝）；转写词级时间戳注入；产物落 studio_artifact（mp4+cover）；封面失败显式降级不阻断成片；数字进度/取消/失败落库沿 P0 语义 | test_render_pipeline.py 5 例（含真实媒体端到端：25s 素材→16s 成片+封面） |
| P2-1d API | POST /studio/projects/{pid}/render（后台线程+409 互斥，业务错误落任务回执）；GET /studio/projects/{pid}/artifacts | test_api_render_flow / 404 用例 |

### 本批修复的真实缺陷

1. **ASS 滤镜路径双层转义**：ffmpeg 滤镜字符串经滤镜图+选项两层解析，单层 `\:` 在第一层被吃掉，Windows 盘符冒号被当选项分隔符截断路径（真实渲染全挂）→ 改双反斜杠 `\\:`，单测同步修正
2. **缺陷扫描正则不容空格**：ffmpeg 各滤镜输出格式不一（black_start:0 无空格，silence/freeze 冒号后带空格）→ 正则加 `\s*`

### 测试口径说明

- mock 逻辑测试必须显式禁用 x264（本机有 ffmpeg 时渲染链真实胜出）：`_mock_render_registry` 注入不可用桩，同时验证降级记录；真实渲染走独立 lavfi 端到端用例
- 自研 studio：**148 passed**（P1 122 → P2-1 148，零失败）；前端本批未改动

### P2-1 已知边界（不阻塞后续批）

- 竖屏为人物取景前的中心裁切 v1（回执标注 center_crop_v1）；人物取景沿上游能力在后续批接
- stream-copy 快路径未做（当前全重编码，正确性优先）
- 封面提名基于成片内响度峰（非全片）；去气口/cold-open 在 P2-2/P2-3
- 缺陷扫描为警告级记录，自动自愈（修复后重检）留后续批

## 1. 前批交付（P1-1 ~ P1-7 全部完成）

| 任务 | 交付物 | 验收证据 |
|---|---|---|
| P1-1 选段契约 | studio/contracts/（text.py + selection.py）：JSON Schema 强校验、句/词边界吸附（偏移>2s 丢弃）、时长硬约束（live_type 预设+项目覆盖）、quote 防幻觉（归一化子串+容错≤2字）、NMS（IoU>0.5 留高分）、批内归一 40-99（<60 标 not_recommended）；编排器内联校验全部替换为契约层 | tests/studio/test_contracts.py 24 例 |
| P1-2 复审环 | studio/pipeline/review.py：judge→repair→rejudge；修复边界代码级校验（必须精确句界/至多相邻一句/时长/quote 仍有效）；每轮输入输出落 task.receipt.review；Settings.review_loop 开关默认开 | test_review.py 8 例 + 管线回执断言 |
| P1-3 提示词资源 | studio/prompts.py：CRUD+版本递增（body 变更才+1）+引用保护；解析优先级 项目 prompt_id > live_type 最新 active > 内置默认；铁律后缀始终保留；intent/background 注入验证 | test_prompts.py 6 例（含管线注入） |
| P1-4 信号 | studio/engines/signals_danmaku.py（录播姬 XML/抖音 JSONL 双格式，5s 分桶，同文本封顶 3/同用户封顶 5，无弹幕→空 series 自动降维）；signals_audio.py（ffmpeg 8kHz PCM 流式分桶 RMS，解码失败返回空不造假零值） | test_signals.py 13 例（含 lavfi 正弦 vs 静音真实媒体） |
| P1-5 信号融合 | studio/pipeline/signals_context.py：LLM 摘要（每路 top5 高能桶）；共振加成（同桶≥2 路≥p80 才加分，+2/桶封顶+8，rank 封顶 99，证据落 scores.signal_evidence）；无信号零改动（A/B 对照臂） | test_signals_context.py 8 例 |
| P1-6a 审核服务 | studio/review_service.py：clips0 只读；采纳/否决/精修全落 clips1（parent_id 溯源，created_by=user:<id>）；边界校验 0≤start<end≤素材时长；逐字稿查询 | test_review_service.py 8 例 |
| P1-6b API | studio/api/app.py：素材入库（ffprobe 探测+指纹）/项目 CRUD/候选审核/逐字稿/任务查询/管线后台线程 run+cancel（重复 run 409）/提示词 CRUD/引擎探测；ReviewError→400、不存在→404；dev_app 工厂 + 本机 CORS | test_api.py 9 例；真实启动冒烟通过（engines/projects 端点） |
| P1-6c 前端 | frontend/src/services/studioApi.ts + pages/StudioReviewPage.tsx（审核台：项目创建/运行/进度轮询/降级提示/采纳/否决/精修）+ StudioTranscriptPage.tsx（句级时间码+词级+热词）；App.tsx 路由缝合（台账 #2） | tsc 通过、生产构建通过、827 测试 824 过/3 挂=基线零新增 |
| P1-7 说话人分离 | studio/engines/diarize_campp.py（CAM++，懒加载/显式探测/秒毫秒双格式/格式不符显式报错不伪造）；Settings.diarize_enabled 默认关；**diarize 开关纳入转写缓存键，分离后才写缓存**（修复了分离标签不落库的真实缺陷） | test_diarize.py 7 例（注入测试） |

## 2. 测试基线（P1 收口）

- 自研 studio：**122 passed**（P0 42 → P1 122，零失败）
- 前端：tsc ✅ / 生产构建 ✅ / 827 测试 824 过 3 挂（3 挂均为已知 Node24 import.meta 基线，已逐一核对）
- 上游 backend 基线口径不变（4340 过/92 环境挂，见 06 报告）；本批未改上游 backend 代码
- 测试封闭性：tests/studio/conftest.py autouse fixture 把 MODELSCOPE_CACHE 指向空临时目录——本机已下载真实模型不再污染测试（P1 期间发现并修复的环境耦合问题）

## 3. 本批修复的真实缺陷（非测试问题）

1. ParaformerASR.available() 只查缓存根目录 → 改查具体模型权重文件
2. 编排器分离标签不落库（缓存在分离前写入）→ diarize 纳入缓存键 + 分离后写缓存
3. CAM++ _to_ms 启发式误判显式毫秒字段 → 显式 *_ms 字段直接按毫秒
4. mock 候选 4s 不符合电商 15-60s 硬约束 → mock 语速改 300ms/字、候选 16s（mock 数据缺陷）
5. 响度引擎 ffmpeg 失败输出假零值 → returncode≠0 返回空 series 走降维

## 4. 已知边界与待办（不阻塞 P2）

- **CAM++ 真实验收未做**：模型未下载（用户仅批准 SeACo-Paraformer）；输出段格式标注【未核实】，下载批准后先实测格式再验收
- **云 ASR 实测待百炼 Key**（用户提供时先说明预估费用）
- **真实直播录音样本验收待用户提供**（当前 ASR 验收为 SAPI 合成样本）
- 审核台 UI 为功能可用版：波形时间轴/逐词切点拖拽/快捷键/安全区遮罩在 P2 渲染批增强；UI 浏览器走查待做（本批验证到 tsc/构建/测试层）
- 分离结果随转写缓存整体重算（无独立分离缓存），P1 可接受
- SSE 进度流未接（前端当前 1.5s 轮询 task 端点，数字进度已可见）；SSE 在 P2 按需加
- 逐字稿页只读；即点改错/热词管理在 P2 与转写纠错合并
- studio API 与上游服务的挂载缝合、Docker Compose 在部署批做（当前独立 uvicorn 26761）

## 5. 启动方式（本机开发）

```powershell
# API（数据 .data-dev/studio.db，与上游示例服务隔离）
.\.venv\Scripts\python.exe -m uvicorn studio.api.app:dev_app --port 26761
# 前端
cd frontend; npm run dev   # 审核台入口 /#/studio/review
```

## 6. 下一批（P2 钩子/成片/合规/剪映）

依赖本批的 clips1 数据模型与审核流。首批任务：P2-1 渲染引擎规范化（04 号 §10）、
P2-6 前先核 capcut-mate 许可证。GPU 效果验收一律留 5080（用户硬件约束）。