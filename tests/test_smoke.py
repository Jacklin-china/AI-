"""W1 冒烟测试：验证开发环境就绪。

这个测试跑通 = uv 环境、依赖安装、pytest 三者全部正常。
以后每加一个新模块，都来这里对应加测试文件（test_xxx.py）。
"""

import sys


def test_python_version():
    """项目要求 Python >= 3.11（见 pyproject.toml requires-python）。"""
    assert sys.version_info >= (3, 11), f"Python 版本过低: {sys.version}"


def test_core_deps_importable():
    """核心依赖必须能导入，且 pydantic 必须是 v2（结构化输出的地基）。"""
    import pydantic

    assert pydantic.VERSION.startswith("2."), f"需要 pydantic v2，实际: {pydantic.VERSION}"
