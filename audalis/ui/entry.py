"""Application bootstrap."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("audalis")
    app.setApplicationDisplayName("Audalis Audio Control")
    app.setOrganizationName("audalis")
    app.setDesktopFileName("audalis")

    from .main import MainWindow

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())