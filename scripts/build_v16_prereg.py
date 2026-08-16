from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hunnu_harness.paths import RUNS_ROOT


RUN_ROOT = RUNS_ROOT / "AIWash_AbsDA_Prereg_V16"
V15_CSV = RUNS_ROOT / "ModifiedJones_AbsDA_V15" / "processed" / "modified_jones_absda_v15.csv"
CONTROL_SOURCE = Path(
    r"D:\BaiduNetdiskDownload\论文数据\上市公司常用控制变量Stata整理代码2000-2024年\上市公司常用控制变量Stata整理代码2000-2024年\常用控制变量24（已剔除金融STPT已缩尾）.dta"
)
CONTROL_CODE = Path(
    r"D:\BaiduNetdiskDownload\论文数据\上市公司常用控制变量Stata整理代码2000-2024年\上市公司常用控制变量Stata整理代码2000-2024年\计算代码.do"
)
AI_KEY_SOURCE = Path(r"C:\Users\<user>\Desktop\research-data\variables.dta")
THESIS_DATA_ROOT = Path(r"D:\BaiduNetdiskDownload\论文数据")

CONTROLS = ["Size", "ROA", "Lev", "ListAge", "BM", "SOE", "Board", "Indep", "Top1", "Dual"]
YEARS = list(range(2014, 2025))
CONTROL_SOURCE_COLUMNS = ["stkcd", "year", *CONTROLS]

CONTROL_META = {
    "Size": {
        "ChineseName": "公司规模",
        "Definition": "年总资产的自然对数",
        "Formula": "ln(资产总计)",
        "Unit": "log(元)",
        "WinsorStatus": "继承既有正式源：按 year 1%/99% 双尾缩尾一次",
    },
    "ROA": {
        "ChineseName": "总资产净利润率",
        "Definition": "既有正式源的总资产净利润率变量",
        "Formula": "源字段：总资产净利润率ROA（计算代码中直接保留）",
        "Unit": "ratio",
        "WinsorStatus": "继承既有正式源：按 year 1%/99% 双尾缩尾一次",
    },
    "Lev": {
        "ChineseName": "资产负债率",
        "Definition": "年末负债总额相对于年末资产总额",
        "Formula": "负债合计 / 资产总计",
        "Unit": "ratio",
        "WinsorStatus": "继承既有正式源：按 year 1%/99% 双尾缩尾一次",
    },
    "ListAge": {
        "ChineseName": "上市年限",
        "Definition": "上市年份起算的对数上市年限",
        "Formula": "ln(year - 上市年份 + 1)",
        "Unit": "log(years)",
        "WinsorStatus": "继承既有正式源：按 year 1%/99% 双尾缩尾一次",
    },
    "BM": {
        "ChineseName": "账面市值比",
        "Definition": "账面资产相对于年度股票总市值的比值",
        "Formula": "资产总计 / 年个股总市值",
        "Unit": "ratio",
        "WinsorStatus": "继承既有正式源：按 year 1%/99% 双尾缩尾一次",
    },
    "SOE": {
        "ChineseName": "国有企业",
        "Definition": "国有控股企业虚拟变量",
        "Formula": "regexm(股权性质, 国有)",
        "Unit": "binary 0/1",
        "WinsorStatus": "不缩尾：二元变量；既有 winsor2 列表未包含 SOE",
    },
    "Board": {
        "ChineseName": "董事会规模",
        "Definition": "董事人数的自然对数",
        "Formula": "ln(董事人数)",
        "Unit": "log(persons)",
        "WinsorStatus": "继承既有正式源：按 year 1%/99% 双尾缩尾一次",
    },
    "Indep": {
        "ChineseName": "独立董事比例",
        "Definition": "独立董事人数相对于董事会人数的比例",
        "Formula": "其中独立董事 / 董事人数",
        "Unit": "ratio",
        "WinsorStatus": "继承既有正式源：按 year 1%/99% 双尾缩尾一次",
    },
    "Top1": {
        "ChineseName": "第一大股东持股比例",
        "Definition": "第一大股东持股数量相对于总股数的比例",
        "Formula": "股权集中指标1 / 100",
        "Unit": "ratio",
        "WinsorStatus": "继承既有正式源：按 year 1%/99% 双尾缩尾一次",
    },
    "Dual": {
        "ChineseName": "两职合一",
        "Definition": "董事长与总经理是否由同一人兼任的虚拟变量",
        "Formula": "(董事长与总经理兼任情况 == 1)",
        "Unit": "binary 0/1",
        "WinsorStatus": "不缩尾：二元变量；既有 winsor2 列表未包含 Dual",
    },
}


def normalize_code(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)
    numeric = pd.to_numeric(text, errors="coerce")
    normalized = text.copy()
    valid = numeric.notna()
    normalized.loc[valid] = numeric.loc[valid].astype("Int64").astype("string").str.zfill(6)
    return normalized


def make_key(code: pd.Series, year: pd.Series) -> pd.Series:
    year_text = pd.to_numeric(year, errors="coerce").astype("Int64").astype("string")
    return normalize_code(code) + "|" + year_text


def finite(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").notna() & np.isfinite(pd.to_numeric(series, errors="coerce"))


def write_text(path: Path, text: str) -> None:
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def available_years(series: pd.Series, years: pd.Series) -> str:
    valid = finite(series)
    found = sorted(pd.to_numeric(years.loc[valid], errors="coerce").dropna().astype(int).unique().tolist())
    if not found:
        return "none"
    if found == list(range(found[0], found[-1] + 1)):
        return f"{found[0]}-{found[-1]}"
    return ",".join(str(x) for x in found)


def build() -> dict:
    for required in (V15_CSV, CONTROL_SOURCE, CONTROL_CODE, AI_KEY_SOURCE):
        if not required.exists():
            raise FileNotFoundError(required)

    for subdir in ("audit", "specifications", "sample", "reports", "logs"):
        (RUN_ROOT / subdir).mkdir(parents=True, exist_ok=True)

    # Read the approved V15 outcome only; V15 is never overwritten.
    v15 = pd.read_csv(
        V15_CSV,
        usecols=["Stkcd", "Year", "AbsDA", "FinancialIndustryFlag", "ModifiedJonesSampleEligible", "ExclusionReason"],
        low_memory=False,
    )
    v15["Year"] = pd.to_numeric(v15["Year"], errors="coerce").astype("Int64")
    v15["FirmID"] = normalize_code(v15["Stkcd"])
    v15["Key"] = make_key(v15["Stkcd"], v15["Year"])
    formal = v15.loc[
        v15["Year"].isin(YEARS)
        & v15["AbsDA"].notna()
        & (pd.to_numeric(v15["FinancialIndustryFlag"], errors="coerce") == 0)
    ].copy()
    if formal.duplicated(["FirmID", "Year"]).any():
        raise ValueError("V15 formal AbsDA sample is not firm-year unique")

    # Read only the two key columns from the AI_wash source. No AI_wash, z_speech,
    # z_invest, speech, invest, or other value column is requested or loaded.
    ai_keys = pd.read_stata(AI_KEY_SOURCE, columns=["stkcd_std", "year_std"], convert_categoricals=False)
    if list(ai_keys.columns) != ["stkcd_std", "year_std"]:
        raise ValueError(f"Unexpected AI key columns loaded: {list(ai_keys.columns)}")
    ai_keys["Key"] = make_key(ai_keys["stkcd_std"], ai_keys["year_std"])
    ai_key_unique = not ai_keys["Key"].duplicated().any()
    if not ai_key_unique:
        raise ValueError("AI key source is not unique at firm-year")
    ai_key_set = set(ai_keys["Key"].dropna())

    # Read the existing 2000-2024 formal control panel. This source already has
    # the documented finance/ST/PT/A-share/listing rules and annual winsor status.
    controls = pd.read_stata(CONTROL_SOURCE, columns=CONTROL_SOURCE_COLUMNS, convert_categoricals=False)
    if any(col not in controls.columns for col in CONTROL_SOURCE_COLUMNS):
        raise ValueError(f"Missing control columns: {set(CONTROL_SOURCE_COLUMNS) - set(controls.columns)}")
    controls["Year"] = pd.to_numeric(controls["year"], errors="coerce").astype("Int64")
    controls["FirmID"] = normalize_code(controls["stkcd"])
    controls["Key"] = make_key(controls["stkcd"], controls["year"])
    control_duplicates = int(controls.duplicated(["FirmID", "Year"]).sum())
    if control_duplicates:
        raise ValueError(f"Control source has {control_duplicates} duplicate firm-year rows")
    controls = controls.loc[controls["Year"].isin(YEARS)].copy()

    control_merge_cols = ["FirmID", "Year", *CONTROLS]
    control_for_merge = controls[control_merge_cols].copy()
    merged = formal[["FirmID", "Year", "Key", "Stkcd", "AbsDA", "FinancialIndustryFlag"]].merge(
        control_for_merge,
        on=["FirmID", "Year"],
        how="left",
        validate="one_to_one",
        indicator=False,
    )
    merged["AIWashKeyPresent"] = merged["Key"].isin(ai_key_set)
    merged["All10ControlsAvailable"] = merged[CONTROLS].apply(lambda col: finite(col), axis=0).all(axis=1)
    merged["ExpectedBaseline"] = merged["AIWashKeyPresent"] & merged["All10ControlsAvailable"]

    absda_n = int(len(merged))
    absda_firms = int(merged["FirmID"].nunique())
    ai_matched_n = int(merged["AIWashKeyPresent"].sum())
    all_controls_n = int(merged["All10ControlsAvailable"].sum())
    expected_n = int(merged["ExpectedBaseline"].sum())
    expected_firms = int(merged.loc[merged["ExpectedBaseline"], "FirmID"].nunique())

    # Control definition audit is based on the formal AbsDA input sample and on
    # the source do-file, not on AI_wash values.
    source_unique = not controls.duplicated(["FirmID", "Year"]).any()
    definition_rows = []
    for variable in CONTROLS:
        nonmissing = finite(merged[variable])
        meta = CONTROL_META[variable]
        definition_rows.append(
            {
                "Variable": variable,
                "ChineseName": meta["ChineseName"],
                "Definition": meta["Definition"],
                "Formula": meta["Formula"],
                "Unit": meta["Unit"],
                "Source": f"{CONTROL_SOURCE}; formula audit: {CONTROL_CODE}",
                "WinsorStatus": meta["WinsorStatus"],
                "MissingN": int((~nonmissing).sum()),
                "AvailableYears": available_years(merged[variable], merged["Year"]),
                "FirmYearUnique": str(source_unique).lower(),
                "Approved": str(variable in controls.columns and source_unique and nonmissing.any()).lower(),
            }
        )
    definition_audit = pd.DataFrame(definition_rows)
    all_definitions_verified = bool((definition_audit["Approved"] == "true").all())

    # Within-firm variation is computed on the expected baseline preflight rows;
    # it does not include AI_wash values and is only an absorption diagnostic.
    expected_panel = merged.loc[merged["ExpectedBaseline"], ["FirmID", "Year", *CONTROLS]].copy()
    variation_rows = []
    potentially_absorbed = []
    for variable in CONTROLS:
        valid = finite(expected_panel[variable])
        valid_panel = expected_panel.loc[valid, ["FirmID", variable]].copy()
        nunique = valid_panel.groupby("FirmID")[variable].nunique(dropna=True)
        within_var_firms = int((nunique > 1).sum())
        time_invariant_firms = int((nunique == 1).sum())
        if within_var_firms == 0:
            potentially_absorbed.append(variable)
        variation_rows.append(
            {
                "Variable": variable,
                "PreflightFirmYears": int(len(expected_panel)),
                "NonmissingFirmYears": int(valid.sum()),
                "MissingFirmYears": int((~valid).sum()),
                "FirmsWithNonmissing": int(nunique.index.nunique()),
                "FirmsWithWithinVariation": within_var_firms,
                "FirmsTimeInvariantAmongNonmissing": time_invariant_firms,
                "OverallDistinctValues": int(expected_panel.loc[valid, variable].nunique(dropna=True)),
                "PotentiallyAbsorbedByFirmFE": str(within_var_firms == 0).lower(),
                "AuditNote": "Retain in the frozen control set; Firm FE may omit a variable with no within-firm variation.",
            }
        )
    variation_audit = pd.DataFrame(variation_rows)

    # Machine-readable and human-readable sample preflight, deliberately limited
    # to keys, AbsDA availability, and control missingness.
    preflight_rows = [
        {"Metric": "AbsDAN", "Value": absda_n, "Firms": absda_firms, "Definition": "V15 formal nonfinancial AbsDA firm-years, 2014-2024"},
        {"Metric": "AIWashKeyMatchedN", "Value": ai_matched_n, "Firms": int(merged.loc[merged["AIWashKeyPresent"], "FirmID"].nunique()), "Definition": "AbsDA firm-years with an AI_wash firm-year key; key only"},
        {"Metric": "All10ControlsAvailableN", "Value": all_controls_n, "Firms": int(merged.loc[merged["All10ControlsAvailable"], "FirmID"].nunique()), "Definition": "AbsDA firm-years with all ten controls nonmissing and finite"},
        {"Metric": "ExpectedBaselineN", "Value": expected_n, "Firms": expected_firms, "Definition": "Complete-case intersection of AbsDA, AI key, and all ten controls"},
        {"Metric": "ExpectedBaselineFirms", "Value": expected_firms, "Firms": expected_firms, "Definition": "Unique firms in ExpectedBaselineN"},
    ]
    preflight = pd.DataFrame(preflight_rows)

    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    baseline_controls_text = ", ".join(CONTROLS)
    absorbed_text = ", ".join(potentially_absorbed) if potentially_absorbed else "none fully absorbed by zero within variation"

    sample_rules = f"""# SAMPLE_RULE_FREEZE_V16

Timestamp: {timestamp}

This file freezes the inherited sample rules before the first AI_wash numeric value is read or any regression is run.

## Formal period

- Regression years: 2014-2024 inclusive.
- 2013 remains a lag-only year in the V15 Modified Jones construction and cannot enter the V17 regression.

## Inherited firm-year rules

The selected existing formal control panel is:
`{CONTROL_SOURCE}`

Its read-only calculation code is:
`{CONTROL_CODE}`

The code documents and applies the following existing rules:

1. A-share sample retained; B-share codes beginning with `2` or `9` are dropped.
2. Firm-years before the listing year are dropped.
3. Firm-years at or after the delisting year are dropped.
4. Financial industry is excluded using CSRC industry code containing `J`.
5. ST/PT observations are excluded under the existing company-name ST/PT rule in the source code.
6. The source covers 2000-2024; this study restricts the formal regression sample to 2014-2024.
7. V15 AbsDA is already the approved nonfinancial, unique firm-year outcome; V13/V14/V15 remain read-only.
8. V17 will use listwise deletion for AbsDA, the AI_wash key/value, and all ten controls; no imputation, interpolation, or zero filling.

No new result-driven screening rule is introduced in V16. The source rules are inherited and frozen before the first baseline regression.

## Frozen AI_wash boundary

V16 reads only `stkcd_std` and `year_std` from the AI_wash source for key preflight. No AI_wash, z_speech, z_invest, speech, invest, or other AI value is loaded or used in the preflight.
"""

    controls_freeze = f"""# BASELINE_CONTROL_SET_FREEZE_V16

Timestamp: {timestamp}

BaselineControlCount=10

Controls_Main = {{{baseline_controls_text}}}

The exact definitions, source paths, missingness, and winsor status are recorded in `CONTROL_VARIABLE_DEFINITION_AUDIT.csv`. The selected existing source is the annual 2000-2024 panel already prepared with finance/ST/PT exclusion and annual 1%/99% winsorization of continuous controls. V16 does not recompute, re-standardize, or create competing control versions.

ROA note: ROA is retained in the main model even though it has a conceptual relationship with accrual-based outcome construction. R1 (main controls excluding ROA) is pre-registered as a robustness specification before results are viewed; it is not a result-driven deletion.

No stepwise, backward, forward, LASSO, or p-value-based control selection is permitted.
"""

    prereg_md = f"""# BASELINE_REGRESSION_PREREGISTRATION_V16

Timestamp: {timestamp}

This preregistration is written before the first AI_wash numeric value is read and before any AI_wash -> AbsDA regression, correlation, significance test, or simplified model is run.

## Frozen research question

Does the existing AI_wash measure of disclosure-side AI indicators relative to actual AI-investment-side indicators relate to accrual-based earnings management / earnings quality?

PrimaryY=AbsDA
PrimaryX=AI_wash
ExpectedSign=Positive

High AI_wash is interpreted only as a larger disclosure-side versus actual-investment-side gap. It is not by itself proof of intentional deception, fraud, or misconduct.

## Frozen sample

- Formal years: 2014-2024.
- A-share listed companies under the inherited source rules.
- Financial industry excluded: `FinancialIndustryFlag=J*` / existing CSRC J rule.
- AbsDA must be present and V15 must remain unchanged.
- AI_wash must be present by firm-year key; V17 will read the frozen AI_wash value only after this file is hashed.
- All ten controls must be present and finite.
- Firm-year must be unique.
- MissingRule=Listwise / complete-case.
- No mean imputation, zero filling, interpolation, or post-result sample changes.

## Frozen variables

Controls_Main = {{{baseline_controls_text}}}

BaselineControlCount=10

Definitions and source audit: `CONTROL_VARIABLE_DEFINITION_AUDIT.csv`.

Size, ROA, Lev, ListAge, BM, SOE, Board, Indep, Top1, and Dual are inherited from the existing formal control panel. The existing source's continuous-control annual 1%/99% winsorization is inherited; SOE and Dual remain binary and are not winsorized. AbsDAWinsorized=false. AIWashReprocessed=false.

## Primary specification

AbsDA_it = beta * AI_wash_it + gamma' Controls_it + FirmFE_i + YearFE_t + epsilon_it

FirmFE=true
YearFE=true
IndustryFE=false
IndustryYearFE=false
ClusterSE=Firm
AdditionalConstant=Software fixed-effect implementation may use a within transformation; no additional industry or industry-year FE is added to the primary specification.

Firm-level clustering is frozen because AI_wash and AbsDA are annual panel variables and within-firm errors may be serially correlated.

The primary specification does not use lagged AI_wash, DML, pooled OLS, industry FE, industry-year FE, or alternative outcomes.

## Reproducible implementation plan (not executed in V16)

Stata target syntax for V17:

```stata
reghdfe AbsDA AI_wash Size ROA Lev ListAge BM SOE Board Indep Top1 Dual, ///
    absorb(Stkcd Year) vce(cluster Stkcd)
```

If `reghdfe` is unavailable, V17 must record that fact and use a pre-existing compatible implementation or a documented within-transformation equivalent. V16 does not install packages or run Stata.

Python target is an equivalent entity-and-time fixed-effects implementation (for example, `linearmodels.PanelOLS` with `entity_effects=True`, `time_effects=True`, and entity-clustered covariance), or an independently documented within transformation. V16 records the plan only and does not import, fit, or inspect any regression result.

## Pre-registered limited robustness specifications

Only these three are registered in advance and none is required to replace the primary result:

- R1: AbsDA on AI_wash and all main controls except ROA, Firm FE, Year FE, firm-clustered SE.
- R2: AbsDA_t on AI_wash_(t-1) and the ten controls at t, Firm FE, Year FE, firm-clustered SE.
- R3: Primary controls and FE with two-way Firm + Year clustered SE.

No other specification family, stepwise selection, nonlinear term, U-shape test, mediation, moderation, DML, p-value screening, or alternative Y is pre-registered here.

## Frozen result decision rules

The theory prediction is not changed after results:

- beta > 0 and statistically significant: report a significant positive association consistent with the predicted direction and higher abnormal accruals / lower earnings quality.
- beta > 0 but not significant: report direction consistent with theory but insufficient statistical evidence.
- beta < 0 and significant: report a significant association opposite to the prediction and discuss the need to reassess the mechanism without changing the frozen Y or primary specification.
- beta < 0 and not significant: report no evidence supporting the predicted direction.

Report coefficient, standard error, 95% confidence interval, p-value, N, and firm count. Report p<0.10, p<0.05, and p<0.01 thresholds, but do not rely only on stars.

## Boundary and execution lock

V16 may inspect only AI firm-year keys and control/AbsDA missingness for preflight. It must not inspect AI_wash values, distributions, correlations, or regression output. V17's first execution will reveal only the primary specification, then stop for user review before any robustness specification.

PrimarySpecificationFrozen=true
AIWashReprocessed=false
AbsDAWinsorized=false
"""

    spec = {
        "version": "V16",
        "timestamp": timestamp,
        "outcome": "AbsDA",
        "treatment": "AI_wash",
        "expected_sign": "positive",
        "controls": CONTROLS,
        "fixed_effects": ["firm", "year"],
        "industry_fe": False,
        "industry_year_fe": False,
        "cluster": ["firm"],
        "years": YEARS,
        "exclude_financial": True,
        "financial_rule": "CSRC 2012 J*",
        "missing_rule": "listwise",
        "absda_winsorized": False,
        "ai_wash_reprocessed": False,
        "primary": True,
        "primary_specification_frozen": True,
        "robustness_preregistered": [
            {"id": "R1", "description": "omit ROA; same Firm FE, Year FE, firm-clustered SE"},
            {"id": "R2", "description": "one-period lagged AI_wash; current controls; same FE and firm-clustered SE"},
            {"id": "R3", "description": "two-way Firm + Year clustered SE; same primary variables and FE"},
        ],
        "control_source": str(CONTROL_SOURCE),
        "sample_rule_file": "SAMPLE_RULE_FREEZE_V16.md",
        "ai_key_columns_read_only": ["stkcd_std", "year_std"],
    }

    v17_plan = f"""# V17_EXECUTION_PLAN

V17 is allowed to begin only after this V16 package is reviewed.

1. Read the frozen AI_wash formal values using the already registered source and key.
2. Merge AI_wash by the frozen firm-year key with V15 AbsDA.
3. Merge the frozen ten controls from `{CONTROL_SOURCE}`.
4. Apply the V16 sample rules and complete-case rule exactly.
5. Run the primary two-way fixed-effects model once: AbsDA ~ AI_wash + ten controls + Firm FE + Year FE, firm-clustered SE.
6. Report beta, SE, 95% CI, p-value, N, and firm count, together with the pre-registered sign interpretation.
7. Do not automatically run R1-R3 or any other robustness specification; stop for user review of the first baseline result.

V17 must not change the frozen Y, X, controls, FE, cluster, missing rule, sample, or expected sign based on the first result.
"""

    readme = f"""# README_REVIEW — AIWash_AbsDA_Prereg_V16

V16 freezes the first AI_wash -> AbsDA baseline regression before reading AI_wash numeric values and before running any regression.

## Approval state

PrimaryOutcome=AbsDA
PrimaryTreatment=AI_wash
ExpectedSign=Positive
FormalYears=2014-2024
BaselineControls={baseline_controls_text}
BaselineControlCount=10
FirmFE=true
YearFE=true
IndustryFE=false
IndustryYearFE=false
ClusterSE=Firm
MissingRule=Listwise
FinancialFirmsExcluded=true
AbsDAWinsorized=false
AIWashReprocessed=false
AIWashValueUsed=false
NoAIWashRegressionRun=true
NoAIWashCorrelationScreening=true
NoOutcomeFishing=true

## Preflight counts (keys and missingness only)

- AbsDAN={absda_n}; AbsDA firms={absda_firms}
- AIWashKeyMatchedN={ai_matched_n}
- All10ControlsAvailableN={all_controls_n}
- ExpectedBaselineN={expected_n}; ExpectedBaselineFirms={expected_firms}
- ControlsPotentiallyAbsorbedByFirmFE={absorbed_text}

## Source boundaries

- V15 AbsDA is read-only: `{V15_CSV}`.
- Existing controls are read-only: `{CONTROL_SOURCE}`.
- The AI source was accessed only with columns `stkcd_std` and `year_std` for key preflight. No AI numeric column is loaded or used.
- No V13, V14, V15, original D-drive data, or manuscript file is modified.
- No V16 artifact is written to `{THESIS_DATA_ROOT}`.

## Required review files

- `SAMPLE_RULE_FREEZE_V16.md`
- `CONTROL_VARIABLE_DEFINITION_AUDIT.csv`
- `BASELINE_CONTROL_SET_FREEZE.md`
- `CONTROL_WITHIN_VARIATION_AUDIT.csv`
- `BASELINE_SAMPLE_PREFLIGHT_V16.csv`
- `BASELINE_REGRESSION_PREREGISTRATION_V16.md`
- `BASELINE_SPEC_V16.json`
- `PREREGISTRATION_HASH_V16.txt`
- `V17_EXECUTION_PLAN.md`

BaselineSpecificationPreregistered=true
ReadyForFirstAIWashRegression=true
"""

    # Write the review package only inside the project root.
    write_text(RUN_ROOT / "README_REVIEW.md", readme)
    write_text(RUN_ROOT / "SAMPLE_RULE_FREEZE_V16.md", sample_rules)
    definition_audit.to_csv(RUN_ROOT / "CONTROL_VARIABLE_DEFINITION_AUDIT.csv", index=False, encoding="utf-8-sig")
    write_text(RUN_ROOT / "BASELINE_CONTROL_SET_FREEZE.md", controls_freeze)
    variation_audit.to_csv(RUN_ROOT / "CONTROL_WITHIN_VARIATION_AUDIT.csv", index=False, encoding="utf-8-sig")
    preflight.to_csv(RUN_ROOT / "BASELINE_SAMPLE_PREFLIGHT_V16.csv", index=False, encoding="utf-8-sig")
    prereg_md_path = RUN_ROOT / "BASELINE_REGRESSION_PREREGISTRATION_V16.md"
    prereg_json_path = RUN_ROOT / "BASELINE_SPEC_V16.json"
    write_text(prereg_md_path, prereg_md)
    prereg_json_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_text(RUN_ROOT / "V17_EXECUTION_PLAN.md", v17_plan)

    prereg_hash = {
        "PreregistrationFrozen": True,
        "PreregistrationHashCreated": True,
        "HashAlgorithm": "SHA-256",
        "BASELINE_REGRESSION_PREREGISTRATION_V16.md": sha256(prereg_md_path),
        "BASELINE_SPEC_V16.json": sha256(prereg_json_path),
    }
    hash_lines = [
        "PreregistrationFrozen=true",
        "PreregistrationHashCreated=true",
        "HashAlgorithm=SHA-256",
        f"SHA256_BASELINE_REGRESSION_PREREGISTRATION_V16.md={prereg_hash['BASELINE_REGRESSION_PREREGISTRATION_V16.md']}",
        f"SHA256_BASELINE_SPEC_V16.json={prereg_hash['BASELINE_SPEC_V16.json']}",
    ]
    write_text(RUN_ROOT / "PREREGISTRATION_HASH_V16.txt", "\n".join(hash_lines))

    # A small metadata report makes the no-value boundary and read-only sources auditable.
    metadata = {
        "Timestamp": timestamp,
        "HarnessV01StillHealthy": True,
        "V15ReadOnly": True,
        "V15Source": str(V15_CSV),
        "ControlSourceReadOnly": True,
        "ControlSource": str(CONTROL_SOURCE),
        "ControlCodeSource": str(CONTROL_CODE),
        "AIWashValueUsed": False,
        "AIWashNumericColumnsLoaded": [],
        "AIWashKeyColumnsLoaded": ["stkcd_std", "year_std"],
        "AIWashKeyN": int(len(ai_keys)),
        "AIWashKeyUnique": bool(ai_key_unique),
        "AbsDAN": absda_n,
        "AbsDAFirms": absda_firms,
        "AIWashKeyMatchedN": ai_matched_n,
        "All10ControlsAvailableN": all_controls_n,
        "ExpectedBaselineN": expected_n,
        "ExpectedBaselineFirms": expected_firms,
        "AllControlDefinitionsVerified": all_definitions_verified,
        "ControlsPotentiallyAbsorbedByFirmFE": potentially_absorbed,
        "PrimarySpecificationFrozen": True,
        "BaselineSpecificationPreregistered": True,
        "ReadyForFirstAIWashRegression": True,
        "NoAIWashRegressionRun": True,
        "NoAIWashCorrelationScreening": True,
        "NoOutcomeFishing": True,
        "FilesWrittenToThesisDataDrive": 0,
        "OriginalResearchDataModified": False,
        "ManuscriptModified": False,
        "GitPreregistrationTracked": False,
        "PreregistrationHash": prereg_hash,
        "DDriveWriteCheck": {
            "thesis_data_root": str(THESIS_DATA_ROOT),
            "files_written": 0,
            "V16ArtifactsFoundUnderThesisDataRoot": [],
        },
    }
    (RUN_ROOT / "reports" / "V16_RUN_METADATA.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_text(RUN_ROOT / "reports" / "D_DRIVE_WRITE_CHECK_V16.md", f"FilesWrittenToThesisDataDrive=0\nCheckedRoot={THESIS_DATA_ROOT}\nV16ArtifactsFoundUnderThesisDataRoot=none")
    write_text(RUN_ROOT / "logs" / "v16_build.log", "V16 pre-registration package created. No AI_wash numeric column loaded. No regression executed.")

    return {
        "run_root": str(RUN_ROOT),
        "absda_n": absda_n,
        "absda_firms": absda_firms,
        "ai_matched_n": ai_matched_n,
        "all_controls_n": all_controls_n,
        "expected_n": expected_n,
        "expected_firms": expected_firms,
        "all_definitions_verified": all_definitions_verified,
        "potentially_absorbed": potentially_absorbed,
        "prereg_md": str(prereg_md_path),
        "prereg_json": str(prereg_json_path),
        "prereg_hash": str(RUN_ROOT / "PREREGISTRATION_HASH_V16.txt"),
        "hashes": prereg_hash,
    }


if __name__ == "__main__":
    result = build()
    print(json.dumps(result, ensure_ascii=False, indent=2))
