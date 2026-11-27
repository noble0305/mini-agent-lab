"""S05 CLI 入口：`python -m s05_service.main`。"""

from __future__ import annotations

from s05_service.app import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()