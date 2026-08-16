from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from hunnu_harness.paths import RUNS_ROOT


V13 = RUNS_ROOT / "CNRDS_2013_2024_ModifiedJones_V13"
V14 = RUNS_ROOT / "ModifiedJones_Industry_V14"
AUDIT = V14 / "audit"
PROC = V14 / "processed"
MANIFESTS = V14 / "manifests"
REPORTS = V14 / "reports"
LOGS = V14 / "logs"
OFFICIAL = V14 / "official_sources"
SCREENSHOTS = V14 / "screenshots"

for folder in (AUDIT, PROC, MANIFESTS, REPORTS, LOGS, OFFICIAL, SCREENSHOTS):
    folder.mkdir(parents=True, exist_ok=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def norm_code(value) -> str:
    if pd.isna(value):
        return ""
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "<na>"}:
        return ""
    m = re.fullmatch(r"(\d+)(?:\.0+)?", s)
    if m:
        return m.group(1).zfill(6)
    if s.isdigit():
        return s.zfill(6)
    return s


def norm_year(value):
    if pd.isna(value):
        return np.nan
    try:
        return int(float(str(value).strip()))
    except Exception:
        return np.nan


def clean_text(value) -> str:
    if pd.isna(value):
        return ""
    s = str(value).strip()
    return "" if s.lower() in {"nan", "none", "<na>"} else s


def valid_csrc_code(value) -> str:
    s = clean_text(value).upper().replace(" ", "")
    return s if re.fullmatch(r"[A-Z]\d{2}", s) else ""


def first_nonempty(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    out = pd.Series("", index=df.index, dtype="object")
    for col in columns:
        if col not in df.columns:
            continue
        candidate = df[col].map(clean_text)
        out = out.mask(out.eq(""), candidate)
    return out


def source_frame(spec: dict) -> tuple[pd.DataFrame, dict]:
    path: Path = spec["path"]
    raw = pd.read_stata(path, columns=spec["columns"], convert_categoricals=False)
    out = pd.DataFrame(index=raw.index)
    out["Stkcd"] = first_nonempty(raw, spec["code_columns"]).map(norm_code)
    out["Year"] = first_nonempty(raw, spec["year_columns"]).map(norm_year)
    out["IndustryCodeRaw"] = first_nonempty(
        raw, spec["code_field_columns"]
    ).map(valid_csrc_code)
    out["IndustryNameRaw"] = first_nonempty(raw, spec["name_field_columns"])
    out = out[
        (out["Stkcd"] != "")
        & out["Year"].notna()
        & out["IndustryCodeRaw"].ne("")
    ].copy()
    out["Year"] = out["Year"].astype(int)
    out["SourceName"] = spec["name"]
    out["IndustrySourceFile"] = str(path)
    out["IndustrySourcePriority"] = int(spec["priority"])
    out["IndustryOfficial"] = 1
    out["Key"] = out["Stkcd"] + "|" + out["Year"].astype(str)

    duplicate_key_count = int((out.groupby(["Stkcd", "Year"]).size() > 1).sum())
    conflict_key_count = int(
        (out.groupby(["Stkcd", "Year"])["IndustryCodeRaw"].nunique() > 1).sum()
    )
    code_counts = (
        out.groupby(["Stkcd", "Year", "IndustryCodeRaw"], as_index=False)
        .size()
        .rename(columns={"size": "CodeCount"})
        .sort_values(
            ["Stkcd", "Year", "CodeCount", "IndustryCodeRaw"],
            ascending=[True, True, False, True],
        )
    )
    selected = code_counts.drop_duplicates(["Stkcd", "Year"], keep="first")
    named = out.merge(
        selected[["Stkcd", "Year", "IndustryCodeRaw"]],
        on=["Stkcd", "Year", "IndustryCodeRaw"],
        how="inner",
    )
    name_counts = (
        named.groupby(
            ["Stkcd", "Year", "IndustryCodeRaw", "IndustryNameRaw"], as_index=False
        )
        .size()
        .rename(columns={"size": "NameCount"})
        .sort_values(
            ["Stkcd", "Year", "IndustryCodeRaw", "NameCount", "IndustryNameRaw"],
            ascending=[True, True, True, False, True],
        )
    )
    chosen_name = name_counts.drop_duplicates(
        ["Stkcd", "Year", "IndustryCodeRaw"], keep="first"
    )
    collapsed = selected.merge(
        chosen_name[["Stkcd", "Year", "IndustryCodeRaw", "IndustryNameRaw"]],
        on=["Stkcd", "Year", "IndustryCodeRaw"],
        how="left",
    )
    collapsed["IndustryNameRaw"] = collapsed["IndustryNameRaw"].fillna("")
    collapsed["SourceName"] = spec["name"]
    collapsed["IndustrySourceFile"] = str(path)
    collapsed["IndustrySourcePriority"] = int(spec["priority"])
    collapsed["IndustryOfficial"] = 1
    collapsed["Key"] = collapsed["Stkcd"] + "|" + collapsed["Year"].astype(str)
    stats = {
        "SourceName": spec["name"],
        "SourceFile": str(path),
        "SourcePriority": int(spec["priority"]),
        "LoadedRowsWithValidCSRCCode": int(len(out)),
        "CollapsedKeyRows": int(len(collapsed)),
        "DuplicateKeyCountBeforeCollapse": duplicate_key_count,
        "ConflictingCodeKeyCountBeforeCollapse": conflict_key_count,
        "KeyUniqueAfterCollapse": bool(not collapsed.duplicated("Key").any()),
        "YearMin": int(collapsed["Year"].min()) if len(collapsed) else None,
        "YearMax": int(collapsed["Year"].max()) if len(collapsed) else None,
        "SHA256": sha256(path),
        "ValidCodeRule": "^[A-Za-z][0-9]{2}$; exact three-character CSRC-style code",
    }
    return collapsed, stats


def market_is_ashare(code: str) -> int:
    return int(not (code.startswith("200") or code.startswith("201") or code.startswith("900")))


def write_dta(path: Path, frame: pd.DataFrame) -> None:
    out = frame.copy()
    if "BankARMissingTreatedAsSourceFailure" in out.columns:
        out = out.rename(
            columns={
                "BankARMissingTreatedAsSourceFailure": "BankARSourceFailureFlag"
            }
        )
    for col in out.columns:
        if pd.api.types.is_object_dtype(out[col]) or pd.api.types.is_string_dtype(out[col]):
            out[col] = out[col].fillna("").astype(str)
        elif pd.api.types.is_bool_dtype(out[col]):
            out[col] = out[col].astype("int8")
    out.to_stata(path, write_index=False, version=118)


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def main() -> None:
    v13_processed = V13 / "processed"
    six_path = v13_processed / "six_field_merged_annual_2013_2024.csv"
    v13_input_path = v13_processed / "modified_jones_input_v13.csv"
    v13_dta_path = v13_processed / "modified_jones_input_v13.dta"

    v13_hash_before = {
        str(p): sha256(p)
        for p in (six_path, v13_input_path, v13_dta_path)
        if p.exists()
    }

    six = pd.read_csv(six_path, low_memory=False)
    six["Stkcd"] = six["Scode"].map(norm_code)
    six["Year"] = six["year"].map(norm_year).astype(int)
    six["TA"] = pd.to_numeric(six["at"], errors="coerce")
    six["AR"] = pd.to_numeric(six["ar"], errors="coerce")
    six["PPE"] = pd.to_numeric(six["ppe"], errors="coerce")
    six["Revenue"] = pd.to_numeric(six["oprev"], errors="coerce")
    six["NI"] = pd.to_numeric(six["ni"], errors="coerce")
    six["CFO"] = pd.to_numeric(six["ncpoa"], errors="coerce")
    six["Key"] = six["Stkcd"] + "|" + six["Year"].astype(str)
    if six["Key"].duplicated().any():
        raise RuntimeError("V13 six-field annual layer is not unique at Stkcd-Year")

    prior_assets = six[["Stkcd", "Year", "TA"]].copy()
    prior_assets["Year"] = prior_assets["Year"] + 1
    prior_assets = prior_assets.rename(columns={"TA": "LaggedAssets"})
    six = six.merge(
        prior_assets, on=["Stkcd", "Year"], how="left", validate="one_to_one"
    )

    source_specs = [
        {
            "name": "ExistingFormalCurrentIndustry",
            "path": Path(
                r"D:\BaiduNetdiskDownload\论文数据\上市公司常用控制变量Stata整理代码2000-2024年"
                r"\上市公司常用控制变量Stata整理代码2000-2024年\行业与所属省份城市.dta"
            ),
            "columns": ["stkcd", "year", "行业代码", "行业名称"],
            "code_columns": ["stkcd"],
            "year_columns": ["year"],
            "code_field_columns": ["行业代码"],
            "name_field_columns": ["行业名称"],
            "priority": 2,
        },
        {
            "name": "ExistingFormalSupplyChainIndustry",
            "path": Path(
                r"D:\BaiduNetdiskDownload\论文数据\上市公司-供应链效率测算（2000-2022年）"
                r"\上市公司-供应链效率.dta"
            ),
            "columns": ["年份", "股票代码", "行业代码", "行业名称"],
            "code_columns": ["股票代码"],
            "year_columns": ["年份"],
            "code_field_columns": ["行业代码"],
            "name_field_columns": ["行业名称"],
            "priority": 2,
        },
        {
            "name": "ExistingFormalGreenInnovationIndustry",
            "path": Path(
                r"D:\BaiduNetdiskDownload\论文数据\企业持续绿色创新水平（1999-2022年）"
                r"\企业持续绿色创新水平（1999-2022年）.dta"
            ),
            "columns": ["年份", "股票代码", "行业代码", "行业名称"],
            "code_columns": ["股票代码"],
            "year_columns": ["年份"],
            "code_field_columns": ["行业代码"],
            "name_field_columns": ["行业名称"],
            "priority": 2,
        },
        {
            "name": "ExistingFormalRelationalCreditIndustry",
            "path": Path(
                r"D:\BaiduNetdiskDownload\论文数据\上市公司-耐心资本数据（2002-2024年）"
                r"\关系型债权-结果.dta"
            ),
            "columns": ["年份", "股票代码", "行业代码", "行业名称"],
            "code_columns": ["股票代码"],
            "year_columns": ["年份"],
            "code_field_columns": ["行业代码"],
            "name_field_columns": ["行业名称"],
            "priority": 2,
        },
        {
            "name": "ExistingFormalDisclosureQualityIndustry",
            "path": Path(r"D:\BaiduNetdiskDownload\论文数据\会计信息披露质量\Database5_Quality.dta"),
            "columns": ["stkcd", "year", "IndustryCode", "IndustryName"],
            "code_columns": ["stkcd"],
            "year_columns": ["year"],
            "code_field_columns": ["IndustryCode"],
            "name_field_columns": ["IndustryName"],
            "priority": 2,
        },
        {
            "name": "ExistingFormalFinancializationIndustry",
            "path": Path(
                r"D:\BaiduNetdiskDownload\论文数据\上市公司企业金融化程度数据+dofile（2008-2024年）_out"
                r"\原始数据与代码\STK_LISTEDCOINFOANL.dta"
            ),
            "columns": ["Symbol", "stkcd", "year", "IndustryCode", "IndustryName"],
            "code_columns": ["Symbol", "stkcd"],
            "year_columns": ["year"],
            "code_field_columns": ["IndustryCode"],
            "name_field_columns": ["IndustryName"],
            "priority": 2,
        },
        {
            "name": "OtherVerifiableMacrodataIndustry",
            "path": Path(
                r"D:\BaiduNetdiskDownload\论文数据\制造业企业-全要素生产率1999-2023年"
                r"\macrodatas_basic.dta"
            ),
            "columns": ["证券代码", "股票代码", "年份", "行业代码", "行业名称"],
            "code_columns": ["证券代码", "股票代码"],
            "year_columns": ["年份"],
            "code_field_columns": ["行业代码"],
            "name_field_columns": ["行业名称"],
            "priority": 3,
        },
    ]
    for spec in source_specs:
        if not spec["path"].exists():
            raise FileNotFoundError(spec["path"])

    source_frames: dict[str, pd.DataFrame] = {}
    source_stats: list[dict] = []
    for spec in source_specs:
        frame, stats = source_frame(spec)
        source_frames[spec["name"]] = frame
        source_stats.append(stats)

    base_name = source_specs[0]["name"]
    base = source_frames[base_name]
    base_keys = set(base["Key"])
    dataset_keys = set(six["Key"])
    missing_before = six[~six["Key"].isin(base_keys)].copy()
    if len(missing_before) != 818:
        raise RuntimeError(
            f"Expected 818 missing current-industry firm-years, observed {len(missing_before)}"
        )

    base_firms = set(base["Stkcd"])
    base_years = base.groupby("Stkcd")["Year"].agg(["min", "max"]).to_dict("index")

    def reason_for_missing(row) -> str:
        code = row["Stkcd"]
        if code.startswith(("200", "201", "900")):
            return "B-share code segment not covered by current A-share industry panel"
        if code not in base_firms:
            return "firm absent from current industry source"
        bounds = base_years.get(code)
        if bounds and (row["Year"] < bounds["min"] or row["Year"] > bounds["max"]):
            return "firm-year outside current source year coverage"
        if row["Year"] >= 2024:
            return "late/new listing or current source coverage gap"
        return "exact firm-year missing; other-year coverage exists"

    diagnosis = pd.DataFrame(
        {
            "Stkcd": missing_before["Stkcd"],
            "Year": missing_before["Year"],
            "CurrentIndustryCode": "",
            "CurrentIndustryName": "",
            "FinancialIndustryFlag": pd.Series(
                pd.NA, index=missing_before.index, dtype="Int64"
            ),
            "TA": missing_before["TA"],
            "Revenue": missing_before["Revenue"],
            "AR_missing": missing_before["AR"].isna().astype(int),
            "SourceOfCurrentIndustry": str(source_specs[0]["path"]),
            "ReasonSuspected": missing_before.apply(reason_for_missing, axis=1),
        },
        index=missing_before.index,
    ).reset_index(drop=True)
    diagnosis.to_csv(AUDIT / "MISSING_INDUSTRY_DIAGNOSIS.csv", index=False, encoding="utf-8-sig")

    missing_by_year = {
        str(int(k)): int(v)
        for k, v in missing_before.groupby("Year").size().sort_index().items()
    }
    missing_code_segments = {
        "200xxx": int(missing_before["Stkcd"].str.startswith("200").sum()),
        "201xxx": int(missing_before["Stkcd"].str.startswith("201").sum()),
        "900xxx": int(missing_before["Stkcd"].str.startswith("900").sum()),
        "other": int(
            (~missing_before["Stkcd"].str.startswith(("200", "201", "900"))).sum()
        ),
    }
    bshare_mask = ~missing_before["Stkcd"].map(market_is_ashare).astype(bool)
    diagnosis_summary = {
        "MissingIndustryFirmYears": int(len(missing_before)),
        "MissingIndustryFirms": int(missing_before["Stkcd"].nunique()),
        "MissingByYear": missing_by_year,
        "MissingCodeSegments": missing_code_segments,
        "BShareMissingFirmYears": int(bshare_mask.sum()),
        "NonBShareMissingFirmYears": int((~bshare_mask).sum()),
        "BShareMissingFirms": int(missing_before.loc[bshare_mask, "Stkcd"].nunique()),
        "NonBShareMissingFirms": int(missing_before.loc[~bshare_mask, "Stkcd"].nunique()),
        "NoForwardFillOrBackwardFill": True,
        "NoCompanyNameGuessing": True,
    }
    write_json(AUDIT / "MISSING_INDUSTRY_DIAGNOSIS_SUMMARY.json", diagnosis_summary)

    selected = base[base["Key"].isin(dataset_keys)].copy()
    selected_frames = [selected]
    selected_keys = set(selected["Key"])
    candidate_fill_counts = {}
    missing_key_set = set(missing_before["Key"])
    for spec in source_specs[1:]:
        frame = source_frames[spec["name"]]
        target = frame[
            frame["Key"].isin(missing_key_set) & ~frame["Key"].isin(selected_keys)
        ].copy()
        candidate_fill_counts[spec["name"]] = int(target["Key"].nunique())
        if len(target):
            selected_frames.append(target)
            selected_keys.update(target["Key"])
    selected = pd.concat(selected_frames, ignore_index=True).drop_duplicates("Key", keep="first")
    selected_source_by_key = dict(zip(selected["Key"], selected["SourceName"]))
    selected_code_by_key = dict(zip(selected["Key"], selected["IndustryCodeRaw"]))

    candidate_union = pd.concat(
        [
            frame[frame["Key"].isin(missing_key_set)]
            for frame in source_frames.values()
        ],
        ignore_index=True,
    )
    conflict_keys = set(
        candidate_union.groupby("Key")["IndustryCodeRaw"]
        .nunique()
        .loc[lambda s: s > 1]
        .index
    )
    conflicts = candidate_union[candidate_union["Key"].isin(conflict_keys)].copy()
    if len(conflicts):
        conflicts["SelectedSource"] = conflicts["Key"].map(selected_source_by_key)
        conflicts["SelectedCode"] = conflicts["Key"].map(selected_code_by_key)
        conflicts["Resolution"] = np.where(
            conflicts["SourceName"].eq(conflicts["SelectedSource"]),
            "selected_by_source_precedence",
            "not_selected_lower_precedence_or_conflict",
        )
        conflicts[
            [
                "Stkcd",
                "Year",
                "SourceName",
                "IndustrySourceFile",
                "IndustryCodeRaw",
                "IndustryNameRaw",
                "SelectedSource",
                "SelectedCode",
                "Resolution",
            ]
        ].sort_values(["Stkcd", "Year", "SourceName"]).to_csv(
            AUDIT / "INDUSTRY_SOURCE_CONFLICTS.csv",
            index=False,
            encoding="utf-8-sig",
        )
    else:
        pd.DataFrame().to_csv(AUDIT / "INDUSTRY_SOURCE_CONFLICTS.csv", index=False)

    selected_by_key = selected.set_index("Key")
    panel = six.copy()
    for col in [
        "IndustryCodeRaw",
        "IndustryNameRaw",
        "SourceName",
        "IndustrySourceFile",
        "IndustrySourcePriority",
        "IndustryOfficial",
    ]:
        panel[col] = panel["Key"].map(selected_by_key[col])
    panel = panel.rename(columns={"SourceName": "IndustrySource"})
    for col in ["IndustryCodeRaw", "IndustryNameRaw", "IndustrySource", "IndustrySourceFile"]:
        panel[col] = panel[col].fillna("")
    panel["IndustryOfficial"] = panel["IndustryOfficial"].fillna(0).astype(int)
    panel["IndustrySourcePriority"] = pd.to_numeric(
        panel["IndustrySourcePriority"], errors="coerce"
    )
    panel["AShareCode"] = panel["Stkcd"].map(market_is_ashare).astype(int)
    panel["ManufacturingFlag"] = np.where(
        panel["IndustryCodeRaw"].str.startswith("C"),
        1,
        np.where(panel["IndustryCodeRaw"].ne(""), 0, np.nan),
    )
    panel["IndustryGroup"] = np.where(
        panel["IndustryCodeRaw"].str.startswith("C"),
        panel["IndustryCodeRaw"],
        np.where(panel["IndustryCodeRaw"].ne(""), panel["IndustryCodeRaw"].str[0], ""),
    )
    panel["FinancialIndustryFlag"] = np.where(
        panel["IndustryCodeRaw"].str.startswith("J"),
        1,
        np.where(panel["IndustryCodeRaw"].ne(""), 0, np.nan),
    )
    panel["BankARMissingTreatedAsSourceFailure"] = False

    missing_panel = panel[panel["Key"].isin(missing_key_set)].copy()
    filled_missing = missing_panel[missing_panel["IndustryCodeRaw"].ne("")]
    still_missing = missing_panel[missing_panel["IndustryCodeRaw"].eq("")]
    financial_missing = filled_missing["FinancialIndustryFlag"].eq(1)
    nonfinancial_missing = filled_missing["FinancialIndustryFlag"].eq(0)
    before_after_rows = [
        ("IndustryMissingBefore", int(len(missing_before))),
        ("IndustryMissingFirmsBefore", int(missing_before["Stkcd"].nunique())),
        ("IndustryFilled", int(len(filled_missing))),
        ("IndustryStillMissing", int(len(still_missing))),
        ("IndustryStillMissingFirms", int(still_missing["Stkcd"].nunique())),
        ("FinancialFirmYearsAmongMissing", int(financial_missing.sum())),
        ("NonFinancialFirmYearsAmongMissing", int(nonfinancial_missing.sum())),
        ("UnclassifiedMissingFirmYears", int(len(still_missing))),
        ("UnclassifiedMissingFirms", int(still_missing["Stkcd"].nunique())),
        ("AshareIndustryStillMissing", int(still_missing["AShareCode"].eq(1).sum())),
        ("NonAShareIndustryStillMissing", int(still_missing["AShareCode"].eq(0).sum())),
    ]
    pd.DataFrame(before_after_rows, columns=["Metric", "Value"]).to_csv(
        AUDIT / "INDUSTRY_MISSING_BEFORE_AFTER.csv", index=False, encoding="utf-8-sig"
    )

    base_key_set = set(base_keys)
    source_map = panel[
        [
            "Stkcd",
            "Year",
            "IndustryCodeRaw",
            "IndustryNameRaw",
            "IndustrySource",
            "IndustrySourceFile",
            "IndustrySourcePriority",
            "IndustryOfficial",
        ]
    ].copy()
    filled_key_set = set(filled_missing["Key"])
    source_map["MappingStatus"] = panel["Key"].map(
        lambda k: "Current"
        if k in base_key_set
        else ("ExactSourceFill" if k in filled_key_set else "Unknown")
    )
    source_map.to_csv(AUDIT / "INDUSTRY_SOURCE_MAP.csv", index=False, encoding="utf-8-sig")

    group_map = (
        panel[panel["IndustryCodeRaw"].ne("")]
        [["IndustryCodeRaw", "IndustryNameRaw", "ManufacturingFlag", "IndustryGroup"]]
        .drop_duplicates()
        .rename(columns={"IndustryCodeRaw": "RawCode", "IndustryNameRaw": "RawName"})
        .sort_values(["RawCode", "RawName"])
    )
    group_map["GroupingRule"] = np.where(
        group_map["ManufacturingFlag"].eq(1),
        "Manufacturing: retain detailed CSRC 2012 three-character code",
        "Non-manufacturing: use broad CSRC 2012 first-letter category",
    )
    group_map.to_csv(AUDIT / "INDUSTRY_GROUP_MAPPING.csv", index=False, encoding="utf-8-sig")

    financial_audit = panel[panel["FinancialIndustryFlag"].eq(1)][
        [
            "Stkcd",
            "Year",
            "IndustryCodeRaw",
            "IndustryNameRaw",
            "IndustryGroup",
            "IndustrySource",
            "IndustrySourceFile",
            "FinancialIndustryFlag",
            "AR",
            "BankARMissingTreatedAsSourceFailure",
        ]
    ].sort_values(["Year", "Stkcd"])
    financial_audit.to_csv(
        AUDIT / "FINANCIAL_INDUSTRY_AUDIT.csv", index=False, encoding="utf-8-sig"
    )

    research = panel[panel["Year"].between(2014, 2024)].copy()
    core_cols = ["TA", "LaggedAssets", "AR", "PPE", "Revenue", "NI", "CFO"]
    research["CoreInputComplete"] = research[core_cols].notna().all(axis=1) & research[
        "LaggedAssets"
    ].gt(0)
    research["FinancialInputRow"] = research["AShareCode"].eq(1) & research[
        "CoreInputComplete"
    ]
    research["FinancialIndustryFlag"] = pd.to_numeric(
        research["FinancialIndustryFlag"], errors="coerce"
    )
    financial_input_n = int(research["FinancialInputRow"].sum())
    # The financial gate removes confirmed J* rows only. Unknown industry
    # rows remain in this intermediate count and are removed by the next
    # valid-industry gate without being guessed into either class.
    after_financial_mask = research["FinancialInputRow"] & ~research[
        "FinancialIndustryFlag"
    ].eq(1)
    after_financial_n = int(after_financial_mask.sum())
    valid_industry_mask = after_financial_mask & research["IndustryGroup"].ne("")
    after_valid_industry_n = int(valid_industry_mask.sum())

    iy = (
        research.loc[valid_industry_mask]
        .groupby(["IndustryGroup", "Year"], as_index=False)
        .size()
        .rename(columns={"size": "IndustryYearN"})
        .sort_values(["Year", "IndustryGroup"])
    )
    iy["MinIndustryYearN"] = 10
    iy["MJIndustryYearEligible"] = iy["IndustryYearN"].ge(10).astype(int)
    iy.to_csv(
        AUDIT / "INDUSTRY_YEAR_COUNTS_2014_2024.csv",
        index=False,
        encoding="utf-8-sig",
    )
    iy[
        ["IndustryGroup", "Year", "IndustryYearN", "MinIndustryYearN", "MJIndustryYearEligible"]
    ].to_csv(
        AUDIT / "MJ_INDUSTRY_YEAR_ELIGIBILITY.csv",
        index=False,
        encoding="utf-8-sig",
    )
    iy_lookup = iy.set_index(["IndustryGroup", "Year"])["IndustryYearN"].to_dict()
    research["IndustryYearN"] = [
        iy_lookup.get((g, y), np.nan) if valid_industry_mask.iloc[i] else np.nan
        for i, (g, y) in enumerate(zip(research["IndustryGroup"], research["Year"]))
    ]
    research["MJIndustryYearEligible"] = np.where(
        research["IndustryYearN"].notna(),
        research["IndustryYearN"].ge(10).astype(int),
        0,
    )

    research["ModifiedJonesSampleEligible"] = 0
    research["Reason"] = "IndustryUnknown"
    research.loc[research["AShareCode"].eq(0), "Reason"] = "NonAShareCode"
    research.loc[
        research["AShareCode"].eq(1) & ~research["CoreInputComplete"], "Reason"
    ] = "MissingCoreInput"
    research.loc[
        research["AShareCode"].eq(1)
        & research["CoreInputComplete"]
        & research["FinancialIndustryFlag"].eq(1),
        "Reason",
    ] = "FinancialIndustry"
    research.loc[
        research["AShareCode"].eq(1)
        & research["CoreInputComplete"]
        & research["FinancialIndustryFlag"].eq(0)
        & research["IndustryGroup"].eq(""),
        "Reason",
    ] = "IndustryUnknown"
    research.loc[
        research["AShareCode"].eq(1)
        & research["CoreInputComplete"]
        & research["FinancialIndustryFlag"].eq(0)
        & research["IndustryGroup"].ne("")
        & research["IndustryYearN"].lt(10),
        "Reason",
    ] = "IndustryYearN<10"
    eligible_mask = (
        research["AShareCode"].eq(1)
        & research["CoreInputComplete"]
        & research["FinancialIndustryFlag"].eq(0)
        & research["IndustryGroup"].ne("")
        & research["IndustryYearN"].ge(10)
    )
    research.loc[eligible_mask, "ModifiedJonesSampleEligible"] = 1
    research.loc[eligible_mask, "Reason"] = "EligibleForModifiedJonesInput"

    ai_path = Path(
        r"C:\Users\71966\Desktop\数据-AI漂洗\AI漂洗企业韧性_正式数据整理\01_AI漂洗正式解释变量.dta"
    )
    ai = pd.read_stata(
        ai_path, columns=["stkcd_std", "year_std"], convert_categoricals=False
    )
    ai["Stkcd"] = ai["stkcd_std"].map(norm_code)
    ai["Year"] = ai["year_std"].map(norm_year)
    ai = ai[ai["Stkcd"].ne("") & ai["Year"].notna()].copy()
    ai["Year"] = ai["Year"].astype(int)
    ai = ai[ai["Year"].between(2014, 2024)][["Stkcd", "Year"]].drop_duplicates()
    ai_keys = set(ai["Stkcd"] + "|" + ai["Year"].astype(str))
    research["AIWashKeyMatched"] = research["Key"].isin(ai_keys).astype(int)
    research[["Stkcd", "Year", "ModifiedJonesSampleEligible", "AIWashKeyMatched"]].sort_values(
        ["Year", "Stkcd"]
    ).to_csv(AUDIT / "AIWASH_KEY_MATCH_V14.csv", index=False, encoding="utf-8-sig")

    after_industry_year_n10 = int(research["ModifiedJonesSampleEligible"].sum())
    aiwash_matched_n = int(
        research.loc[research["ModifiedJonesSampleEligible"].eq(1), "AIWashKeyMatched"].sum()
    )
    aiwash_match_rate = (
        float(aiwash_matched_n / after_industry_year_n10)
        if after_industry_year_n10
        else None
    )
    industry_year_groups_total = int(len(iy))
    industry_year_groups_below10 = int(iy["IndustryYearN"].lt(10).sum())
    firm_years_below10 = int(iy.loc[iy["IndustryYearN"].lt(10), "IndustryYearN"].sum())
    financial_excluded_n = int(
        research.loc[
            research["FinancialInputRow"] & research["FinancialIndustryFlag"].eq(1)
        ].shape[0]
    )
    financial_unknown_input_n = int(
        research.loc[
            research["FinancialInputRow"] & research["FinancialIndustryFlag"].isna()
        ].shape[0]
    )
    ashare_still_missing = still_missing[still_missing["AShareCode"].eq(1)]
    ashare_missing_rate = float(
        len(ashare_still_missing) / max(int(research["AShareCode"].eq(1).sum()), 1)
    )
    industry_classification_available = bool(
        len(ashare_still_missing) <= 20 and ashare_missing_rate < 0.001
    )
    formal_approved = bool(
        industry_classification_available
        and industry_year_groups_total > 0
        and after_industry_year_n10 > 0
        and financial_unknown_input_n <= len(ashare_still_missing)
    )

    output_cols = [
        "Stkcd",
        "Year",
        "TA",
        "LaggedAssets",
        "AR",
        "PPE",
        "Revenue",
        "NI",
        "CFO",
        "IndustryCodeRaw",
        "IndustryNameRaw",
        "IndustryGroup",
        "IndustrySource",
        "IndustrySourceFile",
        "IndustrySourcePriority",
        "IndustryOfficial",
        "AShareCode",
        "ManufacturingFlag",
        "FinancialIndustryFlag",
        "IndustryYearN",
        "MJIndustryYearEligible",
        "ModifiedJonesSampleEligible",
        "Reason",
        "CoreInputComplete",
        "BankARMissingTreatedAsSourceFailure",
        "AIWashKeyMatched",
    ]
    v14_input = research[output_cols].sort_values(["Stkcd", "Year"]).reset_index(drop=True)
    v14_csv = PROC / "modified_jones_input_v14.csv"
    v14_dta = PROC / "modified_jones_input_v14.dta"
    v14_input.to_csv(v14_csv, index=False, encoding="utf-8-sig")
    write_dta(v14_dta, v14_input)

    source_registry = []
    for stats, spec in zip(source_stats, source_specs):
        stats = dict(stats)
        frame = source_frames[spec["name"]]
        stats["ExactFillsAmong818Missing"] = int(frame["Key"].isin(missing_key_set).sum())
        stats["SourceUsedInSelectedMap"] = int(selected["SourceName"].eq(spec["name"]).sum())
        stats["ReadOnly"] = True
        source_registry.append(stats)
    pd.DataFrame(source_registry).to_csv(
        AUDIT / "INDUSTRY_SOURCE_REGISTRY.csv", index=False, encoding="utf-8-sig"
    )

    v13_hash_after = {
        str(p): sha256(p)
        for p in (six_path, v13_input_path, v13_dta_path)
        if p.exists()
    }
    v13_integrity = {
        "V13FilesReadOnly": True,
        "HashesBefore": v13_hash_before,
        "HashesAfter": v13_hash_after,
        "Unchanged": v13_hash_before == v13_hash_after,
    }
    write_json(REPORTS / "v13_readonly_integrity.json", v13_integrity)

    d_drive_root = Path(r"D:\BaiduNetdiskDownload\论文数据")
    d_v14_matches = [
        str(p)
        for p in d_drive_root.rglob("*")
        if p.is_file() and "ModifiedJones_Industry_V14" in str(p)
    ]
    write_json(
        REPORTS / "d_drive_write_check.json",
        {
            "WriteToThesisDataDrive": False,
            "FilesWrittenToThesisDataDrive": 0,
            "DetectedV14FilesOnThesisDataDrive": d_v14_matches,
            "ReadOnlySourcesUsed": True,
        },
    )

    status = {
        "HarnessV01StillHealthy": bool(v13_integrity["Unchanged"]),
        "IndustryMissingBefore": int(len(missing_before)),
        "IndustryMissingFirmsBefore": int(missing_before["Stkcd"].nunique()),
        "FinancialFirmYearsAmongMissing": int(financial_missing.sum()),
        "NonFinancialFirmYearsAmongMissing": int(nonfinancial_missing.sum()),
        "UnclassifiedMissingFirmYears": int(len(still_missing)),
        "UnclassifiedMissingFirms": int(still_missing["Stkcd"].nunique()),
        "IndustryFilled": int(len(filled_missing)),
        "IndustryStillMissing": int(len(still_missing)),
        "IndustryStillMissingFirms": int(still_missing["Stkcd"].nunique()),
        "AshareIndustryStillMissing": int(len(ashare_still_missing)),
        "NonAShareIndustryStillMissing": int(still_missing["AShareCode"].eq(0).sum()),
        "OfficialIndustrySourcesUsed": [
            s["SourceName"] for s in source_registry if int(s["SourceUsedInSelectedMap"]) > 0
        ],
        "HistoricalFirmYearIndustryUsed": True,
        "LatestIndustryBackfilled": False,
        "IndustryClassificationAvailable": industry_classification_available,
        "FinancialIndustryFlagVerified": True,
        "FinancialIndustryFirmYearsExcluded": financial_excluded_n,
        "UnknownIndustryRowsExcludedBeforeIndustryYear": int(
            research["FinancialInputRow"].sum()
            - after_valid_industry_n
            - financial_excluded_n
        ),
        "ManufacturingGroupingRuleApplied": True,
        "NonManufacturingGroupingRuleApplied": True,
        "IndustryYearNCalculated": True,
        "MinIndustryYearN": 10,
        "IndustryYearGroupsTotal": industry_year_groups_total,
        "IndustryYearGroupsNBelow10": industry_year_groups_below10,
        "FirmYearsInNBelow10Groups": firm_years_below10,
        "FinancialInputN": financial_input_n,
        "AfterFinancialExclusionN": after_financial_n,
        "AfterValidIndustryN": after_valid_industry_n,
        "AfterIndustryYearN10N": after_industry_year_n10,
        "AIWashKeyMatchOnly": True,
        "AIWashValueUsed": False,
        "AIWashKeyUniverseN": int(len(ai)),
        "AIWashMatchedN": aiwash_matched_n,
        "AIWashMatchRate": aiwash_match_rate,
        "FormalFinancialInputApproved": formal_approved,
        "ModifiedJonesInputV14CSV": str(v14_csv),
        "ModifiedJonesInputV14DTA": str(v14_dta),
        "FilesWrittenToThesisDataDrive": 0,
        "TotalAccrualsConstructed": False,
        "NDAConstructed": False,
        "DAConstructed": False,
        "AbsDAConstructed": False,
        "NoModifiedJonesRegressionRun": True,
        "NoAIWashRegressionRun": True,
        "NoAIWashCorrelationScreening": True,
        "NoOutcomeFishing": True,
        "OriginalResearchDataModified": False,
        "ManuscriptModified": False,
        "BankARMissingTreatedAsSourceFailure": False,
        "WriteToThesisDataDrive": False,
        "SourceUnionCandidateFillCounts": candidate_fill_counts,
        "CNRDSOfficialExportUsed": False,
        "CNRDSOfficialExportFailureReason": "Current browser session returned invalid Client ID; no CNRDS data was downloaded",
    }
    write_json(REPORTS / "v14_status_summary.json", status)

    readme = f"""# Modified Jones Industry V14 review package

This package is a direct continuation of V13. It reads the V13 six-field annual
layer and read-only existing research datasets to resolve exact Stkcd + Year
CSRC-style industry mappings. It does not re-download CNFS financial tables,
does not overwrite V13, and does not construct accruals or run any regression.

Formal estimation years are 2014–2024. 2013 is used only as the lagged-assets
base year. MinIndustryYearN=10.

## Industry provenance

- Frozen taxonomy: CSRC 2012-style codes.
- Historical firm-year keys are used; no latest-year backfill is used.
- Manufacturing codes beginning with C retain their detailed three-character
  code, for example C13 or C39.
- Non-manufacturing codes use their first-letter broad group.
- Financial industry is identified from code J* and excluded by flag.
- Unknown industry rows are retained in the input layer but are not eligible.
- B-share code segments 200xxx, 201xxx, and 900xxx are flagged
  AShareCode=0 and are not in the formal A-share eligibility count.

## Gate results

- IndustryMissingBefore={len(missing_before)}
- IndustryFilled={len(filled_missing)}
- IndustryStillMissing={len(still_missing)}; A-share unknowns:
  {len(ashare_still_missing)}
- IndustryClassificationAvailable={str(industry_classification_available).lower()}
- FormalFinancialInputApproved={str(formal_approved).lower()}
- AfterIndustryYearN10N={after_industry_year_n10}

The exact source precedence and hashes are in
audit/INDUSTRY_SOURCE_REGISTRY.csv; row-level mappings are in
audit/INDUSTRY_SOURCE_MAP.csv.

Restricted actions not performed: TotalAccruals, NDA, DA, AbsDA, any
Modified Jones regression, AI_wash regression, correlation screening, and
outcome fishing.

All V14 outputs remain inside the desktop Harness. D-drive sources were read
only; no V14 file was written to the thesis-data drive.
"""
    (V14 / "README_REVIEW.md").write_text(readme, encoding="utf-8")

    source_audit = f"""# INDUSTRY_SOURCE_AUDIT

The current industry source leaves exactly {len(missing_before)} firm-years
({missing_before["Stkcd"].nunique()} firms) unmatched in the V13 six-field
annual layer. The first diagnostic is
audit/MISSING_INDUSTRY_DIAGNOSIS.csv.

The selected mapping uses exact Stkcd + Year keys. Source precedence is the
order in audit/INDUSTRY_SOURCE_REGISTRY.csv; the existing current source has
priority over all candidate sources, and candidate sources are used only for
keys still missing from that source. No forward fill, backward fill,
current-year backfill, company-name guessing, or inferred source (priority 9)
is used.

Only exact three-character codes matching the stated CSRC-style rule are
accepted. This prevents five-character or differently coded industry values
from being silently treated as CSRC 2012 codes.

## Result

- Exact source fills among the original missing keys: {len(filled_missing)}
- Still missing: {len(still_missing)} firm-years, {still_missing["Stkcd"].nunique()} firms
- A-share still missing: {len(ashare_still_missing)}
- Non-A-share code-segment still missing: {int(still_missing["AShareCode"].eq(0).sum())}
- Financial rows among the originally missing keys that were exactly classified:
  {int(financial_missing.sum())}
- Non-financial rows among the originally missing keys that were exactly classified:
  {int(nonfinancial_missing.sum())}
- Unclassified rows are not guessed and are excluded by Reason=IndustryUnknown.

## CNRDS access

No CNRDS industry download was performed in this V14 run. The available
browser session returned invalid Client ID; no password, OTP, MFA, CAPTCHA,
or WebVPN page was bypassed. The final mappings use existing read-only
firm-year raw datasets whose fields are explicitly labeled as industry code
and industry name. Those files and SHA-256 hashes are recorded in the source
registry.

## Financial and model boundary

J* is the formal CSRC financial-industry flag. Bank AR missingness is not
treated as a source failure (BankARMissingTreatedAsSourceFailure=false);
financial rows are excluded by industry flag. No financial industry is
identified from names alone.

This package stops before Total Accruals, NDA, DA, AbsDA, Modified Jones
regression, AI_wash regression, correlation screening, or outcome fishing.
"""
    (V14 / "INDUSTRY_SOURCE_AUDIT.md").write_text(source_audit, encoding="utf-8")

    approval = f"""# FORMAL_FINANCIAL_INPUT_APPROVAL_V14

## Decision

IndustryClassificationAvailable={str(industry_classification_available).lower()}

FormalFinancialInputApproved={str(formal_approved).lower()}

## Evidence

- Missing before: {len(missing_before)} firm-years / {missing_before["Stkcd"].nunique()} firms.
- Exact, traceable source fills: {len(filled_missing)}.
- Still missing: {len(still_missing)} firm-years / {still_missing["Stkcd"].nunique()} firms.
- Still missing within code-based A-share scope: {len(ashare_still_missing)}.
- Industry source priority 9 inferred mappings: 0.
- Historical firm-year source used: true.
- Latest industry backfilled: false.
- Financial flag verified from J* CSRC-style raw codes: true.
- Financial industry rows excluded from the valid A-share core input:
  {financial_excluded_n}.
- Industry-year groups: {industry_year_groups_total}; groups with N<10:
  {industry_year_groups_below10}; firm-years in those groups: {firm_years_below10}.
- Final N>=10 eligible input rows: {after_industry_year_n10}.

The remaining A-share unknown rate is {ashare_missing_rate:.6%}; these rows are
retained for audit but assigned ModifiedJonesSampleEligible=0 and
Reason=IndustryUnknown. The unknown rows are excluded before the
industry-year eligibility gate, so no unknown row is guessed into the formal
sample. The exact unknown list remains visible in the source map and input
layer for review.

## Boundary controls

TotalAccrualsConstructed=false

NDAConstructed=false

DAConstructed=false

AbsDAConstructed=false

NoModifiedJonesRegressionRun=true

NoAIWashRegressionRun=true

NoAIWashCorrelationScreening=true

NoOutcomeFishing=true

AIWashKeyMatchOnly=true

AIWashValueUsed=false

FilesWrittenToThesisDataDrive=0
"""
    (V14 / "FORMAL_FINANCIAL_INPUT_APPROVAL_V14.md").write_text(
        approval, encoding="utf-8"
    )
    (OFFICIAL / "README.md").write_text(
        "V14 references read-only existing firm-year industry sources; no source file was copied or modified.\n",
        encoding="utf-8",
    )
    (SCREENSHOTS / "README.md").write_text(
        "No CNRDS download or data-page screenshot was created in V14; browser access returned invalid Client ID.\n",
        encoding="utf-8",
    )
    (LOGS / "v14_build.log").write_text(
        "V14 industry build completed; no downloads, D-drive writes, accrual construction, or regressions performed.\n",
        encoding="utf-8",
    )

    generated = []
    for p in sorted(V14.rglob("*")):
        if p.is_file() and p != MANIFESTS / "manifest_v14.json":
            generated.append(
                {
                    "path": str(p),
                    "relative": str(p.relative_to(V14)),
                    "sha256": sha256(p),
                    "bytes": p.stat().st_size,
                }
            )
    manifest = {
        "Run": "ModifiedJones_Industry_V14",
        "WriteToThesisDataDrive": False,
        "FilesWrittenToThesisDataDrive": 0,
        "GeneratedFiles": generated,
        "SourceRegistry": source_registry,
        "NoRawFinancialDownloads": True,
        "NoResearchAnalysis": True,
        "NoModifiedJonesRegressionRun": True,
        "NoAIWashRegressionRun": True,
    }
    write_json(MANIFESTS / "manifest_v14.json", manifest)

    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
