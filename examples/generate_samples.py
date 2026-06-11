import os
import pandas as pd
import random

random.seed(42)

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

def make_id_card(i):
    region = "310101"
    year = 1980 + (i % 25)
    month = f"{(i % 12) + 1:02d}"
    day = f"{(i % 27) + 1:02d}"
    seq = f"{i:03d}"
    base = f"{region}{year}{month}{day}{seq}"
    weights = [7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2]
    codes = ["1","0","X","9","8","7","6","5","4","3","2"]
    total = sum(int(base[j]) * weights[j] for j in range(17))
    check = codes[total % 11]
    return base + check

surnames = ["张","王","李","赵","刘","陈","杨","黄","周","吴","徐","孙","马","朱","胡","林","郭","何","高","罗"]
names = ["伟","芳","娜","敏","静","丽","强","磊","军","洋","勇","艳","杰","涛","明","超","秀英","霞","平","刚"]
departments = ["技术研发部","市场营销部","人力资源部","财务部","行政管理部","产品运营部","客户服务部"]
positions_map = {
    "技术研发部": ["高级工程师","工程师","架构师","测试工程师","前端开发","后端开发"],
    "市场营销部": ["市场经理","市场专员","品牌经理","渠道经理"],
    "人力资源部": ["HR经理","招聘专员","薪酬专员","培训专员"],
    "财务部": ["财务总监","会计","出纳","税务专员"],
    "行政管理部": ["行政经理","行政专员","前台","司机"],
    "产品运营部": ["产品经理","运营经理","运营专员","数据分析师"],
    "客户服务部": ["客服主管","客服专员","售后工程师"],
}
branches = ["上海总部","北京分公司","深圳分公司","广州分公司"]

def random_name(i):
    s = random.choice(surnames)
    n = random.choice(names) if random.random() < 0.5 else random.choice(names) + random.choice(names)
    return s + n

def random_phone(i):
    prefixes = ["138","139","186","188","158","159","135","136","137","189","177","176"]
    return random.choice(prefixes) + f"{random.randint(1000,9999):04d}{random.randint(1000,9999):04d}"

def gen_roster(n, prefix, with_errors=False):
    rows = []
    for i in range(n):
        dept = random.choice(departments)
        pos = random.choice(positions_map[dept])
        year = 2015 + (i % 10)
        month = (i % 12) + 1
        day = (i % 27) + 1
        join_date = f"{year}-{month:02d}-{day:02d}"
        emp_no = f"{prefix}{i+1:05d}"
        row = {
            "员工编号": emp_no,
            "姓名": random_name(i),
            "身份证号": make_id_card(i),
            "手机号": random_phone(i),
            "性别": random.choice(["男","女"]),
            "出生日期": f"{1980 + (i % 25)}-{month:02d}-{day:02d}",
            "入职日期": join_date,
            "离职日期": "",
            "部门": dept,
            "岗位": pos,
            "职级": random.choice(["P4","P5","P6","P7","P8","M1","M2"]),
            "分公司": random.choice(branches),
            "邮箱": f"user{i+1}@company.com",
            "紧急联系人": random_name(i+100),
            "紧急联系电话": random_phone(i+100),
            "薪资": f"{random.randint(8000, 50000)}",
            "银行卡号": f"622202{random.randint(1000000000, 9999999999):010d}",
        }
        rows.append(row)
    if with_errors:
        rows[0]["身份证号"] = "31010119900101123"
        rows[1]["手机号"] = "12345678901"
        rows[2]["姓名"] = ""
        rows[3]["员工编号"] = rows[4]["员工编号"]
        rows[5]["身份证号"] = rows[6]["身份证号"]
        rows[7]["入职日期"] = "2023/13/01"
        rows[8]["邮箱"] = "bad-email"
    return pd.DataFrame(rows)

branch_a = gen_roster(20, "SH", with_errors=True)
branch_b = gen_roster(15, "BJ")
branch_c = gen_roster(10, "SZ")

branch_a.to_excel(os.path.join(OUT_DIR, "上海分公司_花名册.xlsx"), index=False)
branch_b.to_excel(os.path.join(OUT_DIR, "北京分公司_花名册.xlsx"), index=False)
branch_c.to_csv(os.path.join(OUT_DIR, "深圳分公司_花名册.csv"), index=False, encoding="utf-8-sig")

q1 = gen_roster(30, "EMP")
q2 = q1.copy()
q2 = q2.drop(index=[2, 5, 8]).reset_index(drop=True)
new_rows = []
for i in range(5):
    dept = random.choice(departments)
    pos = random.choice(positions_map[dept])
    row = {
        "员工编号": f"EMP{100+i:05d}",
        "姓名": random_name(200+i),
        "身份证号": make_id_card(200+i),
        "手机号": random_phone(200+i),
        "性别": random.choice(["男","女"]),
        "出生日期": f"199{(i%5)}-02-15",
        "入职日期": "2026-01-15",
        "离职日期": "",
        "部门": dept,
        "岗位": pos,
        "职级": random.choice(["P4","P5","P6"]),
        "分公司": random.choice(branches),
        "邮箱": f"newuser{i+1}@company.com",
        "紧急联系人": random_name(300+i),
        "紧急联系电话": random_phone(300+i),
        "薪资": f"{random.randint(10000, 30000)}",
        "银行卡号": f"622202{random.randint(1000000000, 9999999999):010d}",
    }
    new_rows.append(row)
q2 = pd.concat([q2, pd.DataFrame(new_rows)], ignore_index=True)
q2.loc[q2["员工编号"] == q2.iloc[0]["员工编号"], "部门"] = "产品运营部"
q2.loc[q2["员工编号"] == q2.iloc[1]["员工编号"], "岗位"] = "高级产品经理"
q2.loc[q2["员工编号"] == q2.iloc[3]["员工编号"], "薪资"] = "35000"
q2.loc[2, "离职日期"] = "2026-03-15"

q1.to_excel(os.path.join(OUT_DIR, "Q1_花名册.xlsx"), index=False)
q2.to_excel(os.path.join(OUT_DIR, "Q2_花名册.xlsx"), index=False)

mapping = {"旧字段_姓名": "姓名", "旧字段_身份证": "身份证号", "旧字段_手机": "手机号"}
with open(os.path.join(OUT_DIR, "field_mapping.yaml"), "w", encoding="utf-8") as f:
    for k, v in mapping.items():
        f.write(f"{k}: {v}\n")

print("示例数据生成完毕！")
print(f"  - 上海分公司_花名册.xlsx (20人，含错误)")
print(f"  - 北京分公司_花名册.xlsx (15人)")
print(f"  - 深圳分公司_花名册.csv (10人)")
print(f"  - Q1_花名册.xlsx (30人)")
print(f"  - Q2_花名册.xlsx (32人，用于compare)")
print(f"  - field_mapping.yaml (字段映射样例)")
