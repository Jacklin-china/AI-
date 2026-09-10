"""W1 冒烟测试：验证开发环境与包结构就绪。

跑通这几个测试 = 虚拟环境、包可导入、依赖装好，三者都正常。
这是 W1 步骤 1 的验收物，后续每周的测试都加在 tests/ 下。
"""

import sys


def test_python_version():
    """项目要求 Python 3.11–3.12（见 pyproject 的 requires-python）。"""
    assert sys.version_info >= (3, 11), "项目要求 Python >= 3.11"
    assert sys.version_info < (3, 13), "项目禁用 3.13+ 独有语法（见 01 §2）"


def test_kantoku_package_importable():
    """src 布局 + __init__.py 生效后，kantoku 必须能被 import。"""
    import kantoku

    assert kantoku is not None


def test_subpackages_importable():
    """目录骨架齐备：config / core / agent / tools / perception / memory / shells / schemas。"""
    import importlib

    for name in ("config", "core", "agent", "tools", "perception", "memory", "shells", "schemas"):
        module = importlib.import_module(f"kantoku.{name}")
        assert module is not None, f"kantoku.{name} 无法导入"


def test_pydantic_v2_available():
    """结构化输出依赖 pydantic v2（K1）。"""
    import pydantic

    assert pydantic.VERSION.startswith("2."), "需要 pydantic v2"
