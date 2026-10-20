import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in {"true", "1", "yes"}


@dataclass
class Settings:
    data_dir: Path
    demo: bool = True
    admin_token: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    prompt_base_url: str = ""
    prompt_api_key: str = ""
    prompt_model: str = ""
    review_base_url: str = ""
    review_api_key: str = ""
    review_model: str = ""
    json_mode: bool = True
    max_output_tokens: int = 0
    max_calls: int = 0
    max_drafts_day: int = 0
    max_drafts_run: int = 0
    max_candidates: int = 12
    scheduler: bool = True
    schedule_times: tuple[str, ...] = ("09:00", "18:00")
    interval_minutes: int = 30
    collection_interval_minutes: int = 15
    delivery_mode: str = "local"
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    wechat_thumb: str = ""
    wechat_cover: str = ""
    wechat_author: str = "AI 编辑部"
    news_proxy_url: str = ""
    llm_proxy_url: str = ""
    prompt_proxy_url: str = ""
    review_proxy_url: str = ""
    visual_base_url: str = ""
    visual_api_key: str = ""
    visual_model: str = ""
    visual_proxy_url: str = ""
    editorial_mode: str = 'manual'
    review_mode: str = 'strict'
    fast_mode: bool = False
    # 终稿润色：写作后加一次编辑润色，只改表达不改事实。
    polish_pass: bool = True
    # Legacy programmatic fixtures remain compatible; the desktop application always stops at drafts.
    drafts_only: bool = False
    merged_preparation: bool = False
    image_enabled: bool = False
    image_provider: str = 'auto'
    image_base_url: str = ''
    image_api_key: str = ''
    image_model: str = ''
    image_size: str = '1024x1024'
    image_proxy_url: str = ''

    def __post_init__(self):
        if self.image_provider not in {'auto', 'openai_compatible', 'minimax'}:
            raise ValueError('生图协议配置不合法')
        if self.editorial_mode not in {'auto', 'manual'} or self.review_mode not in {'off', 'advisory', 'strict'}:
            raise ValueError('采编或复审模式不合法')
        if self.delivery_mode not in {"local", "wechat_draft"}:
            raise ValueError("DELIVERY_MODE 只允许 local 或 wechat_draft；发表由人工完成")

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        directory = Path(os.getenv("DATA_DIR", "data"))
        if not directory.is_absolute():
            directory = ROOT / directory
        directory.mkdir(parents=True, exist_ok=True)
        token_path = directory / "admin-token.txt"
        token = os.getenv("ADMIN_TOKEN", "")
        if not token:
            if not token_path.exists():
                token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            token = token_path.read_text(encoding="utf-8").strip()
        result = cls(
            data_dir=directory, admin_token=token, demo=flag("DEMO_MODE", True),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            llm_api_key=os.getenv("LLM_API_KEY", ""), llm_model=os.getenv("LLM_MODEL", ""),
            prompt_base_url=os.getenv("PROMPT_BASE_URL", "").rstrip("/"), prompt_api_key=os.getenv("PROMPT_API_KEY", ""), prompt_model=os.getenv("PROMPT_MODEL", ""),
            review_base_url=os.getenv("REVIEW_BASE_URL", "").rstrip("/"),
            review_api_key=os.getenv("REVIEW_API_KEY", ""), review_model=os.getenv("REVIEW_MODEL", ""),
            json_mode=flag("LLM_JSON_MODE", True), max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "0")),
            max_calls=int(os.getenv("MAX_MODEL_CALLS_PER_RUN", "0")),
            max_drafts_day=int(os.getenv("MAX_DRAFTS_PER_DAY", "0")),
            max_drafts_run=max(0, int(os.getenv("MAX_DRAFTS_PER_RUN", "0"))),
            max_candidates=int(os.getenv("MAX_CANDIDATES_PER_RUN", "12")),
            scheduler=flag("SCHEDULER_ENABLED", True), interval_minutes=max(5, int(os.getenv("COLLECT_INTERVAL_MINUTES", "30"))),
            collection_interval_minutes=max(5, int(os.getenv("SOURCE_POLL_MINUTES", "15"))),
            schedule_times=tuple(value.strip() for value in os.getenv("SCHEDULE_TIMES", "09:00,18:00").split(",")),
            delivery_mode=os.getenv("DELIVERY_MODE", "local"),
            wechat_app_id=os.getenv("WECHAT_APP_ID", ""), wechat_app_secret=os.getenv("WECHAT_APP_SECRET", ""),
            wechat_thumb=os.getenv("WECHAT_THUMB_MEDIA_ID", ""), wechat_cover=os.getenv("WECHAT_COVER_PATH", ""),
            wechat_author=os.getenv("WECHAT_AUTHOR", "AI 编辑部"),
            news_proxy_url=os.getenv('NEWS_PROXY_URL', ''), llm_proxy_url=os.getenv('LLM_PROXY_URL', ''),
            prompt_proxy_url=os.getenv('PROMPT_PROXY_URL', ''), review_proxy_url=os.getenv('REVIEW_PROXY_URL', ''),
            editorial_mode=os.getenv('EDITORIAL_MODE', 'auto'), review_mode=os.getenv('REVIEW_MODE', 'off'),
            fast_mode=flag('FAST_MODE', True), polish_pass=flag('POLISH_PASS', True),
            drafts_only=True, merged_preparation=True,
        )
        for suffix in ('base_url', 'api_key', 'model', 'proxy_url'):
            setattr(result, 'visual_' + suffix, os.getenv('VISUAL_' + suffix.upper(), ''))
        from .runtime_config import load_saved
        result.__dict__.update(load_saved(directory))
        return result


def editorial():
    data = json.loads((ROOT / "config/editorial.json").read_text(encoding="utf-8"))
    if set(data["weights"]) != {"relevance", "impact", "novelty", "timeliness", "reliability"}:
        raise ValueError("评分权重字段错误")
    if abs(sum(data["weights"].values()) - 1) > 0.001 or min(data["weights"].values()) < 0:
        raise ValueError("评分权重必须非负且和为 1")
    return data


def sources():
    return json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8"))
