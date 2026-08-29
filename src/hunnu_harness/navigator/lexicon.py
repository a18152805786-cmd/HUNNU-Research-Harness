"""Auditable cross-language concept table.

The corpus writes one construct several ways -- ``AI washing``, ``人工智能漂洗``,
``AI漂洗``, ``漂智``, ``talk-walk gap`` -- and a query in one language recalls at
most half the works unless the two vocabularies are bridged.

The bridge is this explicit table rather than a learned embedding, for three
reasons drawn from discovery: there is no embedding client in the environment,
the Navigator must work offline, and an agent has to be able to see exactly what
its query was expanded into.  A table is diffable, testable, and reviewable; a
vector is none of those.

Each concept also declares a *facet*, which is what lets a research-question
query report roles (CORE / MECHANISM / OUTCOME / METHOD) instead of an
undifferentiated ranked list.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .tokenize import fold


class Facet(str, Enum):
    """What role a concept plays in a research question."""

    PHENOMENON = "PHENOMENON"
    MECHANISM = "MECHANISM"
    OUTCOME = "OUTCOME"
    METHOD = "METHOD"
    CONTEXT = "CONTEXT"


class RelevanceRole(str, Enum):
    CORE = "CORE"
    MECHANISM = "MECHANISM"
    OUTCOME = "OUTCOME"
    METHOD = "METHOD"
    BACKGROUND = "BACKGROUND"


FACET_TO_ROLE: dict[Facet, RelevanceRole] = {
    Facet.PHENOMENON: RelevanceRole.CORE,
    Facet.MECHANISM: RelevanceRole.MECHANISM,
    Facet.OUTCOME: RelevanceRole.OUTCOME,
    Facet.METHOD: RelevanceRole.METHOD,
    Facet.CONTEXT: RelevanceRole.BACKGROUND,
}


@dataclass(frozen=True)
class Concept:
    """One research construct and every surface form seen in this corpus."""

    key: str
    facet: Facet
    terms: tuple[str, ...]
    topics: tuple[str, ...] = ()

    def folded_terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fold(term) for term in self.terms if fold(term)))


# Terms are grounded in the actual 179-work corpus and in the frozen taxonomy's
# 39 subtopic values.  ``topics`` names taxonomy subtopics whose presence on a
# work is itself evidence for the concept.
CONCEPTS: tuple[Concept, ...] = (
    Concept(
        key="ai_washing",
        facet=Facet.PHENOMENON,
        terms=(
            "ai washing", "aiwashing", "ai-washing", "artificial intelligence washing",
            "人工智能漂洗", "ai漂洗", "漂洗", "漂智", "智能漂洗",
            "talk walk gap", "talk-walk gap", "ai narrative", "ai narratives",
            "虚假的智能", "伦理漂洗",
        ),
        topics=("AI漂洗",),
    ),
    Concept(
        key="greenwashing",
        facet=Facet.PHENOMENON,
        terms=("greenwashing", "greenwash", "green washing", "漂绿", "洗绿", "环境漂绿"),
    ),
    Concept(
        key="artificial_intelligence",
        facet=Facet.CONTEXT,
        terms=(
            "artificial intelligence", "ai", "machine learning", "robot", "robots",
            "人工智能", "智能化", "机器人", "算法", "大模型", "生成式人工智能",
        ),
        topics=("人工智能与机器人",),
    ),
    Concept(
        key="digital_transformation",
        facet=Facet.CONTEXT,
        terms=(
            "digital transformation", "digitalization", "digitization", "digital economy",
            "数字化转型", "数字化", "数字经济", "数智化",
        ),
        topics=("数字化转型", "数字经济"),
    ),
    Concept(
        key="audit_risk",
        facet=Facet.MECHANISM,
        terms=(
            "audit", "audits", "audit risk", "auditor", "auditors",
            "audit fee", "audit fees", "audit opinion", "audit quality",
            "audit effort", "assurance", "internal control",
            "审计", "审计风险", "审计师", "审计费用", "审计意见", "注册会计师",
            "内部控制", "风险决策", "鉴证",
        ),
        topics=("审计与内部控制",),
    ),
    Concept(
        key="information_disclosure",
        facet=Facet.MECHANISM,
        terms=(
            "disclosure", "information disclosure", "voluntary disclosure",
            "information asymmetry", "textual disclosure", "annual report tone",
            "信息披露", "披露", "信息不对称", "年报", "文本披露", "语调",
        ),
        topics=("信息披露",),
    ),
    Concept(
        key="agency_governance",
        facet=Facet.MECHANISM,
        terms=(
            "agency cost", "agency costs", "corporate governance", "monitoring",
            "external governance", "board", "institutional investor",
            "代理成本", "公司治理", "外部治理", "监督", "董事会", "机构投资者",
        ),
        topics=("公司治理与代理成本",),
    ),
    Concept(
        key="financing_constraint",
        facet=Facet.MECHANISM,
        terms=(
            "financing constraint", "financing constraints", "financial constraint",
            "bank loan", "bank loans", "credit", "cost of debt", "cost of capital",
            "融资约束", "银行贷款", "信贷", "债务成本", "资本成本", "融资成本",
        ),
        topics=("融资约束与资本配置", "债务与资本成本", "银行信贷与金融发展"),
    ),
    Concept(
        key="earnings_quality",
        facet=Facet.OUTCOME,
        terms=(
            "earnings quality", "earnings management", "accrual", "accruals",
            "discretionary accruals", "financial reporting quality", "restatement",
            "盈余质量", "盈余管理", "应计", "可操控性应计", "财务报告质量", "财务重述",
        ),
        topics=("盈余质量与财务报告",),
    ),
    Concept(
        key="innovation",
        facet=Facet.OUTCOME,
        terms=(
            "innovation", "innovative", "patent", "patents", "r&d", "research and development",
            "创新", "技术创新", "专利", "研发", "创新绩效", "新质生产力",
        ),
        topics=("企业创新", "技术进步与产业升级"),
    ),
    Concept(
        key="firm_value",
        facet=Facet.OUTCOME,
        terms=(
            "firm value", "firm performance", "market value", "tobin q", "tobin's q",
            "stock price crash", "crash risk", "market reaction", "stock return",
            "企业价值", "企业绩效", "市值", "股价崩盘", "崩盘风险", "市场反应", "股票收益",
        ),
        topics=("股价与市场波动",),
    ),
    Concept(
        key="productivity",
        facet=Facet.OUTCOME,
        terms=(
            "productivity", "total factor productivity", "tfp", "efficiency",
            "resource allocation", "misallocation",
            "生产率", "全要素生产率", "效率", "资源配置", "资源错配",
        ),
        topics=("生产率与资源配置", "金融错配与资源配置"),
    ),
    Concept(
        key="risk_resilience",
        facet=Facet.OUTCOME,
        terms=(
            "risk taking", "risk-taking", "uncertainty", "resilience",
            "organizational resilience", "supply chain resilience",
            "风险承担", "不确定性", "韧性", "组织韧性", "供应链韧性",
        ),
        topics=("风险承担与不确定性", "企业与组织韧性", "供应链韧性"),
    ),
    Concept(
        key="esg",
        facet=Facet.OUTCOME,
        terms=(
            "esg", "corporate social responsibility", "csr", "environmental",
            "carbon", "green finance", "green innovation",
            "环境", "社会责任", "绿色金融", "绿色创新", "碳排放", "环境规制",
        ),
        topics=("ESG与社会责任", "绿色金融与绿色创新", "环境规制与污染治理"),
    ),
    Concept(
        key="labor",
        facet=Facet.OUTCOME,
        terms=(
            "employment", "labor", "labour", "wage", "wages", "human capital",
            "income distribution", "pay gap",
            "就业", "劳动", "工资", "人力资本", "收入分配", "薪酬差距",
        ),
        topics=("劳动市场与就业", "工资与收入分配", "人力资本"),
    ),
    Concept(
        key="supply_chain",
        facet=Facet.CONTEXT,
        terms=(
            "supply chain", "global value chain", "gvc", "customer", "supplier",
            "trade", "export", "import",
            "供应链", "全球价值链", "产业链", "客户", "供应商", "贸易", "出口", "进口",
        ),
        topics=("供应链与产业链", "全球价值链与国际化"),
    ),
    Concept(
        key="executive_behaviour",
        facet=Facet.MECHANISM,
        terms=(
            "ceo", "executive", "management", "manager", "overconfidence",
            "incentive", "compensation", "managerial myopia",
            "高管", "管理者", "过度自信", "激励", "薪酬", "管理层短视",
        ),
        topics=("高管激励与管理者行为",),
    ),
    Concept(
        key="analyst_market",
        facet=Facet.MECHANISM,
        terms=(
            "analyst", "analysts", "forecast", "short selling", "media coverage",
            "media monitoring", "investor attention",
            "分析师", "预测", "卖空", "融资融券", "媒体监督", "媒体关注", "投资者关注",
        ),
        topics=("分析师与资本市场", "卖空与市场机制"),
    ),
    Concept(
        key="identification_method",
        facet=Facet.METHOD,
        terms=(
            "difference in differences", "difference-in-differences", "did",
            "instrumental variable", "instrumental variables", "iv",
            "regression discontinuity", "rdd", "propensity score matching", "psm",
            "natural experiment", "quasi natural experiment", "endogeneity",
            "robustness", "placebo", "staggered", "fixed effects",
            "双重差分", "工具变量", "断点回归", "倾向得分匹配", "自然实验",
            "准自然实验", "内生性", "稳健性", "安慰剂", "固定效应", "识别策略",
        ),
        topics=("计量方法与研究设计",),
    ),
    Concept(
        key="measurement",
        facet=Facet.METHOD,
        terms=(
            "measurement", "index construction", "text analysis", "textual analysis",
            "machine learning measure", "multimodal", "word embedding",
            "指标构建", "测度", "文本分析", "多模态", "词向量", "指数构建",
        ),
    ),
)


CONCEPTS_BY_KEY: dict[str, Concept] = {concept.key: concept for concept in CONCEPTS}


def _build_lookup() -> tuple[dict[str, tuple[Concept, ...]], int]:
    lookup: dict[str, list[Concept]] = {}
    longest = 1
    for concept in CONCEPTS:
        for term in concept.folded_terms():
            lookup.setdefault(term, []).append(concept)
            longest = max(longest, len(term))
    return {term: tuple(items) for term, items in lookup.items()}, longest


_TERM_LOOKUP, _LONGEST_TERM = _build_lookup()


@dataclass(frozen=True)
class ConceptMatch:
    """One concept detected in a query, with the surface form that triggered it."""

    concept: Concept
    matched_term: str

    @property
    def key(self) -> str:
        return self.concept.key

    @property
    def facet(self) -> Facet:
        return self.concept.facet


def match_concepts(text: str | None) -> tuple[ConceptMatch, ...]:
    """Find every concept whose surface form occurs in *text*.

    Matching is substring-based over the folded query.  For Latin terms the
    match is additionally required to fall on a word boundary so that ``ai``
    does not fire inside ``said`` or ``domain``.  CJK has no such boundaries,
    so a substring match is the correct test there.
    """

    folded = fold(text)
    if not folded:
        return ()
    padded = f" {folded} "
    matches: list[ConceptMatch] = []
    seen: set[tuple[str, str]] = set()
    for term, concepts in _TERM_LOOKUP.items():
        if not term:
            continue
        if _is_latin_term(term):
            if f" {term} " not in padded and not _latin_boundary_hit(padded, term):
                continue
        elif term not in folded:
            continue
        for concept in concepts:
            key = (concept.key, term)
            if key in seen:
                continue
            seen.add(key)
            matches.append(ConceptMatch(concept=concept, matched_term=term))
    matches.sort(key=lambda item: (-len(item.matched_term), item.concept.key))
    return tuple(matches)


def _is_latin_term(term: str) -> bool:
    return all(ord(char) < 0x2E80 for char in term)


def _latin_boundary_hit(padded: str, term: str) -> bool:
    start = 0
    while True:
        position = padded.find(term, start)
        if position < 0:
            return False
        before = padded[position - 1] if position > 0 else " "
        after_index = position + len(term)
        after = padded[after_index] if after_index < len(padded) else " "
        if not before.isalnum() and not after.isalnum():
            return True
        start = position + 1


def expansion_terms(matches: tuple[ConceptMatch, ...], *, exclude: set[str] | None = None) -> dict[str, tuple[str, ...]]:
    """Return, per concept key, the surface forms not already in the query."""

    exclude = exclude or set()
    expansions: dict[str, tuple[str, ...]] = {}
    for match in matches:
        added = tuple(
            term
            for term in match.concept.folded_terms()
            if term not in exclude and term != match.matched_term
        )
        if added:
            expansions[match.concept.key] = added
    return expansions


def topics_for_concepts(matches: tuple[ConceptMatch, ...]) -> tuple[str, ...]:
    seen: list[str] = []
    for match in matches:
        for topic in match.concept.topics:
            if topic not in seen:
                seen.append(topic)
    return tuple(seen)


__all__ = [
    "CONCEPTS",
    "CONCEPTS_BY_KEY",
    "Concept",
    "ConceptMatch",
    "FACET_TO_ROLE",
    "Facet",
    "RelevanceRole",
    "expansion_terms",
    "match_concepts",
    "topics_for_concepts",
]
