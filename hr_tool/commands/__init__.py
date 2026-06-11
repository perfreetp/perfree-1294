import os
import re
import sys
import json
import yaml
import click
import pandas as pd
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from collections import Counter, defaultdict

from ..config import (
    HRConfig, create_default_config, save_config, load_config,
    DEFAULT_CONFIG_FILENAME, find_config_path,
)
from ..validator import (
    validate_phone, validate_id_card, validate_email, validate_date,
    validate_enum, validate_number, mask_value, get_id_card_info,
)
from ..io_utils import read_file, write_file, read_multiple_files, ensure_columns
from ..logger import log_operation, prepare_backup, rollback_last, list_operations, clear_history, get_last_operation, preview_rollback, export_audit_ledger, generate_batch_id, save_checkpoint, load_checkpoint, clear_checkpoint


def _existing_paths(paths):
    return [p for p in paths if p and os.path.exists(os.path.abspath(p))]


# ============================================================
# init 命令
# ============================================================
def init_command(force: bool, output: str, base_dir: str):
    output_path = os.path.abspath(output)
    if os.path.exists(output_path) and not force:
        click.echo(f"错误: 文件 {output_path} 已存在，使用 -f 覆盖。", err=True)
        sys.exit(1)
    config = create_default_config()
    save_config(config, output_path)
    click.echo(f"✅ 配置文件已生成: {click.format_filename(output_path)}")
    click.echo(f"   包含 {len(config.fields)} 个默认字段，可按需编辑。")
    click.echo("   敏感字段: " + ", ".join(config.sensitive_fields))
    click.echo("   必填字段: " + ", ".join(config.get_required_fields()))


# ============================================================
# check 命令
# ============================================================
def check_command(ctx, input_path: str, output_path: str, skip_dup: bool, skip_format: bool):
    config = ctx.config
    input_abs = os.path.abspath(input_path)
    output_abs = os.path.abspath(output_path)

    click.echo(f"📂 读取文件: {click.format_filename(input_abs)}")
    df = read_file(input_abs)
    click.echo(f"   共 {len(df)} 条记录，{len(df.columns)} 个字段")

    errors = []
    total_checks = 0
    template_failed = False

    required_fields = config.get_required_fields()
    unique_fields = config.get_unique_fields()
    all_field_names = [f.name for f in config.fields]

    missing_cols = [c for c in all_field_names if c not in df.columns]
    missing_required_cols = [c for c in required_fields if c not in df.columns]
    extra_cols = [c for c in df.columns if c not in all_field_names]

    if missing_cols:
        click.echo(f"⚠️  缺少配置字段: {', '.join(missing_cols)}")
        for c in missing_cols:
            severity = "严重（必填字段缺失）" if c in required_fields else "一般（可选字段缺失）"
            errors.append({
                "行号": "整列",
                "字段": c,
                "错误类型": "模板不合格-字段缺失",
                "错误详情": f"数据文件缺少配置字段 '{c}' ({severity})",
                "当前值": "",
            })
    if missing_required_cols:
        template_failed = True
        click.echo(f"❌ 模板不合格！缺少必填字段: {', '.join(missing_required_cols)}")
    if extra_cols and ctx.verbose:
        click.echo(f"ℹ️  额外字段（将忽略）: {', '.join(extra_cols)}")

    for field_name in required_fields:
        if field_name not in df.columns:
            continue
        for idx, val in enumerate(df[field_name].tolist()):
            total_checks += 1
            if val is None or str(val).strip() == "":
                errors.append({
                    "行号": idx + 2,
                    "字段": field_name,
                    "错误类型": "必填项缺失",
                    "错误详情": f"{field_name} 不能为空",
                    "当前值": "",
                })

    if not skip_format:
        for field_def in config.fields:
            fname = field_def.name
            if fname not in df.columns:
                continue
            ftype = field_def.type
            for idx, raw_val in enumerate(df[fname].tolist()):
                val = str(raw_val).strip() if raw_val is not None else ""
                if val == "":
                    continue
                total_checks += 1
                ok, msg = True, ""
                if ftype == "phone":
                    ok, msg = validate_phone(val)
                elif ftype == "id_card":
                    ok, msg = validate_id_card(val)
                elif ftype == "email":
                    ok, msg = validate_email(val)
                elif ftype == "date":
                    ok, msg = validate_date(val, fname)
                elif ftype == "number":
                    ok, msg = validate_number(val, fname)
                elif ftype == "enum" and field_def.values:
                    ok, msg = validate_enum(val, field_def.values, fname)
                if not ok:
                    errors.append({
                        "行号": idx + 2,
                        "字段": fname,
                        "错误类型": f"{ftype}格式错误",
                        "错误详情": msg,
                        "当前值": val,
                    })

    if not skip_dup:
        for fname in unique_fields:
            if fname not in df.columns:
                continue
            seen = defaultdict(list)
            for idx, raw_val in enumerate(df[fname].tolist()):
                val = str(raw_val).strip() if raw_val is not None else ""
                if val == "":
                    continue
                seen[val].append(idx + 2)
            for val, rows in seen.items():
                if len(rows) > 1:
                    total_checks += 1
                    errors.append({
                        "行号": ",".join(map(str, rows)),
                        "字段": fname,
                        "错误类型": "重复值",
                        "错误详情": f"{fname}='{val}' 重复出现在 {len(rows)} 行",
                        "当前值": val,
                    })

    error_count = len(errors)
    click.echo(f"\n🔍 检查完成：{total_checks} 项检查，发现 {error_count} 个错误")
    if template_failed:
        click.echo(f"   ❗ 模板不合格（缺少必填字段），请先修正列头后再重试")

    by_type = Counter(e["错误类型"] for e in errors)
    if by_type:
        click.echo("   错误类型分布：")
        for t, c in by_type.most_common():
            click.echo(f"     - {t}: {c}")

    pre_existing = _existing_paths([output_abs])
    prepared = prepare_backup(ctx.base_dir, [output_abs], pre_existing_files=pre_existing)
    write_file(pd.DataFrame(errors) if errors else pd.DataFrame([{
        "行号": "", "字段": "", "错误类型": "无", "错误详情": "全部数据校验通过", "当前值": ""
    }]), output_abs)
    if errors:
        click.echo(f"\n📝 错误清单已写入: {click.format_filename(output_abs)}")
    else:
        click.echo(f"\n📝 校验通过报告已写入: {click.format_filename(output_abs)}")
        click.echo("\n🎉 全部数据校验通过！")

    log_operation(
        ctx.base_dir, "check",
        [output_abs],
        {"input": input_abs, "output": output_abs, "error_count": error_count, "template_failed": template_failed},
        pre_existing_files=pre_existing,
        operator=ctx.operator, batch_id=ctx.batch_id, input_files=[input_abs],
        prepared=prepared,
    )
    sys.exit(0 if error_count == 0 and not template_failed else 2 if template_failed else 1)


# ============================================================
# merge 命令
# ============================================================
def _parse_maps(maps_list: List[str], map_file: str) -> Dict[str, str]:
    result = {}
    for m in maps_list:
        if "=" in m:
            old, new = m.split("=", 1)
            result[old.strip()] = new.strip()
    if map_file:
        with open(map_file, "r", encoding="utf-8") as f:
            ext = os.path.splitext(map_file)[1].lower()
            if ext in (".yaml", ".yml"):
                data = yaml.safe_load(f) or {}
            elif ext == ".json":
                data = json.load(f) or {}
            else:
                data = {}
                for line in f:
                    line = line.strip()
                    if line and "=" in line and not line.startswith("#"):
                        old, new = line.split("=", 1)
                        data[old.strip()] = new.strip()
        result.update({str(k): str(v) for k, v in data.items()})
    return result


def merge_command(ctx, inputs: List[str], output: str, maps: List[str], map_file: str, source_col: str):
    config = ctx.config
    output_abs = os.path.abspath(output)
    input_abss = [os.path.abspath(i) for i in inputs]
    field_mapping = _parse_maps(maps, map_file)

    if field_mapping and ctx.verbose:
        click.echo("字段映射:")
        for k, v in field_mapping.items():
            click.echo(f"  {k} -> {v}")

    all_dfs = []
    total_before = 0
    for fp in inputs:
        fp_abs = os.path.abspath(fp)
        click.echo(f"📂 读取: {click.format_filename(fp_abs)}")
        df = read_file(fp_abs)
        if field_mapping:
            df = df.rename(columns=field_mapping)
        df.columns = [str(c).strip() for c in df.columns]
        source_name = os.path.basename(fp)
        if source_col:
            df[source_col] = source_name
        click.echo(f"   {len(df)} 条记录，{len(df.columns)} 列")
        total_before += len(df)
        all_dfs.append(df)

    merged = pd.concat(all_dfs, ignore_index=True, sort=False)
    merged = merged.fillna("")
    col_order = []
    for f in config.fields:
        if f.name in merged.columns:
            col_order.append(f.name)
    for c in merged.columns:
        if c not in col_order:
            col_order.append(c)
    merged = merged[col_order]

    click.echo(f"\n🔗 合并完成: {total_before} -> {len(merged)} 条记录")
    dup_count = 0
    unique_fields = config.get_unique_fields()
    if unique_fields:
        for fname in unique_fields:
            if fname in merged.columns:
                vals = merged[fname].astype(str).str.strip()
                non_empty = vals[vals != ""]
                dup = non_empty.duplicated().sum()
                dup_count += dup
                if dup > 0:
                    click.echo(f"   ⚠️  {fname} 有 {dup} 条重复")
    pre_existing = _existing_paths([output_abs])
    prepared = prepare_backup(ctx.base_dir, [output_abs], pre_existing_files=pre_existing)
    write_file(merged, output_abs)
    click.echo(f"💾 写入: {click.format_filename(output_abs)}")

    log_operation(
        ctx.base_dir, "merge", [output_abs],
        {"inputs": input_abss, "output": output_abs, "maps": field_mapping},
        pre_existing_files=pre_existing,
        operator=ctx.operator, batch_id=ctx.batch_id, input_files=input_abss,
        prepared=prepared,
    )


# ============================================================
# split 命令
# ============================================================
def _safe_filename(name: str) -> str:
    name = str(name).strip()
    if not name:
        name = "未分类"
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def split_command(ctx, input_path: str, output_dir: str, dept_field: str, file_format: str):
    config = ctx.config
    input_abs = os.path.abspath(input_path)
    output_abs = os.path.abspath(output_dir)

    dept = dept_field or config.department_field
    click.echo(f"📂 读取: {click.format_filename(input_abs)}")
    df = read_file(input_abs)
    click.echo(f"   共 {len(df)} 条记录")

    if dept not in df.columns:
        click.echo(f"❌ 找不到部门字段: {dept}，现有字段: {', '.join(df.columns)}", err=True)
        sys.exit(1)

    dir_existed_before = os.path.exists(output_abs)
    os.makedirs(output_abs, exist_ok=True)
    pre_existing_dirs = [output_abs] if dir_existed_before else []
    dept_vals = df[dept].astype(str).str.strip()
    dept_vals = dept_vals.where(dept_vals != "", "未分类")
    df[dept] = dept_vals

    groups = df.groupby(dept, sort=False)
    generated_files = []
    total = 0
    for dept_name, group in groups:
        safe_name = _safe_filename(dept_name)
        ext = ".xlsx" if file_format == "xlsx" else ".csv"
        out_path = os.path.join(output_abs, f"{safe_name}{ext}")
        generated_files.append(out_path)
    pre_existing_files = _existing_paths(generated_files)

    prepared = prepare_backup(ctx.base_dir, generated_files + [output_abs],
                              pre_existing_files=pre_existing_files + pre_existing_dirs)

    generated_files = []
    total = 0
    for dept_name, group in groups:
        safe_name = _safe_filename(dept_name)
        ext = ".xlsx" if file_format == "xlsx" else ".csv"
        out_path = os.path.join(output_abs, f"{safe_name}{ext}")
        write_file(group.reset_index(drop=True), out_path)
        click.echo(f"   ✅ {dept_name}: {len(group)} 人 -> {click.format_filename(out_path)}")
        total += len(group)
        generated_files.append(out_path)

    click.echo(f"\n📊 拆分完成: {len(groups)} 个部门，{total} 条记录 -> {click.format_filename(output_abs)}")

    log_operation(
        ctx.base_dir, "split", generated_files + [output_abs],
        {"input": input_abs, "output_dir": output_abs, "dept_field": dept},
        pre_existing_files=pre_existing_files + pre_existing_dirs,
        operator=ctx.operator, batch_id=ctx.batch_id, input_files=[input_abs],
        prepared=prepared,
    )


# ============================================================
# compare 命令
# ============================================================
def compare_command(ctx, old_path: str, new_path: str, output: str,
                    key_field: str, dept_field: str, pos_field: str):
    config = ctx.config
    old_abs = os.path.abspath(old_path)
    new_abs = os.path.abspath(new_path)
    output_abs = os.path.abspath(output)

    key = key_field or (config.unique_keys[0] if config.unique_keys else "员工编号")
    dept = dept_field or config.department_field
    pos = pos_field

    click.echo(f"📂 旧文件: {click.format_filename(old_abs)}")
    old_df = read_file(old_abs)
    click.echo(f"   {len(old_df)} 条记录")
    click.echo(f"📂 新文件: {click.format_filename(new_abs)}")
    new_df = read_file(new_path)
    click.echo(f"   {len(new_df)} 条记录")

    for col in [key, dept]:
        if col not in old_df.columns:
            click.echo(f"❌ 旧文件缺少字段: {col}", err=True)
            sys.exit(1)
        if col not in new_df.columns:
            click.echo(f"❌ 新文件缺少字段: {col}", err=True)
            sys.exit(1)

    old_df[key] = old_df[key].astype(str).str.strip()
    new_df[key] = new_df[key].astype(str).str.strip()
    old_df = old_df[old_df[key] != ""]
    new_df = new_df[new_df[key] != ""]
    old_df = old_df.drop_duplicates(subset=[key], keep="first").reset_index(drop=True)
    new_df = new_df.drop_duplicates(subset=[key], keep="first").reset_index(drop=True)

    old_keys = set(old_df[key].tolist())
    new_keys = set(new_df[key].tolist())

    added_keys = new_keys - old_keys
    removed_keys = old_keys - new_keys
    common_keys = old_keys & new_keys

    old_map = old_df.set_index(key).to_dict("index")
    new_map = new_df.set_index(key).to_dict("index")

    changes = []
    changed_count = 0
    dept_changed = 0
    pos_changed = 0

    change_cols = [c for c in old_df.columns if c in new_df.columns and c != key]

    for k in sorted(common_keys):
        o = old_map[k]
        n = new_map[k]
        row_change = {"人员类型": "在职", key: k}
        row_changed = False
        for c in change_cols:
            ov = str(o.get(c, "")).strip()
            nv = str(n.get(c, "")).strip()
            row_change[c] = nv
            if ov != nv:
                if c == dept:
                    row_change["部门变化"] = f"{ov} -> {nv}"
                    dept_changed += 1
                    row_changed = True
                elif c == pos:
                    row_change["岗位变化"] = f"{ov} -> {nv}"
                    pos_changed += 1
                    row_changed = True
                else:
                    row_change[f"{c}(变化)"] = f"{ov} -> {nv}"
                    row_changed = True
        if row_changed:
            if "部门变化" in row_change or "岗位变化" in row_change:
                row_change["人员类型"] = "调岗"
            else:
                row_change["人员类型"] = "信息变更"
            changes.append(row_change)
            changed_count += 1

    for k in sorted(added_keys):
        row = {"人员类型": "新增", key: k}
        for c in new_df.columns:
            row[c] = str(new_map[k].get(c, "")).strip()
        changes.append(row)

    for k in sorted(removed_keys):
        row = {"人员类型": "离职", key: k}
        for c in old_df.columns:
            row[c] = str(old_map[k].get(c, "")).strip()
        changes.append(row)

    result_df = pd.DataFrame(changes)
    col_order = ["人员类型", key]
    if "部门变化" in result_df.columns:
        col_order.append("部门变化")
    if "岗位变化" in result_df.columns:
        col_order.append("岗位变化")
    name_col = "姓名"
    if name_col in result_df.columns:
        col_order.append(name_col)
    if dept in result_df.columns:
        col_order.append(dept)
    if pos in result_df.columns:
        col_order.append(pos)
    for c in result_df.columns:
        if c not in col_order:
            col_order.append(c)
    result_df = result_df[[c for c in col_order if c in result_df.columns]]

    pre_existing = _existing_paths([output_abs])
    prepared = prepare_backup(ctx.base_dir, [output_abs], pre_existing_files=pre_existing)
    write_file(result_df, output_abs)

    click.echo(f"\n📊 对比结果:")
    click.echo(f"   新增: {len(added_keys)} 人")
    click.echo(f"   离职: {len(removed_keys)} 人")
    click.echo(f"   调岗(部门/岗位变化): {dept_changed + pos_changed} 人")
    click.echo(f"   信息变更: {changed_count - (dept_changed + pos_changed)} 人")
    click.echo(f"   在职: {len(common_keys)} 人")
    click.echo(f"\n💾 对比报告: {click.format_filename(output_abs)}")

    log_operation(
        ctx.base_dir, "compare", [output_abs],
        {"old": old_abs, "new": new_abs, "output": output_abs, "key": key},
        pre_existing_files=pre_existing,
        operator=ctx.operator, batch_id=ctx.batch_id, input_files=[old_abs, new_abs],
        prepared=prepared,
    )


# ============================================================
# mask 命令
# ============================================================
def mask_command(ctx, input_path: str, output: str, fields: List[str], mask_all: bool):
    config = ctx.config
    input_abs = os.path.abspath(input_path)
    output_abs = os.path.abspath(output)

    click.echo(f"📂 读取: {click.format_filename(input_abs)}")
    df = read_file(input_abs)

    target_fields = list(fields) if fields else []
    if not target_fields:
        target_fields = config.get_sensitive_fields()
    if mask_all:
        sensitive_keywords = ["证", "手机", "电话", "银行", "卡", "薪", "工资", "邮箱", "联系"]
        for c in df.columns:
            if any(kw in c for kw in sensitive_keywords):
                if c not in target_fields:
                    target_fields.append(c)

    actual_fields = [f for f in target_fields if f in df.columns]
    missing_fields = [f for f in target_fields if f not in df.columns]
    if missing_fields and ctx.verbose:
        click.echo(f"⚠️  以下字段不存在，跳过: {', '.join(missing_fields)}")

    if not actual_fields:
        click.echo("❌ 没有需要脱敏的字段")
        sys.exit(1)

    click.echo(f"🔒 脱敏字段: {', '.join(actual_fields)}")
    for fname in actual_fields:
        df[fname] = df[fname].apply(lambda v: mask_value(v, fname))

    pre_existing = _existing_paths([output_abs])
    prepared = prepare_backup(ctx.base_dir, [output_abs], pre_existing_files=pre_existing)
    write_file(df, output_abs)
    click.echo(f"✅ {len(df)} 条记录已脱敏")
    click.echo(f"💾 输出: {click.format_filename(output_abs)}")

    log_operation(
        ctx.base_dir, "mask", [output_abs],
        {"input": input_abs, "output": output_abs, "fields": actual_fields},
        pre_existing_files=pre_existing,
        operator=ctx.operator, batch_id=ctx.batch_id, input_files=[input_abs],
        prepared=prepared,
    )


# ============================================================
# report 命令
# ============================================================
_FILTER_OPS = {
    "=": lambda a, b: str(a).strip() == str(b).strip(),
    "==": lambda a, b: str(a).strip() == str(b).strip(),
    "!=": lambda a, b: str(a).strip() != str(b).strip(),
    ">": lambda a, b: _num_cmp(a, b) > 0,
    "<": lambda a, b: _num_cmp(a, b) < 0,
    ">=": lambda a, b: _num_cmp(a, b) >= 0,
    "<=": lambda a, b: _num_cmp(a, b) <= 0,
    "包含": lambda a, b: str(b).strip() in str(a).strip(),
    "不包含": lambda a, b: str(b).strip() not in str(a).strip(),
    "开头": lambda a, b: str(a).strip().startswith(str(b).strip()),
    "结尾": lambda a, b: str(a).strip().endswith(str(b).strip()),
}


def _num_cmp(a: Any, b: Any) -> int:
    try:
        fa = float(str(a).strip())
        fb = float(str(b).strip())
        return (fa > fb) - (fa < fb)
    except (ValueError, TypeError):
        sa, sb = str(a).strip(), str(b).strip()
        return (sa > sb) - (sa < sb)


def _parse_filter(expr: str) -> Tuple[str, str, str]:
    for op in ["==", "!=", ">=", "<=", "包含", "不包含", "开头", "结尾", "=", ">", "<"]:
        if op in expr:
            idx = expr.find(op)
            field = expr[:idx].strip()
            value = expr[idx + len(op):].strip()
            return field, op, value
    raise ValueError(f"无法解析筛选表达式: {expr}，应为 字段{list(_FILTER_OPS.keys())}值")


def _apply_filter(df: pd.DataFrame, field: str, op: str, value: str) -> pd.DataFrame:
    if field not in df.columns:
        raise ValueError(f"字段不存在: {field}")
    cmp_fn = _FILTER_OPS[op]
    mask = df[field].apply(lambda x: cmp_fn(x, value))
    return df[mask].copy()


def _parse_date(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        import pandas as _pd
        ts = _pd.to_datetime(s, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.to_pydatetime()
    except Exception:
        return None


def _series_to_dates(series: pd.Series) -> pd.Series:
    return series.apply(_parse_date)


def _group_count(df: pd.DataFrame, fields: List[str], count_name: str = "人数") -> pd.DataFrame:
    valid = [f for f in fields if f in df.columns]
    if not valid:
        return pd.DataFrame(columns=fields + [count_name])
    tmp = df.copy()
    for f in valid:
        tmp[f] = tmp[f].astype(str).str.strip().where(tmp[f].astype(str).str.strip() != "", "(空)")
    g = tmp.groupby(valid, dropna=False).size().reset_index(name=count_name)
    return g.sort_values(count_name, ascending=False).reset_index(drop=True)


def _write_multi_sheet_excel(sheets: Dict[str, pd.DataFrame], output_path: str):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for name, sdf in sheets.items():
            safe_name = str(name)[:31].replace(":", "_").replace("\\", "_").replace("/", "_").replace("?", "_").replace("*", "_").replace("[", "_").replace("]", "_")
            if sdf is None or len(sdf) == 0:
                sdf = pd.DataFrame([{"说明": "无数据"}])
            sdf.to_excel(writer, sheet_name=safe_name, index=False)


def _build_report_sheets(
    report_df: pd.DataFrame,
    original_df: pd.DataFrame,
    config,
    applied_filters: List[Tuple[str, str, str]],
    group_by: Optional[str],
    input_abs: str,
) -> Tuple[Dict[str, pd.DataFrame], List[Dict[str, Any]]]:
    stats_sheets: Dict[str, pd.DataFrame] = {}
    summary_rows: List[Dict[str, Any]] = []

    def add_summary(metric: str, value: Any, note: str = ""):
        summary_rows.append({"指标": metric, "值": str(value), "备注": note})

    add_summary("总记录数", len(original_df), "未筛选前")
    if applied_filters:
        add_summary("筛选后记录数", len(report_df), "；".join(f"{f} {op} {v}" for f, op, v in applied_filters))
    add_summary("字段数", len(original_df.columns))
    add_summary("源文件", input_abs)

    dept_field = config.department_field
    branch_field = "分公司"
    level_field = "职级"
    join_field = config.join_date_field
    leave_field = config.leave_date_field

    if dept_field in report_df.columns:
        s = _group_count(report_df, [dept_field])
        stats_sheets[f"按{dept_field}统计"] = s
        add_summary(f"部门数（按{dept_field}）", len(s))

    if branch_field in report_df.columns:
        s = _group_count(report_df, [branch_field])
        stats_sheets["按分公司统计"] = s
        add_summary("分公司数", len(s))

    if level_field in report_df.columns:
        s = _group_count(report_df, [level_field])
        stats_sheets["按职级统计"] = s

    if branch_field in report_df.columns and dept_field in report_df.columns:
        s = _group_count(report_df, [branch_field, dept_field])
        stats_sheets["分公司×部门"] = s

    if branch_field in report_df.columns and level_field in report_df.columns:
        s = _group_count(report_df, [branch_field, level_field])
        stats_sheets["分公司×职级"] = s

    if dept_field in report_df.columns and level_field in report_df.columns:
        s = _group_count(report_df, [dept_field, level_field])
        stats_sheets["部门×职级"] = s

    if join_field in report_df.columns:
        join_dates = _series_to_dates(report_df[join_field])
        valid_join = [d for d in join_dates.tolist() if d is not None and not pd.isna(d)]
        if len(valid_join) > 0:
            tmp = report_df.copy()
            tmp["入职月份"] = join_dates.apply(
                lambda d: d.strftime("%Y-%m") if d is not None and not pd.isna(d) else None
            )
            tmp_join = tmp[tmp["入职月份"].notna()]
            s = _group_count(tmp_join, ["入职月份"])
            s = s.sort_values("入职月份")
            stats_sheets["按入职月份"] = s.reset_index(drop=True)
            this_year = datetime.now().year
            year_count = sum(1 for d in valid_join if d.year == this_year)
            add_summary(f"{this_year}年入职人数", year_count)
            add_summary("最早入职日期", min(valid_join).strftime("%Y-%m-%d"))
            add_summary("最新入职日期", max(valid_join).strftime("%Y-%m-%d"))

    if leave_field in report_df.columns:
        leave_dates = _series_to_dates(report_df[leave_field])
        valid_leave = [d for d in leave_dates.tolist() if d is not None and not pd.isna(d)]
        add_summary("已离职人数", len(valid_leave))
        if len(valid_leave) > 0:
            tmp = report_df.copy()
            tmp["离职月份"] = leave_dates.apply(
                lambda d: d.strftime("%Y-%m") if d is not None and not pd.isna(d) else None
            )
            tmp_leave = tmp[tmp["离职月份"].notna()]
            s = _group_count(tmp_leave, ["离职月份"])
            s = s.sort_values("离职月份")
            stats_sheets["按离职月份"] = s.reset_index(drop=True)

    if join_field in report_df.columns and dept_field in report_df.columns:
        join_dates = _series_to_dates(report_df[join_field])
        tmp = report_df.copy()
        tmp["入职月份"] = join_dates.apply(
            lambda d: d.strftime("%Y-%m") if d is not None and not pd.isna(d) else None
        )
        tmp = tmp[tmp["入职月份"].notna()]
        if len(tmp) > 0:
            s = _group_count(tmp, [dept_field, "入职月份"])
            stats_sheets["部门×入职月份"] = s

    if branch_field in report_df.columns and dept_field in report_df.columns and level_field in report_df.columns:
        s = _group_count(report_df, [branch_field, dept_field, level_field])
        stats_sheets["分公司×部门×职级"] = s

    unique_fields = config.get_unique_fields()
    if unique_fields:
        for uf in unique_fields:
            if uf in report_df.columns:
                vals = report_df[uf].astype(str).str.strip()
                non_empty = vals[vals != ""]
                add_summary(f"唯一{uf}数", non_empty.nunique())
                dups = len(non_empty) - non_empty.nunique()
                if dups > 0:
                    add_summary(f"  其中{uf}重复数", dups)

    status_field = config.status_field
    if status_field in report_df.columns:
        status_counts = report_df[status_field].astype(str).str.strip()
        status_counts = status_counts[status_counts != ""].value_counts()
        for s_name, s_count in status_counts.items():
            add_summary(f"状态-{s_name}", s_count)

    if group_by:
        extra_fields = [g.strip() for g in group_by.split(",") if g.strip()]
        valid_extra = [g for g in extra_fields if g in report_df.columns]
        if valid_extra:
            s = _group_count(report_df, valid_extra)
            stats_sheets[f"自定义-按{'×'.join(valid_extra)}"] = s

    stats_sheets["0-摘要"] = pd.DataFrame(summary_rows)

    if applied_filters:
        filter_desc = "；".join(f"{f}{op}{v}" for f, op, v in applied_filters)
        stats_sheets["筛选口径"] = pd.DataFrame([{
            "筛选表达式": filter_desc,
            "筛选后人数": len(report_df),
            "源文件": input_abs,
            "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }])
        stats_sheets["筛选结果明细"] = report_df.reset_index(drop=True)

    return stats_sheets, summary_rows


def _compare_batches(ctx, batch_1_path: str, batch_2_path: str, output: Optional[str],
                    export_stats: Optional[str] = None) -> None:
    b1_abs = os.path.abspath(batch_1_path)
    b2_abs = os.path.abspath(batch_2_path)
    click.echo(f"🔍 跨批次对比")
    click.echo(f"   批次1（基准）: {click.format_filename(b1_abs)}")
    click.echo(f"   批次2（对比）: {click.format_filename(b2_abs)}")
    click.echo()

    try:
        xls1 = pd.ExcelFile(b1_abs)
        xls2 = pd.ExcelFile(b2_abs)
    except Exception as e:
        click.echo(f"❌ 读取集团总册失败: {e}", err=True)
        sys.exit(1)

    required_sheets = {"分公司对比", "异常数据汇总"}
    missing_1 = required_sheets - set(xls1.sheet_names)
    missing_2 = required_sheets - set(xls2.sheet_names)
    if missing_1:
        click.echo(f"❌ 批次1缺少必要 Sheet: {', '.join(missing_1)}", err=True)
        sys.exit(1)
    if missing_2:
        click.echo(f"❌ 批次2缺少必要 Sheet: {', '.join(missing_2)}", err=True)
        sys.exit(1)

    b1_compare = pd.read_excel(xls1, sheet_name="分公司对比")
    b2_compare = pd.read_excel(xls2, sheet_name="分公司对比")
    b1_anomaly = pd.read_excel(xls1, sheet_name="异常数据汇总")
    b2_anomaly = pd.read_excel(xls2, sheet_name="异常数据汇总")

    branch_col = b1_compare.columns[0]
    branch_field = str(branch_col)

    b1_dict = {}
    for _, row in b1_compare.iterrows():
        bv = str(row[branch_col]).strip()
        b1_dict[bv] = row.to_dict()

    b2_dict = {}
    for _, row in b2_compare.iterrows():
        bv = str(row[branch_col]).strip()
        b2_dict[bv] = row.to_dict()

    all_branches = sorted(set(list(b1_dict.keys()) + list(b2_dict.keys())))

    compare_rows = []
    total_b1 = {"人数": 0, "部门数": 0, "职级数": 0}
    total_b2 = {"人数": 0, "部门数": 0, "职级数": 0}
    numeric_cols = ["人数", "部门数", "职级数"]

    for bv in all_branches:
        r1 = b1_dict.get(bv, {})
        r2 = b2_dict.get(bv, {})
        row = {branch_field: bv}
        for col in numeric_cols:
            v1 = int(r1.get(col, 0) or 0)
            v2 = int(r2.get(col, 0) or 0)
            row[f"批次1-{col}"] = v1
            row[f"批次2-{col}"] = v2
            row[f"变化量-{col}"] = v2 - v1
            change_pct = f"{((v2-v1)/v1*100):+.1f}%" if v1 > 0 else ("+" if v2 > 0 else "=")
            row[f"变化率-{col}"] = change_pct
            if col in total_b1:
                total_b1[col] += v1
                total_b2[col] += v2

        in_b1 = bv in b1_dict
        in_b2 = bv in b2_dict
        if in_b1 and not in_b2:
            row["状态"] = "已关闭"
        elif not in_b1 and in_b2:
            row["状态"] = "新增"
        else:
            row["状态"] = "存续"
        compare_rows.append(row)

    total_row = {branch_field: "合计"}
    for col in numeric_cols:
        v1 = total_b1[col]
        v2 = total_b2[col]
        total_row[f"批次1-{col}"] = v1
        total_row[f"批次2-{col}"] = v2
        total_row[f"变化量-{col}"] = v2 - v1
        total_row[f"变化率-{col}"] = f"{((v2-v1)/v1*100):+.1f}%" if v1 > 0 else ("+" if v2 > 0 else "=")
    total_row["状态"] = ""
    compare_rows.append(total_row)

    compare_df = pd.DataFrame(compare_rows)

    b1_anom_count = 0 if (len(b1_anomaly) == 1 and "说明" in b1_anomaly.columns and str(b1_anomaly.iloc[0]["说明"]).startswith("未发现")) else len(b1_anomaly)
    b2_anom_count = 0 if (len(b2_anomaly) == 1 and "说明" in b2_anomaly.columns and str(b2_anomaly.iloc[0]["说明"]).startswith("未发现")) else len(b2_anomaly)

    anom_by_type_b1: Dict[str, int] = {}
    anom_by_type_b2: Dict[str, int] = {}
    if b1_anom_count > 0:
        for _, row in b1_anomaly.iterrows():
            t = str(row.get("异常类型", "其他"))
            cnt = int(row.get("出现次数", 1) or 1)
            anom_by_type_b1[t] = anom_by_type_b1.get(t, 0) + cnt
    if b2_anom_count > 0:
        for _, row in b2_anomaly.iterrows():
            t = str(row.get("异常类型", "其他"))
            cnt = int(row.get("出现次数", 1) or 1)
            anom_by_type_b2[t] = anom_by_type_b2.get(t, 0) + cnt

    all_anom_types = sorted(set(list(anom_by_type_b1.keys()) + list(anom_by_type_b2.keys())))
    anomaly_rows = []
    for t in all_anom_types:
        v1 = anom_by_type_b1.get(t, 0)
        v2 = anom_by_type_b2.get(t, 0)
        anomaly_rows.append({
            "异常类型": t,
            "批次1-出现次数": v1,
            "批次2-出现次数": v2,
            "变化量": v2 - v1,
            "变化率": f"{((v2-v1)/v1*100):+.1f}%" if v1 > 0 else ("+" if v2 > 0 else "="),
        })
    anomaly_rows.append({
        "异常类型": "合计",
        "批次1-出现次数": b1_anom_count,
        "批次2-出现次数": b2_anom_count,
        "变化量": b2_anom_count - b1_anom_count,
        "变化率": f"{((b2_anom_count-b1_anom_count)/b1_anom_count*100):+.1f}%" if b1_anom_count > 0 else ("+" if b2_anom_count > 0 else "="),
    })
    anomaly_df = pd.DataFrame(anomaly_rows)

    summary_rows = []
    summary_rows.append({"指标": "总人数", "批次1": total_b1["人数"], "批次2": total_b2["人数"], "变化量": total_b2["人数"]-total_b1["人数"]})
    summary_rows.append({"指标": "总部门数", "批次1": total_b1["部门数"], "批次2": total_b2["部门数"], "变化量": total_b2["部门数"]-total_b1["部门数"]})
    summary_rows.append({"指标": "总职级数", "批次1": total_b1["职级数"], "批次2": total_b2["职级数"], "变化量": total_b2["职级数"]-total_b1["职级数"]})
    summary_rows.append({"指标": "分公司数", "批次1": len(b1_dict), "批次2": len(b2_dict), "变化量": len(b2_dict)-len(b1_dict)})
    summary_rows.append({"指标": "异常条目数", "批次1": b1_anom_count, "批次2": b2_anom_count, "变化量": b2_anom_count-b1_anom_count})
    summary_df = pd.DataFrame(summary_rows)

    output_rows = []
    for bv in all_branches:
        if bv in b1_dict and bv not in b2_dict:
            output_rows.append({"变动类型": "分公司关闭", branch_field: bv, "原有人数": b1_dict[bv].get("人数", 0), "现有人数": 0})
        elif bv not in b1_dict and bv in b2_dict:
            output_rows.append({"变动类型": "分公司新增", branch_field: bv, "原有人数": 0, "现有人数": b2_dict[bv].get("人数", 0)})
    for t in all_anom_types:
        v1 = anom_by_type_b1.get(t, 0)
        v2 = anom_by_type_b2.get(t, 0)
        if v2 > v1:
            output_rows.append({"变动类型": f"异常增加:{t}", branch_field: "全集团", "原有人数": v1, "现有人数": v2})
        elif v2 < v1:
            output_rows.append({"变动类型": f"异常减少:{t}", branch_field: "全集团", "原有人数": v1, "现有人数": v2})
    changes_df = pd.DataFrame(output_rows) if output_rows else pd.DataFrame(columns=["变动类型", branch_field, "原有人数", "现有人数"])

    click.echo("=" * 60)
    click.echo("📊 跨批次对比摘要")
    click.echo("=" * 60)
    for _, row in summary_df.iterrows():
        change = row["变化量"]
        arrow = "↑" if change > 0 else ("↓" if change < 0 else "=")
        color = "🟢" if (change < 0 and row["指标"] == "异常条目数") else ("🔴" if change > 0 and row["指标"] == "异常条目数" else ("🔴" if change > 0 else ("🟢" if change < 0 else "⚪")))
        sign = "+" if change > 0 else ""
        click.echo(f"   {color} {row['指标']:12s}  {row['批次1']:>6d} → {row['批次2']:>6d}  ({sign}{change}) {arrow}")

    if not changes_df.empty:
        click.echo()
        click.echo("⚠️  重点关注变动:")
        for _, row in changes_df.iterrows():
            click.echo(f"   · {row['变动类型']} - {row[branch_field]}: {row['原有人数']} → {row['现有人数']}")

    if output or export_stats:
        out_path = output or export_stats
        if not out_path:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(os.path.dirname(b1_abs), f"批次对比_{timestamp}.xlsx")
        out_abs = os.path.abspath(out_path)

        sheets = {
            "0-管理层摘要": summary_df,
            "1-分公司对比明细": compare_df,
            "2-异常数据对比": anomaly_df,
            "3-重点变动清单": changes_df,
        }
        _write_multi_sheet_excel(sheets, out_abs)
        click.echo()
        click.echo(f"💾 跨批次对比报告: {click.format_filename(out_abs)}")
        click.echo(f"   Sheet 列表: {', '.join(sheets.keys())}")

    log_operation(
        ctx.base_dir, "report",
        [out_abs] if (output or export_stats) else [],
        {"compare_batches": True, "batch_1": b1_abs, "batch_2": b2_abs},
        operator=ctx.operator, batch_id=ctx.batch_id, input_files=[b1_abs, b2_abs],
    )


def report_command(ctx, input_path: Optional[str], output: str, filters: List[str],
                   filter_output: str, group_by: str, export_stats: str,
                   per_branch: bool = False, branch_field: str = "分公司",
                   branch_dir: Optional[str] = None,
                   compare_batches: bool = False,
                   batch_1_path: Optional[str] = None,
                   batch_2_path: Optional[str] = None):
    if compare_batches:
        if not batch_1_path or not batch_2_path:
            click.echo("❌ 请指定 --batch-1 和 --batch-2 两个集团总册路径", err=True)
            sys.exit(1)
        _compare_batches(ctx, batch_1_path, batch_2_path, output, export_stats)
        return

    if not input_path:
        click.echo("❌ 请指定输入文件路径", err=True)
        sys.exit(1)

    config = ctx.config
    input_abs = os.path.abspath(input_path)
    click.echo(f"📂 读取: {click.format_filename(input_abs)}")
    df = read_file(input_abs)
    click.echo(f"   {len(df)} 条记录，{len(df.columns)} 个字段")

    original_df = df.copy()
    report_df = df.copy()
    applied_filters: List[Tuple[str, str, str]] = []
    all_outputs = []
    pre_existing_all = []
    _filter_output_pre = []
    _filter_output_path = None
    _filter_prepared = None

    if filters:
        click.echo("\n🔍 应用筛选条件:")
        for expr in filters:
            try:
                field, op, value = _parse_filter(expr)
                before = len(report_df)
                report_df = _apply_filter(report_df, field, op, value)
                applied_filters.append((field, op, value))
                click.echo(f"   {field} {op} {value}: {before} -> {len(report_df)}")
            except Exception as e:
                click.echo(f"   ⚠️  跳过无效条件 '{expr}': {e}")
        click.echo(f"\n   筛选结果: {len(report_df)} 条")
        if filter_output:
            fout = os.path.abspath(filter_output)
            all_outputs.append(fout)
            pre_f = _existing_paths([fout])
            _filter_prepared = prepare_backup(ctx.base_dir, [fout], pre_existing_files=pre_f)
            write_file(report_df.reset_index(drop=True), fout)
            click.echo(f"💾 筛选明细已保存: {click.format_filename(fout)}")
            _filter_output_pre = pre_f
            _filter_output_path = fout

    if per_branch:
        if branch_field not in report_df.columns:
            click.echo(f"❌ 找不到分公司字段: {branch_field}", err=True)
            sys.exit(1)

        if branch_dir is None:
            branch_dir = os.path.join(os.path.dirname(input_abs), "branch_reports")
        branch_dir_abs = os.path.abspath(branch_dir)
        os.makedirs(branch_dir_abs, exist_ok=True)

        branch_values = sorted(report_df[branch_field].astype(str).str.strip().unique())
        click.echo(f"\n🏢 按 '{branch_field}' 分册: 共 {len(branch_values)} 个分公司")

        all_outputs = []
        for bv in branch_values:
            bv_str = str(bv).strip()
            out_path = os.path.join(branch_dir_abs, f"{_safe_filename(bv_str)}_统计.xlsx")
            all_outputs.append(out_path)
        master_path = os.path.join(branch_dir_abs, "集团总册.xlsx")
        all_outputs.append(master_path)
        if _filter_output_pre:
            pre_existing_all.extend(_filter_output_pre)
        non_filter_outputs = [o for o in all_outputs if o != _filter_output_path]
        pre_existing_all.extend(_existing_paths(non_filter_outputs))
        prepared = prepare_backup(ctx.base_dir, non_filter_outputs, pre_existing_files=pre_existing_all)
        if _filter_prepared:
            prepared["recorded_files"] = _filter_prepared["recorded_files"] + prepared["recorded_files"]
            if _filter_prepared["timestamp"] < prepared["timestamp"]:
                prepared["timestamp"] = _filter_prepared["timestamp"]
                prepared["snapshot_dir"] = _filter_prepared["snapshot_dir"]

        for bv in branch_values:
            bv_str = str(bv).strip()
            branch_df = report_df[report_df[branch_field].astype(str).str.strip() == bv_str].copy()
            click.echo(f"\n🏢 分公司: {bv_str} ({len(branch_df)} 条)")
            stats_sheets, summary_rows = _build_report_sheets(
                branch_df, original_df, config, applied_filters, group_by, input_abs)
            out_path = os.path.join(branch_dir_abs, f"{_safe_filename(bv_str)}_统计.xlsx")
            _write_multi_sheet_excel(stats_sheets, out_path)
            click.echo(f"   💾 已写入: {click.format_filename(out_path)}")

        master_sheets, master_summary = _build_report_sheets(
            report_df, original_df, config, applied_filters, group_by, input_abs)

        dept_field = config.department_field
        level_field = "职级"
        join_field = config.join_date_field
        leave_field = config.leave_date_field
        this_year = datetime.now().year

        compare_rows = []
        for bv in branch_values:
            bv_str = str(bv).strip()
            bv_df = report_df[report_df[branch_field].astype(str).str.strip() == bv_str]
            row: Dict[str, Any] = {branch_field: bv_str, "人数": len(bv_df)}
            if dept_field in bv_df.columns:
                row["部门数"] = bv_df[dept_field].astype(str).str.strip().nunique()
            if level_field in bv_df.columns:
                row["职级数"] = bv_df[level_field].astype(str).str.strip().nunique()
            if join_field in bv_df.columns:
                join_dates = _series_to_dates(bv_df[join_field])
                row[f"{this_year}年入职"] = sum(
                    1 for d in join_dates if d is not None and not pd.isna(d) and d.year == this_year)
            if leave_field in bv_df.columns:
                leave_dates = _series_to_dates(bv_df[leave_field])
                row["已离职"] = sum(1 for d in leave_dates if d is not None and not pd.isna(d))
            compare_rows.append(row)
        master_sheets["分公司对比"] = pd.DataFrame(compare_rows)

        if join_field in report_df.columns or leave_field in report_df.columns:
            trend_rows = []
            for bv in branch_values:
                bv_str = str(bv).strip()
                bv_df = report_df[report_df[branch_field].astype(str).str.strip() == bv_str]
                if join_field in bv_df.columns:
                    join_dates = _series_to_dates(bv_df[join_field])
                    join_months = [d.strftime("%Y-%m") for d in join_dates if d is not None and not pd.isna(d)]
                    for m, cnt in Counter(join_months).items():
                        trend_rows.append({branch_field: bv_str, "月份": m, "类型": "入职", "人数": cnt})
                if leave_field in bv_df.columns:
                    leave_dates = _series_to_dates(bv_df[leave_field])
                    leave_months = [d.strftime("%Y-%m") for d in leave_dates if d is not None and not pd.isna(d)]
                    for m, cnt in Counter(leave_months).items():
                        trend_rows.append({branch_field: bv_str, "月份": m, "类型": "离职", "人数": cnt})
            if trend_rows:
                trend_df = pd.DataFrame(trend_rows).sort_values(["月份", branch_field, "类型"])
                master_sheets["分公司趋势对比"] = trend_df.reset_index(drop=True)

        anomaly_rows = []
        unique_fields = config.get_unique_fields()
        for uf in unique_fields:
            if uf in report_df.columns:
                vals = report_df[uf].astype(str).str.strip()
                non_empty = vals[vals != ""]
                dup_vals = non_empty[non_empty.duplicated(keep=False)]
                for dv in dup_vals.unique():
                    count = (non_empty == dv).sum()
                    anomaly_rows.append({
                        "异常类型": f"{uf}重复",
                        "字段": uf,
                        "异常值": dv,
                        "出现次数": count,
                    })
        for field_def in config.fields:
            if field_def.required and field_def.name in report_df.columns:
                empty_mask = report_df[field_def.name].astype(str).str.strip().isin(["", "nan", "None"])
                empty_count = empty_mask.sum()
                if empty_count > 0:
                    anomaly_rows.append({
                        "异常类型": "必填字段为空",
                        "字段": field_def.name,
                        "异常值": f"(空值 {empty_count} 条)",
                        "出现次数": int(empty_count),
                    })
        if join_field in report_df.columns and leave_field in report_df.columns:
            join_s = _series_to_dates(report_df[join_field])
            leave_s = _series_to_dates(report_df[leave_field])
            for idx in report_df.index:
                jd = join_s.iloc[idx] if idx < len(join_s) else None
                ld = leave_s.iloc[idx] if idx < len(leave_s) else None
                if jd is not None and ld is not None and not pd.isna(jd) and not pd.isna(ld):
                    if ld < jd:
                        key_col = config.unique_fields[0] if config.unique_fields else None
                        key_val = str(report_df.iloc[idx].get(key_col, f"行{idx+1}")) if key_col and key_col in report_df.columns else f"行{idx+1}"
                        anomaly_rows.append({
                            "异常类型": "离职早于入职",
                            "字段": f"{join_field}/{leave_field}",
                            "异常值": f"入职={jd.strftime('%Y-%m-%d')} 离职={ld.strftime('%Y-%m-%d')} ({key_val})",
                            "出现次数": 1,
                        })
        if anomaly_rows:
            master_sheets["异常数据汇总"] = pd.DataFrame(anomaly_rows)
        else:
            master_sheets["异常数据汇总"] = pd.DataFrame([{"说明": "未发现异常数据"}])

        _write_multi_sheet_excel(master_sheets, master_path)
        click.echo(f"\n💾 集团总册: {click.format_filename(master_path)}")
        click.echo(f"   分册 {len(branch_values)} 份 + 集团总册 1 份，共 {len(all_outputs)} 个文件")

        log_operation(
            ctx.base_dir, "report", all_outputs,
            {"input": input_abs, "filters": filters, "group_by": group_by,
             "per_branch": True, "branch_field": branch_field, "branch_dir": branch_dir_abs},
            pre_existing_files=pre_existing_all,
            operator=ctx.operator, batch_id=ctx.batch_id, input_files=[input_abs],
            prepared=prepared,
        )
        return

    stats_sheets, summary_rows = _build_report_sheets(
        report_df, original_df, config, applied_filters, group_by, input_abs)

    click.echo("\n" + "=" * 50)
    click.echo("📊 核心摘要")
    click.echo("=" * 50)
    for row in summary_rows:
        click.echo(f"  {row['指标']:<22} {row['值']:<18} {row['备注']}")

    if filters:
        click.echo(f"\n🔎 筛选口径: {' 且 '.join(f'{f}{op}{v}' for f, op, v in applied_filters)}")
        click.echo("   以上所有分布统计均基于筛选后数据。")

    if group_by:
        extra_fields = [g.strip() for g in group_by.split(",") if g.strip()]
        valid_extra = [g for g in extra_fields if g in report_df.columns]
        if valid_extra:
            key_name = f"自定义-按{'×'.join(valid_extra)}"
            if key_name in stats_sheets:
                s = stats_sheets[key_name]
                click.echo(f"\n📈 自定义分组 ({' × '.join(valid_extra)}):")
                for _, row in s.head(20).iterrows():
                    keys = " | ".join(str(row[g]) for g in valid_extra)
                    click.echo(f"  {keys:<40} {row['人数']} 人")
                if len(s) > 20:
                    click.echo(f"  ... 共 {len(s)} 行，完整数据见 Excel")

    excel_output = None
    if export_stats:
        excel_output = os.path.abspath(export_stats)
    elif output and os.path.splitext(output)[1].lower() in (".xlsx", ".xls"):
        excel_output = os.path.abspath(output)

    if excel_output:
        all_outputs.append(excel_output)
        pre_existing_all.extend(_existing_paths([excel_output]))
    elif output:
        out_abs = os.path.abspath(output)
        all_outputs.append(out_abs)
        pre_existing_all.extend(_existing_paths([out_abs]))

    if _filter_output_pre:
        pre_existing_all.extend(_filter_output_pre)

    non_filter_outputs = [o for o in all_outputs if o != _filter_output_path]
    prepared = prepare_backup(ctx.base_dir, non_filter_outputs, pre_existing_files=pre_existing_all)
    if _filter_prepared:
        prepared["recorded_files"] = _filter_prepared["recorded_files"] + prepared["recorded_files"]
        if _filter_prepared["timestamp"] < prepared["timestamp"]:
            prepared["timestamp"] = _filter_prepared["timestamp"]
            prepared["snapshot_dir"] = _filter_prepared["snapshot_dir"]

    if excel_output:
        _write_multi_sheet_excel(stats_sheets, excel_output)
        click.echo(f"\n💾 多 Sheet 统计报告: {click.format_filename(excel_output)}")
        click.echo(f"   Sheet 列表: {', '.join(stats_sheets.keys())}")
    elif output:
        out_abs = os.path.abspath(output)
        with open(out_abs, "w", encoding="utf-8") as f:
            f.write("HR 统计摘要\n")
            f.write("=" * 50 + "\n")
            for row in summary_rows:
                f.write(f"{row['指标']}: {row['值']}  {row['备注']}\n")
            if filters:
                f.write("\n筛选条件:\n")
                for field, op, value in applied_filters:
                    f.write(f"  {field} {op} {value}\n")
        click.echo(f"\n💾 摘要文本已保存: {click.format_filename(out_abs)}")

    log_operation(
        ctx.base_dir, "report", all_outputs,
        {"input": input_abs, "filters": filters, "group_by": group_by, "output": output, "export_stats": export_stats},
        pre_existing_files=pre_existing_all,
        operator=ctx.operator, batch_id=ctx.batch_id, input_files=[input_abs],
        prepared=prepared,
    )


# ============================================================
# rollback 命令
# ============================================================
def _display_rollback_preview(info: Dict[str, Any]) -> None:
    op = info["operation"]
    click.echo(f"📋 将回滚操作: {op['timestamp']}  {op['command']}  操作人={op.get('operator', '')}  批次={op.get('batch_id', '')}")

    if info.get("to_restore"):
        click.echo(f"\n   ✅ 将恢复 {len(info['to_restore'])} 个文件（回到覆盖/删除前版本）:")
        for r in info["to_restore"]:
            label = " (原被删除)" if r.get("was_deleted") else " (原被覆盖)"
            click.echo(f"     - {click.format_filename(r['path'])}{label}")

    if info.get("to_delete"):
        click.echo(f"\n   🗑️  将删除 {len(info['to_delete'])} 个文件/目录（操作新建的产物）:")
        for d in info["to_delete"]:
            kind = "目录" if d.get("is_dir") else "文件"
            click.echo(f"     - {click.format_filename(d['path'])} [{kind}]")

    if info.get("to_skip"):
        click.echo(f"\n   ⏭️  将跳过 {len(info['to_skip'])} 项:")
        for s in info["to_skip"]:
            click.echo(f"     - {s['path']} ({s.get('reason', '')})")

    restore_count = len(info.get("to_restore", []))
    delete_count = len(info.get("to_delete", []))
    skip_count = len(info.get("to_skip", []))
    click.echo(f"\n   📊 合计: 恢复 {restore_count}  删除 {delete_count}  跳过 {skip_count}")


def rollback_command(ctx, steps: int, list_ops: bool, clear: int,
                     preview: bool = False, audit_export: Optional[str] = None,
                     batch_id: Optional[str] = None, confirm: bool = True):
    if list_ops:
        ops = list_operations(ctx.base_dir, limit=20, batch_id=batch_id)
        if not ops:
            click.echo("ℹ️  暂无操作记录")
            return
        click.echo(f"📋 最近 {len(ops)} 条操作记录:")
        for i, op in enumerate(reversed(ops), 1):
            ts = op.get("timestamp", "")
            cmd = op.get("command", "")
            operator = op.get("operator", "")
            bid = op.get("batch_id", "")
            files = len(op.get("files", []))
            line = f"  [{len(ops) - i + 1}] {ts}  {cmd:<8}  操作人={operator}  批次={bid}  ({files} 个文件)"
            click.echo(line)
        return

    if audit_export:
        out_path = export_audit_ledger(ctx.base_dir, audit_export, batch_id=batch_id)
        click.echo(f"📊 审计台账已导出: {click.format_filename(os.path.abspath(out_path))}")
        return

    if clear is not None:
        removed = clear_history(ctx.base_dir, keep_last=clear)
        click.echo(f"🧹 已清理 {removed} 条历史记录，保留最近 {clear} 条")
        return

    if preview:
        info = preview_rollback(ctx.base_dir)
        if not info.get("has_operation"):
            click.echo("ℹ️  没有可回滚的操作记录")
            return
        _display_rollback_preview(info)
        return

    if confirm:
        info = preview_rollback(ctx.base_dir)
        if not info.get("has_operation"):
            click.echo("ℹ️  没有可回滚的操作记录")
            return
        _display_rollback_preview(info)
        if not click.confirm("\n确认执行回滚？", default=False):
            click.echo("已取消。")
            return

    if steps != 1:
        click.echo("⚠️  当前仅支持回滚最近1步操作")
    try:
        result = rollback_last(ctx.base_dir)
        op = result["operation"]
        click.echo(f"\n↩️  回滚执行结果: {op['timestamp']}  {op['command']}  操作人={op.get('operator', '')}  批次={op.get('batch_id', '')}")

        if result["restored"]:
            click.echo(f"\n   ✅ 已恢复 {len(result['restored'])} 个文件:")
            for rd in result.get("restored_digests", []):
                digest_info = f"  恢复后摘要={rd.get('rollback_digest', '?')}" if rd.get("rollback_digest") else ""
                click.echo(f"     - {click.format_filename(rd['path'])}{digest_info}")

        if result["deleted"]:
            click.echo(f"\n   🗑️  已删除 {len(result['deleted'])} 个文件/目录（操作新建的产物）:")
            for d in result["deleted"]:
                click.echo(f"     - {click.format_filename(d)}")

        if result["skipped"]:
            click.echo(f"\n   ⏭️  跳过 {len(result['skipped'])} 项:")
            for s in result["skipped"]:
                click.echo(f"     - {s}")

        if result["errors"]:
            click.echo(f"\n   ❌ 出现 {len(result['errors'])} 个错误:")
            for e in result["errors"]:
                click.echo(f"     - {e}")

        if not any([result["restored"], result["deleted"], result["skipped"], result["errors"]]):
            click.echo("   没有可处理的文件变更。")

        click.echo(f"\n   📊 回滚结果合计: 恢复 {len(result['restored'])}  删除 {len(result['deleted'])}  跳过 {len(result['skipped'])}  错误 {len(result['errors'])}")
    except ValueError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)


# ============================================================
# batch 命令
# ============================================================

ALLOWED_BATCH_COMMANDS = {"check", "merge", "split", "compare", "mask", "report"}


def _load_batch_plan(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"任务清单文件不存在: {path}")
    ext = os.path.splitext(path)[1].lower()
    with open(path, "r", encoding="utf-8") as f:
        if ext in (".yaml", ".yml"):
            data = yaml.safe_load(f)
        elif ext == ".json":
            data = json.load(f)
        else:
            content = f.read()
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError("任务清单必须是字典（包含 batch/options/steps 等顶层字段）")
    return data


def _normalize_step(raw: Dict[str, Any], idx: int, base_dir: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"第 {idx+1} 步不是字典")
    name = str(raw.get("name") or raw.get("command") or f"step_{idx+1}")
    cmd = str(raw.get("command") or raw.get("cmd") or "").strip().lower()
    if cmd not in ALLOWED_BATCH_COMMANDS:
        raise ValueError(f"第 {idx+1} 步命令 '{cmd}' 不合法，允许: {sorted(ALLOWED_BATCH_COMMANDS)}")
    args = raw.get("args") or raw.get("params") or {}
    if not isinstance(args, dict):
        raise ValueError(f"第 {idx+1} 步 args 必须是字典")
    for k, v in list(args.items()):
        if isinstance(v, str):
            args[k] = os.path.expandvars(v)
    return {"name": name, "command": cmd, "args": args, "index": idx}


_BATCH_CMD_PARAMS = {
    "check": [("input", True), ("output", False), ("no_dup", False), ("no_format", False)],
    "merge": [("inputs", True), ("output", False), ("maps", False), ("map_file", False), ("source_col", False)],
    "split": [("input", True), ("output_dir", False), ("dept_field", False), ("file_format", False)],
    "compare": [("old", True), ("new", True), ("output", False), ("key_field", False), ("dept_field", False), ("pos_field", False)],
    "mask": [("input", True), ("output", False), ("fields", False), ("mask_all", False)],
    "report": [("input", True), ("output", False), ("filters", False), ("filter_output", False), ("group_by", False), ("export_stats", False)],
}


def _resolve_path(p: str, base_dir: str) -> str:
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(base_dir, p))


def _resolve_step_args(args: Dict[str, Any], plan_dir: str) -> Dict[str, Any]:
    resolved = {}
    for k, v in args.items():
        if k in ("inputs",) and isinstance(v, list):
            resolved[k] = [_resolve_path(str(x), plan_dir) for x in v]
        elif k in ("input", "output", "output_dir", "old", "new", "map_file",
                   "filter_output", "export_stats") and isinstance(v, str):
            resolved[k] = _resolve_path(v, plan_dir)
        elif k == "fields" and isinstance(v, list):
            resolved[k] = [str(x) for x in v]
        elif k == "filters" and isinstance(v, list):
            resolved[k] = [str(x) for x in v]
        elif k == "maps" and isinstance(v, list):
            resolved[k] = [str(x) for x in v]
        else:
            resolved[k] = v
    return resolved


def _resolve_step_io(step: Dict[str, Any], plan_dir: str) -> Tuple[Dict[str, Any], List[str], List[str]]:
    cmd = step["command"]
    resolved = _resolve_step_args(step["args"], plan_dir)

    input_files: List[str] = []
    for k in ("input", "old", "new", "map_file"):
        v = resolved.get(k)
        if v and isinstance(v, str):
            input_files.append(v)
    if "inputs" in resolved:
        input_files.extend(resolved["inputs"])

    output_files: List[str] = []
    if cmd == "check":
        output_files = [resolved.get("output", _resolve_path("error_report.xlsx", plan_dir))]
    elif cmd == "merge":
        output_files = [resolved.get("output", _resolve_path("merged_roster.xlsx", plan_dir))]
    elif cmd == "split":
        output_dir = resolved.get("output_dir", _resolve_path("split_output", plan_dir))
        output_files = [output_dir]
    elif cmd == "compare":
        output_files = [resolved.get("output", _resolve_path("compare_result.xlsx", plan_dir))]
    elif cmd == "mask":
        output_files = [resolved.get("output", _resolve_path("masked_output.xlsx", plan_dir))]
    elif cmd == "report":
        for k in ("output", "filter_output", "export_stats"):
            v = resolved.get(k)
            if v:
                output_files.append(v)

    return resolved, input_files, output_files


def _run_batch_step(ctx, step: Dict[str, Any], plan_dir: str) -> Dict[str, Any]:
    import time
    cmd = step["command"]
    args = step["args"]
    start = time.time()
    status = "success"
    exit_code = 0
    error_msg = ""

    resolved_args, _step_inputs, output_files = _resolve_step_io(step, plan_dir)

    try:
        if cmd == "check":
            check_command(ctx,
                          resolved_args["input"],
                          resolved_args.get("output", "error_report.xlsx"),
                          bool(resolved_args.get("no_dup", False)),
                          bool(resolved_args.get("no_format", False)))
        elif cmd == "merge":
            maps = resolved_args.get("maps", []) or []
            merge_command(ctx,
                          resolved_args["inputs"],
                          resolved_args.get("output", "merged_roster.xlsx"),
                          list(maps),
                          resolved_args.get("map_file"),
                          resolved_args.get("source_col", "来源文件"))
        elif cmd == "split":
            split_command(ctx,
                          resolved_args["input"],
                          resolved_args.get("output_dir", "split_output"),
                          resolved_args.get("dept_field"),
                          resolved_args.get("file_format", "xlsx"))
        elif cmd == "compare":
            compare_command(ctx,
                            resolved_args["old"],
                            resolved_args["new"],
                            resolved_args.get("output", "compare_result.xlsx"),
                            resolved_args.get("key_field"),
                            resolved_args.get("dept_field"),
                            resolved_args.get("pos_field", "岗位"))
        elif cmd == "mask":
            mask_command(ctx,
                         resolved_args["input"],
                         resolved_args.get("output", "masked_output.xlsx"),
                         list(resolved_args.get("fields", []) or []),
                         bool(resolved_args.get("mask_all", False)))
        elif cmd == "report":
            report_command(ctx,
                           resolved_args["input"],
                           resolved_args.get("output"),
                           list(resolved_args.get("filters", []) or []),
                           resolved_args.get("filter_output"),
                           resolved_args.get("group_by"),
                           resolved_args.get("export_stats"))
    except SystemExit as e:
        exit_code = int(e.code) if e.code is not None else 0
        status = "success" if exit_code == 0 else ("failed_exit" if exit_code >= 2 else "warnings")
        error_msg = f"命令返回退出码 {exit_code}"
    except Exception as e:
        status = "failed"
        exit_code = 1
        error_msg = f"{type(e).__name__}: {e}"

    duration = round(time.time() - start, 3)
    return {
        "name": step["name"],
        "command": cmd,
        "index": step["index"],
        "status": status,
        "exit_code": exit_code,
        "duration_sec": duration,
        "args": args,
        "output_files": [os.path.abspath(f) for f in output_files if f],
        "error": error_msg,
    }


def batch_command(ctx, plan_path: str, report_path: str, stop_on_error: bool,
                  dry_run: bool = False, resume_from: Optional[str] = None,
                  resume_auto: bool = False):
    plan_abs = os.path.abspath(plan_path)
    plan_dir = os.path.dirname(plan_abs)
    click.echo(f"📋 读取批处理任务: {click.format_filename(plan_abs)}")
    try:
        plan = _load_batch_plan(plan_abs)
    except Exception as e:
        click.echo(f"❌ 任务清单解析失败: {e}", err=True)
        sys.exit(1)

    batch_name = str(plan.get("name") or os.path.splitext(os.path.basename(plan_abs))[0])
    batch_desc = str(plan.get("description") or "")
    options = plan.get("options") or {}
    if isinstance(options, dict):
        stop_on_error = bool(options.get("stop_on_error", stop_on_error))
    raw_steps = plan.get("steps") or plan.get("tasks") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        click.echo("❌ 任务清单 steps 为空或不是列表", err=True)
        sys.exit(1)

    try:
        steps = [_normalize_step(s, i, plan_dir) for i, s in enumerate(raw_steps)]
    except ValueError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)

    click.echo(f"🎯 批处理名称: {batch_name}")
    if batch_desc:
        click.echo(f"   描述: {batch_desc}")
    click.echo(f"   共 {len(steps)} 步，失败时{'立即停止' if stop_on_error else '继续执行'}\n")

    if not ctx.batch_id:
        ctx.batch_id = generate_batch_id()

    checkpoint = None
    if resume_auto:
        checkpoint = load_checkpoint(ctx.base_dir, ctx.batch_id)
        if checkpoint:
            last_step = checkpoint.get("last_completed_step", -1)
            last_name = checkpoint.get("last_step_name", "")
            last_status = checkpoint.get("last_step_status", "")
            click.echo(f"📍 发现断点: 批次 {ctx.batch_id} 上次执行到步骤 {last_step+1}({last_name})，状态={last_status}")

            if last_step >= len(steps) - 1 and last_status in ("success", "warnings"):
                click.echo(f"✅ 批次 {ctx.batch_id} 已全部完成（共 {len(steps)} 步），无需继续。")
                click.echo(f"   如需重新执行，请使用新的批次号从头开始，或不指定 --batch-id 自动生成新批次。")
                return

            if last_status in ("success", "warnings"):
                resume_from = str(last_step + 2)
            else:
                resume_from = str(last_step + 1)
                click.echo(f"   上次步骤未成功，将从该步骤重新执行")
        else:
            ops_with_batch = list_operations(ctx.base_dir, limit=100, batch_id=ctx.batch_id)
            if ops_with_batch:
                latest = ops_with_batch[-1]
                click.echo(f"ℹ️  批次 {ctx.batch_id} 已有操作记录（{latest.get('timestamp', '')}），但无断点信息。")
                click.echo(f"   该批次可能已完成，或历史记录已清理。")
                if not click.confirm("是否仍要从头开始执行？", default=False):
                    click.echo("已取消。建议使用新批次号重新运行。")
                    return
            click.echo(f"ℹ️  从头开始执行批次 {ctx.batch_id}")

    if dry_run:
        click.echo("🔍 DRY RUN 模式 - 仅预览，不实际执行\n")
        total_inputs: List[str] = []
        total_outputs: List[str] = []
        for i, step in enumerate(steps):
            resolved, step_inputs, step_outputs = _resolve_step_io(step, plan_dir)
            click.echo(f"▶️  [{i+1}/{len(steps)}] {step['name']}  ({step['command']})")
            click.echo(f"   📖 将读取 (INPUT):")
            for f in step_inputs:
                click.echo(f"     - {click.format_filename(f)}")
                total_inputs.append(f)
            if not step_inputs:
                click.echo(f"     (无)")
            click.echo(f"   📝 将写入 (OUTPUT):")
            for f in step_outputs:
                abs_f = os.path.abspath(f)
                if os.path.exists(abs_f):
                    click.echo(f"     - {click.format_filename(f)}  ⚠️  将覆盖")
                else:
                    click.echo(f"     - {click.format_filename(f)}  ✨ 新建")
                total_outputs.append(f)
            if not step_outputs:
                click.echo(f"     (无)")
            click.echo()
        click.echo("=" * 60)
        click.echo("📊 DRY RUN 摘要")
        click.echo(f"   共 {len(steps)} 步")
        click.echo(f"   将读取 {len(set(total_inputs))} 个唯一输入文件")
        click.echo(f"   将写入 {len(total_outputs)} 个输出文件")
        overwrite_count = sum(1 for f in total_outputs if os.path.exists(os.path.abspath(f)))
        new_count = len(total_outputs) - overwrite_count
        click.echo(f"   其中 {overwrite_count} 个文件将被覆盖")
        click.echo(f"   其中 {new_count} 个文件将新建")
        click.echo("=" * 60)
        return

    start_idx = 0
    is_retry_step = False
    if resume_from:
        try:
            start_idx = int(resume_from) - 1
        except ValueError:
            for i, step in enumerate(steps):
                if step["name"] == resume_from:
                    start_idx = i
                    break
            else:
                click.echo(f"❌ 找不到步骤: {resume_from}", err=True)
                sys.exit(1)
        if start_idx >= len(steps):
            click.echo(f"⚠️  步骤 {resume_from} 超出任务清单范围（共 {len(steps)} 步）。")
            if checkpoint and checkpoint.get("last_completed_step", -1) >= len(steps) - 1:
                click.echo(f"✅ 批次 {ctx.batch_id} 已全部完成，无需继续。")
                click.echo(f"   如需重新执行，请使用新的批次号。")
            else:
                click.echo(f"   请指定 1-{len(steps)} 之间的步骤号，或使用 --resume 自动判断。")
            return
        if start_idx < 0:
            start_idx = 0
        if checkpoint and checkpoint.get("last_step_status") not in ("success", "warnings"):
            is_retry_step = True
            click.echo(f"⏩ 从步骤 {start_idx + 1} ({steps[start_idx]['name']}) 失败重跑\n")
        else:
            click.echo(f"⏩ 从步骤 {start_idx + 1} ({steps[start_idx]['name']}) 继续执行\n")

    import time
    batch_start = time.time()
    results: List[Dict[str, Any]] = []
    for i, step in enumerate(steps):
        if i < start_idx:
            click.echo(f"⏭️  [{i+1}/{len(steps)}] {step['name']}  ({step['command']}) - 跳过(已执行)")
            results.append({
                "name": step["name"],
                "command": step["command"],
                "index": step["index"],
                "status": "skipped(resume)",
                "exit_code": 0,
                "duration_sec": 0,
                "args": step["args"],
                "output_files": [],
                "error": "",
            })
            continue

        if i == start_idx and is_retry_step:
            step_status_label = "failed_retried"
        elif i == start_idx and resume_from:
            step_status_label = "continued"
        else:
            step_status_label = ""

        click.echo(f"▶️  [{i+1}/{len(steps)}] {step['name']}  ({step['command']})"
                   + (f" [{step_status_label}]" if step_status_label else ""))
        result = _run_batch_step(ctx, step, plan_dir)
        if step_status_label:
            result["status_label"] = step_status_label

        last_op = get_last_operation(ctx.base_dir)
        if last_op and last_op.get("command") == step["command"]:
            result["audit_id"] = last_op.get("timestamp", "")
            result["input_digests"] = last_op.get("input_digests", [])
            result["file_records"] = last_op.get("files", [])
        else:
            result["audit_id"] = ""
            result["input_digests"] = []
            result["file_records"] = []

        results.append(result)
        icon = {"success": "✅", "warnings": "⚠️ ", "failed": "❌", "failed_exit": "❌"}.get(result["status"], "?")
        label_tag = f" [{step_status_label}]" if step_status_label else ""
        click.echo(f"   {icon} {result['status']}{label_tag}  耗时 {result['duration_sec']}s  退出码 {result['exit_code']}")
        if result["error"]:
            click.echo(f"   ℹ️  {result['error']}")
        if result["output_files"]:
            click.echo(f"   📄 输出: {', '.join(os.path.basename(f) for f in result['output_files'][:5])}"
                       + (" ..." if len(result["output_files"]) > 5 else ""))
        click.echo()

        save_checkpoint(ctx.base_dir, ctx.batch_id, plan_abs, len(steps), i,
                        step["name"], result["status"])

        if stop_on_error and result["status"] in ("failed", "failed_exit"):
            click.echo(f"🛑 步骤失败，按 stop_on_error=true 停止剩余 {len(steps)-i-1} 步")
            break

    batch_duration = round(time.time() - batch_start, 3)
    success = sum(1 for r in results if r["status"] == "success")
    warnings = sum(1 for r in results if r["status"] == "warnings")
    failed = sum(1 for r in results if r["status"] in ("failed", "failed_exit"))
    skipped_resume = sum(1 for r in results if r["status"] == "skipped(resume)")
    continued = sum(1 for r in results if r.get("status_label") == "continued")
    failed_retried = sum(1 for r in results if r.get("status_label") == "failed_retried")

    if failed == 0 and failed_retried == 0:
        clear_checkpoint(ctx.base_dir, ctx.batch_id)

    click.echo("=" * 60)
    click.echo(f"🏁 批处理完成: {batch_name}")
    click.echo(f"   总耗时 {batch_duration}s  成功 {success}  告警 {warnings}  失败 {failed}"
               f"  跳过(resume) {skipped_resume}  继续(continued) {continued}  失败重跑(failed_retried) {failed_retried}")
    click.echo("=" * 60)

    report_abs = os.path.abspath(report_path) if report_path else os.path.join(
        plan_dir, f"batch_report_{batch_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )
    report_abs = os.path.abspath(report_abs)

    summary_df = pd.DataFrame([
        {"项目": "批处理名称", "值": batch_name},
        {"项目": "任务清单", "值": plan_abs},
        {"项目": "描述", "值": batch_desc},
        {"项目": "批次号", "值": ctx.batch_id},
        {"项目": "总步数", "值": len(steps)},
        {"项目": "已执行", "值": len(results) - skipped_resume},
        {"项目": "跳过(resume)", "值": skipped_resume},
        {"项目": "继续(continued)", "值": continued},
        {"项目": "失败重跑(failed_retried)", "值": failed_retried},
        {"项目": "成功", "值": success},
        {"项目": "告警（有错误但继续）", "值": warnings},
        {"项目": "失败", "值": failed},
        {"项目": "总耗时(秒)", "值": batch_duration},
        {"项目": "生成时间", "值": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    ])
    steps_df = pd.DataFrame([{
        "步骤序号": r["index"] + 1,
        "步骤名称": r["name"],
        "命令": r["command"],
        "状态": r["status"],
        "状态标签": r.get("status_label", ""),
        "退出码": r["exit_code"],
        "耗时(秒)": r["duration_sec"],
        "输出文件": " | ".join(r["output_files"]),
        "错误信息": r["error"],
        "参数": json.dumps(r["args"], ensure_ascii=False),
    } for r in results])

    handoff_rows = []
    for r in results:
        step_idx = r["index"]
        step_cmd = r["command"]
        audit_id = r.get("audit_id", "")
        input_digests = r.get("input_digests", [])
        files = r.get("file_records", [])

        output_by_path = {}
        for rf in files:
            output_by_path[rf.get("original", "")] = rf

        input_paths = [d.get("path", "") for d in input_digests]
        input_str = " | ".join([os.path.basename(p) for p in input_paths]) if input_paths else ""
        input_digest_str = " | ".join([d.get("digest", "")[:12] for d in input_digests]) if input_digests else ""

        for out_file in r["output_files"]:
            rf = output_by_path.get(os.path.abspath(out_file), {})
            action = rf.get("action", "")
            act_label = {"create": "新建", "overwrite": "覆盖"}.get(action, "未知")
            pre_digest = rf.get("pre_digest", "") or ""
            post_digest = rf.get("post_digest", "") or ""
            handoff_rows.append({
                "步骤序号": step_idx + 1,
                "步骤名称": r["name"],
                "命令": step_cmd,
                "状态标签": r.get("status_label", ""),
                "输入文件": input_str,
                "输入摘要": input_digest_str,
                "输出文件": out_file,
                "操作类型": act_label,
                "覆盖前摘要": pre_digest,
                "覆盖后摘要": post_digest,
                "审计记录号": audit_id,
                "退出码": r["exit_code"],
                "耗时(秒)": r["duration_sec"],
            })

    handoff_df = pd.DataFrame(handoff_rows) if handoff_rows else pd.DataFrame(columns=[
        "步骤序号", "步骤名称", "命令", "状态标签", "输入文件", "输入摘要",
        "输出文件", "操作类型", "覆盖前摘要", "覆盖后摘要", "审计记录号", "退出码", "耗时(秒)"
    ])

    all_outputs = []
    for r in results:
        for f in r["output_files"]:
            all_outputs.append({"步骤序号": r["index"]+1, "步骤名称": r["name"], "输出文件": f})
    outputs_df = pd.DataFrame(all_outputs) if all_outputs else pd.DataFrame(columns=["步骤序号", "步骤名称", "输出文件"])

    sheets = {"0-批处理摘要": summary_df, "1-步骤明细": steps_df, "2-所有输出文件": outputs_df, "3-交接清单": handoff_df}
    pre_existing = _existing_paths([report_abs])
    prepared = prepare_backup(ctx.base_dir, [report_abs], pre_existing_files=pre_existing)
    if report_abs.lower().endswith((".xlsx", ".xls")):
        _write_multi_sheet_excel(sheets, report_abs)
        click.echo(f"📊 批处理报告（多 Sheet Excel）: {click.format_filename(report_abs)}")
    else:
        with open(report_abs, "w", encoding="utf-8") as f:
            f.write(f"批处理报告: {batch_name}\n")
            f.write("=" * 50 + "\n")
            for _, row in summary_df.iterrows():
                f.write(f"{row['项目']}: {row['值']}\n")
            f.write("\n步骤明细:\n")
            f.write(steps_df.to_string(index=False))
            f.write("\n")
        click.echo(f"📊 批处理报告: {click.format_filename(report_abs)}")

    log_operation(
        ctx.base_dir, "batch", [report_abs],
        {"plan": plan_abs, "report": report_abs, "steps": len(results),
         "success": success, "failed": failed, "duration": batch_duration,
         "batch_id": ctx.batch_id, "skipped_resume": skipped_resume,
         "continued": continued, "failed_retried": failed_retried},
        pre_existing_files=pre_existing,
        operator=ctx.operator, batch_id=ctx.batch_id, input_files=[plan_abs],
        prepared=prepared,
    )

    if failed > 0:
        sys.exit(1)
