# AI Newsroom · AI 新闻编辑台

面向个人创作者的 AI 新闻采编与微信公众号草稿工具。使用 Python、FastAPI 和原生 JavaScript，支持一键启动、页面配置模型、按篇数采编、新闻原图与概念插画，以及批量建立微信草稿。

Local-first AI news collection, evidence-based writing, illustrations, and WeChat draft delivery. Bring your own model API credentials. The first launch uses fictional demo data and makes no model or WeChat calls.

**当前为个人项目的实验性开源版本，仍有已知问题。它不会自动正式发表文章，也不包含安卓 APK。**

## 功能

- 采集官方公告、研究与 AI 媒体，检查来源和时效，去重、排序并对照同事件材料。
- 提示词模型合并提炼证据与整篇策划，写作模型生成中长篇；不固定评论比例或小编角色。
- 优先获取原报道与官方图片；策划可生成插画 brief，调用独立图片服务按缺口补图。
- 支持 Chat Completions 兼容文字服务，以及 MiniMax 原生／兼容图片服务。
- 页面配置 API、微信凭据和代理，提供连接测试、用量提醒、缓存、运行记录与任务控制。
- 手动选择本轮 1–20 篇，或配置北京时间定时任务。
- 编辑、预览、导出 HTML／Markdown／ZIP，一键批量提交未提交的当前版本到微信草稿箱。
- 微信错误报告可显示需加入白名单的出口 IP；结果未知时先对账，避免盲目重复创建。

## 快速开始

需要 Python 3.11+。首次安装依赖需要网络；无需 Node.js 前端构建，也不需要 GPU。

```bash
git clone https://github.com/NagasakiSaki/ai-newsroom.git
cd ai-newsroom
python launcher.py
```

Windows 也可以双击 **一键启动.cmd**。启动器创建 .venv、安装锁定依赖、复制空白示例配置并打开 http://127.0.0.1:8088，通过一次性登录票据自动进入页面。

- 首次为演示模式：虚构新闻、确定性模型，不消耗模型 API，也不创建真实微信草稿。
- 默认定时关闭，额外润色与独立复审在示例配置中关闭。
- 关闭浏览器不代表停止后台。Windows 使用 stop.cmd；也可运行 `python launcher.py --stop`。
- run-now.cmd 或 `python launcher.py --run-now` 会请求立即采编；真实模式可能产生费用。

## 切换真实使用

1. 打开“API 与网络设置”，填写写作与策划服务的 URL、模型 ID 和自己的 API Key。同一服务可继承连接，但需要有效的模型 ID。
2. 关闭演示模式；保存配置本身不会测试模型，测试按钮才会发出实际请求。
3. 生图是可选服务，在页面单独配置协议、地址、模型和凭据。MiniMax 原生路径为 `/v1/image_generation`，不能当成兼容接口再次拼接路径。
4. 如需建立微信草稿，填写自己的 AppID／AppSecret，处理白名单与账号权限，先测试连接，再启用草稿交付。令牌测试成功不代表草稿权限已验证。
5. 从一篇开始，查看正文、图片、实际调用和 token 消耗，再决定是否开启定时或自动审定。

所有 API 费用由你自己的供应商账户承担。`0` 额度通常表示不限，不是零费用；token 提醒不是硬性预算封顶。生成图是概念示意，不能冒充现场照片或真实实验图。

## 已知限制

- 独立补插画任务尚未完全接入统一暂停／取消和重启恢复；见 [已知问题](docs/KNOWN_ISSUES.md)。不要假定全局暂停一定能拦住该任务。
- 生图每次最多四张，且受剩余方案数量限制；方案用完后仍可能缺图。
- 策划引用校验可能触发重复请求；长文长度、结构和完整配图尚不稳定，额外润色与复审可能明显增加成本。
- 代理、TUN、网站限流、反爬和动态出口 IP 会影响新闻图片下载或微信连接。
- 服务默认仅监听本机；尚未完成公网多用户、安卓或容器部署的产品化验证。

## 文档与开发

| 文档 | 内容 |
|---|---|
| [使用说明](docs/USAGE.md) | 启动、配置、采编、补图、草稿和常见问题 |
| [开发架构](docs/ARCHITECTURE.md) | 模块、数据、缓存和状态机 |
| [接口说明](docs/API.md) | 鉴权、请求、异步任务和费用边界 |
| [OpenAPI](docs/openapi.json) | 机器可读接口 |
| [已知问题与后续工作](docs/KNOWN_ISSUES.md) | 当前限制、优先级和验收要求 |
| [安全说明](SECURITY.md) | 凭据、本机访问与漏洞报告 |

```bash
python -m pip install -r requirements.lock.txt
python -m pytest -q
python scripts/export_api_docs.py --check
python scripts/check_secrets.py
```

修改接口后运行 `python scripts/export_api_docs.py` 更新机器文档。测试使用临时数据与替身客户端，不需要真实模型或微信凭据。首次开源打包前完成了隔离副本测试；具体版本记录见 [CHANGELOG](CHANGELOG.md)。

仓库附带 [GitHub Actions 模板](docs/ci.example.yml)。发布时的 GitHub 授权不含 workflow 权限，因此模板未放入 .github/workflows、不会自动运行；具有工作流权限的维护者可将其复制到 .github/workflows/ci.yml 后启用。

## 隐私与许可证

发布内容仅包括代码、公开来源配置、合成测试和通用文档。仓库不包含私人 .env、密钥、账号配置、数据库、日志、真实稿件、私有交接、抓取图片或生产环境历史。请勿将它们提交到 Git。

代码采用 [MIT License](LICENSE)。第三方库保留各自许可证；新闻文字、图片、商标和生成服务的使用权限需分别确认，不由本项目的 MIT 授权覆盖。
