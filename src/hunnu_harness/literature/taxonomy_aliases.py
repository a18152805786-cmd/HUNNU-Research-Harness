"""English aliases for the frozen taxonomy's subtopic names.

The taxonomy-name signal is what corroborates an assignment: a title containing
a subtopic's own name is filed under that subtopic with measured precision
1.000.  The frozen values are Chinese, so an English title can never contain
one, which left English works with a single uncorroborated lexicon signal and
a 16% auto-coverage against 65% for Chinese works.

This table is the bridge.  It maps each frozen subtopic value to the standard
English terms for the *same construct* -- the translation of the name and its
routine surface variants, never terms fitted to an individual paper.  It is
additive and lives outside ``topic_taxonomy.json``: the taxonomy stays frozen,
an alias can never create a topic, and an alias hit is credited through the
same ``SIGNAL_TAXONOMY_NAME`` path a Chinese name hit uses, at the same
thresholds.

Like the Navigator lexicon, the table is explicit rather than learned: it is
diffable, testable, and reviewable.  It is also a calibration artifact -- the
backtest in ``tests/test_post_acquisition_classification.py`` re-measures
precision and per-language coverage over the real corpus, and an edit here is
acceptable only with those numbers held or improved.

Matching rules, and why they differ from the Chinese name terms:

* **Word boundaries.**  Chinese name terms match by substring because CJK has
  no word boundaries.  A Latin alias matched the same way fires inside other
  words (``ai`` in ``chain``), so alias hits require word boundaries, exactly
  as the lexicon's Latin terms do.
* **Best hit only, per field.**  The parts of a compound Chinese name are
  distinct constructs (``融资约束``, ``资本配置``) and both appearing is
  genuinely stronger evidence, so they accumulate.  Aliases for one subtopic
  are *synonymous variants* of one construct (``audit`` / ``auditing`` /
  ``audit quality``); counting several of them would multiply one piece of
  evidence, so only the most specific hit per field is credited.
* **Specificity by word count.**  A Chinese name term's weight grows with its
  character length.  The equivalent unit for English is the word: one English
  word carries about what a two-character Chinese term does (``创新`` /
  ``innovation``), and a two-word phrase what a four-character compound does
  (``融资约束`` / ``financing constraints``).  ``ALIAS_WORD_CJK_EQUIVALENT``
  states that conversion; the weight then goes through the same formula and
  the same cap as a Chinese name term.

What is deliberately absent:

* Bare high-frequency abbreviations already covered by the Chinese name terms
  themselves (``esg`` is a taxonomy name part of ``ESG与社会责任`` and already
  matches).  Duplicating one here would double-credit every hit and change the
  calibrated Chinese behaviour.
* Econometric tool words (``difference in differences``, ``instrumental
  variable``).  An applied paper names its method in the title without being
  *about* methods; crediting the method subtopic for them misfiles applied
  work.
* Aliases for ``综合经济与管理`` -- it is the human catch-all, and routing
  English works into it automatically is exactly what review is for.
"""

from __future__ import annotations

from typing import Mapping

from ..navigator.tokenize import fold

# One English word counts as this many CJK characters when its specificity
# weight is computed (see the module docstring).
ALIAS_WORD_CJK_EQUIVALENT = 2

# Keyed by the frozen subtopic value, exactly as it appears in
# ``topic_taxonomy.json``.  Every key must resolve against the frozen taxonomy
# and every alias must be Latin-only -- both are asserted by tests, not
# silently repaired here, so a bad edit fails loudly.
ENGLISH_SUBTOPIC_ALIASES: Mapping[str, tuple[str, ...]] = {
    # 01_人工智能与数字经济
    "数字化转型": ("digital transformation", "digitalization", "digitalisation"),
    "AI漂洗": ("ai washing", "aiwashing", "artificial intelligence washing"),
    "人工智能与机器人": (
        "artificial intelligence",
        "machine learning",
        "robot",
        "robots",
        "robotics",
        "generative ai",
        "large language model",
        "large language models",
    ),
    "数字经济": ("digital economy", "platform economy"),
    # 02_企业创新与生产率
    "生产率与资源配置": (
        "productivity",
        "total factor productivity",
        "resource allocation",
        "resource misallocation",
    ),
    "技术进步与产业升级": (
        "technological progress",
        "technological change",
        "industrial upgrading",
    ),
    "企业创新": ("innovation", "corporate innovation", "firm innovation", "enterprise innovation"),
    # 03_公司金融与企业投资
    "企业投资与现金持有": (
        "corporate investment",
        "firm investment",
        "cash holdings",
        "cash holding",
        "investment efficiency",
        "overinvestment",
        "over investment",
        "underinvestment",
        "under investment",
    ),
    "融资约束与资本配置": (
        "financing constraints",
        "financing constraint",
        "financial constraints",
        "financial constraint",
        "capital allocation",
    ),
    "债务与资本成本": ("cost of debt", "cost of capital", "debt financing", "corporate debt", "leverage"),
    # 04_会计审计与信息披露
    "信息披露": ("disclosure", "information disclosure", "voluntary disclosure", "corporate disclosure"),
    "盈余质量与财务报告": (
        "earnings quality",
        "earnings management",
        "financial reporting",
        "accrual quality",
        "accruals",
    ),
    "审计与内部控制": (
        "audit",
        "auditing",
        "auditor",
        "auditors",
        "audit quality",
        "audit fees",
        "internal control",
        "internal controls",
    ),
    # 05_资本市场与证券
    "股价与市场波动": (
        "stock price",
        "stock prices",
        "stock return",
        "stock returns",
        "market volatility",
        "stock price crash",
        "crash risk",
    ),
    "分析师与资本市场": (
        "analyst",
        "analysts",
        "analyst coverage",
        "analyst forecasts",
        "capital market",
        "capital markets",
    ),
    "卖空与市场机制": ("short selling", "short sale", "short sales", "short sellers", "margin trading"),
    # 06_公司治理与高管行为
    "公司治理与代理成本": (
        "corporate governance",
        "agency cost",
        "agency costs",
        "agency problem",
        "agency problems",
    ),
    "高管激励与管理者行为": (
        "executive compensation",
        "executive incentives",
        "managerial incentives",
        "managerial behavior",
        "managerial behaviour",
        "ceo",
        "ceos",
    ),
    "股权与所有制": (
        "ownership structure",
        "state ownership",
        "ownership concentration",
        "state owned enterprise",
        "state owned enterprises",
    ),
    # 07_风险与企业韧性
    "风险承担与不确定性": (
        "risk taking",
        "uncertainty",
        "economic policy uncertainty",
        "corporate risk taking",
    ),
    "企业与组织韧性": (
        "organizational resilience",
        "organisational resilience",
        "corporate resilience",
        "firm resilience",
        "resilience",
    ),
    "供应链韧性": ("supply chain resilience",),
    # 08_供应链与国际贸易
    "全球价值链与国际化": (
        "global value chain",
        "global value chains",
        "internationalization",
        "internationalisation",
        "export",
        "exports",
    ),
    "供应链与产业链": ("supply chain", "supply chains", "industrial chain", "supplier network"),
    # 09_ESG与绿色发展
    "环境规制与污染治理": (
        "environmental regulation",
        "environmental regulations",
        "pollution",
        "pollution control",
        "environmental governance",
    ),
    "绿色金融与绿色创新": ("green finance", "green innovation", "green credit", "green bonds"),
    # ``esg`` is a Latin name part of the frozen value itself and already
    # matches; an alias built around the same word ("esg rating") would let a
    # single phrase earn the name credit *and* the alias credit and clear the
    # gate alone.
    "ESG与社会责任": (
        "corporate social responsibility",
        "csr",
        "social responsibility",
    ),
    # 10_劳动与收入分配
    "劳动市场与就业": (
        "labor market",
        "labor markets",
        "labour market",
        "labour markets",
        "employment",
        "unemployment",
        "jobs",
    ),
    "工资与收入分配": (
        "wage",
        "wages",
        "income distribution",
        "income inequality",
        "wage inequality",
        "pay gap",
    ),
    "人力资本": ("human capital",),
    # 11_产业经济与区域发展
    "产业政策与结构": ("industrial policy", "industrial policies", "industrial structure"),
    "区域经济与城市发展": (
        "regional economy",
        "regional development",
        "urban development",
        "urban economics",
    ),
    "高质量发展": ("high quality development",),
    # 12_金融机构与信贷
    "金融风险与信用": ("financial risk", "financial risks", "credit risk", "systemic risk", "default risk"),
    "银行信贷与金融发展": (
        "bank credit",
        "bank loan",
        "bank loans",
        "bank lending",
        "bank financing",
        "financial development",
    ),
    "金融错配与资源配置": (
        "financial misallocation",
        "financial mismatch",
        "capital misallocation",
        "credit misallocation",
    ),
    # 13_宏观经济与人口
    "宏观经济与人口": (
        "macroeconomic",
        "macroeconomics",
        "macroeconomy",
        "business cycle",
        "business cycles",
        "population aging",
        "population ageing",
        "demographic change",
    ),
    # 14_计量方法与研究设计
    "计量方法与研究设计": ("econometric", "econometrics", "research design"),
    # 99_其他 -- the human catch-all takes no aliases (see the module docstring).
    "综合经济与管理": (),
}


def _folded_table() -> dict[str, tuple[str, ...]]:
    """Fold every alias once, longest-word-count first so the best hit wins early."""

    table: dict[str, tuple[str, ...]] = {}
    for subtopic, aliases in ENGLISH_SUBTOPIC_ALIASES.items():
        folded = [term for term in (fold(alias) for alias in aliases) if term]
        folded.sort(key=lambda term: (-len(term.split()), -len(term), term))
        table[subtopic] = tuple(dict.fromkeys(folded))
    return table


_FOLDED_ALIASES = _folded_table()


def folded_aliases_for(subtopic: str) -> tuple[str, ...]:
    """Every folded alias for one frozen subtopic value, best-first."""

    return _FOLDED_ALIASES.get(subtopic, ())


def alias_word_count(folded_alias: str) -> int:
    return len(folded_alias.split())


def _boundary_hit(padded: str, term: str) -> bool:
    """True when *term* occurs in *padded* on word boundaries.

    Same contract as the lexicon's Latin matching: ``ai`` must not fire inside
    ``said`` or ``chain``.  *padded* is the folded text wrapped in one space on
    each side so the boundary test needs no edge cases.
    """

    start = 0
    while True:
        position = padded.find(term, start)
        if position < 0:
            return False
        before = padded[position - 1]
        after_index = position + len(term)
        after = padded[after_index] if after_index < len(padded) else " "
        if not before.isalnum() and not after.isalnum():
            return True
        start = position + 1


def best_alias_hit(folded_text: str, subtopic: str) -> str | None:
    """The most specific alias of *subtopic* that occurs in *folded_text*.

    Returns one folded alias or ``None``.  One hit at most, by design: the
    aliases of a subtopic are synonymous variants of a single construct, and
    crediting several of them would count the same evidence twice.
    """

    if not folded_text:
        return None
    padded = f" {folded_text} "
    for alias in _FOLDED_ALIASES.get(subtopic, ()):
        if _boundary_hit(padded, alias):
            return alias
    return None


__all__ = [
    "ALIAS_WORD_CJK_EQUIVALENT",
    "ENGLISH_SUBTOPIC_ALIASES",
    "alias_word_count",
    "best_alias_hit",
    "folded_aliases_for",
]
