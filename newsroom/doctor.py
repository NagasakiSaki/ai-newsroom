"""只读配置与可选的 RSS 连通性检查，不调用付费模型或发布。"""
import argparse
import sys

import feedparser

from .collect import fetch_text, parse_news_html
from .config import Settings, editorial, sources


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="读取已配置 RSS，检查网络与格式")
    args = parser.parse_args()
    settings = Settings.load()
    rules = editorial()
    print("模式：", "虚构演示" if settings.demo else "真实采编")
    print("模型配置：", "齐全" if settings.llm_api_key and settings.llm_model else "待填写 Key / 模型 ID")
    print("审核模型：", "单独配置" if settings.review_model else "复用写作模型，生产时可配置不同审核模型")
    print('提示词模型：', settings.prompt_model or '待配置 PROMPT_MODEL 或小编专用模型 ID')
    print('RSS 采集：', settings.collection_interval_minutes, '分钟；与写稿计划分开')
    print("交稿目标：", settings.delivery_mode, "；正式发表始终由人工完成")
    print("微信凭证：", "已填写，尚不代表有接口权限" if settings.wechat_app_id and settings.wechat_app_secret else "待填写")
    from .db import Store
    try:
        plan = Store(settings.data_dir / "newsroom.sqlite3").get("schedule", "main")
        print("定时采编：", "开启" if plan["enabled"] else "暂停", "；北京时间", ",".join(plan["times"]), "；下次", plan.get("next_run_at"))
    except KeyError:
        print("首次计划：", ",".join(settings.schedule_times))
    print("受众：", rules["audience"])
    print("口令文件：", settings.data_dir / "admin-token.txt")
    if args.network:
        for source in sources():
            if not source.get("enabled", True):
                continue
            try:
                document = fetch_text(source["url"], allowed_hosts=source.get('allowed_hosts'), settings=settings)
                feed = parse_news_html(document, source) if source.get('format') == 'news_html' else feedparser.parse(document)
                print(source["name"], "→ 订阅 / 列表条目", len(feed.entries))
            except Exception as exc:
                print(source["name"], "→", type(exc).__name__, str(exc)[:180])
        print('固定官方 HTTPS 信源的 Fake-IP 连接会额外校验公网 DNS 和 TLS；其他内网及保留地址仍拒绝。')


if __name__ == "__main__":
    main()
