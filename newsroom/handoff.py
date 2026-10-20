"""自动生成可供人工审核、排版复制和发表的交稿包。"""
import json
from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .db import now
from .render import export_document, render_article


def build_handoff(root: Path, draft):
    directory = root / "exports" / draft["id"] / f"v{draft['revision']}"
    directory.mkdir(parents=True, exist_ok=True)
    title = draft["article"]["title"]
    files = {
        "article.html": export_document(draft, image_prefix='media/', original_prefix='originals/'),
        "wechat.html": render_article(draft, image_prefix='media/', original_prefix='originals/'),
        "article.md": "# " + title + "\n\n" + draft["article"]["markdown"],
        "review.json": json.dumps(draft, ensure_ascii=False, indent=2),
        "sources.json": json.dumps(draft["evidence"], ensure_ascii=False, indent=2),
        'writing-prompt.json': json.dumps({'editor': draft.get('editor'), 'prompt': draft.get('writing_prompt')}, ensure_ascii=False, indent=2),
        'source-report.json': json.dumps(draft.get('source_snapshot'), ensure_ascii=False, indent=2),
        'crosscheck.json': json.dumps(draft.get('crosscheck'), ensure_ascii=False, indent=2),
        'editorial-plan.json': json.dumps(draft.get('editorial_plan'), ensure_ascii=False, indent=2),
        'visual-plan.json': json.dumps(draft.get('visuals'), ensure_ascii=False, indent=2),
        "README.txt": "人工发表交稿包\n\n标题：" + title + "\n摘要：" + draft["article"]["digest"] + "\n\n"
            "1. 打开 article.html 查看排版，核对 sources.json 中的原始来源和 review.json 中的审核意见。\n"
            "2. 在审核台使用「复制公众号排版」，粘贴到微信公众平台编辑器，另填标题、摘要和有使用权的封面。\n"
            "3. 核对手机预览和平台 AI 标识，人工决定是否发表。系统不会发表或群发。\n"
            "4. 发表后可以在审核台记录文章链接。稿件修改后请使用最新版本交稿包。\n"
            + ("\n本包是虚构演示，不可作为真实新闻发表。\n" if draft["demo"] else ""),
    }
    for name, content in files.items():
        temporary = directory / (name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(directory / name)
    package = directory / "handoff.zip"
    temporary = directory / "handoff.zip.tmp"
    with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
        for name in files:
            archive.write(directory / name, name)
        from .editorial_team import asset_file, original_asset_file
        import shutil
        for asset in (draft.get('visuals') or {}).get('assets', []):
            source = asset_file(root, asset)
            (directory / 'media').mkdir(exist_ok=True)
            shutil.copyfile(source, directory / 'media' / source.name)
            archive.write(source, 'media/' + source.name)
            if asset.get('original_file'):
                original = original_asset_file(root, asset)
                (directory / 'originals').mkdir(exist_ok=True)
                shutil.copyfile(original, directory / 'originals' / original.name)
                archive.write(original, 'originals/' + original.name)
    temporary.replace(package)
    return {"revision": draft["revision"], "created_at": now(), "files": list(files) + ["handoff.zip"], "state": "ready"}
