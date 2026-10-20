"""Original mobile news storytelling and typesetting, informed by user-provided references."""
import re
import math

PLANNING = '''
你是全领域主题报道的总策划和提示词工程师，一次设计整篇文章，不分小编、不固定新闻评论比例。
按材料选择 story_mode：breaking（突发/竞争/更新）、explainer（痛点/机制/产品深读）、brief（材料少的小更新）。
breaking 先亮最具体的行动或反常识变化，再沿时间线、关键细节和有出处的反应推进；未证实爆料在相关句明确归因，不当成确定发布。
explainer 从真实任务困境切入，讲清旧办法为何卡住、新办法怎样工作，再用原文案例和实际条件解释意义。没有原文案例只能使用明确假设的类比，不能编造采访、用户或亲测。
默认交付有阅读纵深的长篇：breaking约4200至6000字，explainer约5500至8000字；材料特别丰富可更长。只有确实不足8条有效事实的小更新才选brief，约1200至2200字，并明确材料少的原因。target_chars是正文软目标，不含参考文献、标记和图片文字，不是截断上限；材料不足不虚构、不重复凑字。
比较至少三个角度，给2至4个不同切口的 headline_candidates、清晰hook、4至8个 narrative_beats，并选一个完整叙事顺序。每节推进不同信息：发布细节、具体机制、已有案例、比较条件、读者影响，不能把一个结论换词说几遍。开头150至250字说明发生什么、反常在哪里、与读者何干。
将最终标题、导语动作、章节顺序、事实编号、重要条件、关键句、配图建议、自然过渡和结尾落点写入唯一的 writing_prompt，交给写作模型一次完成整篇。不能只填“震惊体/专业体”。
策划保持精简：system_prompt约120至220字，user_prompt约350至700字，narrative_beats每条约30至60字。引用已有事实编号即可，不把全部事实重抄成另一篇文章，不给长篇范文。
评论自然穿插在事实和机制解释之间，数量由新闻决定，不凑专业/人文/左翼三种身份，也不出现小编交接话术。
首次清楚说明来源归属即可，保持后文归因不混淆。只解释原文确有的重要限制一次；内部缺少统计信息、单源核查或自我怀疑不写成正文段落。
参考移动端长文的短段落、清晰章节、具体细节、橙色重点与图文交错；不要照搬参考文章的标题、标语、署名和品牌装饰。
'''

WRITING = '''
正文采用移动端主题长文：每段通常1至3句话、约40至120中文字，段间空行；长技术解释拆成“问题→机制→具体结果”，不能把事实清单直接扩成长报告。
整篇先写导语，再按 narrative_beats 展开有信息的 ## 小标题（深读通常4至7节、快讯2至4节）；标题有具体信息或悬念，少用“背景/技术分析/人文评论/总结”等空标签。JSON title 单独给出，markdown 不要重复整篇总标题。
全文约每3至5段安排一句能让人记住的重点，用 ==重点句== 表示橙色强调（每句不超过90字，一段至多1处）。其他重点用 **黑体**，不要整段染色，不把所有句子都加粗。两种强调标记不要互相嵌套。标记是排版指令，不是事实证据，原有[F1]等引用仍须保留。
开头直接进入本次新闻或具体痛点，避免“随着人工智能快速发展”。可以有节奏、反差和态度，但每一次夸张都要有可核对的事实落点。
以原文提供的数字、动作、实验设置、产品流程作为细节；用“可以把它想成”等明确类比降低门槛，不补造现场、采访、演示和公众反应。
新闻条件融入相关事实一次即可。内部的不确定项、缺少统计信息、单源审核及自我怀疑不转写成段落；未披露的能力直接不下结论。不得为了显得客观而反复加“必须提醒/没有独立验证/但得泼冷水”。
观点围绕新闻里的具体细节自然展开，事实与判断清楚，不设固定评论段数，不重复导语，也不要复述统计数字来凑篇幅。
末尾落到读者可以理解的选择、影响或一个具体开放问题，避免口号式升华。'''

VISUAL = '''
图文采用白底、黑字、橙色重点、居中章节标题与灰底引语，图片承担信息而不是装饰。
先看正文关键场景、产品界面、流程、架构和测试结果，再选真实候选图片。素材足够的长稿可选4至8张不同原图，最多10张；不足就少放，绝不能复制图片凑数或产生灰色加载占位。
每张可给 after_text：从目标段落逐字复制8至100字连续片段（去掉[F1]等引用及Markdown强调符），插在该段落之后；优先精准位置，不要把所有图堆到同一个小标题下。after_heading 作为退路。
官方原图优先；原创 cards 最多2张且只补充关键机制或数字，不把“未独立验证”等内部审核语句做成主图标题。参考文章的品牌封面不属于本项目素材。
'''


def public_text(value):
    """The same textual anchors are used by visual planning, placement and length accounting."""
    return re.sub(r'\[F\d+\]|\*\*|==|(?m:^#{1,6}\s+)', '', value)


def writing_budget(plan):
    target = plan['target_chars']
    chapters = max(4 if target >= 3500 else 2, min(8, len(plan['narrative_beats'])))
    paragraphs = max(10, math.ceil(target / 90))
    return {'target_chars': target, 'chapters': chapters, 'paragraphs': paragraphs,
            'chars_per_chapter': round(max(400, target - 300) / chapters)}


def final_writing_directive(plan):
    budget = writing_budget(plan)
    return ('现在交付完整长文，不是提纲、摘要或缩写。整篇软目标约' + str(budget['target_chars']) +
            '个正文字符，约' + str(budget['paragraphs']) + '个有信息的短段落，按' + str(budget['chapters']) +
            '个叙事节点展开，每个节点约' + str(budget['chars_per_chapter']) +
            '字。请在这一次输出中完整展开机制、已有案例细节、条件对比与具体读者影响；观点和明确标注的假设类比可以自然穿插，不能新增新闻事实或反复重述。'
            '不要把深读压缩成简讯。只输出符合Article的JSON，Markdown保留有效[F1]等引用。'
            '正文不得写“未经独立验证”“尚无独立验证”“非本文验证”“所有数字均为单一信源”等编辑部审核状态，'
            '这些是内部记录，不是新闻条件。来源归属首次清楚说明、后文不混淆；原文真实限制、测试范围和预印本身份仍自然保留。')


def article_chars(markdown):
    return len(re.sub(r'\s|[#*`]', '', public_text(markdown)))


def visual_target(draft):
    size = article_chars(draft['article']['markdown'])
    return 8 if size >= 5500 else 6 if size >= 3000 else 4


def feature_plan(plan, evidence):
    """Strengthen the requested feature length without turning sparse source material into padding."""
    count = len(evidence.get('facts', []))
    if count >= 10 and plan['story_mode'] == 'brief':
        plan['story_mode'] = 'breaking'
    floor = (5500 if plan['story_mode'] == 'explainer' else 4200) if count >= 10 else (
        4000 if plan['story_mode'] == 'explainer' else 3200) if count >= 8 else 0
    plan['target_chars'] = max(plan['target_chars'], floor)
    return plan
