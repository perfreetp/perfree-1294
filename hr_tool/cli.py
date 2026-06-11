import os
import click
from .config import load_config, find_config_path
from . import commands as cmd


class HRContext:
    def __init__(self):
        self.base_dir = os.getcwd()
        self._config = None
        self.verbose = False
        self.operator = None
        self.batch_id = None

    @property
    def config(self):
        if self._config is None:
            self._config = load_config()
        return self._config

    def load_config_safe(self):
        try:
            return self.config
        except Exception:
            return None


pass_hr = click.make_pass_decorator(HRContext, ensure=True)


@click.group(help="HR 批量档案命令行工具 - 统一整理各分公司员工资料")
@click.version_option(package_name="hr-batch-tool", prog_name="hr")
@click.option("-v", "--verbose", is_flag=True, help="显示详细输出")
@click.option("-C", "--cwd", type=click.Path(file_okay=False), help="设置工作目录")
@click.option("--operator", "operator_name", type=str, default=None, help="操作人姓名（写入审计日志）")
@click.option("--batch-id", type=str, default=None, help="指定批次号（默认自动生成）")
@pass_hr
def main(ctx: HRContext, verbose: bool, cwd: str, operator_name: str, batch_id: str):
    ctx.verbose = verbose
    ctx.operator = operator_name
    ctx.batch_id = batch_id
    if cwd:
        ctx.base_dir = os.path.abspath(cwd)
        os.chdir(ctx.base_dir)


@main.command("init", help="初始化字段模板配置文件")
@click.option("-f", "--force", is_flag=True, help="覆盖已有配置")
@click.option("-o", "--output", type=click.Path(), default="hr_config.yaml", help="输出文件路径")
@pass_hr
def init_cmd(ctx: HRContext, force: bool, output: str):
    cmd.init_command(force, output, ctx.base_dir)


@main.command("check", help="校验身份证/手机号、必填项、重复员工，生成错误清单")
@click.argument("input", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", type=click.Path(), default="error_report.xlsx", help="错误清单输出路径")
@click.option("--no-dup", is_flag=True, help="跳过重复检查")
@click.option("--no-format", is_flag=True, help="跳过格式校验")
@pass_hr
def check_cmd(ctx: HRContext, input: str, output: str, no_dup: bool, no_format: bool):
    cmd.check_command(ctx, input, output, no_dup, no_format)


@main.command("merge", help="合并多份花名册，支持批量改字段名")
@click.argument("inputs", nargs=-1, type=click.Path(exists=True, dir_okay=False), required=True)
@click.option("-o", "--output", type=click.Path(), default="merged_roster.xlsx", help="合并后输出路径")
@click.option("-m", "--map", "maps", multiple=True, type=str, help="字段映射，格式: 旧名=新名，可多次使用")
@click.option("--map-file", type=click.Path(exists=True, dir_okay=False), help="YAML/JSON 映射文件")
@click.option("--source-col", type=str, default="来源文件", help="添加来源列的列名（留空则不添加）")
@pass_hr
def merge_cmd(ctx: HRContext, inputs: tuple, output: str, maps: tuple, map_file: str, source_col: str):
    cmd.merge_command(ctx, list(inputs), output, list(maps), map_file, source_col)


@main.command("split", help="按部门拆分文件")
@click.argument("input", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output-dir", type=click.Path(), default="split_output", help="输出目录")
@click.option("-d", "--dept-field", type=str, default=None, help="指定部门字段名，默认从配置读取")
@click.option("--format", "file_format", type=click.Choice(["xlsx", "csv"]), default="xlsx", help="输出格式")
@pass_hr
def split_cmd(ctx: HRContext, input: str, output_dir: str, dept_field: str, file_format: str):
    cmd.split_command(ctx, input, output_dir, dept_field, file_format)


@main.command("compare", help="对比两期人员变化，标记新增/离职/调岗")
@click.argument("old", type=click.Path(exists=True, dir_okay=False))
@click.argument("new", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", type=click.Path(), default="compare_result.xlsx", help="对比结果输出路径")
@click.option("-k", "--key", "key_field", type=str, default=None, help="主键字段，默认从配置读取")
@click.option("--dept-field", type=str, default=None, help="部门字段，默认从配置读取")
@click.option("--pos-field", type=str, default="岗位", help="岗位字段，用于识别调岗")
@pass_hr
def compare_cmd(ctx: HRContext, old: str, new: str, output: str, key_field: str, dept_field: str, pos_field: str):
    cmd.compare_command(ctx, old, new, output, key_field, dept_field, pos_field)


@main.command("mask", help="隐藏敏感字段（身份证/手机号/薪资等）")
@click.argument("input", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", type=click.Path(), default="masked_output.xlsx", help="输出文件路径")
@click.option("-f", "--field", "fields", multiple=True, type=str, help="指定要脱敏的字段，可多次使用；默认使用配置中的敏感字段")
@click.option("--all", "mask_all", is_flag=True, help="脱敏所有可能包含敏感信息的字段")
@pass_hr
def mask_cmd(ctx: HRContext, input: str, output: str, fields: tuple, mask_all: bool):
    cmd.mask_command(ctx, input, output, list(fields), mask_all)


@main.command("report", help="生成统计摘要、按条件筛选员工，支持按分公司出分册")
@click.argument("input", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", type=click.Path(), default=None, help="摘要输出路径（默认控制台）")
@click.option("-f", "--filter", "filters", multiple=True, type=str, help="筛选条件: 字段=值，支持 != > < 包含 等，可多次使用")
@click.option("--filter-output", type=click.Path(), default=None, help="筛选结果输出路径")
@click.option("--group-by", "group_by", type=str, default=None, help="按指定字段分组统计，如: 部门,岗位")
@click.option("--export-stats", type=click.Path(), default=None, help="将统计明细导出到指定文件")
@click.option("--per-branch", is_flag=True, help="按分公司字段分别生成分册 Excel + 集团总册")
@click.option("--branch-field", type=str, default="分公司", help="分公司字段名（配合 --per-branch）")
@click.option("--branch-dir", type=str, default=None, help="分册输出目录（配合 --per-branch，默认同目录下 branch_reports/）")
@pass_hr
def report_cmd(ctx: HRContext, input: str, output: str, filters: tuple, filter_output: str,
               group_by: str, export_stats: str, per_branch: bool, branch_field: str, branch_dir: str):
    cmd.report_command(ctx, input, output, list(filters), filter_output, group_by, export_stats,
                       per_branch=per_branch, branch_field=branch_field, branch_dir=branch_dir)


@main.command("rollback", help="回滚上一次操作（支持预览确认）")
@click.option("-n", "--steps", type=int, default=1, help="回滚步数（暂仅支持1步）")
@click.option("--list", "list_ops", is_flag=True, help="列出最近的操作记录")
@click.option("--clear", type=int, default=None, help="清理历史记录，保留最近N条")
@click.option("--preview", is_flag=True, help="仅预览将要恢复/删除的文件，不执行回滚")
@click.option("--audit-export", type=click.Path(), default=None, help="导出审计台账到指定文件（支持 .xlsx/.csv）")
@click.option("--batch-id", type=str, default=None, help="按批次号筛选操作记录（配合 --list 或 --audit-export）")
@click.option("--yes", "-y", is_flag=True, help="跳过回滚确认提示")
@pass_hr
def rollback_cmd(ctx: HRContext, steps: int, list_ops: bool, clear: int,
                 preview: bool, audit_export: str, batch_id: str, yes: bool):
    cmd.rollback_command(ctx, steps, list_ops, clear, preview=preview, audit_export=audit_export,
                         batch_id=batch_id, confirm=not yes)


@main.command("batch", help="按 YAML/JSON 任务清单串行执行多个命令，生成批处理报告")
@click.argument("plan", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", "output", type=click.Path(), default=None, help="批处理报告输出路径（默认同目录 batch_report_*.xlsx）")
@click.option("--stop-on-error", is_flag=True, help="任一步失败立即停止，默认继续后续步骤")
@click.option("--dry-run", is_flag=True, help="预览每步将读写哪些文件、哪些会覆盖，不实际执行")
@click.option("--resume-from", type=str, default=None, help="从指定步骤继续执行（步骤名或序号如 3）")
@click.option("--resume", "resume_auto", is_flag=True, help="自动从上次断点继续执行（需配合 --batch-id）")
@pass_hr
def batch_cmd(ctx: HRContext, plan: str, output: str, stop_on_error: bool, dry_run: bool, resume_from: str, resume_auto: bool):
    cmd.batch_command(ctx, plan, output, stop_on_error, dry_run=dry_run, resume_from=resume_from, resume_auto=resume_auto)


if __name__ == "__main__":
    main()
