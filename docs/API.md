# HTTP API

Base URL：`http://127.0.0.1:8088`。完整机器契约：[OpenAPI](openapi.json)、[请求与领域 schema](schemas/domain-models.json)。以下示例只使用占位符，不含真实账号。

## 无模型来源设置与资料库（v0.2.0）

| 方法 | 路径 | 内容 |
|---|---|---|
| GET | /api/sources/settings | CollectionSettings：revision、topic、audience、writing_style、include_builtins、keywords、exclude_keywords、keyword_mode、max_age_hours、custom_sources |
| PUT | /api/sources/settings | 完整替换；revision 防覆盖，活跃采集／采编任务时 409；保存不抓网页 |
| POST | /api/sources/check | 启用来源的采集入队，与写作 demo 模式独立，不调用模型 |
| GET | /api/library | q、source_id、limit（1–200，默认 50）、offset，返回 total/items；q 空格分词需全部命中 |
| GET | /api/library/{story_id} | 已保存正文、时间和出处，不请求原站，不返回原始 crawl_html |
| GET | /api/library/export | format=json/csv，可传 q/source_id，最多 5000 条；CSV 防公式注入 |

CrawlSource 包含 id（custom- 前缀，缺省生成）、name、url、mode（auto/rss/html/article/sitemap）、enabled、max_pages（1–30）、max_depth（0–2）、delay_seconds（0.2–30）、article_selector、content_selector、extra_hosts（最多十个确切域名）、include_keywords、allow_writing。

```json
{
  "revision": 0,
  "topic": "公共教育",
  "audience": "社区读者",
  "include_builtins": false,
  "keywords": ["图书馆", "教育"],
  "exclude_keywords": ["广告"],
  "keyword_mode": "any",
  "max_age_hours": 0,
  "custom_sources": [{
    "name": "示例公告",
    "url": "https://publisher.example/notices/",
    "mode": "html",
    "max_pages": 8,
    "article_selector": ".notice-list a",
    "content_selector": ".article-body",
    "extra_hosts": ["www.publisher.example"],
    "allow_writing": false
  }]
}
```

示例域名是占位符。先 GET 再提交相同 revision；保存后版本递增。URL 拒绝认证信息、常见密钥／签名参数、私网地址或非常规端口；请求继续验证 DNS 与每次跳转。关键词是登记站点过滤，不提供全网搜索。

max_age_hours=0 表示自定义爬虫不按时间过滤；未知日期 date_basis=unknown、published_at=null。内置包仍用既有新闻窗口；允许写作不等于来源权威认证，也不绕过新闻时间门槛。正文最多 24,000 字符。

GET /api/state 新增 collection_settings，sources 返回内置包选择及自定义来源，editorial 使用当前主题／受众／文风。线索列表不返回原始 crawl_html。

## 鉴权与状态

除健康检查和一次性票据兑换外，/api/* 使用：

```http
Authorization: Bearer <local-admin-token>
Content-Type: application/json
```

管理员口令不是模型 API Key。启动器生成一次性登录票据，兑换后前端保存本地登录 token。静态资源不受相同的 /api 中间件保护，所以服务只应本机访问。

- 200：可能只代表任务入队；仍需检查 job、run、batch 的业务结果。
- 401：本机管理员鉴权失败。
- 404：资源不存在。
- 409：版本、编辑锁、状态或配置冲突，detail 解释原因。
- 422：请求 schema 不合法。

revision 是乐观并发条件，不能硬填 1 或忽略 409。记录时间通常为 UTC ISO 8601，计划按北京时间解释。

## 主要端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /api/health | 无鉴权的健康摘要 |
| GET | /api/state | 聚合状态、稿件、线索、近期任务与今日 usage；部分列表有截断 |
| GET / PUT | /api/config | 脱敏读取／部分保存模型、微信和图片配置；保存不测试 |
| POST | /api/config/models/{profile}/test | writer/prompt/review/visual 最小文字测试，可能收费 |
| POST | /api/run | 完整 pipeline 入队 |
| POST | /api/compose | 指定线索新写或按 revision 重写 |
| POST | /api/sources/check | 仅采集新闻，不调用文字模型 |
| PUT | /api/schedule | 保存 daily／interval 计划 |
| POST | /api/workflow/control | pause/resume/cancel，受任务种类限制 |
| PUT | /api/usage/preferences | run_alert_tokens，0 关闭软提醒 |
| GET | /api/wechat/status | 本地配置与历史测试摘要，无微信请求 |
| POST | /api/wechat/test-connection | 测试令牌，不上传素材或建草稿 |
| GET / PUT | /api/publication | 草稿交付设置 |
| POST | /api/publication/capability | 只读检测草稿相关能力 |
| GET | /api/drafts/{id}/preview | 当前 HTML 预览，不调用模型 |
| GET | /api/drafts/{id}/export/{format} | html/md/json/zip/wechat 导出 |
| PUT | /api/drafts/{id} | {revision,article} 人工保存新版本 |
| POST | /api/drafts/{id}/images/refresh | {revision} 重新抓原图，无模型费用 |
| POST | /api/drafts/{id}/illustrations | {revision,confirmed:true} 付费补插画，不改正文、不写微信 |
| POST | /api/drafts/{id}/review | {revision} 独立复审，可能收费 |
| POST | /api/drafts/{id}/approve | {revision,reviewer,confirmed:true} 人工确认，配置可能随后排队建草稿 |
| POST | /api/drafts/{id}/delete | {confirmed:true} 只删本地记录与交稿包 |
| POST | /api/drafts/wechat-batch | {confirmed:true} 全部符合条件的未提交当前版本入队 |
| GET | /api/publications | 最近传输记录，不是全库分页 |

其他兼容端点、枚举与字段限制以 OpenAPI 为准。

## 运行示例

```json
{"article_count":1,"format":"news","custom_style":"具体、清楚，解释技术变化与实际影响"}
```

POST /api/run 的 article_count 是 1–20 的可选成功目标；不传沿用全局限额。候选失败可能继续尝试其他新闻，达到目标结束，不保证材料不足时也能完成。单次目标不改定时配置。

POST /api/compose 使用 story_id；重写时同时传 draft_id 与当前 revision。旧审定失效，新版本不会自动覆盖微信旧稿。

```json
{"action":"pause","job_id":null}
```

控制种类为 pipeline、collect、review、draft_batch、images。**illustrations 尚未接入统一控制**，不能承诺全局暂停、指定 job_id 中断或重启恢复有相同行为。已发出的请求仍可能收费。

GET /api/state 中 settings.polish_pass 可读；它不是 PUT /api/config 的可写字段，当前通过 POLISH_PASS 环境配置并重启更改。

## 图片协议

```json
{
  "image_generation": {
    "enabled":false,
    "provider":"auto",
    "base_url":"https://api.minimax.cn/v1/image_generation",
    "model":"image-01",
    "size":"1024x1024",
    "api_key":"<your-image-key>",
    "clear_api_key":false,
    "proxy_url":""
  }
}
```

provider 为 auto/minimax/openai_compatible。原生 MiniMax 使用 base_resp 与 image_base64/image_urls；兼容协议使用 /images/generations 与 data 数组。auto 不擅自换主机。image-01 自定义宽高 512–2048 且为八的倍数；auto 采用 16:9。

GET 配置中的 endpoint、resolved_provider、last_result 由系统计算，不是可写字段。last_result 为历史结果，不证明刚保存的 Key 已验证。目前无专用生图测试 HTTP 路由。

ArticlePlan.illustrations 为 0–6 条，含 purpose、section_hint、prompt、fact_ids、category。按缺口和剩余方案最多生成四张；方案耗尽可能仍缺图。参数不能误放入 image_generation 配置。

## 微信与费用语义

连接失败可返回 wechat_errcode、whitelist_ip、failure_stage；40164 可解析时显示需加白名单的出口 IP。令牌成功不保证草稿权限。

批次 entries 分别记录 saved/failed/blocked/needs_check 等；job.done 不保证整批全部成功。media_id 表示草稿存在，不代表公开发表。unknown 或已有写入 intent 时先对账，不能盲目 retry。

批量准备可能触发已启用的补图服务，不能承诺存草稿过程绝无图片费用。格式修正与本地校验不应重复调用文字复审。运行结束后用 calls 与 image_calls 统计实际消耗，不能把中途提醒当最终值。

旧 /publish 路由在桌面仅建草稿；正式 submit 在 drafts_only 模式被拒绝。远程微信草稿删除尚未实现。
