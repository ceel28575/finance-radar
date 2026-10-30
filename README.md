# 财经雷达 · 研究工作台

私有网站：https://finance-radar-jenson.sunlitbison257.chatgpt.site

## 当前功能

- Windows 本机 Playwright 登录 MX 后采集，SQLite 保留原始记录。
- 每 60 秒导出并尝试同步新增/变更内容；失败下轮重试。
- 私有网站 D1 按北京时间日期累计、按 ID 合并、按房间区分来源。
- 网页每 60 秒读取当日数据，可切换日期、搜索、按来源筛选、导入/导出。
- 自选、研究笔记、持仓、收藏、报告对比。自选/笔记/持仓/收藏仍是浏览器本机记录。

**当前边界：** 图片 OCR 尚未实现；同名房间稳定识别与完整房间覆盖验证仍待完成。双模型 AI（本地 Qwen + 云端 Doubao）已加入采集器代码，但需要 Windows 实机启动验证；不能将未验证的部署状态显示为已运行。60 秒同步不等于全部房间在 60 秒内扫描完。时分日期推算仍需校验。

## 使用

见 [采集器安装与升级](collector/README.md)。新版网站「采集与设置」提供同步配置下载；配置放入 `collector` 文件夹后启动。无配置时只导出本机，不上传。

## 代码与认证

- `dashboard.html`：原界面与云端读取/导入逻辑；`app/route.ts` 返回 HTML。
- `app/api/radar/[action]/route.ts`：接收、分页读取、状态、同步配置下载。
- `lib/radar-validation.mjs`：校验日期、大小、来源、报告及股票名称。
- `db/schema.ts` 与 `drizzle/`：数据库结构及迁移。采集数据不在代码仓库。
- `collector/local_collect.py`：启动本机采集、导出、同步线程。
- `collector/cloud_sync.py`：只向固定私有网站发送；不跟随重定向。成功确认后记录内容指纹，并同步 AI 引擎状态供网站诊断。

### 请求流程

1. MX 凭据由用户在本机 Chromium 中手动输入，会话保留在 `mx_profile`，网站不接收 MX 密码或 Cookie。
2. 网站保持 owner-private，由 Sites 平台负责 ChatGPT 登录和访问限制。浏览器请求通过平台鉴权后访问数据库。
3. 网站所有者登录后可下载 `cloud-config.json`；此接口要求平台注入的用户身份。
4. 采集器使用配置中的服务凭据，通过 `OAI-Sites-Authorization` 请求同一网站。平台验证并消费该凭据；不会给服务请求注入用户身份。
5. 接收端依赖 owner-private 平台边界；校验 JSON、来源与日期，事务写入 D1。浏览器跨来源写入另行拒绝。

服务凭据是该私有网站的访问权限，不是 MX 密码。服务端环境变量 `COLLECTOR_SITE_TOKEN` 仅用于所有者下载配置；不打包进浏览器代码。配置、登录档案、SQLite、导出文件、日志均忽略提交。**若将网站改为 public，必须先重新设计 API 身份校验，不可直接改变访问模式。**

## 开发与验证

使用 Sites 的 Vinext/Cloudflare Worker 构建，D1 绑定为 `DB`。源码由 GitHub 保存，托管部署仍由 Sites 执行；GitHub Pages 无法运行这些后端接口。

- `node --test tests/validation.test.mjs`
- `python tests/test_cloud_sync.py`
- `npm run db:generate`（仅 schema 变化时）
- `npm run build`

测试涵盖北京时间归档、来源过滤、非法时间、批次大小、买卖动作归类与否定/冲突处理、空股票池冻结、积压处理节奏，以及同步失败重试、重复和变更处理。Windows 实机升级后的同步仍需要用户运行新版验证。
