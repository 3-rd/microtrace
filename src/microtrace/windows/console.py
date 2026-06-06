"""Windows 终端兼容设置"""
from __future__ import annotations


def _setup_windows_console() -> None:
    """Windows 上启用 ANSI 颜色支持"""
    import sys
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7
            )
        except Exception:
            pass
