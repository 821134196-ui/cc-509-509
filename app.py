"""一条命令启动：python app.py [--reseed] [--port 8000]

首次运行自动建库并预置临界时刻样例；--reseed 可重置演示数据。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from presale.seed import seed
from presale.web import serve

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presale.db")


def main():
    ap = argparse.ArgumentParser(description="预售名额管理演示（SQLite + 模拟支付）")
    ap.add_argument("--reseed", action="store_true", help="清空并重新预置样例")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    if args.reseed or not os.path.exists(DB_PATH):
        seed(DB_PATH)
    serve(DB_PATH, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
