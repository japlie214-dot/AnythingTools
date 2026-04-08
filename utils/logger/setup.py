# utils/logger/setup.py
import sys
try:
    from colorama import just_fix_windows_console
except ImportError:
    def just_fix_windows_console() -> None:
        return None
just_fix_windows_console()
