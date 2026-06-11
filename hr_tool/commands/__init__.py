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

    required_fields = config.get_required_fields()
    unique_fields = config.get_unique_fields()
    all_field_names = [f.name for f in config.fields]

    missing_cols = [c for c in all_field_names if c not in df.columns]
    extra_cols = [c for c in df.columns if c not in all_field_names]
    if missing_cols:
        click.echo(f"⚠️  缺少配置字段: {', '.join(missing_cols)}")
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

    by_type = Counter(e["错误类型"] for e in errors)
    if by_type:
        click.echo("   错误类型分布：")
        for t, c in by_type.most_common():
            click.echo(f"     - {t}: {c}")

    if errors:
        write_file(pd.DataFrame(errors), output_abs)
        click.echo(f"\n📝 错误清单已写入: {click.format_filename(output_abs)}")
    else:
        click.echo("\n🎉 全部数据校验通过！")

    log_operation(
        ctx.base_dir, "check",
        [output_abs] if errors else [],
        {"input": input_abs, "output": output_abs},
    )
    sys.exit(0 if error_count == 0 else 1)


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
    write_file(merged, output_abs)
    click.echo(f"💾 写入: {click.format_filename(output_abs)}")

    log_operation(
        ctx.base_dir, "merge", [output_abs],
        {"inputs": [os.path.abspath(i) for i in inputs], "output": output_abs, "maps": field_mapping},
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
        write_file(group.reset_index(drop=True), out_path)
        click.echo(f"   ✅ {dept_name}: {len(group)} 人 -> {click.format_filename(out_path)}")
        total += len(group)
        generated_files.append(out_path)

    click.echo(f"\n📊 拆分完成: {len(groups)} 个部门，{total} 条记录 -> {click.format_filename(output_abs)}")

    log_operation(
        ctx.base_dir, "split", generated_files,
        {"input": input_abs, "output_dir": output_abs, "dept_field": dept},
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

    write_file(df, output_abs)
    click.echo(f"✅ {len(df)} 条记录已脱敏")
    click.echo(f"💾 输出: {click.format_filename(output_abs)}")

    log_operation(
        ctx.base_dir, "mask", [output_abs],
        {"input": input_abs, "output": output_abs, "fields": actual_fields},
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


def report_command(ctx, input_path: str, output: str, filters: List[str],
                   filter_output: str, group_by: str, export_stats: str):
    config = ctx.config
    input_abs = os.path.abspath(input_path)
    click.echo(f"📂 读取: {click.format_filename(input_abs)}")
    df = read_file(input_abs)
    click.echo(f"   {len(df)} 条记录")

    original_df = df.copy()
    if filters:
        click.echo("\n🔍 应用筛选条件:")
        for expr in filters:
            try:
                field, op, value = _parse_filter(expr)
                before = len(df)
                df = _apply_filter(df, field, op, value)
                click.echo(f"   {field} {op} {value}: {before} -> {len(df)}")
            except Exception as e:
                click.echo(f"   ⚠️  跳过无效条件 '{expr}': {e}")

        click.echo(f"\n   筛选结果: {len(df)} 条")

        if filter_output:
            fout = os.path.abspath(filter_output)
            write_file(df.reset_index(drop=True), fout)
            click.echo(f"💾 筛选结果已保存: {click.format_filename(fout)}")

    click.echo("\n" + "=" * 50)
    click.echo("📊 统计摘要")
    click.echo("=" * 50)

    lines = []
    lines.append(("总记录数", str(len(original_df))))
    if filters:
        lines.append(("筛选后记录数", str(len(df))))
    lines.append(("字段数", str(len(original_df.columns))))
    lines.append(("文件路径", input_abs))

    report_df = df if filters else original_df

    dept_field = config.department_field
    if dept_field in report_df.columns:
        dept_counts = report_df[dept_field].astype(str).str.strip()
        dept_counts = dept_counts[dept_counts != ""].value_counts()
        lines.append((f"部门数（按{dept_field}）", str(len(dept_counts))))

    unique_fields = config.get_unique_fields()
    if unique_fields:
        for uf in unique_fields:
            if uf in report_df.columns:
                non_empty = report_df[uf].astype(str).str.strip()
                non_empty = non_empty[non_empty != ""]
                lines.append((f"唯一{uf}数", str(non_empty.nunique())))
                dups = len(non_empty) - non_empty.nunique()
                if dups > 0:
                    lines.append((f"  其中重复", str(dups)))

    status_field = config.status_field
    if status_field in report_df.columns:
        status_counts = report_df[status_field].astype(str).str.strip()
        status_counts = status_counts[status_counts != ""].value_counts()
        for s, c in status_counts.items():
            lines.append((f"{s}", str(c)))

    join_field = config.join_date_field
    if join_field in report_df.columns:
        valid_dates = []
        for d in report_df[join_field].tolist():
            s = str(d).strip()
            if not s:
                continue
            for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d"]:
                try:
                    valid_dates.append(datetime.strptime(s, fmt))
                    break
                except ValueError:
                    continue
        if valid_dates:
            this_year = datetime.now().year
            this_year_count = sum(1 for d in valid_dates if d.year == this_year)
            lines.append((f"今年入职({this_year})", str(this_year_count)))
            lines.append(("最早入职日期", min(valid_dates).strftime("%Y-%m-%d")))

    leave_field = config.leave_date_field
    if leave_field in report_df.columns:
        leave_count = sum(1 for d in report_df[leave_field].tolist() if str(d).strip() != "")
        lines.append(("已离职人数", str(leave_count)))

    for label, val in lines:
        line = f"{label:<20} {val}"
        click.echo(line)

    if group_by:
        group_fields = [g.strip() for g in group_by.split(",") if g.strip()]
        valid_groups = [g for g in group_fields if g in report_df.columns]
        if valid_groups:
            click.echo("\n" + "-" * 50)
            click.echo(f"📈 按 {', '.join(valid_groups)} 分组统计:")
            click.echo("-" * 50)
            grouped = report_df.groupby(valid_groups).size().reset_index(name="人数")
            grouped = grouped.sort_values("人数", ascending=False)
            for _, row in grouped.iterrows():
                keys = " | ".join(str(row[g]) for g in valid_groups)
                click.echo(f"  {keys:<40} {row['人数']} 人")
            if export_stats:
                stats_path = os.path.abspath(export_stats)
                write_file(grouped.reset_index(drop=True), stats_path)
                click.echo(f"\n💾 分组明细已导出: {click.format_filename(stats_path)}")

    summary_lines = "\n".join(f"{k}: {v}" for k, v in lines)
    if output:
        out_abs = os.path.abspath(output)
        with open(out_abs, "w", encoding="utf-8") as f:
            f.write(summary_lines + "\n")
        click.echo(f"\n💾 摘要已保存: {click.format_filename(out_abs)}")

    generated = []
    if output:
        generated.append(os.path.abspath(output))
    if filter_output:
        generated.append(os.path.abspath(filter_output))
    if export_stats:
        generated.append(os.path.abspath(export_stats))
    log_operation(
        ctx.base_dir, "report", generated,
        {"input": input_abs, "filters": filters, "group_by": group_by},
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
            click.echo(f"   已恢复 {len(result['restored'])} 个文件:")
            for r in result["restored"]:
                click.echo(f"     - {click.format_filename(r)}")
        if result["errors"]:
            click.echo(f"   ⚠️  出现 {len(result['errors'])} 个错误:")
            for e in result["errors"]:
                click.echo(f"     - {e}")
    except ValueError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)
