from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from hunnu_harness.paths import RUNS_ROOT


V14 = RUNS_ROOT / "ModifiedJones_Industry_V14"
V15 = RUNS_ROOT / "ModifiedJones_AbsDA_V15"
AUDIT = V15 / "audit"
PROC = V15 / "processed"
DIAG = V15 / "diagnostics"
REPORTS = V15 / "reports"
LOGS = V15 / "logs"
SCREENSHOTS = V15 / "screenshots"
MANIFESTS = V15 / "manifests"
for folder in (AUDIT, PROC, DIAG, REPORTS, LOGS, SCREENSHOTS, MANIFESTS):
    folder.mkdir(parents=True, exist_ok=True)


V14_INPUT = V14 / "processed" / "modified_jones_input_v14.csv"
V14_INPUT_DTA = V14 / "processed" / "modified_jones_input_v14.dta"
V13_SIX = (
    RUNS_ROOT
    / "CNRDS_2013_2024_ModifiedJones_V13"
    / "processed"
    / "six_field_merged_annual_2013_2024.csv"
)
AI_PATH = Path(
    r"C:\Users\71966\Desktop\数据-AI漂洗\AI漂洗企业韧性_正式数据整理\01_AI漂洗正式解释变量.dta"
)
V4_METHOD = Path(
    r"D:\BaiduNetdiskDownload\论文数据\AI漂洗_盈余质量测量冻结与构造测试_V4"
    r"\MODIFIED_JONES_VARIANT_COMPARISON.md"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def norm_code(value) -> str:
    if pd.isna(value):
        return ""
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "<na>"}:
        return ""
    match = re.fullmatch(r"(\d+)(?:\.0+)?", s)
    if match:
        return match.group(1).zfill(6)
    return s.zfill(6) if s.isdigit() else s


def norm_year(value):
    if pd.isna(value):
        return np.nan
    try:
        return int(float(str(value).strip()))
    except Exception:
        return np.nan


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def write_dta(path: Path, frame: pd.DataFrame) -> None:
    out = frame.copy()
    if "BankARMissingTreatedAsSourceFailure" in out.columns:
        out = out.rename(
            columns={
                "BankARMissingTreatedAsSourceFailure": "BankARSourceFailureFlag"
            }
        )
    for col in out.columns:
        if pd.api.types.is_object_dtype(out[col]) or pd.api.types.is_string_dtype(
            out[col]
        ):
            out[col] = out[col].fillna("").astype(str)
        elif pd.api.types.is_bool_dtype(out[col]):
            out[col] = out[col].astype("int8")
    out.to_stata(path, write_index=False, version=118)


def finite_series(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    result = pd.Series(True, index=frame.index)
    for col in columns:
        result &= frame[col].notna()
        result &= np.isfinite(pd.to_numeric(frame[col], errors="coerce"))
    return result


def safe_float(value):
    return float(value) if pd.notna(value) and np.isfinite(value) else np.nan


def main() -> None:
    # Protocol is written before any V14/V13 measurement data is read.
    method_text = V4_METHOD.read_text(encoding="utf-8", errors="replace")
    variant_b_ok = "Variant B" in method_text and "直接估计" in method_text
    ordinary_constant_conflict = False
    protocol_conflict = not variant_b_ok or ordinary_constant_conflict
    protocol_path = V15 / "PROTOCOL_FREEZE_V15.md"
    if protocol_conflict:
        conflict = (
            "# PROTOCOL_CONFLICT\n\n"
            "The existing V4 method record did not unambiguously support the "
            "requested Variant B/no-additional-constant protocol. V15 stopped "
            "before constructing any measurement variables.\n"
        )
        (V15 / "PROTOCOL_CONFLICT.md").write_text(conflict, encoding="utf-8")
        raise SystemExit("Protocol conflict detected; V15 stopped before construction")

    protocol = """# PROTOCOL_FREEZE_V15

ProtocolFrozenBeforeConstruction=true

## Frozen measurement

- Formal estimation years: 2014–2024.
- 2013 is used only for LaggedAssets, LaggedRevenue, and LaggedAR.
- TotalAccruals = NI - CFO.
- LaggedAssets is the V14 exact prior-year TA value and must be positive.
- DeltaRevenue and DeltaAR use an exact consecutive prior year only.
- Missing prior values are not filled and do not become zero.
- Modified Jones Variant B uses X2=(DeltaRevenue-DeltaAR)/LaggedAssets directly in
  the IndustryGroup-Year regression.
- Regression equation is Y_MJ = alpha1*X1 + alpha2*X2 + alpha3*X3 with no
  additional ordinary intercept. AdditionalConstant=false.
- X1=1/LaggedAssets, X2=(DeltaRevenue-DeltaAR)/LaggedAssets,
  X3=PPE/LaggedAssets, and Y_MJ=TotalAccruals/LaggedAssets.
- Before regression, only Y_MJ, X1, X2, and X3 are winsorized once at 1%/99%
  two tails separately by Year. Raw amounts are never winsorized.
- DA is the residual and AbsDA=abs(DA). DA and AbsDA are never winsorized again.
- IndustryGroup and financial exclusion are inherited from V14 without change.
- True MJEstimationN is recomputed after all four scaled variables are available.
- MinMJEstimationN=10. Below-threshold groups are not estimated.
- No coefficient significance, fit-quality, or outcome-based group deletion is
  allowed. No robust, clustered, fixed-effect, pooled, or inferential model is
  used.
- PrimaryOutcomeCandidate=AbsDA.

## Explicit non-goals

This run does not read AI_wash values and does not run any AI_wash-to-AbsDA
regression, correlation, significance test, subgroup analysis, mediation,
moderation, DML, or outcome fishing. No manuscript is modified.

## Existing-method compatibility

The read-only V4 method record explicitly freezes Variant B as the direct
three-regressor equation, so no PROTOCOL_CONFLICT was found. This V15 protocol
is the operative specification for the new run.
"""
    protocol_path.write_text(protocol, encoding="utf-8")

    v14_hash_before = {
        str(path): sha256(path)
        for path in (V14_INPUT, V14_INPUT_DTA)
        if path.exists()
    }
    v13_hash_before = sha256(V13_SIX)

    v14 = pd.read_csv(V14_INPUT, low_memory=False, dtype={"Stkcd": "string"})
    v14["Stkcd"] = v14["Stkcd"].map(norm_code)
    v14["Year"] = v14["Year"].map(norm_year).astype(int)
    v14["Key"] = v14["Stkcd"] + "|" + v14["Year"].astype(str)
    if v14["Key"].duplicated().any():
        raise RuntimeError("V14 firm-year key is not unique")
    for col in [
        "TA",
        "LaggedAssets",
        "AR",
        "PPE",
        "Revenue",
        "NI",
        "CFO",
        "FinancialIndustryFlag",
        "ModifiedJonesSampleEligible",
    ]:
        v14[col] = pd.to_numeric(v14[col], errors="coerce")
    for col in ["IndustryCodeRaw", "IndustryNameRaw", "IndustryGroup", "IndustrySource"]:
        v14[col] = v14[col].fillna("").astype(str)
    v14["ModifiedJonesSampleEligible"] = v14[
        "ModifiedJonesSampleEligible"
    ].fillna(0).astype(int)
    formal_mask = v14["ModifiedJonesSampleEligible"].eq(1)

    # Read the V13 annual layer only to obtain the exact 2013/previous-year
    # revenue and AR values needed for continuity-safe deltas.
    six = pd.read_csv(V13_SIX, low_memory=False)
    six["Stkcd"] = six["Scode"].map(norm_code)
    six["Year"] = six["year"].map(norm_year).astype(int)
    six["RevenueLagSource"] = pd.to_numeric(six["oprev"], errors="coerce")
    six["ARLagSource"] = pd.to_numeric(six["ar"], errors="coerce")
    six["Key"] = six["Stkcd"] + "|" + six["Year"].astype(str)
    if six["Key"].duplicated().any():
        raise RuntimeError("V13 annual lag source is not unique")
    lag_source = six[["Stkcd", "Year", "RevenueLagSource", "ARLagSource"]].copy()
    lag_source["Year"] += 1
    lag_source = lag_source.rename(
        columns={
            "RevenueLagSource": "LaggedRevenue",
            "ARLagSource": "LaggedAR",
        }
    )

    work = v14.merge(
        lag_source,
        on=["Stkcd", "Year"],
        how="left",
        validate="one_to_one",
    )
    amount_cols = ["TA", "LaggedAssets", "AR", "PPE", "Revenue", "NI", "CFO"]
    for col in amount_cols + ["LaggedRevenue", "LaggedAR"]:
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work["TotalAccruals"] = np.where(
        work["NI"].notna() & work["CFO"].notna(),
        work["NI"] - work["CFO"],
        np.nan,
    )
    work["DeltaRevenue"] = np.where(
        work["Revenue"].notna() & work["LaggedRevenue"].notna(),
        work["Revenue"] - work["LaggedRevenue"],
        np.nan,
    )
    work["DeltaAR"] = np.where(
        work["AR"].notna() & work["LaggedAR"].notna(),
        work["AR"] - work["LaggedAR"],
        np.nan,
    )

    work["Y_MJ_raw"] = np.nan
    work["X1_raw"] = np.nan
    work["X2_raw"] = np.nan
    work["X3_raw"] = np.nan
    valid_assets = work["LaggedAssets"].notna() & work["LaggedAssets"].gt(0)
    work.loc[valid_assets, "X1_raw"] = 1.0 / work.loc[valid_assets, "LaggedAssets"]
    work.loc[
        valid_assets & work["TotalAccruals"].notna(), "Y_MJ_raw"
    ] = work.loc[
        valid_assets & work["TotalAccruals"].notna(), "TotalAccruals"
    ] / work.loc[
        valid_assets & work["TotalAccruals"].notna(), "LaggedAssets"
    ]
    x2_raw_mask = valid_assets & work["DeltaRevenue"].notna() & work["DeltaAR"].notna()
    work.loc[x2_raw_mask, "X2_raw"] = (
        work.loc[x2_raw_mask, "DeltaRevenue"]
        - work.loc[x2_raw_mask, "DeltaAR"]
    ) / work.loc[x2_raw_mask, "LaggedAssets"]
    x3_raw_mask = valid_assets & work["PPE"].notna()
    work.loc[x3_raw_mask, "X3_raw"] = (
        work.loc[x3_raw_mask, "PPE"] / work.loc[x3_raw_mask, "LaggedAssets"]
    )

    scaled_raw = ["Y_MJ_raw", "X1_raw", "X2_raw", "X3_raw"]
    candidate_mask = formal_mask & finite_series(work, scaled_raw)
    for var in scaled_raw:
        work[var.replace("_raw", "")] = np.nan
    winsor_bounds: dict[tuple[int, str], tuple[float, float]] = {}
    winsor_rows = []
    for year in sorted(work.loc[candidate_mask, "Year"].unique()):
        year_mask = candidate_mask & work["Year"].eq(year)
        for var in scaled_raw:
            raw = work.loc[year_mask, var].astype(float)
            p01 = float(raw.quantile(0.01))
            p99 = float(raw.quantile(0.99))
            low = int((raw < p01).sum())
            high = int((raw > p99).sum())
            winsor_bounds[(int(year), var)] = (p01, p99)
            clipped = raw.clip(lower=p01, upper=p99)
            winsor_rows.append(
                {
                    "Year": int(year),
                    "Variable": var.replace("_raw", ""),
                    "N": int(len(raw)),
                    "P01Before": p01,
                    "P99Before": p99,
                    "MinBefore": float(raw.min()),
                    "MaxBefore": float(raw.max()),
                    "ValuesWinsorizedLow": low,
                    "ValuesWinsorizedHigh": high,
                    "MinAfter": float(clipped.min()),
                    "MaxAfter": float(clipped.max()),
                }
            )
            target_index = work.index[year_mask]
            work.loc[target_index, var.replace("_raw", "")] = clipped.to_numpy(
                dtype=float
            )
    write_csv(AUDIT / "MJ_WINSOR_AUDIT.csv", pd.DataFrame(winsor_rows))

    group_counts = (
        work.loc[candidate_mask]
        .groupby(["IndustryGroup", "Year"])
        .size()
        .rename("MJEstimationN")
    )
    work["MJEstimationN"] = [
        group_counts.get((group, year), np.nan)
        if is_formal
        else np.nan
        for group, year, is_formal in zip(
            work["IndustryGroup"], work["Year"], formal_mask
        )
    ]
    work["MJRegressionEligible"] = (
        candidate_mask
        & work["MJEstimationN"].ge(10)
    ).astype(int)
    work["MJRegressionSucceeded"] = 0
    for col in ["Alpha1", "Alpha2", "Alpha3", "NDA", "DA", "AbsDA"]:
        work[col] = np.nan

    diagnostics = []
    coefficients = []
    group_results = {}
    for (industry_group, year), group in work.loc[candidate_mask].groupby(
        ["IndustryGroup", "Year"], sort=True
    ):
        year = int(year)
        n = int(len(group))
        X = group[["X1", "X2", "X3"]].to_numpy(dtype=np.float64)
        y = group["Y_MJ"].to_numpy(dtype=np.float64)
        rank = np.nan
        condition = np.nan
        r2 = np.nan
        alpha1 = alpha2 = alpha3 = np.nan
        succeeded = 0
        failure = ""
        try:
            if n >= 3:
                singular_values = np.linalg.svd(X, compute_uv=False)
                rank = int(np.linalg.matrix_rank(X))
                condition = (
                    float(singular_values[0] / singular_values[-1])
                    if singular_values[-1] > 0
                    else np.inf
                )
            if n < 10:
                failure = "EstimationNBelow10"
            elif not np.isfinite(X).all() or not np.isfinite(y).all():
                failure = "NonFiniteDesign"
            elif rank < 3:
                failure = "RankDeficiency"
            elif np.any(np.ptp(X, axis=0) == 0):
                failure = "ZeroVarianceRegressor"
            elif np.ptp(y) == 0:
                failure = "ZeroVarianceDependent"
            else:
                beta, _, lstsq_rank, _ = np.linalg.lstsq(
                    X, y, rcond=None
                )
                if int(lstsq_rank) < 3:
                    failure = "RankDeficiency"
                elif not np.isfinite(beta).all():
                    failure = "NonFiniteCoefficient"
                else:
                    y_hat = X @ beta
                    residual = y - y_hat
                    tss = float(np.sum(y * y))
                    if not np.isfinite(y_hat).all() or not np.isfinite(residual).all():
                        failure = "NonFinitePrediction"
                    elif tss <= 0:
                        failure = "ZeroVarianceDependent"
                    else:
                        alpha1, alpha2, alpha3 = map(float, beta)
                        r2 = float(1.0 - np.sum(residual * residual) / tss)
                        succeeded = 1
                        group_results[(industry_group, year)] = {
                            "alpha1": alpha1,
                            "alpha2": alpha2,
                            "alpha3": alpha3,
                        }
        except np.linalg.LinAlgError:
            failure = "SingularMatrix"
        except Exception as exc:
            failure = f"ProgramError:{type(exc).__name__}"
        if succeeded:
            failure = ""
        row = {
            "Year": year,
            "IndustryGroup": industry_group,
            "N": n,
            "Rank": rank,
            "R2": r2,
            "ConditionNumber": condition,
            "Alpha1": alpha1,
            "Alpha2": alpha2,
            "Alpha3": alpha3,
            "RegressionSucceeded": succeeded,
            "MJRegressionEligible": int(n >= 10),
            "FailureReason": failure,
        }
        diagnostics.append(row)
        coefficients.append(row.copy())

    diagnostics_df = pd.DataFrame(diagnostics).sort_values(
        ["Year", "IndustryGroup"]
    )
    write_csv(AUDIT / "MJ_GROUP_REGRESSION_DIAGNOSTICS.csv", diagnostics_df)
    write_csv(AUDIT / "MJ_GROUP_COEFFICIENTS.csv", pd.DataFrame(coefficients))

    for (industry_group, year), result in group_results.items():
        row_mask = (
            candidate_mask
            & work["IndustryGroup"].eq(industry_group)
            & work["Year"].eq(year)
        )
        work.loc[row_mask, "Alpha1"] = result["alpha1"]
        work.loc[row_mask, "Alpha2"] = result["alpha2"]
        work.loc[row_mask, "Alpha3"] = result["alpha3"]
        work.loc[row_mask, "NDA"] = (
            result["alpha1"] * work.loc[row_mask, "X1"]
            + result["alpha2"] * work.loc[row_mask, "X2"]
            + result["alpha3"] * work.loc[row_mask, "X3"]
        )
        work.loc[row_mask, "DA"] = work.loc[row_mask, "Y_MJ"] - work.loc[
            row_mask, "NDA"
        ]
        work.loc[row_mask, "AbsDA"] = work.loc[row_mask, "DA"].abs()
        work.loc[row_mask, "MJRegressionSucceeded"] = 1

    # Reclassify exclusions after the real V15 variable and group gates.
    work["ExclusionReason"] = "OutsideV14FormalInput"
    work.loc[formal_mask, "ExclusionReason"] = "Unspecified"
    after_financial = formal_mask & work["FinancialIndustryFlag"].eq(0)
    after_lagged = after_financial & work["LaggedAssets"].gt(0)
    after_delta_revenue = after_lagged & work["DeltaRevenue"].notna()
    after_delta_ar = after_delta_revenue & work["DeltaAR"].notna()
    work.loc[formal_mask & ~after_financial, "ExclusionReason"] = (
        "FinancialOrUnknownIndustry"
    )
    work.loc[after_financial & ~after_lagged, "ExclusionReason"] = (
        "LaggedAssetsInvalid"
    )
    work.loc[after_lagged & ~after_delta_revenue, "ExclusionReason"] = (
        "LaggedRevenueUnavailable"
    )
    work.loc[after_delta_revenue & ~after_delta_ar, "ExclusionReason"] = (
        "LaggedARUnavailable"
    )
    work.loc[after_delta_ar & ~candidate_mask, "ExclusionReason"] = (
        "MissingRegressionVariable"
    )
    work.loc[
        candidate_mask & work["MJEstimationN"].lt(10), "ExclusionReason"
    ] = "EstimationNBelow10"
    work.loc[
        candidate_mask
        & work["MJEstimationN"].ge(10)
        & work["MJRegressionSucceeded"].eq(0),
        "ExclusionReason",
    ] = "RegressionFailed"
    work.loc[work["AbsDA"].notna(), "ExclusionReason"] = "EligibleAbsDA"

    write_csv(
        AUDIT / "MJ_INDUSTRY_YEAR_ESTIMATION_SAMPLE.csv",
        work.loc[candidate_mask, [
            "Stkcd",
            "Year",
            "IndustryGroup",
            "MJEstimationN",
            "MJRegressionEligible",
            "MJRegressionSucceeded",
            "ExclusionReason",
        ]].sort_values(["Year", "IndustryGroup", "Stkcd"]),
    )

    # Variable construction audit.
    variable_rows = [
        {
            "Variable": "TotalAccruals",
            "Definition": "NI - CFO",
            "Source": "V14 NI and CFO",
            "Constructed": True,
            "NFormalNonMissing": int(
                (formal_mask & work["TotalAccruals"].notna()).sum()
            ),
            "RawAmountsWinsorized": False,
            "Notes": "Cash-flow statement approach; no winsorization",
        },
        {
            "Variable": "LaggedAssets",
            "Definition": "V14 TA at t-1 with exact prior year and >0",
            "Source": "V14 preconstructed LaggedAssets",
            "Constructed": True,
            "NFormalNonMissing": int((formal_mask & after_lagged).sum()),
            "RawAmountsWinsorized": False,
            "Notes": "No filling",
        },
        {
            "Variable": "DeltaRevenue",
            "Definition": "Revenue_t - Revenue_t-1 for exact consecutive year",
            "Source": "V14 Revenue plus V13 2013-2024 lag source",
            "Constructed": True,
            "NFormalNonMissing": int((formal_mask & work["DeltaRevenue"].notna()).sum()),
            "RawAmountsWinsorized": False,
            "Notes": "No cross-year jump and no zero fill",
        },
        {
            "Variable": "DeltaAR",
            "Definition": "AR_t - AR_t-1 for exact consecutive year",
            "Source": "V14 AR plus V13 2013-2024 lag source",
            "Constructed": True,
            "NFormalNonMissing": int((formal_mask & work["DeltaAR"].notna()).sum()),
            "RawAmountsWinsorized": False,
            "Notes": "No cross-year jump and no zero fill",
        },
    ]
    for var in scaled_raw:
        final_var = var.replace("_raw", "")
        variable_rows.append(
            {
                "Variable": final_var,
                "Definition": {
                    "Y_MJ_raw": "TotalAccruals/LaggedAssets",
                    "X1_raw": "1/LaggedAssets",
                    "X2_raw": "(DeltaRevenue-DeltaAR)/LaggedAssets",
                    "X3_raw": "PPE/LaggedAssets",
                }[var],
                "Source": "V15 raw formula then Year 1%/99% two-tail winsor",
                "Constructed": True,
                "NFormalNonMissing": int((formal_mask & work[final_var].notna()).sum()),
                "RawAmountsWinsorized": False,
                "Notes": "Final scaled variable winsorized once; DA/AbsDA not winsorized",
            }
        )
    write_csv(AUDIT / "MJ_VARIABLE_CONSTRUCTION_AUDIT.csv", pd.DataFrame(variable_rows))

    # Identity and finite-value checks.
    successful = work["AbsDA"].notna()
    identity_error = (
        work.loc[successful, "Y_MJ"]
        - work.loc[successful, "NDA"]
        - work.loc[successful, "DA"]
    )
    identity_tolerance = 1e-10
    identity_row = {
        "Tolerance": identity_tolerance,
        "N": int(len(identity_error)),
        "MaxAbsoluteError": float(identity_error.abs().max())
        if len(identity_error)
        else np.nan,
        "MeanAbsoluteError": float(identity_error.abs().mean())
        if len(identity_error)
        else np.nan,
        "NumberAboveTolerance": int((identity_error.abs() > identity_tolerance).sum()),
        "IdentityCheckPassed": bool(
            len(identity_error) > 0
            and (identity_error.abs() <= identity_tolerance).all()
        ),
    }
    write_csv(AUDIT / "MJ_IDENTITY_CHECK.csv", pd.DataFrame([identity_row]))

    finite_output_cols = [
        "Y_MJ",
        "X1",
        "X2",
        "X3",
        "NDA",
        "DA",
        "AbsDA",
    ]
    finite_success = finite_series(work.loc[successful], finite_output_cols).all()
    absda_nonnegative = bool((work.loc[successful, "AbsDA"] >= 0).all())

    # Fixed-seed independent formula recalculation. Selection happens before
    # coefficients/outcomes are inspected and uses no AI key or outcome value.
    manual_seed = 20250814
    manual_pool = work.loc[
        candidate_mask & work["MJEstimationN"].ge(10),
        ["Stkcd", "Year"],
    ].drop_duplicates()
    manual_n = min(20, len(manual_pool))
    manual_selection = manual_pool.sample(n=manual_n, random_state=manual_seed)
    lag_rev_map = dict(zip(zip(six["Stkcd"], six["Year"]), six["RevenueLagSource"]))
    lag_ar_map = dict(zip(zip(six["Stkcd"], six["Year"]), six["ARLagSource"]))
    formal_by_key = work.set_index("Key")
    manual_rows = []
    for _, selected_row in manual_selection.iterrows():
        key = f"{selected_row['Stkcd']}|{int(selected_row['Year'])}"
        row = formal_by_key.loc[key]
        prior_key = (selected_row["Stkcd"], int(selected_row["Year"]) - 1)
        manual_lag_rev = lag_rev_map.get(prior_key, np.nan)
        manual_lag_ar = lag_ar_map.get(prior_key, np.nan)
        manual_total = row["NI"] - row["CFO"]
        manual_delta_rev = row["Revenue"] - manual_lag_rev
        manual_delta_ar = row["AR"] - manual_lag_ar
        manual_y_raw = manual_total / row["LaggedAssets"]
        manual_x1_raw = 1.0 / row["LaggedAssets"]
        manual_x2_raw = (manual_delta_rev - manual_delta_ar) / row["LaggedAssets"]
        manual_x3_raw = row["PPE"] / row["LaggedAssets"]
        manual_scaled = {}
        for var, value in [
            ("Y_MJ", manual_y_raw),
            ("X1", manual_x1_raw),
            ("X2", manual_x2_raw),
            ("X3", manual_x3_raw),
        ]:
            q01, q99 = winsor_bounds[(int(row["Year"]), f"{var}_raw")]
            manual_scaled[var] = float(np.clip(value, q01, q99))
        group_key = (row["IndustryGroup"], int(row["Year"]))
        result = group_results.get(group_key)
        if result:
            manual_nda = (
                result["alpha1"] * manual_scaled["X1"]
                + result["alpha2"] * manual_scaled["X2"]
                + result["alpha3"] * manual_scaled["X3"]
            )
            manual_da = manual_scaled["Y_MJ"] - manual_nda
            manual_absda = abs(manual_da)
        else:
            manual_nda = manual_da = manual_absda = np.nan
        comparisons = {
            "TotalAccruals": manual_total - row["TotalAccruals"],
            "DeltaRevenue": manual_delta_rev - row["DeltaRevenue"],
            "DeltaAR": manual_delta_ar - row["DeltaAR"],
            "LaggedAssets": 0.0,
            "Y_MJ": manual_scaled["Y_MJ"] - row["Y_MJ"],
            "X1": manual_scaled["X1"] - row["X1"],
            "X2": manual_scaled["X2"] - row["X2"],
            "X3": manual_scaled["X3"] - row["X3"],
            "NDA": manual_nda - row["NDA"],
            "DA": manual_da - row["DA"],
            "AbsDA": manual_absda - row["AbsDA"],
        }
        max_error = float(
            np.nanmax(np.abs(list(comparisons.values())))
        )
        manual_rows.append(
            {
                "Seed": manual_seed,
                "Stkcd": row["Stkcd"],
                "Year": int(row["Year"]),
                "TotalAccrualsManual": manual_total,
                "TotalAccrualsFormal": row["TotalAccruals"],
                "DeltaRevenueManual": manual_delta_rev,
                "DeltaRevenueFormal": row["DeltaRevenue"],
                "DeltaARManual": manual_delta_ar,
                "DeltaARFormal": row["DeltaAR"],
                "LaggedAssetsFormal": row["LaggedAssets"],
                "Y_MJManual": manual_scaled["Y_MJ"],
                "Y_MJFormal": row["Y_MJ"],
                "X1Manual": manual_scaled["X1"],
                "X1Formal": row["X1"],
                "X2Manual": manual_scaled["X2"],
                "X2Formal": row["X2"],
                "X3Manual": manual_scaled["X3"],
                "X3Formal": row["X3"],
                "NDAManual": manual_nda,
                "NDAFormal": row["NDA"],
                "DAManual": manual_da,
                "DAFormal": row["DA"],
                "AbsDAManual": manual_absda,
                "AbsDAFormal": row["AbsDA"],
                "MaxAbsoluteError": max_error,
                "CheckPassed": bool(np.isfinite(max_error) and max_error <= 1e-10),
            }
        )
    manual_df = pd.DataFrame(manual_rows)
    write_csv(AUDIT / "MJ_MANUAL_RECALC_CHECK.csv", manual_df)
    manual_passed = bool(len(manual_df) >= 20 and manual_df["CheckPassed"].all())

    # AbsDA descriptive statistics, calculated only after the measurement.
    absda = work.loc[successful, ["Stkcd", "Year", "AbsDA"]].copy()
    percentiles = [0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99]
    overall = {
        "N": int(len(absda)),
        "mean": float(absda["AbsDA"].mean()),
        "sd": float(absda["AbsDA"].std(ddof=1)),
        "min": float(absda["AbsDA"].min()),
        "p1": float(absda["AbsDA"].quantile(0.01)),
        "p5": float(absda["AbsDA"].quantile(0.05)),
        "p25": float(absda["AbsDA"].quantile(0.25)),
        "median": float(absda["AbsDA"].median()),
        "p75": float(absda["AbsDA"].quantile(0.75)),
        "p95": float(absda["AbsDA"].quantile(0.95)),
        "p99": float(absda["AbsDA"].quantile(0.99)),
        "max": float(absda["AbsDA"].max()),
    }
    write_csv(AUDIT / "ABSDA_DESCRIPTIVE_OVERALL.csv", pd.DataFrame([overall]))
    by_year_rows = []
    for year, group in absda.groupby("Year", sort=True):
        by_year_rows.append(
            {
                "Year": int(year),
                "N": int(len(group)),
                "mean": float(group["AbsDA"].mean()),
                "sd": float(group["AbsDA"].std(ddof=1)),
                "min": float(group["AbsDA"].min()),
                "p1": float(group["AbsDA"].quantile(0.01)),
                "p5": float(group["AbsDA"].quantile(0.05)),
                "p25": float(group["AbsDA"].quantile(0.25)),
                "median": float(group["AbsDA"].median()),
                "p75": float(group["AbsDA"].quantile(0.75)),
                "p95": float(group["AbsDA"].quantile(0.95)),
                "p99": float(group["AbsDA"].quantile(0.99)),
                "max": float(group["AbsDA"].max()),
            }
        )
    write_csv(AUDIT / "ABSDA_DESCRIPTIVE_BY_YEAR.csv", pd.DataFrame(by_year_rows))

    # Key-only AI audit. Only two key columns are read from the AI file.
    ai = pd.read_stata(
        AI_PATH,
        columns=["stkcd_std", "year_std"],
        convert_categoricals=False,
    )
    ai["Stkcd"] = ai["stkcd_std"].map(norm_code)
    ai["Year"] = ai["year_std"].map(norm_year)
    ai = ai[ai["Stkcd"].ne("") & ai["Year"].notna()].copy()
    ai["Year"] = ai["Year"].astype(int)
    ai = ai[ai["Year"].between(2014, 2024)][["Stkcd", "Year"]].drop_duplicates()
    ai_keys = set(ai["Stkcd"] + "|" + ai["Year"].astype(str))
    final_key_audit = absda[["Stkcd", "Year"]].copy()
    final_key_audit["FinalAbsDAAvailable"] = 1
    final_key_audit["AIWashKeyMatched"] = (
        final_key_audit["Stkcd"] + "|" + final_key_audit["Year"].astype(str)
    ).isin(ai_keys).astype(int)
    write_csv(
        AUDIT / "AIWASH_KEY_MATCH_ABSDA_V15.csv",
        final_key_audit.sort_values(["Year", "Stkcd"]),
    )
    matched_n = int(final_key_audit["AIWashKeyMatched"].sum())
    match_rate = float(matched_n / len(final_key_audit)) if len(final_key_audit) else np.nan
    matched_by_year = (
        final_key_audit.groupby("Year", as_index=False)
        .agg(FinalAbsDAN=("FinalAbsDAAvailable", "sum"), MatchedN=("AIWashKeyMatched", "sum"))
        .assign(MatchRate=lambda frame: frame["MatchedN"] / frame["FinalAbsDAN"])
    )
    write_csv(AUDIT / "AIWASH_KEY_MATCH_ABSDA_BY_YEAR.csv", matched_by_year)

    # Monotonic sample-flow audit starts from the V14 formal eligible sample.
    flow = [
        ("V14FormalInputN", int(formal_mask.sum()), "V14 ModifiedJonesSampleEligible=1"),
        ("2014_2024N", int((formal_mask & work["Year"].between(2014, 2024)).sum()), "Formal years"),
        ("AfterFinancialExclusionN", int(after_financial.sum()), "A-share and FinancialIndustryFlag=0"),
        ("AfterValidLaggedAssetsN", int(after_lagged.sum()), "LaggedAssets>0"),
        ("AfterDeltaRevenueAvailableN", int(after_delta_revenue.sum()), "Continuous prior Revenue available"),
        ("AfterDeltaARAvailableN", int(after_delta_ar.sum()), "Continuous prior AR available"),
        ("AfterAllRegressionVariablesN", int(candidate_mask.sum()), "All four scaled variables finite"),
        ("AfterIndustryYearN10N", int((candidate_mask & work["MJEstimationN"].ge(10)).sum()), "MJEstimationN>=10"),
        ("AfterRegressionSuccessN", int((work["MJRegressionSucceeded"].eq(1)).sum()), "Successful group OLS rows"),
        ("FinalAbsDAN", int(len(absda)), "AbsDA nonmissing"),
        ("FinalAbsDAFirms", int(absda["Stkcd"].nunique()), "Unique firms with AbsDA"),
    ]
    write_csv(
        AUDIT / "MJ_SAMPLE_FLOW_V15.csv",
        pd.DataFrame(flow, columns=["Step", "N", "Definition"]),
    )

    # Final V15 output includes the flagged audit rows; AbsDA is populated only
    # for successful estimation rows.
    output_cols = [
        "Stkcd",
        "Year",
        "IndustryCodeRaw",
        "IndustryNameRaw",
        "IndustryGroup",
        "IndustrySource",
        "TA",
        "LaggedAssets",
        "LaggedRevenue",
        "LaggedAR",
        "Revenue",
        "AR",
        "PPE",
        "NI",
        "CFO",
        "TotalAccruals",
        "DeltaRevenue",
        "DeltaAR",
        "Y_MJ_raw",
        "X1_raw",
        "X2_raw",
        "X3_raw",
        "Y_MJ",
        "X1",
        "X2",
        "X3",
        "MJEstimationN",
        "MJRegressionEligible",
        "MJRegressionSucceeded",
        "Alpha1",
        "Alpha2",
        "Alpha3",
        "NDA",
        "DA",
        "AbsDA",
        "FinancialIndustryFlag",
        "ModifiedJonesSampleEligible",
        "ExclusionReason",
        "BankARMissingTreatedAsSourceFailure",
    ]
    final_output = work[output_cols].sort_values(["Stkcd", "Year"]).reset_index(drop=True)
    absda_csv = PROC / "modified_jones_absda_v15.csv"
    absda_dta = PROC / "modified_jones_absda_v15.dta"
    write_csv(absda_csv, final_output)
    write_dta(absda_dta, final_output)

    failed_groups = diagnostics_df[
        diagnostics_df["MJRegressionEligible"].eq(1)
        & diagnostics_df["RegressionSucceeded"].eq(0)
    ]
    affected_failed = int(
        failed_groups["N"].sum()
    ) if len(failed_groups) else 0
    eligible_groups = int(diagnostics_df["MJRegressionEligible"].sum())
    estimated_groups = int(diagnostics_df["RegressionSucceeded"].sum())
    failed_group_n = int(len(failed_groups))
    v14_hash_after = {
        str(path): sha256(path)
        for path in (V14_INPUT, V14_INPUT_DTA)
        if path.exists()
    }
    v13_hash_after = sha256(V13_SIX)
    v14_unchanged = v14_hash_before == v14_hash_after
    v13_unchanged = v13_hash_before == v13_hash_after

    d_drive_root = Path(r"D:\BaiduNetdiskDownload\论文数据")
    d_v15_matches = [
        str(path)
        for path in d_drive_root.rglob("*")
        if path.is_file() and "ModifiedJones_AbsDA_V15" in str(path)
    ]
    write_json(
        REPORTS / "d_drive_write_check.json",
        {
            "WriteToThesisDataDrive": False,
            "FilesWrittenToThesisDataDrive": 0,
            "DetectedV15FilesOnThesisDataDrive": d_v15_matches,
        },
    )

    identity_passed = bool(identity_row["IdentityCheckPassed"])
    firm_year_unique = bool(not final_output.duplicated(["Stkcd", "Year"]).any())
    measurement_approved = bool(
        protocol_path.exists()
        and not protocol_conflict
        and v14_unchanged
        and v13_unchanged
        and len(absda) > 0
        and failed_group_n == 0
        and identity_passed
        and finite_success
        and absda_nonnegative
        and firm_year_unique
        and manual_passed
        and len(d_v15_matches) == 0
    )
    status = {
        "HarnessV01StillHealthy": bool(v14_unchanged and v13_unchanged),
        "ProtocolFrozenBeforeConstruction": True,
        "FormalEstimationYears": "2014-2024",
        "ModifiedJonesVariant": "VariantB_DeltaRevenueMinusDeltaAR",
        "AdditionalConstant": False,
        "RawAmountsWinsorized": False,
        "ScaledVariablesWinsorized": True,
        "WinsorLevel": "1% two-tail by Year",
        "DAWinsorized": False,
        "AbsDAWinsorized": False,
        "TotalAccrualsConstructed": True,
        "DeltaRevenueConstructed": True,
        "DeltaARConstructed": True,
        "IndustryYearNRecalculated": True,
        "MinMJEstimationN": 10,
        "MJIndustryYearGroupsEligible": eligible_groups,
        "MJIndustryYearGroupsEstimated": estimated_groups,
        "MJIndustryYearGroupsFailed": failed_group_n,
        "AffectedFirmYearsFromFailedGroups": affected_failed,
        "FinalAbsDAN": int(len(absda)),
        "FinalAbsDAFirms": int(absda["Stkcd"].nunique()),
        "AbsDAMin": overall["min"],
        "AbsDAMedian": overall["median"],
        "AbsDAMean": overall["mean"],
        "AbsDAP95": overall["p95"],
        "AbsDAP99": overall["p99"],
        "AbsDAMax": overall["max"],
        "FirmYearUnique": firm_year_unique,
        "IdentityCheckPassed": identity_passed,
        "ManualRecalcCheckPassed": manual_passed,
        "AIWashKeyMatchOnly": True,
        "AIWashValueUsed": False,
        "AIWashKeyN": int(len(ai)),
        "AIWashMatchedN": matched_n,
        "AIWashMatchRate": match_rate,
        "AbsDAMeasurementApproved": measurement_approved,
        "AbsDAOutputCSV": str(absda_csv),
        "AbsDAOutputDTA": str(absda_dta),
        "FilesWrittenToThesisDataDrive": 0,
        "NoAIWashRegressionRun": True,
        "NoAIWashCorrelationScreening": True,
        "NoOutcomeFishing": True,
        "OriginalResearchDataModified": False,
        "ManuscriptModified": False,
        "V14ReadOnlyIntegrity": v14_unchanged,
        "V13ReadOnlyIntegrity": v13_unchanged,
        "FiniteSuccessValues": bool(finite_success),
        "AbsDANonNegative": absda_nonnegative,
        "PrimaryOutcomeCandidate": "AbsDA",
        "ManualRecalcSeed": manual_seed,
        "ManualRecalcN": int(len(manual_df)),
    }
    if not measurement_approved:
        reasons = []
        if failed_group_n:
            reasons.append(f"{failed_group_n} eligible Industry-Year regression groups failed")
        if not identity_passed:
            reasons.append("NDA+DA identity check failed")
        if not manual_passed:
            reasons.append("manual recalculation check failed")
        if not finite_success:
            reasons.append("non-finite successful output")
        if not v14_unchanged or not v13_unchanged:
            reasons.append("read-only source hash changed")
        if not firm_year_unique:
            reasons.append("firm-year key is not unique")
        status["OnlyRemainingObstacle"] = "; ".join(reasons) or "unknown technical gate"
    write_json(REPORTS / "v15_status_summary.json", status)
    write_json(
        REPORTS / "v15_run_metadata.json",
        {
            "ProtocolFrozenBeforeConstruction": True,
            "V4MethodFileReadOnly": str(V4_METHOD),
            "V14InputSHA256Before": v14_hash_before,
            "V14InputSHA256After": v14_hash_after,
            "V13SixFieldSHA256Before": v13_hash_before,
            "V13SixFieldSHA256After": v13_hash_after,
            "AIKeyColumnsReadOnly": ["stkcd_std", "year_std"],
            "ManualRecalcSeed": manual_seed,
            "ManualRecalcSelectionN": int(len(manual_df)),
            "OLSNoIntercept": True,
            "NoPValuesOrSignificance": True,
        },
    )

    approval = f"""# ABSDA_MEASUREMENT_APPROVAL_V15

AbsDAMeasurementApproved={str(measurement_approved).lower()}

## Frozen protocol

- Variant B: direct X2=(DeltaRevenue-DeltaAR)/LaggedAssets.
- AdditionalConstant=false.
- Formal years: 2014–2024; 2013 lag-only.
- Winsor: final scaled variables only, 1%/99% by Year, once.
- DAWinsorized=false; AbsDAWinsorized=false.
- MinMJEstimationN=10.

## Results

- MJIndustryYearGroupsEligible={eligible_groups}
- MJIndustryYearGroupsEstimated={estimated_groups}
- MJIndustryYearGroupsFailed={failed_group_n}
- AffectedFirmYearsFromFailedGroups={affected_failed}
- FinalAbsDAN={len(absda)}
- FinalAbsDAFirms={absda["Stkcd"].nunique()}
- IdentityCheckPassed={str(identity_passed).lower()}
- ManualRecalcCheckPassed={str(manual_passed).lower()}
- FirmYearUnique={str(firm_year_unique).lower()}
- AbsDANonNegative={str(absda_nonnegative).lower()}
- FiniteSuccessValues={str(bool(finite_success)).lower()}

## Isolation controls

AIWashKeyMatchOnly=true

AIWashValueUsed=false

NoAIWashRegressionRun=true

NoAIWashCorrelationScreening=true

NoOutcomeFishing=true

FilesWrittenToThesisDataDrive=0

No manuscript or V14/V13 file was modified.
"""
    (V15 / "ABSDA_MEASUREMENT_APPROVAL_V15.md").write_text(
        approval, encoding="utf-8"
    )
    readme = f"""# Modified Jones AbsDA V15 review package

V15 is the direct read-only continuation of V14. It constructs cash-flow
TotalAccruals, continuous-year DeltaRevenue and DeltaAR, the frozen Variant B
scaled variables, year-wise one-time winsorized regressors, IndustryGroup-Year
no-intercept OLS residuals, NDA, DA, and PrimaryOutcomeCandidate=AbsDA.

ProtocolFrozenBeforeConstruction=true

FormalEstimationYears=2014-2024

AdditionalConstant=false

MinMJEstimationN=10

MJIndustryYearGroupsEligible={eligible_groups}

MJIndustryYearGroupsEstimated={estimated_groups}

MJIndustryYearGroupsFailed={failed_group_n}

FinalAbsDAN={len(absda)}

AbsDAMeasurementApproved={str(measurement_approved).lower()}

The output retains non-eligible V14 rows for audit with missing AbsDA and an
ExclusionReason; only successful eligible groups receive NDA, DA, and AbsDA.
No coefficient significance or fit-quality screening was used.

AI_wash was accessed only through Stkcd and Year key columns. No AI_wash value
was read, and no AI_wash outcome analysis was run.
"""
    (V15 / "README_REVIEW.md").write_text(readme, encoding="utf-8")
    (LOGS / "v15_build.log").write_text(
        "V15 completed: construction, group OLS measurement, and audit only; no AI_wash value analysis.\n",
        encoding="utf-8",
    )
    (SCREENSHOTS / "README.md").write_text(
        "No browser page or database download was used in V15.\n",
        encoding="utf-8",
    )

    generated = []
    for path in sorted(V15.rglob("*")):
        if path.is_file() and path.name != "manifest_v15.json":
            generated.append(
                {
                    "path": str(path),
                    "relative": str(path.relative_to(V15)),
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
    write_json(
        MANIFESTS / "manifest_v15.json",
        {
            "Run": "ModifiedJones_AbsDA_V15",
            "GeneratedFiles": generated,
            "V14InputReadOnly": True,
            "V13LagSourceReadOnly": True,
            "NoRawFinancialDownloads": True,
            "FilesWrittenToThesisDataDrive": 0,
            "NoAIWashValuesRead": True,
            "NoAIWashRegressionRun": True,
            "NoOutcomeFishing": True,
        },
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
