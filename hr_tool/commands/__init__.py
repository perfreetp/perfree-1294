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
from ..logger import log_operation, rollback_last, list_operations, clear_history, get_last_operation


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
    write_file(merged, output_abs)
    click.echo(f"💾 写入: {click.format_filename(output_abs)}")

    log_operation(
        ctx.base_dir, "merge", [output_abs],
        {"inputs": [os.path.abspath(i) for i in inputs], "output": output_abs, "maps": field_mapping},
        pre_existing_files=pre_existing,
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

    os.makedirs(output_abs, exist_ok=True)
    pre_existing_dirs = [output_abs] if os.path.exists(output_abs) else []
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
    write_file(df, output_abs)
    click.echo(f"✅ {len(df)} 条记录已脱敏")
    click.echo(f"💾 输出: {click.format_filename(output_abs)}")

    log_operation(
        ctx.base_dir, "mask", [output_abs],
        {"input": input_abs, "output": output_abs, "fields": actual_fields},
        pre_existing_files=pre_existing,
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


def report_command(ctx, input_path: str, output: str, filters: List[str],
                   filter_output: str, group_by: str, export_stats: str):
    config = ctx.config
    input_abs = os.path.abspath(input_path)
    click.echo(f"📂 读取: {click.format_filename(input_abs)}")
    df = read_file(input_abs)
    click.echo(f"   {len(df)} 条记录，{len(df.columns)} 个字段")

    original_df = df.copy()
    report_df = df.copy()
    applied_filters = []
    all_outputs = []
    _filter_output_pre = []
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
            write_file(report_df.reset_index(drop=True), fout)
            click.echo(f"💾 筛选明细已保存: {click.format_filename(fout)}")
            # 已写入，记录 pre_existing
            _filter_output_pre = pre_f

    stats_sheets: Dict[str, pd.DataFrame] = {}
    summary_rows: List[Dict[str, Any]] = []

    def add_summary(metric: str, value: Any, note: str = ""):
        summary_rows.append({"指标": metric, "值": str(value), "备注": note})

    add_summary("总记录数", len(original_df), "未筛选前")
    if filters:
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
            click.echo(f"\n📈 自定义分组 ({' × '.join(valid_extra)}):")
            for _, row in s.head(20).iterrows():
                keys = " | ".join(str(row[g]) for g in valid_extra)
                click.echo(f"  {keys:<40} {row['人数']} 人")
            if len(s) > 20:
                click.echo(f"  ... 共 {len(s)} 行，完整数据见 Excel")

    click.echo("\n" + "=" * 50)
    click.echo("📊 核心摘要")
    click.echo("=" * 50)
    for row in summary_rows:
        click.echo(f"  {row['指标']:<22} {row['值']:<18} {row['备注']}")

    if filters:
        click.echo(f"\n🔎 筛选口径: {' 且 '.join(f'{f}{op}{v}' for f, op, v in applied_filters)}")
        click.echo("   以上所有分布统计均基于筛选后数据。")

    stats_sheets["0-摘要"] = pd.DataFrame(summary_rows)

    if filters:
        filter_desc = "；".join(f"{f}{op}{v}" for f, op, v in applied_filters)
        stats_sheets["筛选口径"] = pd.DataFrame([{
            "筛选表达式": filter_desc,
            "筛选后人数": len(report_df),
            "源文件": input_abs,
            "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }])
        stats_sheets["筛选结果明细"] = report_df.reset_index(drop=True)

    excel_output = None
    if export_stats:
        excel_output = os.path.abspath(export_stats)
    elif output and os.path.splitext(output)[1].lower() in (".xlsx", ".xls"):
        excel_output = os.path.abspath(output)

    pre_existing_all = []
    if excel_output:
        all_outputs.append(excel_output)
        pre_existing_all.extend(_existing_paths([excel_output]))
        _write_multi_sheet_excel(stats_sheets, excel_output)
        click.echo(f"\n💾 多 Sheet 统计报告: {click.format_filename(excel_output)}")
        click.echo(f"   Sheet 列表: {', '.join(stats_sheets.keys())}")
    elif output:
        out_abs = os.path.abspath(output)
        all_outputs.append(out_abs)
        pre_existing_all.extend(_existing_paths([out_abs]))
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

    if filters and filter_output:
        pre_existing_all.extend(_filter_output_pre)

    log_operation(
        ctx.base_dir, "report", all_outputs,
        {"input": input_abs, "filters": filters, "group_by": group_by, "output": output, "export_stats": export_stats},
        pre_existing_files=pre_existing_all,
    )


# ============================================================
# rollback 命令
# ============================================================
def rollback_command(ctx, steps: int, list_ops: bool, clear: int):
    if list_ops:
        ops = list_operations(ctx.base_dir, limit=20)
        if not ops:
            click.echo("ℹ️  暂无操作记录")
            return
        click.echo(f"📋 最近 {len(ops)} 条操作记录:")
        for i, op in enumerate(reversed(ops), 1):
            ts = op["timestamp"]
            cmd = op["command"]
            files = len(op.get("files", []))
            click.echo(f"  [{len(ops) - i + 1}] {ts}  {cmd:<8}  ({files} 个文件)")
        return

    if clear is not None:
        removed = clear_history(ctx.base_dir, keep_last=clear)
        click.echo(f"🧹 已清理 {removed} 条历史记录，保留最近 {clear} 条")
        return

    if steps != 1:
        click.echo("⚠️  当前仅支持回滚最近1步操作")
    try:
        result = rollback_last(ctx.base_dir)
        op = result["operation"]
        click.echo(f"↩️  已回滚操作: {op['timestamp']}  {op['command']}")
        if result["restored"]:
            click.echo(f"   ✅ 已恢复 {len(result['restored'])} 个文件（覆盖/删除前版本）:")
            for r in result["restored"]:
                click.echo(f"     - {click.format_filename(r)}")
        if result["deleted"]:
            click.echo(f"   🗑️  已删除 {len(result['deleted'])} 个文件（操作新建的产物）:")
            for d in result["deleted"]:
                click.echo(f"     - {click.format_filename(d)}")
        if result["skipped"]:
            click.echo(f"   ⏭️  跳过 {len(result['skipped'])} 项:")
            for s in result["skipped"]:
                click.echo(f"     - {s}")
        if result["errors"]:
            click.echo(f"   ❌ 出现 {len(result['errors'])} 个错误:")
            for e in result["errors"]:
                click.echo(f"     - {e}")
        if not any([result["restored"], result["deleted"], result["skipped"], result["errors"]]):
            click.echo("   没有可处理的文件变更。")
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


def _run_batch_step(ctx, step: Dict[str, Any], plan_dir: str) -> Dict[str, Any]:
    import time
    cmd = step["command"]
    args = step["args"]
    start = time.time()
    status = "success"
    exit_code = 0
    output_files: List[str] = []
    error_msg = ""
    try:
        resolved_args = {}
        for k, v in args.items():
            if k in ("inputs",) and isinstance(v, list):
                resolved_args[k] = [_resolve_path(str(x), plan_dir) for x in v]
            elif k in ("input", "output", "output_dir", "old", "new", "map_file",
                       "filter_output", "export_stats") and isinstance(v, str):
                resolved_args[k] = _resolve_path(v, plan_dir)
            elif k == "fields" and isinstance(v, list):
                resolved_args[k] = [str(x) for x in v]
            elif k == "filters" and isinstance(v, list):
                resolved_args[k] = [str(x) for x in v]
            elif k == "maps" and isinstance(v, list):
                resolved_args[k] = [str(x) for x in v]
            else:
                resolved_args[k] = v
        if cmd == "check":
            check_command(ctx,
                          resolved_args["input"],
                          resolved_args.get("output", "error_report.xlsx"),
                          bool(resolved_args.get("no_dup", False)),
                          bool(resolved_args.get("no_format", False)))
            output_files = [resolved_args.get("output", "error_report.xlsx")]
        elif cmd == "merge":
            maps = resolved_args.get("maps", []) or []
            merge_command(ctx,
                          resolved_args["inputs"],
                          resolved_args.get("output", "merged_roster.xlsx"),
                          list(maps),
                          resolved_args.get("map_file"),
                          resolved_args.get("source_col", "来源文件"))
            output_files = [resolved_args.get("output", "merged_roster.xlsx")]
        elif cmd == "split":
            split_command(ctx,
                          resolved_args["input"],
                          resolved_args.get("output_dir", "split_output"),
                          resolved_args.get("dept_field"),
                          resolved_args.get("file_format", "xlsx"))
            output_dir = resolved_args.get("output_dir", "split_output")
            if os.path.isdir(output_dir):
                output_files = [os.path.join(output_dir, f) for f in os.listdir(output_dir)
                                if os.path.splitext(f)[1].lower() in (".xlsx", ".xls", ".csv")]
            output_files = [output_dir]
        elif cmd == "compare":
            compare_command(ctx,
                            resolved_args["old"],
                            resolved_args["new"],
                            resolved_args.get("output", "compare_result.xlsx"),
                            resolved_args.get("key_field"),
                            resolved_args.get("dept_field"),
                            resolved_args.get("pos_field", "岗位"))
            output_files = [resolved_args.get("output", "compare_result.xlsx")]
        elif cmd == "mask":
            mask_command(ctx,
                         resolved_args["input"],
                         resolved_args.get("output", "masked_output.xlsx"),
                         list(resolved_args.get("fields", []) or []),
                         bool(resolved_args.get("mask_all", False)))
            output_files = [resolved_args.get("output", "masked_output.xlsx")]
        elif cmd == "report":
            report_command(ctx,
                           resolved_args["input"],
                           resolved_args.get("output"),
                           list(resolved_args.get("filters", []) or []),
                           resolved_args.get("filter_output"),
                           resolved_args.get("group_by"),
                           resolved_args.get("export_stats"))
            ofs = []
            for k in ("output", "filter_output", "export_stats"):
                v = resolved_args.get(k)
                if v:
                    ofs.append(v)
            output_files = ofs
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


def batch_command(ctx, plan_path: str, report_path: str, stop_on_error: bool):
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

    import time
    batch_start = time.time()
    results = []
    for i, step in enumerate(steps):
        click.echo(f"▶️  [{i+1}/{len(steps)}] {step['name']}  ({step['command']})")
        result = _run_batch_step(ctx, step, plan_dir)
        results.append(result)
        icon = {"success": "✅", "warnings": "⚠️ ", "failed": "❌", "failed_exit": "❌"}.get(result["status"], "?")
        click.echo(f"   {icon} {result['status']}  耗时 {result['duration_sec']}s  退出码 {result['exit_code']}")
        if result["error"]:
            click.echo(f"   ℹ️  {result['error']}")
        if result["output_files"]:
            click.echo(f"   📄 输出: {', '.join(os.path.basename(f) for f in result['output_files'][:5])}"
                       + (" ..." if len(result["output_files"]) > 5 else ""))
        click.echo()
        if stop_on_error and result["status"] in ("failed", "failed_exit"):
            click.echo(f"🛑 步骤失败，按 stop_on_error=true 停止剩余 {len(steps)-i-1} 步")
            break

    batch_duration = round(time.time() - batch_start, 3)
    success = sum(1 for r in results if r["status"] == "success")
    warnings = sum(1 for r in results if r["status"] == "warnings")
    failed = sum(1 for r in results if r["status"] in ("failed", "failed_exit"))

    click.echo("=" * 60)
    click.echo(f"🏁 批处理完成: {batch_name}")
    click.echo(f"   总耗时 {batch_duration}s  成功 {success}  告警 {warnings}  失败 {failed}")
    click.echo("=" * 60)

    report_abs = os.path.abspath(report_path) if report_path else os.path.join(
        plan_dir, f"batch_report_{batch_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )
    report_abs = os.path.abspath(report_abs)

    summary_df = pd.DataFrame([
        {"项目": "批处理名称", "值": batch_name},
        {"项目": "任务清单", "值": plan_abs},
        {"项目": "描述", "值": batch_desc},
        {"项目": "总步数", "值": len(steps)},
        {"项目": "已执行", "值": len(results)},
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
        "退出码": r["exit_code"],
        "耗时(秒)": r["duration_sec"],
        "输出文件": " | ".join(r["output_files"]),
        "错误信息": r["error"],
        "参数": json.dumps(r["args"], ensure_ascii=False),
    } for r in results])

    all_outputs = []
    for r in results:
        for f in r["output_files"]:
            all_outputs.append({"步骤序号": r["index"]+1, "步骤名称": r["name"], "输出文件": f})
    outputs_df = pd.DataFrame(all_outputs) if all_outputs else pd.DataFrame(columns=["步骤序号", "步骤名称", "输出文件"])

    sheets = {"0-批处理摘要": summary_df, "1-步骤明细": steps_df, "2-所有输出文件": outputs_df}
    pre_existing = _existing_paths([report_abs])
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
         "success": success, "failed": failed, "duration": batch_duration},
        pre_existing_files=pre_existing,
    )

    if failed > 0:
        sys.exit(1)
