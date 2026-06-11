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


def log_operation(
    base_dir: str,
    command: str,
    output_files: List[str],
    args: Optional[Dict[str, Any]] = None,
) -> str:
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    snap_dir = _make_snapshot_dir(base_dir, timestamp)
    recorded_files = []
    for out_file in output_files:
        if not os.path.exists(out_file):
            continue
        abs_out = os.path.abspath(out_file)
        rel_name = os.path.basename(out_file)
        backup_path = os.path.join(snap_dir, rel_name + ".bak")
        try:
            shutil.copy2(abs_out, backup_path)
            recorded_files.append({
                "original": abs_out,
                "backup": os.path.abspath(backup_path),
            })
        except IOError:
            pass
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
        f.write("修改的文件:\n")
        for rf in recorded_files:
            f.write(f"  - {rf['original']}\n")
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
    errors = []
    for rf in last_op.get("files", []):
        backup = rf["backup"]
        original = rf["original"]
        try:
            if os.path.exists(backup):
                os.makedirs(os.path.dirname(original) or ".", exist_ok=True)
                shutil.copy2(backup, original)
                restored.append(original)
            else:
                errors.append(f"备份文件丢失: {backup}")
        except IOError as e:
            errors.append(f"恢复 {original} 失败: {e}")
    _save_index(base_dir, index)
    rollback_log = os.path.join(last_op["snapshot_dir"], "rollback.log")
    with open(rollback_log, "w", encoding="utf-8") as f:
        f.write(f"回滚时间: {datetime.datetime.now().isoformat()}\n")
        f.write(f"原命令: {last_op['command']}\n")
        f.write(f"已恢复文件:\n")
        for r in restored:
            f.write(f"  - {r}\n")
        if errors:
            f.write(f"错误:\n")
            for e in errors:
                f.write(f"  - {e}\n")
    return {
        "operation": last_op,
        "restored": restored,
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
