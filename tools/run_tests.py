"""跑全部測試。

原版的測試是照「每人各自獨立」寫的（直接看個人資料夾裡的檔案），所以這裡固定用 REA_TEAM_MODE=solo 跑；
全店共用的行為由 tests/test_team_mode.py 自己切成 shared 來測。兩種模式都會被測到。

    python tools/run_tests.py
"""
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    os.environ["REA_TEAM_MODE"] = "solo"
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
