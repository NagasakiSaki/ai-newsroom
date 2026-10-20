"""离线导出接口与领域 Schema，不读 .env、不启动 worker、不调用网络。"""
import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic.json_schema import models_json_schema

from newsroom.app import ComposeRequest, DecisionRequest, EditRequest, PublicationRequest, PublishRequest, BatchDraftRequest, create_app
from newsroom.config import Settings
from newsroom.editors import Editor, WritingChoice
from newsroom.models import Angle, Article, Crosscheck, Evidence, Review, Score, WritingPrompt, EditorialPlan, VisualPlan, AutonomousPlan
from newsroom.models import ArticlePlan, ScoreBatch, PreparedArticle
from newsroom.schedule import SchedulePlan
from newsroom.connection import WechatConnectionStatus, WechatTestResult
from newsroom.desktop_login import DesktopLoginRequest, DesktopLoginResponse, DesktopTicketResponse
from newsroom.publication import CapabilityResult, PublicationAction, PublishConfigPatch
from newsroom.runtime_config import RuntimeConfigPatch, ModelTestResult, ImageGenerationPatch
from newsroom.workflow_control import ControlRequest, UsagePreferences


def documents():
    # 用临时数据库满足应用工厂初始化，避免写入实际 data/。
    with TemporaryDirectory(prefix='newsroom-api-docs-') as directory:
        app = create_app(Settings(data_dir=Path(directory), admin_token='documentation-placeholder', scheduler=False), start_worker=False)
        spec = app.openapi()
    models = [ComposeRequest, DecisionRequest, EditRequest, PublicationRequest, Editor, WritingChoice,
              SchedulePlan, Article, Evidence, Angle, Score, Review, WritingPrompt, Crosscheck, EditorialPlan, VisualPlan, AutonomousPlan, ArticlePlan, ScoreBatch,
              WechatConnectionStatus, WechatTestResult, DesktopLoginRequest, DesktopLoginResponse, DesktopTicketResponse,
              RuntimeConfigPatch, ModelTestResult, PublishRequest, PublishConfigPatch, PublicationAction, CapabilityResult,
              ControlRequest, UsagePreferences, BatchDraftRequest, PreparedArticle, ImageGenerationPatch]
    refs, domain = models_json_schema([(model, 'validation') for model in models], title='AI Newsroom request and model-output schemas')
    domain['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
    domain['x-roots'] = {model.__name__: refs[(model, 'validation')] for model in models}
    return {'openapi.json': spec, 'schemas/domain-models.json': domain}


def export(destination, check=False):
    stale = []
    for relative, document in documents().items():
        path = Path(destination) / relative
        content = json.dumps(document, ensure_ascii=False, indent=2) + '\n'
        if check:
            if not path.is_file() or path.read_text(encoding='utf-8') != content:
                stale.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
    return stale


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs')
    parser.add_argument('--check', action='store_true', help='只检查文档是否与代码同步')
    args = parser.parse_args()
    stale = export(args.output, args.check)
    if stale:
        print('Outdated API documents: ' + ', '.join(stale))
        return 1
    print('API documents are current.' if args.check else 'Exported OpenAPI and domain schemas.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
