"""支持 python -m bili_fans 直接运行。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
