import os
import hashlib
import shutil
import json
import datetime
import getpass
import uuid
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


def _file_digest(path: str, algo: str = "sha256", block_size: int = 1 << 16) -> Optional[str]:
    if not os.path.exists(path) or os.path.isdir(path):
        return None
    h = hashlib.new(algo)
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(block_size)
                if not chunk:
                    break
                h.update(chunk)
        return f"{algo}:{h.hexdigest()[:16]}"
    except (IOError, OSError):
        return None


def _file_meta(path: str) -> Dict[str, Any]:
    abs_p = os.path.abspath(path)
    if not os.path.exists(abs_p):
        return {"path": abs_p, "exists": False}
    if os.path.isdir(abs_p):
        return {"path": abs_p, "exists": True, "is_dir": True}
    try:
        size = os.path.getsize(abs_p)
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(abs_p)).isoformat()
        digest = _file_digest(abs_p)
    except (IOError, OSError):
        size = None
        mtime = None
        digest = None
    return {"path": abs_p, "exists": True, "is_dir": False, "size": size, "mtime": mtime, "digest": digest}


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


def generate_batch_id() -> str:
    return datetime.datetime.now().strftime("B%Y%m%d") + "-" + uuid.uuid4().hex[:8]


def log_operation(
    base_dir: str,
    command: str,
    output_files: List[str],
    args: Optional[Dict[str, Any]] = None,
    deleted_files: Optional[List[str]] = None,
    pre_existing_files: Optional[List[str]] = None,
    operator: Optional[str] = None,
    batch_id: Optional[str] = None,
    input_files: Optional[List[str]] = None,
) -> str:
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

    input_digests = []
    for inp in (input_files or []):
        meta = _file_meta(inp)
        if meta.get("exists") and not meta.get("is_dir"):
            input_digests.append({"path": meta["path"], "digest": meta.get("digest"), "size": meta.get("size")})

    output_digests = []
    for out_file in output_files:
        abs_out = os.path.abspath(out_file)
        if os.path.exists(abs_out) and not os.path.isdir(abs_out):
            meta = _file_meta(abs_out)
            output_digests.append({"path": meta["path"], "digest": meta.get("digest"), "size": meta.get("size")})

    record = {
        "timestamp": timestamp,
        "command": command,
        "args": args or {},
        "files": recorded_files,
        "snapshot_dir": os.path.abspath(snap_dir),
        "operator": operator or getpass.getuser(),
        "batch_id": batch_id or "",
        "input_digests": input_digests,
        "output_digests": output_digests,
    }
    index = _load_index(base_dir)
    index.append(record)
    _save_index(base_dir, index)

    log_file = os.path.join(snap_dir, "operation.log")
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(f"命令: {command}\n")
        f.write(f"时间: {timestamp}\n")
        f.write(f"操作人: {record['operator']}\n")
        f.write(f"批次号: {record['batch_id']}\n")
        f.write(f"参数: {json.dumps(args or {}, ensure_ascii=False, indent=2)}\n")
        f.write("文件变更:\n")
        for rf in recorded_files:
            mark = {"create": "[新 建]", "overwrite": "[覆 盖]", "delete": "[删 除]"}.get(rf["action"], "[?]")
            f.write(f"  {mark} {rf['original']}")
            if rf["backup"]:
                f.write(f"  (备份: {rf['backup']})")
            f.write("\n")
        if input_digests:
            f.write("输入文件摘要:\n")
            for id_ in input_digests:
                f.write(f"  {id_['path']}  {id_.get('digest', '?')}  {id_.get('size', '?')}B\n")
        if output_digests:
            f.write("输出文件摘要:\n")
            for od in output_digests:
                f.write(f"  {od['path']}  {od.get('digest', '?')}  {od.get('size', '?')}B\n")
    return timestamp


def get_last_operation(base_dir: str) -> Optional[Dict[str, Any]]:
    index = _load_index(base_dir)
    if not index:
        return None
    return index[-1]


def list_operations(base_dir: str, limit: int = 10, batch_id: Optional[str] = None) -> List[Dict[str, Any]]:
    index = _load_index(base_dir)
    if batch_id:
        index = [op for op in index if op.get("batch_id") == batch_id]
    return list(reversed(index[-limit:]))


def preview_rollback(base_dir: str) -> Dict[str, Any]:
    index = _load_index(base_dir)
    if not index:
        return {"has_operation": False}
    last_op = index[-1]
    to_restore = []
    to_delete = []
    to_skip = []
    for rf in last_op.get("files", []):
        original = rf["original"]
        action = rf.get("action", "overwrite")
        backup = rf.get("backup")
        if action == "create":
            if os.path.exists(original):
                to_delete.append({"path": original, "is_dir": os.path.isdir(original)})
            else:
                to_skip.append({"path": original, "reason": "文件已不存在"})
        elif action == "overwrite":
            if backup and os.path.exists(backup):
                to_restore.append({"path": original, "backup": backup, "is_dir": os.path.isdir(backup)})
            else:
                to_skip.append({"path": original, "reason": "备份丢失，无法恢复"})
        elif action == "delete":
            if backup and os.path.exists(backup):
                to_restore.append({"path": original, "backup": backup, "is_dir": os.path.isdir(backup), "was_deleted": True})
            else:
                to_skip.append({"path": original, "reason": "原删除操作无备份"})
    return {
        "has_operation": True,
        "operation": last_op,
        "to_restore": to_restore,
        "to_delete": to_delete,
        "to_skip": to_skip,
    }


def _cleanup_empty_dirs(path: str, stop_at: str) -> None:
    current = os.path.dirname(path)
    stop_abs = os.path.abspath(stop_at)
    while current and os.path.abspath(current) != stop_abs and len(os.path.abspath(current)) > len(stop_abs):
        try:
            if os.path.isdir(current) and not os.listdir(current):
                os.rmdir(current)
            else:
                break
        except (IOError, OSError):
            break
        current = os.path.dirname(current)


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
                    _cleanup_empty_dirs(original, base_dir)
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
                        if os.path.exists(original) and os.path.isdir(original):
                            shutil.rmtree(original, ignore_errors=True)
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

    cleanup_dirs = set()
    for d in deleted:
        parent = os.path.dirname(d)
        if parent and os.path.abspath(parent) != os.path.abspath(base_dir):
            cleanup_dirs.add(os.path.abspath(parent))
    for d in sorted(cleanup_dirs, key=len, reverse=True):
        _cleanup_empty_dirs(d, base_dir)
        if os.path.isdir(d) and not os.listdir(d):
            try:
                os.rmdir(d)
            except (IOError, OSError):
                pass

    rollback_log = os.path.join(last_op["snapshot_dir"], "rollback.log")
    with open(rollback_log, "w", encoding="utf-8") as f:
        f.write(f"回滚时间: {datetime.datetime.now().isoformat()}\n")
        f.write(f"原命令: {last_op['command']}  {last_op['timestamp']}\n")
        f.write(f"操作人: {last_op.get('operator', '?')}  批次号: {last_op.get('batch_id', '')}\n")
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


def export_audit_ledger(base_dir: str, output_path: str, batch_id: Optional[str] = None) -> str:
    index = _load_index(base_dir)
    if batch_id:
        index = [op for op in index if op.get("batch_id") == batch_id]
    records = []
    for op in index:
        row = {
            "时间戳": op.get("timestamp", ""),
            "命令": op.get("command", ""),
            "操作人": op.get("operator", ""),
            "批次号": op.get("batch_id", ""),
            "参数摘要": json.dumps(op.get("args", {}), ensure_ascii=False)[:200],
            "文件变更数": len(op.get("files", [])),
            "输入文件": " | ".join(
                f"{d['path']}({d.get('digest', '?')})" for d in op.get("input_digests", [])
            )[:500],
            "输出文件": " | ".join(
                f"{d['path']}({d.get('digest', '?')})" for d in op.get("output_digests", [])
            )[:500],
        }
        file_details = []
        for rf in op.get("files", []):
            act = {"create": "新建", "overwrite": "覆盖", "delete": "删除"}.get(rf.get("action"), "?")
            file_details.append(f"{rf['original']}[{act}]")
        row["文件变更明细"] = " | ".join(file_details)[:500]
        records.append(row)
    if not records:
        records = [{"说明": "无操作记录"}]
    import pandas as pd
    df = pd.DataFrame(records)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    ext = os.path.splitext(output_path)[1].lower()
    if ext in (".xlsx", ".xls"):
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="审计台账")
    else:
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
    return output_path


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
