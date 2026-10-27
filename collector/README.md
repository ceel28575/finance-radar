# MX 本地采集恢复版

1. 安装 Python 3.10 或更新版本（安装时勾选 Add Python to PATH）。
2. 解压整个仓库，打开 collector 文件夹，双击 setup.bat 安装依赖。
3. 双击 start.bat，在弹出的专用浏览器中手动登录 MX 并填写验证码。此浏览器与平时浏览器登录状态独立。
4. 保持窗口开启。资料去重保存在 mx_outbox.db，每 60 秒导出到 exports/YYYY-MM-DD.json。
5. 在财经雷达网站点击「导入数据」，选择当日 JSON。重复导入按 ID 合并。

此版本只做本地采集与导出，不上传旧 Base44 后端，不需要 COLLECTOR_TOKEN，不会自动生成 AI 研判。60秒是导出间隔；遍历全部房间可能超过一分钟。尚未在你的 Windows / MX 登录环境完成验收。旧采集逻辑对重名房间保守跳过并记录状态，图片仅记录引用，OCR未接入。没有有效原文时间的记录保留在数据库中但不进入当日导出。

mx_profile 为专用登录会话，mx_outbox.db、exports 含私人资讯，不要上传 GitHub。Ctrl+C 停止。
