import os
import yaml
import json
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

DEFAULT_CONFIG_FILENAME = "hr_config.yaml"
DEFAULT_TEMPLATE = {
    "version": "1.0",
    "fields": [
        {"name": "员工编号", "required": True, "unique": True, "type": "string"},
        {"name": "姓名", "required": True, "unique": False, "type": "string"},
        {"name": "身份证号", "required": True, "unique": True, "type": "id_card"},
        {"name": "手机号", "required": True, "unique": True, "type": "phone"},
        {"name": "性别", "required": False, "unique": False, "type": "enum", "values": ["男", "女"]},
        {"name": "出生日期", "required": False, "unique": False, "type": "date"},
        {"name": "入职日期", "required": True, "unique": False, "type": "date"},
        {"name": "离职日期", "required": False, "unique": False, "type": "date"},
        {"name": "部门", "required": True, "unique": False, "type": "string"},
        {"name": "岗位", "required": True, "unique": False, "type": "string"},
        {"name": "职级", "required": False, "unique": False, "type": "string"},
        {"name": "分公司", "required": True, "unique": False, "type": "string"},
        {"name": "邮箱", "required": False, "unique": False, "type": "email"},
        {"name": "紧急联系人", "required": False, "unique": False, "type": "string"},
        {"name": "紧急联系电话", "required": False, "unique": False, "type": "phone"},
        {"name": "薪资", "required": False, "unique": False, "type": "number", "sensitive": True},
        {"name": "银行卡号", "required": False, "unique": False, "type": "string", "sensitive": True},
    ],
    "sensitive_fields": ["身份证号", "手机号", "薪资", "银行卡号", "紧急联系电话"],
    "unique_keys": ["员工编号", "身份证号", "手机号"],
    "department_field": "部门",
    "status_field": "员工状态",
    "join_date_field": "入职日期",
    "leave_date_field": "离职日期",
}


@dataclass
class FieldDef:
    name: str
    required: bool = False
    unique: bool = False
    type: str = "string"
    values: Optional[List[str]] = None
    sensitive: bool = False


@dataclass
class HRConfig:
    fields: List[FieldDef] = field(default_factory=list)
    sensitive_fields: List[str] = field(default_factory=list)
    unique_keys: List[str] = field(default_factory=list)
    department_field: str = "部门"
    status_field: str = "员工状态"
    join_date_field: str = "入职日期"
    leave_date_field: str = "离职日期"
    raw_config: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HRConfig":
        fields = []
        for f in data.get("fields", []):
            fields.append(FieldDef(
                name=f["name"],
                required=f.get("required", False),
                unique=f.get("unique", False),
                type=f.get("type", "string"),
                values=f.get("values", None),
                sensitive=f.get("sensitive", False),
            ))
        return cls(
            fields=fields,
            sensitive_fields=data.get("sensitive_fields", []),
            unique_keys=data.get("unique_keys", []),
            department_field=data.get("department_field", "部门"),
            status_field=data.get("status_field", "员工状态"),
            join_date_field=data.get("join_date_field", "入职日期"),
            leave_date_field=data.get("leave_date_field", "离职日期"),
            raw_config=data,
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.raw_config

    def get_field(self, name: str) -> Optional[FieldDef]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def get_required_fields(self) -> List[str]:
        return [f.name for f in self.fields if f.required]

    def get_unique_fields(self) -> List[str]:
        return [f.name for f in self.fields if f.unique]

    def get_sensitive_fields(self) -> List[str]:
        explicit = [f.name for f in self.fields if f.sensitive]
        return list(set(explicit + self.sensitive_fields))


def find_config_path(start_dir: Optional[str] = None) -> Optional[str]:
    current = start_dir or os.getcwd()
    while True:
        cfg_path = os.path.join(current, DEFAULT_CONFIG_FILENAME)
        if os.path.exists(cfg_path):
            return cfg_path
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def load_config(path: Optional[str] = None) -> HRConfig:
    if path is None:
        path = find_config_path()
    if path is None:
        raise FileNotFoundError(
            f"未找到配置文件 {DEFAULT_CONFIG_FILENAME}，请先运行 `hr init` 初始化。"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return HRConfig.from_dict(data)


def save_config(config: HRConfig, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config.to_dict(), f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def create_default_config() -> HRConfig:
    return HRConfig.from_dict(DEFAULT_TEMPLATE.copy())
