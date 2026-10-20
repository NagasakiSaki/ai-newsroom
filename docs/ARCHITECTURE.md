# 开发架构

## 运行方式

FastAPI＋uvicorn 提供本机 HTTP API 与静态 HTML/CSS/JavaScript。后台 Engine 使用单进程 worker 处理任务，SQLite 存储 JSON records 与 events。服务固定监听 127.0.0.1:8088；没有生产公网多用户部署或安卓运行时。

launcher.py 准备环境、启动后台并签发短期一次性登录票据。管理员 Bearer 与模型 Key 是不同凭据。前端无需编译；修改 Python 后需重启，修改静态文件后刷新。

## 无模型采集与资料库

source_catalog.py 定义 CollectionSettings／CrawlSource，存于 source_settings/main，不改公开来源配置。保存带 revision 和活跃任务校验；内置 AI 包可关闭。

crawler.py 复用安全 HTTP fetcher、BeautifulSoup、feedparser、标准库 XML／robots 解析，读取 RSS、HTML、文章和站点地图，无模型／搜索 SDK。每轮受页数、深度、主机、大小、robots 与间隔限制。自定义代理／TUN 主机仍需独立公网 DNS 检查。

SourceControl.collect_catalog 合并内置包与自定义来源，在受控 collect 或 pipeline 中逐条入库。取消保留已存资料；未知日期可为 null，去重与排序能处理，不冒充新新闻。

library.py 本地查询标题／正文，提供 JSON／CSV 导出，无模型或原站请求。collection.js 默认打开资料库，主按钮采集、另设可选写作；只用 textContent 显示原文，不注入 crawl_html。正文最多 24,000 字符。

writing_rules 把主题、受众、文风传入可选模型。自定义源默认 allow_writing=false；允许后仍检查新闻时间与引用。source_settings 是数据记录，不是新的静态配置文件。

## 采编流程

```text
手动／定时
  → 复用近期采集或有界并发采集
  → 时效、来源与同事件筛选、批量评分
  → 提炼证据＋整篇策划（包含可选 illustrations）
  → 多源 crosscheck
  → 整篇写作，材料充分且偏短时最多一次增补
  → 可选 polish
  → 真实图片发现／下载、可选按缺口生图与排版
  → 可选独立 review、程序检查、本地交稿
  → 人工确认或符合门禁的自动审定
  → 建立微信草稿；正式发表人工处理
```

Review 关闭不代表取消 crosscheck。引用校验和 schema 通过不保证所有语义事实为真。热榜只影响候选排序，不作为事实证据。

## 代码导航

| 模块 | 责任 |
|---|---|
| app.py / server.py | 路由、鉴权、服务、静态资源 |
| engine.py / workflow_control.py | 队列、运行状态、检查点、单 worker 锁 |
| collect.py / sourcing.py / source_quality.py / crosscheck.py | 来源采集、时间、原文、同事件与引用检查 |
| llm.py / model_contracts.py / models.py | 文字任务、模型请求、结构契约、缓存及用量 |
| editorial_team.py / article_style.py | 整篇策划写作、长度、润色、图文与版式 |
| visual_discovery.py / images.py | 真实图发现、公网检查、下载、解码、哈希与存储 |
| image_protocol.py / image_service.py | MiniMax／兼容生图、逐张缓存与缺口补图 |
| actions.py / draft_workflow.py / publication.py / wechat.py | 本地编辑、版本、审批、微信草稿与传输状态机 |
| runtime_config.py / connection.py / network.py | 配置脱敏、存储、连接检测与分流 |
| schedule.py / trending.py | 定时计划、公开热榜与缓存 |
| render.py / handoff.py | 预览与导出交稿包 |
| static/ | 无打包的前端界面；draftdesk.js 后加载功能覆写 |
| config/ | 公开信源、来源策略、编辑规则、兼容模型／小编定义 |

## 数据与缓存

SQLite 表 records(collection,id,data JSON) 保存 stories、drafts、runs、jobs、calls、image_calls、model_artifacts、publications、draft_batches、source_health、source_settings、preferences 和 schedule 等；events 保存过程事件。

Store.all 按插入 rowid 倒序，更新不会自动移到前面；诊断按显式时间排序。文字 usage 与生图次数分开计算。run.timings 是阶段最近一次进入时距本轮起点的秒数，不能当作分段累计时长。

结构化结果与可复用正文缓存绑定模型、服务、输入、提示词及 schema。图片保存原文件与展示／微信副本，按哈希去重。生图缓存绑定协议、端点和实际请求体；失败冷却绑定凭据指纹，修正凭据可隔离旧失败。

## 任务与外部写入

Engine 使用进程锁、RLock 与线程本地任务绑定，不直接增加 uvicorn workers 或并行整篇流水线。原图网络下载最多四路预取，数据库、文件归档与占位仍在主线程串行。

已接入控制的种类为 pipeline、collect、review、draft_batch、images。独立 illustrations 尚不在 WorkflowControl.kinds，是优先修复项；增加调用处 checkpoint 本身不能替代任务绑定与恢复语义。

微信写入前持久化意图；账号、draft_id、revision、approved_hash 与 media_id 防止重复。明确可修正格式错误有限重试；网络超时、5xx 或解析失败的未知结果先对账。旧 cancelled 记录仍可能有 media_id，不能一概当作未写入。

publication.py 保留旧正式发布兼容状态；应用加载配置后 drafts_only=true，不能因旧 mode=auto 恢复正式发表。测试中的程序化 Settings 可能覆盖兼容路径，不代表桌面产品默认开放这些动作。

## 开发检查

```bash
python -m pytest -q
python scripts/export_api_docs.py --check
python scripts/check_secrets.py
```

接口修改后重导出 OpenAPI 与领域 schema。使用替身网络客户端与临时数据做回归；真实单篇验收应显式记录请求数、最终 token、图数、总耗时和微信实际结果。当前待办见 [已知问题](KNOWN_ISSUES.md)。
