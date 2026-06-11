from setuptools import setup, find_packages

setup(
    name="hr-batch-tool",
    version="1.0.0",
    description="HR 批量档案命令行工具 - 统一整理各分公司员工资料",
    packages=find_packages(),
    install_requires=[
        "pandas>=2.0.0",
        "openpyxl>=3.1.0",
        "click>=8.1.0",
        "PyYAML>=6.0",
        "python-dateutil>=2.8.0",
    ],
    entry_points={
        "console_scripts": [
            "hr=hr_tool.cli:main",
        ],
    },
    python_requires=">=3.9",
)
