import os
import shutil
import json
import datetime
from typing import List, Dict, Any, Optional

LOG_DIRNAME = ".hr_logs"
ROLLBACK_INDEX = "rollback_index.json"


def _ensure_log_dir(base_dir: str) -> str:
    log_dir = os.path.join(base_dir, LOG_DIRNAME)
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _get_index_path(base_dir: str) -> str:
    return os.path.join(base_dir, LOG_DIRNAME, ROLLBACK_INDEX)


def _load_index(base_dir: str) -> List[Dict[str, Any]]:
    path = _get_index_path(base_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_index(base_dir: str, index: List[Dict[str, Any]]) -> None:
    path = _get_index_path(base_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def _make_snapshot_dir(base_dir: str, timestamp: str) -> str:
    snap_dir = os.path.join(base_dir, LOG_DIRNAME, f"snapshot_{timestamp}")
    os.makedirs(snap_dir, exist_ok=True)
    return snap_dir


def _safe_filename(name: str, idx: int = 0) -> str:
    import re
    base = re.sub(r'[\\/:*?"<>|]', "_", os.path.basename(name))
    if idx > 0:
        root, ext = os.path.splitext(base)
        base = f"{root}_{idx}{ext}"
    return base


def _backup_file(src: str, snap_dir: str, existing_names: Dict[str, int]) -> Optional[str]:
    if not os.path.exists(src):
        return None
    base = _safe_filename(src)
    count = existing_names.get(base, 0)
    fname = _safe_filename(src, count) if count > 0 else base
    existing_names[base] = count + 1
    backup_path = os.path.join(snap_dir, fname + ".bak")
    try:
        if os.path.isdir(src):
            dst_dir = os.path.join(snap_dir, _safe_filename(src, count) + "_dir.bak")
            shutil.copytree(src, dst_dir)
            return os.path.abspath(dst_dir)
        shutil.copy2(src, backup_path)
        return os.path.abspath(backup_path)
    except (IOError, OSError):
        return None


def log_operation(
    base_dir: str,
    command: str,
    output_files: List[str],
    args: Optional[Dict[str, Any]] = None,
    deleted_files: Optional[List[str]] = None,
    pre_existing_files: Optional[List[str]] = None,
) -> str:
    """记录一次操作。

    output_files: 命令写入/覆盖/创建的文件列表。
                  - 若路径在 pre_existing_files 中（或执行时已存在且未在 pre_existing_files 传空）→ 标记为 overwrite，并备份旧版本
                  - 否则 → 标记为 create，不备份（回滚时删除即可）
    deleted_files: 命令主动删除的文件列表（可选，回滚时尝试从备份恢复）
    pre_existing_files: 显式指定"写文件前就存在"的路径集合；
                        传 None 表示按写入后实时文件系统状态判断；
                        传空列表 [] 表示 output_files 全部是新建的。
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    snap_dir = _make_snapshot_dir(base_dir, timestamp)
    recorded_files = []
    name_counter: Dict[str, int] = {}
    pre_existing_set = None
    if pre_existing_files is not None:
        pre_existing_set = {os.path.abspath(p) for p in pre_existing_files}

    for out_file in output_files:
        abs_out = os.path.abspath(out_file)
        if pre_existing_set is not None:
            existed_before = abs_out in pre_existing_set
        else:
            existed_before = os.path.exists(abs_out)
        entry = {
            "original": abs_out,
            "action": "overwrite" if existed_before else "create",
            "backup": None,
        }
        if existed_before:
            backup = _backup_file(abs_out, snap_dir, name_counter)
            entry["backup"] = backup
        recorded_files.append(entry)

    deleted_files = deleted_files or []
    for del_file in deleted_files:
        abs_del = os.path.abspath(del_file)
        backup = _backup_file(abs_del, snap_dir, name_counter)
        recorded_files.append({
            "original": abs_del,
            "action": "delete",
            "backup": backup,
        })

    record = {
        "timestamp": timestamp,
        "command": command,
        "args": args or {},
        "files": recorded_files,
        "snapshot_dir": os.path.abspath(snap_dir),
    }
    index = _load_index(base_dir)
    index.append(record)
    _save_index(base_dir, index)

    log_file = os.path.join(snap_dir, "operation.log")
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(f"命令: {command}\n")
        f.write(f"时间: {timestamp}\n")
        f.write(f"参数: {json.dumps(args or {}, ensure_ascii=False, indent=2)}\n")
        f.write("文件变更:\n")
        for rf in recorded_files:
            mark = {"create": "[新 建]", "overwrite": "[覆 盖]", "delete": "[删 除]"}.get(rf["action"], "[?]")
            f.write(f"  {mark} {rf['original']}")
            if rf["backup"]:
                f.write(f"  (备份: {rf['backup']})")
            f.write("\n")
    return timestamp


def get_last_operation(base_dir: str) -> Optional[Dict[str, Any]]:
    index = _load_index(base_dir)
    if not index:
        return None
    return index[-1]


def list_operations(base_dir: str, limit: int = 10) -> List[Dict[str, Any]]:
    index = _load_index(base_dir)
    return list(reversed(index[-limit:]))


def rollback_last(base_dir: str) -> Dict[str, Any]:
    index = _load_index(base_dir)
    if not index:
        raise ValueError("没有可回滚的操作记录")
    last_op = index.pop()
    restored = []
    deleted = []
    skipped = []
    errors = []

    for rf in last_op.get("files", []):
        original = rf["original"]
        action = rf.get("action", "overwrite")
        backup = rf.get("backup")

        try:
            if action == "create":
                if os.path.exists(original):
                    if os.path.isdir(original):
                        shutil.rmtree(original, ignore_errors=True)
                    else:
                        os.remove(original)
                    deleted.append(original)
                else:
                    skipped.append(f"{original} (create, 文件已不存在)")

            elif action == "overwrite":
                if backup and os.path.exists(backup):
                    os.makedirs(os.path.dirname(original) or ".", exist_ok=True)
                    if os.path.isdir(backup):
                        if os.path.exists(original):
                            shutil.rmtree(original, ignore_errors=True)
                        shutil.copytree(backup, original)
                    else:
                        shutil.copy2(backup, original)
                    restored.append(original)
                else:
                    errors.append(f"文件 {original} 原为覆盖写入，但备份丢失，无法恢复")

            elif action == "delete":
                if backup and os.path.exists(backup):
                    os.makedirs(os.path.dirname(original) or ".", exist_ok=True)
                    if os.path.isdir(backup):
                        shutil.copytree(backup, original)
                    else:
                        shutil.copy2(backup, original)
                    restored.append(original + " (原被删除，已恢复)")
                else:
                    errors.append(f"文件 {original} 原为删除操作，无备份，无法恢复")
            else:
                skipped.append(f"{original} (未知动作 {action})")
        except (IOError, OSError) as e:
            errors.append(f"处理 {original} 失败: {e}")

    _save_index(base_dir, index)

    rollback_log = os.path.join(last_op["snapshot_dir"], "rollback.log")
    with open(rollback_log, "w", encoding="utf-8") as f:
        f.write(f"回滚时间: {datetime.datetime.now().isoformat()}\n")
        f.write(f"原命令: {last_op['command']}  {last_op['timestamp']}\n")
        f.write("\n恢复的文件 (原覆盖/删除):\n")
        for r in restored:
            f.write(f"  ✅ {r}\n")
        f.write("\n删除的文件 (原新建):\n")
        for d in deleted:
            f.write(f"  🗑️  {d}\n")
        if skipped:
            f.write("\n跳过:\n")
            for s in skipped:
                f.write(f"  ⏭️  {s}\n")
        if errors:
            f.write("\n错误:\n")
            for e in errors:
                f.write(f"  ❌ {e}\n")

    return {
        "operation": last_op,
        "restored": restored,
        "deleted": deleted,
        "skipped": skipped,
        "errors": errors,
    }


def clear_history(base_dir: str, keep_last: int = 0) -> int:
    log_dir = os.path.join(base_dir, LOG_DIRNAME)
    if not os.path.exists(log_dir):
        return 0
    index = _load_index(base_dir)
    to_remove = index[:-keep_last] if keep_last > 0 else index[:]
    removed_count = 0
    for op in to_remove:
        snap_dir = op.get("snapshot_dir", "")
        if os.path.exists(snap_dir) and os.path.isdir(snap_dir):
            shutil.rmtree(snap_dir, ignore_errors=True)
            removed_count += 1
    new_index = index[-keep_last:] if keep_last > 0 else []
    _save_index(base_dir, new_index)
    return removed_count
