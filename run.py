#!/usr/bin/env python3
"""一条命令启动预售系统并预置临界时刻样例。

用法：
  python3 run.py                # 重新播种并在 8000 端口启动
  PORT=8123 python3 run.py      # 自定义端口
  KEEP_DATA=1 python3 run.py    # 保留现有数据库不重新播种
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db, seed as seed_mod, server

DB_PATH = Path(__file__).resolve().parent / "instance" / "presale.db"
PORT = int(os.environ.get("PORT", "8000"))
RESEED = os.environ.get("KEEP_DATA") != "1"


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if RESEED and DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
    server.serve(str(DB_PATH), port=PORT, reseed=RESEED)


if __name__ == "__main__":
    main()
