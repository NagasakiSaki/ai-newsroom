# 信息工作台 · Information Workbench

自定义信源网址和关键词，用内置爬虫获取公开信息，在本地阅读、搜索和导出。适用于教育、文化、政策、体育、财经、科研及其他主题，**采集不依赖大模型或付费搜索 API**。

Local-first, topic-agnostic collection from your own URLs: RSS/HTML/article/sitemap crawler, keyword filters, local full-text search and JSON/CSV export. Model-assisted writing, illustrations and WeChat draft delivery are optional.

仓库保留 ai-newsroom 名称兼容既有链接；v0.2.0 起界面为通用信息工作台。仍是实验性个人项目，未提供安卓 APK 或生产多用户服务。

## 不配置 Key 也能使用

| 能力 | 是否需要模型 API |
|---|---|
| 配置信源网址、主题、关键词和排除词 | 不需要 |
| RSS / Atom、网页列表、文章、XML 站点地图抓取与正文提取 | 不需要 |
| 本地全文搜索、阅读、来源筛选、JSON／CSV 导出 | 不需要 |
| 策划与写稿、可选润色／复审 | 需要自己的文字模型 |
| 生成概念插画、存入微信草稿 | 分别需要图片服务／公众号凭据 |

关键词只筛选登记站点和本地资料，**不是全网搜索引擎**。不支持 JavaScript 渲染、登录、验证码、付费内容或 PDF 提取。

## 快速开始

需要 Python 3.11+，首次安装依赖需要网络，不需要 GPU 或前端构建。

```bash
git clone https://github.com/NagasakiSaki/ai-newsroom.git
cd ai-newsroom
python launcher.py
```

Windows 可双击“一键启动.cmd”。启动器准备 .venv 和空白配置，打开 http://127.0.0.1:8088，通过一次性票据自动登录。

1. 到 **“来源与策略”**，填写主题、包含关键词和排除关键词。
2. 关闭不需要的 **“内置 AI 新闻信源包”**；旧包只为兼容保留，不限制主题。
3. 点“添加信源网址”，选择自动、RSS、网页列表、文章或站点地图；必要时设置 CSS 选择器和额外域名。
4. 保存后点击 **“一键采集资料（无模型）”**。
5. 在默认打开的 **“资料库（无模型）”** 搜索、阅读正文和导出。

采集不必关闭写作演示模式，也不需填写假的 Key。原有完整采编单列为 **“采编写稿（需模型）”**，本次篇数只影响可选写稿。

## 内置爬虫

- 默认每源最多尝试 8 页，可设 1–30 页；网页深度 0–2，额外域名最多 10 个。
- 只跟随来源及明确允许的主机，支持 RSS 自动发现和站点地图索引。
- 每次请求验证公网地址与跳转，保留 HTTPS 验证，不发送模型或微信凭据。
- 自定义爬虫读取 robots.txt，遵循禁止路径与抓取间隔；404/410 视为没有规则，无法读取或被拒绝时停止该站。
- 包含词支持任意／全部命中，排除词任意命中即过滤；可设置来源附加词。
- 自定义资料可设日期范围，未知日期保留并标记，不把采集时间当发布日期。内置包仍沿用旧新闻窗口。
- 结果保存在本机 data/，相同 URL 更新快照；阅读、搜索与导出不重新请求原站或模型。

每条正文最多保存 24,000 字符。自动提取质量取决于站点结构，复杂页面可配选择器。未经允许或无日期资料作为参考材料保存，不绕过新闻时效条件自动写成“最新消息”。

## 可选模型与微信

在“API 与网络设置”填写自己的服务。策划接收主题、受众和文风，不再强制写 AI／科技稿。示例配置关闭独立复审、额外润色与定时写稿；真实 API 费用由供应商计收。

原有 Chat Completions、MiniMax 生图、新闻原图、手动篇数、本地文章导出和微信草稿能力保留。正式发表人工完成。

采集轮询与写稿定时独立：SOURCE_POLL_MINUTES 控制资料刷新，页面定时计划控制可选写稿。全局暂停会暂停受控采集；独立补插画仍有已知控制限制。关闭浏览器不停止后台，用 stop.cmd 或 `python launcher.py --stop` 停止服务。

## 文档与开发

| 文档 | 内容 |
|---|---|
| [自定义采集指南](docs/COLLECTION.md) | 网址、关键词、选择器、日期与范围 |
| [使用说明](docs/USAGE.md) | 启动、可选模型、草稿与费用 |
| [架构](docs/ARCHITECTURE.md) | 来源目录、爬虫、资料库与任务 |
| [接口说明](docs/API.md) | HTTP 请求与异步语义 |
| [OpenAPI](docs/openapi.json) | 机器接口定义 |
| [已知问题](docs/KNOWN_ISSUES.md) | 当前限制 |
| [安全说明](SECURITY.md) | 凭据与本机访问 |

```bash
python -m pip install -r requirements.lock.txt
python -m pytest -q
python scripts/export_api_docs.py --check
python scripts/check_secrets.py
```

接口修改后运行 `python scripts/export_api_docs.py`。测试使用临时数据与替身客户端。[CI 模板](docs/ci.example.yml)仍需有 workflow 权限的维护者手动启用。

## 隐私与许可证

不提交 .env、Key、AppSecret、口令、data/、真实稿件、图片或爬取结果；用户来源设置也存于 data/，不改公开 config/。服务仍默认本机单用户，请勿直接暴露公网。

采用 [MIT License](LICENSE)。第三方库、原文、图片、商标和模型服务保留各自权限，MIT 不授权复制第三方内容。
