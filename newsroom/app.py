import hmac
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from .config import ROOT, Settings, editorial, sources
from .engine import Engine
from .models import Article
from .render import export_document
from .schedule import SchedulePlan
from .editors import Editor, WritingChoice, choose_editor, public_profiles, profile_definitions
from .source_quality import policy
from .api_schema import install_openapi
from .connection import WechatConnectionStatus, WechatTestResult
from .desktop_login import DesktopTickets, DesktopLoginRequest, DesktopLoginResponse, DesktopTicketResponse
from .runtime_config import RuntimeConfigPatch, ModelTestResult, public_config, save_config, test_model
from .publication import PublicationAction, PublishConfigPatch, CapabilityResult
from .workflow_control import ControlRequest, UsagePreferences, usage_summary


class RunRequest(WritingChoice):
    article_count: int | None = Field(default=None, ge=1, le=20)


class ComposeRequest(WritingChoice):
    story_id: str
    draft_id: str | None = None
    revision: int | None = Field(default=None, ge=1)


class EditRequest(BaseModel):
    revision: int = Field(ge=1)
    article: Article


class DecisionRequest(BaseModel):
    revision: int = Field(ge=1)
    reviewer: str = Field(default="", max_length=80)
    confirmed: bool = False
    note: str = Field(default="人工退回，请修改后重新审核", max_length=2000)


class PublicationRequest(BaseModel):
    revision: int = Field(ge=1)
    url: str = Field(min_length=1, max_length=2000)
    reviewer: str = Field(min_length=1, max_length=80)


class DeleteRequest(BaseModel):
    confirmed: bool = False
    note: str = Field(default='', max_length=200)


class PublishRequest(BaseModel):
    revision: int = Field(ge=1)
    confirmed: bool = False


class BatchDraftRequest(BaseModel):
    confirmed: bool = False


def create_app(settings=None, start_worker=True):
    settings = settings or Settings.load()
    engine = Engine(settings)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            engine.start()
        yield
        engine.close()

    app = FastAPI(title="AI 新闻编辑台", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.engine = engine
    (settings.data_dir / 'media').mkdir(exist_ok=True)
    app.mount('/media', StaticFiles(directory=settings.data_dir / 'media'), name='article-media')
    desktop_tickets = DesktopTickets()

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/health", "/api/desktop/login"}:
            authorization = request.headers.get("authorization", "")
            expected = "Bearer " + settings.admin_token
            if not settings.admin_token or not hmac.compare_digest(authorization.encode(), expected.encode()):
                return JSONResponse({"detail": "请输入正确的审核台口令"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)[:500]}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # 配置请求可能带密钥；FastAPI 默认校验错误中的 input/context 不得回显。
        if request.url.path.startswith('/api/config'):
            return JSONResponse({'detail': [{'loc': error['loc'], 'type': error['type'], 'msg': error['msg']}
                                           for error in exc.errors()]}, status_code=422)
        from fastapi.exception_handlers import request_validation_exception_handler
        return await request_validation_exception_handler(request, exc)

    @app.get("/api/health")
    def health():
        return {"ok": True, "application": "ai-newsroom", "manual_publication": True,
                "wechat_publish": not settings.drafts_only, 'draft_storage': True}

    @app.post("/api/shutdown")
    def shutdown():
        callback = getattr(app.state, "request_shutdown", None)
        if not callback:
            raise HTTPException(409, "当前以前台命令运行，请在启动终端停止服务")
        callback()
        return {"ok": True}

    @app.get("/")
    def home():
        return FileResponse(ROOT / "newsroom/static/index.html")

    @app.post('/api/desktop/login-ticket', response_model=DesktopTicketResponse)
    def desktop_ticket():
        """启动器凭管理员口令换取 60 秒单次票据；不调用模型或微信。"""
        return desktop_tickets.issue()

    @app.post('/api/desktop/login', response_model=DesktopLoginResponse)
    def desktop_login(body: DesktopLoginRequest):
        """仅接受未过期的一次性票据；不能绕过其他 API 的认证。"""
        if not settings.admin_token or not desktop_tickets.consume(body.ticket):
            raise HTTPException(401, '启动登录链接已过期或已使用，请重新双击一键启动文件')
        return {'token': settings.admin_token}

    @app.get("/api/state")
    def state():
        drafts = engine.store.all("drafts")
        all_stories = engine.store.all('stories')
        # 官方消息和编辑部报道先展示，避免论文批量更新挤掉所有新闻。
        visible_stories = sorted(all_stories, key=lambda item: (bool(item.get('demo')), item.get('source_report', {}).get('tier') in {'primary', 'secondary'}, item.get('published_at', '')), reverse=True)[:500]
        publish = engine.publish_summary()
        return {"settings": {"demo": settings.demo, "delivery_mode": settings.delivery_mode, "manual_publication": True,
                            "publish_enabled": publish["config"]["enabled"], "publish_mode": publish["config"]["mode"],
                            "publish_ready": publish["ready"],
                            "max_drafts_day": settings.max_drafts_day, "max_calls": settings.max_calls,
                            "max_drafts_run": settings.max_drafts_run,
                            'editorial_mode': settings.editorial_mode, 'review_mode': settings.review_mode, 'fast_mode': settings.fast_mode,
                            'drafts_only': settings.drafts_only, 'merged_preparation': settings.merged_preparation,
                            'image_enabled': settings.image_enabled, 'image_model': settings.image_model,
                            'polish_pass': settings.polish_pass,
                            "llm_model": settings.llm_model, "review_model": settings.review_model or settings.llm_model,
                            "wechat_configured": bool(settings.wechat_app_id and settings.wechat_app_secret)},
                "schedule": engine.get_schedule(), "editorial": editorial(), "sources": sources(), "drafts": drafts,
                'editors': engine.store.all('editors'), 'writing_default': engine.store.get('preferences', 'writing'),
                'model_profiles': public_profiles(settings), 'source_health': engine.store.all('source_health'),
                'source_policy': policy(), 'collection_interval_minutes': settings.collection_interval_minutes,
                'event_candidates': engine.store.all('event_candidates'),
                'story_total': len(all_stories),
                "stories": [{k: v for k, v in story.items() if k not in {"text", "excerpt", 'feed_html', 'feed_text'}} for story in visible_stories],
                "runs": engine.store.all("runs")[:30], "jobs": engine.store.all("jobs")[:100],
                "events": engine.store.events(), "calls": engine.store.all("calls")[:100],
                "publication": publish, "publications": [{**p, 'account_current': p.get('account') == settings.wechat_app_id}
                                                         for p in engine.store.all("publications")[:50]],
                'workflow_control': engine.controls.state(), 'usage': usage_summary(engine.store),
                'draft_batches': engine.store.all('draft_batches')[:10]}

    @app.post('/api/drafts/wechat-batch')
    def batch_wechat_drafts(body: BatchDraftRequest):
        if not body.confirmed:
            raise ValueError('请确认将所有尚未提交的真实稿件建立为微信草稿')
        return engine.enqueue_draft_batch()

    @app.post('/api/workflow/control')
    def workflow_control(body: ControlRequest):
        return engine.controls.request(body.action, body.job_id)

    @app.put('/api/usage/preferences')
    def configure_usage(body: UsagePreferences):
        return engine.store.put('preferences', {'id': 'usage_alert', **body.model_dump()})

    @app.post("/api/run")
    def run(body: RunRequest | None = None):
        choice = body.model_dump(exclude={'article_count'}) if body else None
        payload = {'editor': choose_editor(engine.store, choice)}
        if body and body.article_count:
            payload['article_count'] = body.article_count
        return engine.enqueue("pipeline", payload=payload)

    @app.post('/api/sources/check')
    def check_sources():
        return engine.enqueue('collect')

    @app.get('/api/wechat/status', response_model=WechatConnectionStatus)
    def wechat_status():
        """只读本机配置状态与上次手动测试结果，不请求微信。"""
        return engine.wechat_connection.status()

    @app.get('/api/config')
    def configuration():
        """读取本机配置，不返回密钥，不触发外部请求。"""
        with engine.lock:
            return public_config(settings, engine.store)

    @app.put('/api/config')
    def configure(body: RuntimeConfigPatch):
        """空白密钥保留；显式清除另设标志。原子保存，空闲时立即生效。"""
        return save_config(engine, body)

    @app.post('/api/config/models/{profile}/test', response_model=ModelTestResult)
    def test_configured_model(profile: Literal['writer', 'prompt', 'review', 'visual']):
        """用户点击后发送一次最小真实模型请求，可能产生少量费用。"""
        return test_model(engine, profile)

    @app.post('/api/wechat/test-connection', response_model=WechatTestResult)
    def wechat_test_connection():
        """用户主动点击后，仅实际请求一次 stable_token；不重试、不创建或发表文章。"""
        return engine.wechat_connection.test()

    @app.put('/api/editors/{editor_id}')
    def save_editor(editor_id: str, body: Editor):
        if body.id != editor_id:
            raise ValueError('小编 ID 与请求路径不一致')
        ids = {p['id'] for p in profile_definitions()}
        if body.prompt_profile not in ids or body.writer_profile not in ids:
            raise ValueError('模型服务配置不存在')
        return engine.store.put('editors', body.model_dump())

    @app.put('/api/writing-default')
    def writing_default(body: WritingChoice):
        editor = choose_editor(engine.store, body.model_dump())
        return engine.store.put('preferences', {'id': 'writing', 'editor_id': editor['id'],
                                                'custom_style': editor['custom_style'], 'format': editor['format']})

    @app.post('/api/compose')
    def compose(body: ComposeRequest):
        engine.store.get('stories', body.story_id)
        if body.draft_id:
            draft = engine.store.get('drafts', body.draft_id)
            if draft['story_id'] != body.story_id or draft['revision'] != body.revision or draft['status'] in {'manual_published', 'published', 'submitted', 'publish_unknown'}:
                raise ValueError('重写稿件与线索或版本不一致，或已经发表')
            engine.assert_editable(body.draft_id)
        editor = choose_editor(engine.store, {'editor_id': body.editor_id, 'custom_style': body.custom_style, 'format': body.format})
        return engine.enqueue('pipeline', subject=body.draft_id or body.story_id, revision=body.revision,
                              payload={'editor': editor, 'story_id': body.story_id, 'draft_id': body.draft_id})

    @app.put("/api/schedule")
    def schedule(body: SchedulePlan):
        return engine.set_schedule(body.model_dump())

    @app.get("/api/drafts/{draft_id}/preview", response_class=HTMLResponse)
    def preview(draft_id: str):
        return export_document(engine.store.get("drafts", draft_id))

    @app.get("/api/drafts/{draft_id}/export/{format}")
    def export(draft_id: str, format: Literal["html", "md", "json", "zip", "wechat"]):
        draft = engine.store.get("drafts", draft_id)
        file = settings.data_dir / "exports" / draft["id"] / f"v{draft['revision']}" / {"html": "article.html", "md": "article.md", "json": "review.json", "zip": "handoff.zip", "wechat": "wechat.html"}[format]
        if draft["status"] not in {"pending_human", "ready_to_publish", "manual_published", "demo_published", "exported", "wechat_draft", "published"}:
            raise HTTPException(409, "当前版本尚未完成采编检查，不能作为交稿下载")
        if draft['status'] in {'pending_human', 'ready_to_publish'}:
            if settings.drafts_only:
                engine.refresh_local_handoff(draft)
            elif settings.review_mode != 'strict' and settings.delivery_mode == 'local' and draft['status'] == 'pending_human':
                engine.verify_source(draft, allow_cached=True)
            else:
                engine.verify_source(draft)
        if not file.exists():
            raise HTTPException(409, "交稿文件尚未生成，请重试生成交稿包")
        return FileResponse(file, filename=file.name)

    @app.put("/api/drafts/{draft_id}")
    def edit(draft_id: str, body: EditRequest):
        return engine.edit(draft_id, body.revision, body.article.model_dump())

    @app.post("/api/drafts/{draft_id}/approve")
    def approve(draft_id: str, body: DecisionRequest):
        return engine.approve(draft_id, body.revision, body.reviewer, body.confirmed)

    @app.post("/api/drafts/{draft_id}/reject")
    def reject(draft_id: str, body: DecisionRequest):
        return engine.reject(draft_id, body.revision, body.note)

    @app.post("/api/drafts/{draft_id}/deliver")
    def deliver(draft_id: str, body: DecisionRequest):
        draft = engine.store.get("drafts", draft_id)
        if draft["revision"] != body.revision or draft["status"] not in {"pending_human", "ready_to_publish"}:
            raise ValueError("当前版本需要先通过 AI 审核")
        return engine.enqueue("deliver", draft_id, body.revision)

    @app.post("/api/drafts/{draft_id}/publication")
    def publication(draft_id: str, body: PublicationRequest):
        return engine.record_publication(draft_id, body.revision, body.url, body.reviewer)

    # ---------- 公众号自动发布 ----------

    @app.get('/api/publication')
    def publication_status():
        """只读发布设置、账号绑定与能力检测结果，不调用微信。"""
        with engine.lock:
            return {'summary': engine.publish_summary(), 'records': engine.store.all('publications')[:50]}

    @app.put('/api/publication')
    def publication_configure(body: PublishConfigPatch):
        """保存发布设置；启用时校验凭证、封面与署名，不发送任何微信请求。"""
        return engine.save_publish_config(body)

    @app.post('/api/publication/capability', response_model=CapabilityResult)
    def publication_capability():
        """用户点击后只读检测令牌、草稿、素材与发布接口权限；不创建草稿、不发表。"""
        return engine.capability_check()

    @app.post('/api/drafts/{draft_id}/publish')
    def publish_draft(draft_id: str, body: PublishRequest):
        """人工确认当前版本后触发发布；版本、审批与来源在外部请求前再次校验。"""
        if not body.confirmed:
            raise ValueError('发布到公众号需要确认：只有当前版本、人工终审有效且来源未变化时才会提交')
        return engine.create_publication(draft_id, body.revision, trigger='manual')

    @app.post('/api/drafts/{draft_id}/wechat-draft')
    def one_click_wechat_draft(draft_id: str, body: PublishRequest):
        if not body.confirmed:
            raise ValueError('请确认保存当前版本到微信草稿箱')
        return engine.submit_wechat_draft(draft_id, body.revision)

    @app.post('/api/drafts/{draft_id}/images/refresh')
    def refresh_images(draft_id: str, body: PublishRequest):
        """重新读取原文及其明确链接的一手页面搜集实际图片；不调用文字／生图模型，不提交微信。"""
        with engine.lock:
            draft = engine.store.get('drafts', draft_id)
            if draft['revision'] != body.revision:
                raise ValueError('版本已更新，请刷新后重试')
            engine.assert_editable(draft_id)
            pending = next((j for j in engine.store.all('jobs') if j['kind'] == 'images' and j['subject'] == draft_id
                            and j['status'] in {'queued', 'running', 'paused'}), None)
            return pending or engine.enqueue('images', draft_id, body.revision)

    @app.post('/api/drafts/{draft_id}/delete')
    def delete_draft(draft_id: str, body: DeleteRequest):
        """删除本地稿件与交稿包；公众号草稿和已发表文章不受影响。"""
        return engine.delete_draft(draft_id, body.confirmed)

    @app.post('/api/drafts/{draft_id}/illustrations')
    def generate_illustrations(draft_id: str, body: PublishRequest):
        """按缺口补齐概念插画；会调用生图服务并产生费用，不提交微信。"""
        if not body.confirmed:
            raise ValueError('补齐插画会调用生图服务并产生费用，请确认后再提交')
        with engine.lock:
            draft = engine.store.get('drafts', draft_id)
            if draft['revision'] != body.revision:
                raise ValueError('版本已更新，请刷新后重试')
            engine.assert_editable(draft_id)
            pending = next((j for j in engine.store.all('jobs') if j['kind'] == 'illustrations' and j['subject'] == draft_id
                            and j['status'] in {'queued', 'running', 'paused'}), None)
            return pending or engine.enqueue('illustrations', draft_id, body.revision)

    @app.get('/api/publications')
    def publications():
        return engine.store.all('publications')[:50]

    @app.post('/api/publications/{publication_id}/{action}')
    def publication_action(publication_id: str, action: Literal['check', 'retry', 'resume', 'submit', 'abandon'],
                           body: PublicationAction | None = None):
        """check 只查询；retry 仅限明确失败；resume／submit／abandon 需要显式确认。"""
        return engine.publication_action(publication_id, action, body or PublicationAction())

    @app.post("/api/drafts/{draft_id}/review")
    def review(draft_id: str, body: DecisionRequest):
        with engine.lock:
            draft = engine.store.get("drafts", draft_id)
            if draft["revision"] != body.revision or draft["status"] not in {"needs_ai_review", "needs_revision"}:
                raise ValueError("只有当前待审或待修改稿件可重新审核")
            draft["status"] = "needs_ai_review"
            engine.store.put("drafts", draft)
            return engine.enqueue("review", draft_id, body.revision)

    @app.post("/api/stories/{story_id}/retry")
    def retry(story_id: str):
        with engine.lock:
            story = engine.store.get("stories", story_id)
            if story["status"] != "failed":
                raise ValueError("仅可重试失败的新闻任务")
            story["status"] = "new"
            story['pipeline_failures'] = 0
            story.pop('retry_after', None)
            story.pop("error", None)
            engine.store.put("stories", story)
            return engine.enqueue("pipeline")

    app.mount("/static", StaticFiles(directory=ROOT / "newsroom/static"), name="static")
    install_openapi(app)
    return app
