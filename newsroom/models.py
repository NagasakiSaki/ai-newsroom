import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OutputCheckError(ValueError):
    """模型语义校验失败，只携带程序定义的原因和安全位置，不携带原始输出。"""
    def __init__(self, message, code, location=''):
        super().__init__(message)
        self.code, self.location = code, location


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Score(StrictModel):
    relevance: float = Field(ge=0, le=100)
    impact: float = Field(ge=0, le=100)
    novelty: float = Field(ge=0, le=100)
    timeliness: float = Field(ge=0, le=100)
    reliability: float = Field(ge=0, le=100)
    reason: str = Field(min_length=1)


class Fact(StrictModel):
    id: str = Field(pattern=r"^F[1-9][0-9]*$")
    text: str = Field(min_length=1)
    source_url: str
    quote: str = Field(min_length=8)
    claim_type: Literal["reported_release", "vendor_claim", "research_result", "independently_verified", "unknown"] = "unknown"


class Evidence(StrictModel):
    summary: str
    facts: list[Fact] = Field(min_length=1, max_length=24)
    uncertainties: list[str]


class Angle(StrictModel):
    selected: str
    reader_value: str
    alternatives: list[str] = Field(min_length=2, max_length=4)
    outline: list[str] = Field(min_length=2)
    reason: str


class Article(StrictModel):
    title: str = Field(min_length=1, max_length=64)
    digest: str = Field(min_length=1, max_length=120)
    markdown: str = Field(min_length=80)


class EditorialSection(StrictModel):
    editor_id: Literal['flash', 'rigor', 'common']
    heading: str = Field(min_length=1, max_length=80)
    focus: str = Field(min_length=1)


class EditorialPlan(StrictModel):
    reason: str = Field(min_length=1)
    sections: list[EditorialSection] = Field(min_length=1, max_length=3)


class VisualCard(StrictModel):
    after_heading: str = ''
    after_text: str = Field(default='', max_length=160)
    title: str = Field(min_length=1, max_length=60)
    fact_ids: list[str] = Field(min_length=1, max_length=5)


class SourceImagePlacement(StrictModel):
    image_id: str
    after_heading: str = ''
    after_text: str = Field(default='', max_length=160)


class VisualPlan(StrictModel):
    theme: Literal['indigo', 'red', 'green'] = 'indigo'
    reason: str = ''
    cards: list[VisualCard] = Field(default_factory=list, max_length=5)
    images: list[SourceImagePlacement] = Field(default_factory=list, max_length=10)
    reference_image_ids: list[str] = Field(default_factory=list, max_length=8)


class WritingPrompt(StrictModel):
    system_prompt: str = Field(min_length=50, max_length=10000)
    user_prompt: str = Field(min_length=50, max_length=16000)
    style_checklist: list[str] = Field(min_length=2, max_length=12)
    evidence_rules: list[str] = Field(min_length=2, max_length=12)


class ArticleWritingPrompt(WritingPrompt):
    # House rules are application defaults, not a fabricated model review.
    style_checklist: list[str] = Field(default_factory=lambda: ['短段落与清晰叙事', '重点句有事实依据'], min_length=2, max_length=12)
    evidence_rules: list[str] = Field(default_factory=lambda: ['只使用已有事实编号', '保留来源归属和真实条件'], min_length=2, max_length=12)


class IllustrationBrief(StrictModel):
    """整篇策划里的一条概念插画要求；不是新闻现场照片。"""
    purpose: str = Field(min_length=4, max_length=200)
    section_hint: str = Field(default='', max_length=80)
    prompt: str = Field(min_length=20, max_length=600)
    fact_ids: list[str] = Field(default_factory=list, max_length=4)
    category: Literal['concept', 'diagram', 'metaphor'] = 'concept'


class ArticlePlan(StrictModel):
    story_mode: Literal['breaking', 'explainer', 'brief']
    target_chars: int = Field(ge=600, le=12000)
    angle: Angle
    headline_candidates: list[str] = Field(min_length=1, max_length=8)
    hook: str
    narrative_beats: list[str] = Field(min_length=2, max_length=16)
    reason: str
    writing_prompt: ArticleWritingPrompt
    illustrations: list[IllustrationBrief] = Field(default_factory=list, max_length=6)


class CandidateScore(StrictModel):
    story_id: str
    score: Score


class ScoreBatch(StrictModel):
    scores: list[CandidateScore] = Field(min_length=1, max_length=50)


class AutonomousSection(StrictModel):
    role_name: str = Field(min_length=1, max_length=40)
    voice: str = Field(min_length=5, max_length=2000)
    content_type: Literal['news', 'commentary']
    heading: str = Field(min_length=1, max_length=80)
    focus: str = Field(min_length=1)
    writing_prompt: WritingPrompt


class AutonomousPlan(StrictModel):
    story_mode: Literal['breaking', 'explainer'] = 'explainer'
    headline_candidates: list[str] = Field(default_factory=list, max_length=4)
    narrative_beats: list[str] = Field(default_factory=list, max_length=7)
    angle: Angle
    headline_strategy: str = Field(min_length=1)
    hook: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    sections: list[AutonomousSection] = Field(min_length=3, max_length=3)


class FactComparison(StrictModel):
    fact_id: str = Field(pattern=r'^F[1-9][0-9]*$')
    judgment: Literal['supports', 'contradicts', 'different_scope', 'not_mentioned']
    quote: str = Field(default='', max_length=2000)
    explanation: str = Field(min_length=1, max_length=2000)


class SourceComparison(StrictModel):
    story_id: str
    same_event: bool
    confidence: float = Field(ge=0, le=1)
    relationship: Literal['primary_material', 'original_reporting', 'announcement_rewrite', 'syndicated', 'unknown']
    reason: str = Field(min_length=1, max_length=2000)
    comparisons: list[FactComparison] = Field(max_length=24)


class Crosscheck(StrictModel):
    anchor_relationship: Literal['primary_material', 'original_reporting', 'announcement_rewrite', 'syndicated', 'unknown'] = 'unknown'
    sources: list[SourceComparison] = Field(max_length=4)
    summary: str = Field(min_length=1, max_length=2000)


class Issue(StrictModel):
    severity: Literal["blocker", "major", "minor"]
    category: str
    detail: str
    suggestion: str


class Review(StrictModel):
    passed: bool
    score: float = Field(ge=0, le=100)
    issues: list[Issue]
    summary: str


class PreparedArticle(StrictModel):
    evidence: Evidence
    plan: ArticlePlan


def content_hash(article: dict) -> str:
    data = {key: article[key] for key in ("title", "digest", "markdown")}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
