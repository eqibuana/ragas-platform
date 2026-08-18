import io

import pandas as pd
from fastapi import HTTPException

# Same column-alias set proven in services/ragas-service/ragas_evaluation.py, so files
# already used with the ragas eval tool upload cleanly here too.
COLUMN_ALIASES = {
    "test scenario": "test_scenario",
    "query": "test_scenario",
    "expected result": "expected_result",
    "expected answer": "expected_result",
    "ground truth": "expected_result",
}

# ragas_data.xlsx (and similar workbooks) carry one sheet per domain with these exact
# names. If the uploaded file has a matching sheet, prefer it; otherwise fall back to
# the first sheet, since callers may also upload a flat, single-domain spreadsheet.
DOMAIN_SHEET_NAMES = {
    "hr": "HR",
    "contact_center": "Contact Center",
}


def parse_scenario_excel(content: bytes, domain: str) -> list[dict]:
    """Parse an uploaded .xlsx into a list of {row_index, test_scenario, expected_result} dicts."""
    try:
        workbook = pd.ExcelFile(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read Excel file: {e}")

    sheet_name = DOMAIN_SHEET_NAMES.get(domain)
    if sheet_name not in workbook.sheet_names:
        sheet_name = workbook.sheet_names[0]

    df = pd.read_excel(workbook, sheet_name=sheet_name)

    rename_map = {}
    for col in df.columns:
        key = str(col).strip().lower()
        if key in COLUMN_ALIASES:
            rename_map[col] = COLUMN_ALIASES[key]
    df = df.rename(columns=rename_map)

    if "test_scenario" not in df.columns or "expected_result" not in df.columns:
        raise HTTPException(
            status_code=400,
            detail=(
                "Sheet must have 'Test Scenario' and 'Expected Result' columns "
                f"(found: {list(df.columns)})"
            ),
        )

    df["test_scenario"] = df["test_scenario"].astype(str).str.strip()
    df["expected_result"] = df["expected_result"].astype(str).str.strip()
    df = df[df["test_scenario"].astype(bool) & (df["test_scenario"].str.lower() != "nan")]
    df = df.reset_index(drop=True)

    rows = []
    for i, row in df.iterrows():
        expected = row["expected_result"]
        rows.append(
            {
                "row_index": i,
                "test_scenario": row["test_scenario"],
                "expected_result": "" if expected.lower() == "nan" else expected,
            }
        )
    return rows
