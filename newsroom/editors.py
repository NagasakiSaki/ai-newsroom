import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import ROOT


class Editor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    name: str = Field(min_length=1, max_length=40)
    style_name: str = Field(min_length=1, max_length=60)
    instructions: str = Field(min_length=10, max_length=4000)
    prompt_profile: str = "prompt"
    writer_profile: str = "writer"
    prompt_model: str = Field(default="", max_length=200)
    writer_model: str = Field(default="", max_length=200)

    @field_validator("prompt_model", "writer_model")
    @classmethod
    def model_id(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("模型 ID 不得包含控制字符")
        return value.strip()


class WritingChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    editor_id: str | None = None
    custom_style: str = Field(default="", max_length=4000)
    format: Literal["news", "commentary"] | None = None


def profile_definitions():
    return json.loads((ROOT / "config/model_profiles.json").read_text(encoding="utf-8"))


def resolve_profile(settings, profile_id, model_override=""):
    definition = next((p for p in profile_definitions() if p["id"] == profile_id), None)
    if not definition:
        raise ValueError("未找到模型服务配置：" + profile_id)
    if profile_id == "writer":
        base, key, model = settings.llm_base_url, settings.llm_api_key, settings.llm_model
    elif profile_id == "prompt":
        if settings.prompt_base_url and not settings.prompt_api_key:
            raise ValueError("独立提示词服务地址需要独立配置 PROMPT_API_KEY")
        base = settings.prompt_base_url or settings.llm_base_url
        key = settings.prompt_api_key or settings.llm_api_key
        model = settings.prompt_model  # 专用模型未设置时不默默退回写作模型。
    elif profile_id == "review":
        if settings.review_base_url and not settings.review_api_key:
            raise ValueError("独立审核服务地址需要独立配置 REVIEW_API_KEY")
        base = settings.review_base_url or settings.llm_base_url
        key = settings.review_api_key or settings.llm_api_key
        model = settings.review_model or settings.llm_model
    elif profile_id == 'visual':
        if settings.visual_base_url and not settings.visual_api_key:
            raise ValueError('独立图文服务地址需要独立 API Key')
        base = settings.visual_base_url or settings.llm_base_url
        key = settings.visual_api_key or settings.llm_api_key
        model = settings.visual_model or settings.llm_model
    else:
        base, key, model = (os.getenv(definition[field], "") for field in ("base_url_env", "api_key_env", "model_env"))
    return {"id": profile_id, "base_url": base.rstrip("/"), "api_key": key, "model": model_override or model}


def public_profiles(settings):
    result = []
    for definition in profile_definitions():
        try:
            route = resolve_profile(settings, definition["id"])
            configured = bool(route["base_url"].startswith("https://") and route["api_key"] and route["model"])
            result.append({"id": definition["id"], "name": definition["name"], "role": definition["role"], "model": route["model"], "configured": configured})
        except ValueError:
            result.append({"id": definition["id"], "name": definition["name"], "role": definition["role"], "model": "", "configured": False})
    return result


def initialize_editors(store):
    for raw in json.loads((ROOT / "config/editors.json").read_text(encoding="utf-8")):
        try:
            store.get("editors", raw["id"])
        except KeyError:
            store.put("editors", Editor.model_validate(raw).model_dump())
    try:
        store.get("preferences", "writing")
    except KeyError:
        store.put("preferences", {"id": "writing", "editor_id": "brief", "custom_style": "", "format": "news"})


def choose_editor(store, choice=None):
    choice = WritingChoice.model_validate(choice or {}).model_dump()
    default = store.get("preferences", "writing")
    editor = store.get("editors", choice["editor_id"] or default["editor_id"])
    editor = Editor.model_validate(editor).model_dump()
    editor["custom_style"] = choice["custom_style"] if choice.get("editor_id") or choice["custom_style"] else default.get("custom_style", "")
    editor["format"] = choice.get("format") or default.get("format") or "news"
    ids = {p["id"] for p in profile_definitions()}
    if editor["prompt_profile"] not in ids or editor["writer_profile"] not in ids:
        raise ValueError("小编绑定的模型服务不存在")
    return editor
