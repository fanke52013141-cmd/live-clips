# 仓库根 conftest：保证 pytest 将仓库根加入 sys.path，使 `import studio` 可用。

# ---------------------------------------------------------------------------
# 测试数据隔离（防误伤开发库）
#
# 背景：后端默认 DATABASE_URL=sqlite:///./data/autoclip.db（相对进程 cwd）。
# 直接在仓库根跑 pytest 时，若不显式隔离，部分测试会读写/清理**开发库**
# （2026-11-14 实测：全量回归清空了 data/autoclip.db 的项目记录）。
# 因此：pytest 进程内若未显式设置隔离变量，自动指向一次性临时目录，
# 保证「跑测试 = 永远不碰开发数据」。显式设置过的（CI/eval 回放）不覆盖。
# ---------------------------------------------------------------------------

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if not os.environ.get("AUTOCLIP_DATA_DIR"):
    _test_data_dir = os.path.join(tempfile.gettempdir(), "autoclip-pytest-data")
    os.environ["AUTOCLIP_DATA_DIR"] = _test_data_dir
if not os.environ.get("DATABASE_URL"):
    _db_dir = os.path.join(_test_data_dir, "data")
    os.makedirs(_db_dir, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{_db_dir.replace(os.sep, '/')}/autoclip.db"
