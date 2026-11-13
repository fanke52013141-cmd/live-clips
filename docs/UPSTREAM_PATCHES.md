# UPSTREAM_PATCHES — 对上游 AutoClip 文件的改动台账

原则：自研代码进 studio/ 独立包；上游文件只做必要缝合点改动，每处登记于此，降低合并漂移。

| # | 日期 | 文件 | 改动 | 原因 | 验证 |
|---|---|---|---|---|---|
| 1 | 2026-11-13 | .gitattributes | 新增 *.ts/*.tsx/*.js/*.cjs/*.mjs/*.json/*.css/*.html → text eol=lf | 上游测试用 LF 正则扫描源码（output-share.test.cjs），Windows autocrlf=true 检出 CRLF 导致断言失败；上游规则未覆盖前端文件 | 前端重检出后 npm test 复测（见 06 基线报告更新） |
| 2 | 2026-11-13 | frontend/src/App.tsx | 新增 2 个 import + 3 条 Route（/studio/review、/studio/review/:id、/studio/transcript/:id） | P1-6c 切片审核台/逐字稿页入口；页面本体为新增文件（StudioReviewPage/StudioTranscriptPage/studioApi），不改上游既有组件 | tsc + 生产构建 + 前端测试对照基线 |
| 3 | 2026-11-13 | frontend/src/App.tsx | 新增 2 个 import + 2 条 Route（/studio/render、/studio/compliance） | 前端批 F-5 成片审阅/钩子工坊/合规发布页入口；页面本体为新增文件（StudioRenderPage/StudioCompliancePage），studioApi.ts 为自研文件扩展 | tsc + 生产构建 + 前端测试对照基线 |

## 环境级修复（不改上游代码，仅记录）

| 日期 | 问题 | 处置 |
|---|---|---|
| 2026-11-13 | desktop_main.py 启动横幅 emoji 在 GBK 代码页崩溃 | 启动环境设 PYTHONIOENCODING=utf-8 / PYTHONUTF8=1 |
| 2026-11-13 | git clone schannel SSL 握手失败 | `git -c http.sslBackend=openssl clone` 同一官方地址（不换镜像） |
| 2026-11-13 | studio/alembic.ini 被 Alembic 按 GBK 读取 | 自研文件，注释改纯 ASCII |