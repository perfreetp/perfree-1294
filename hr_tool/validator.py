import re
import datetime
from typing import Optional, Tuple, List, Dict, Any


PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
ID_CARD_PATTERN_18 = re.compile(r"^[1-9]\d{5}(18|19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]$")
ID_CARD_PATTERN_15 = re.compile(r"^[1-9]\d{5}\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}$")
EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

ID_CARD_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
ID_CARD_CHECK_CODES = ["1", "0", "X", "9", "8", "7", "6", "5", "4", "3", "2"]


def validate_phone(phone: str) -> Tuple[bool, str]:
    if phone is None:
        return False, "手机号为空"
    phone_str = str(phone).strip()
    if not phone_str:
        return False, "手机号为空"
    if not PHONE_PATTERN.match(phone_str):
        return False, f"手机号格式不正确: {phone_str}"
    return True, ""


def _validate_id_card_checksum(id_card: str) -> bool:
    if len(id_card) != 18:
        return True
    total = 0
    for i in range(17):
        total += int(id_card[i]) * ID_CARD_WEIGHTS[i]
    check_code = ID_CARD_CHECK_CODES[total % 11]
    return id_card[17].upper() == check_code


def validate_id_card(id_card: str) -> Tuple[bool, str]:
    if id_card is None:
        return False, "身份证号为空"
    id_str = str(id_card).strip().upper()
    if not id_str:
        return False, "身份证号为空"
    if len(id_str) == 18:
        if not ID_CARD_PATTERN_18.match(id_str):
            return False, f"身份证号格式不正确: {id_str}"
        if not _validate_id_card_checksum(id_str):
            return False, f"身份证号校验码错误: {id_str}"
        birth_str = id_str[6:14]
        try:
            datetime.datetime.strptime(birth_str, "%Y%m%d")
        except ValueError:
            return False, f"身份证号出生日期无效: {id_str}"
    elif len(id_str) == 15:
        if not ID_CARD_PATTERN_15.match(id_str):
            return False, f"身份证号格式不正确: {id_str}"
        birth_str = "19" + id_str[6:12]
        try:
            datetime.datetime.strptime(birth_str, "%Y%m%d")
        except ValueError:
            return False, f"身份证号出生日期无效: {id_str}"
    else:
        return False, f"身份证号长度不正确: {id_str}"
    return True, ""


def get_id_card_info(id_card: str) -> Dict[str, Any]:
    info = {}
    id_str = str(id_card).strip().upper()
    try:
        if len(id_str) == 18:
            birth_str = id_str[6:14]
            birth = datetime.datetime.strptime(birth_str, "%Y%m%d").date()
            info["出生日期"] = birth
            gender_code = int(id_str[16])
            info["性别"] = "男" if gender_code % 2 == 1 else "女"
        elif len(id_str) == 15:
            birth_str = "19" + id_str[6:12]
            birth = datetime.datetime.strptime(birth_str, "%Y%m%d").date()
            info["出生日期"] = birth
            gender_code = int(id_str[14])
            info["性别"] = "男" if gender_code % 2 == 1 else "女"
    except (ValueError, IndexError):
        pass
    return info


def validate_email(email: str) -> Tuple[bool, str]:
    if email is None or str(email).strip() == "":
        return True, ""
    email_str = str(email).strip()
    if not EMAIL_PATTERN.match(email_str):
        return False, f"邮箱格式不正确: {email_str}"
    return True, ""


def validate_date(date_val: Any, field_name: str = "日期") -> Tuple[bool, str]:
    if date_val is None or str(date_val).strip() == "":
        return True, ""
    date_str = str(date_val).strip()
    formats = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"]
    for fmt in formats:
        try:
            datetime.datetime.strptime(date_str, fmt)
            return True, ""
        except ValueError:
            continue
    if isinstance(date_val, datetime.datetime) or isinstance(date_val, datetime.date):
        return True, ""
    return False, f"{field_name}格式不正确: {date_str}"


def validate_enum(value: Any, allowed_values: List[str], field_name: str = "字段") -> Tuple[bool, str]:
    if value is None or str(value).strip() == "":
        return True, ""
    val_str = str(value).strip()
    if val_str not in allowed_values:
        return False, f"{field_name}值无效: {val_str}，允许值: {', '.join(allowed_values)}"
    return True, ""


def validate_number(value: Any, field_name: str = "数值") -> Tuple[bool, str]:
    if value is None or str(value).strip() == "":
        return True, ""
    try:
        float(str(value).strip())
        return True, ""
    except (ValueError, TypeError):
        return False, f"{field_name}不是有效数字: {value}"


def mask_id_card(id_card: str) -> str:
    if id_card is None:
        return ""
    s = str(id_card).strip()
    if len(s) <= 6:
        return s
    if len(s) == 18:
        return s[:6] + "********" + s[-4:]
    elif len(s) == 15:
        return s[:6] + "******" + s[-3:]
    else:
        return s[:3] + "*" * (len(s) - 6) + s[-3:]


def mask_phone(phone: str) -> str:
    if phone is None:
        return ""
    s = str(phone).strip()
    if len(s) < 7:
        return s
    return s[:3] + "****" + s[-4:]


def mask_bank_card(card: str) -> str:
    if card is None:
        return ""
    s = str(card).strip().replace(" ", "")
    if len(s) <= 8:
        return s
    return s[:4] + " **** **** " + s[-4:]


def mask_salary(salary: Any) -> str:
    if salary is None or str(salary).strip() == "":
        return ""
    s = str(salary).strip()
    if len(s) <= 1:
        return "*"
    return "*" * (len(s) - 1) + s[-1]


def mask_email(email: str) -> str:
    if email is None:
        return ""
    s = str(email).strip()
    if "@" not in s:
        return s
    name, domain = s.split("@", 1)
    if len(name) <= 2:
        return "*" * len(name) + "@" + domain
    return name[0] + "*" * (len(name) - 2) + name[-1] + "@" + domain


def mask_value(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    name_lower = field_name.lower()
    if "身份证" in field_name or "id" in name_lower and "card" in name_lower:
        return mask_id_card(str(value))
    if "手机" in field_name or "phone" in name_lower:
        return mask_phone(str(value))
    if "银行" in field_name or "card" in name_lower:
        return mask_bank_card(str(value))
    if "薪资" in field_name or "工资" in field_name or "salary" in name_lower:
        return mask_salary(value)
    if "邮箱" in field_name or "email" in name_lower:
        return mask_email(str(value))
    s = str(value)
    if len(s) <= 2:
        return "*" * len(s)
    return s[0] + "*" * (len(s) - 2) + s[-1]
