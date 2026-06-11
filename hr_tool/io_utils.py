import os
import pandas as pd
from typing import List, Dict, Any, Optional, Tuple


SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv"}


def detect_file_type(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".xlsx", ".xls"):
        return "excel"
    if ext == ".csv":
        return "csv"
    raise ValueError(f"不支持的文件格式: {ext}，仅支持 .xlsx/.xls/.csv")


def read_file(filepath: str, sheet_name: Optional[str] = None, **kwargs) -> pd.DataFrame:
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")
    file_type = detect_file_type(filepath)
    if file_type == "excel":
        df = pd.read_excel(filepath, sheet_name=sheet_name or 0, dtype=str, **kwargs)
    else:
        encodings = ["utf-8-sig", "utf-8", "gbk", "gb18030"]
        last_err = None
        df = None
        for enc in encodings:
            try:
                df = pd.read_csv(filepath, dtype=str, encoding=enc, **kwargs)
                break
            except UnicodeDecodeError as e:
                last_err = e
                continue
        if df is None:
            raise last_err if last_err else ValueError(f"无法读取 CSV 文件: {filepath}")
    df = df.fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def write_file(df: pd.DataFrame, filepath: str, **kwargs) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    if ext in (".xlsx", ".xls"):
        with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, **kwargs)
    elif ext == ".csv":
        df.to_csv(filepath, index=False, encoding="utf-8-sig", **kwargs)
    else:
        raise ValueError(f"不支持的输出格式: {ext}")
    return filepath


def read_multiple_files(filepaths: List[str]) -> List[Tuple[str, pd.DataFrame]]:
    result = []
    for fp in filepaths:
        df = read_file(fp)
        result.append((fp, df))
    return result


def ensure_columns(df: pd.DataFrame, required: List[str]) -> Tuple[pd.DataFrame, List[str]]:
    missing = [c for c in required if c not in df.columns]
    for c in missing:
        df[c] = ""
    return df, missing


def normalize_columns(df: pd.DataFrame, mapping: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    if mapping:
        df = df.rename(columns=mapping)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def write_error_report(errors: List[Dict[str, Any]], filepath: str) -> str:
    df = pd.DataFrame(errors)
    return write_file(df, filepath)
