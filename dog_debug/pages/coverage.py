"""协议覆盖页：协议目录的方法、来源和待核差异"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..registry import MODULES
from ..ui import card, GRAY, MODULE_COLORS, PillDelegate


class CoveragePage:
    """协议覆盖页：指令目录、来源和待核差异"""

    def __init__(self, app):
        self.app = app

    def _coverage_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        counts = "  ·  ".join(f"{m.upper()} {sum(x['module'] == m for x in self.app.catalog)}" for m in MODULES)
        self.app._page_header(layout, "协议覆盖", f"共 {len(self.app.catalog)} 条：{counts}　　双击控制指令可直接打开")
        search = QLineEdit()
        search.setObjectName("search")
        search.setPlaceholderText("搜索方法、模块或来源")
        layout.addWidget(search)
        layout.addSpacing(4)
        box, bv = card(0, 6)
        table = QTableWidget(len(self.app.catalog), 5)
        table.setHorizontalHeaderLabels(["模块", "方法", "类型", "来源", "待核差异"])
        table.setItemDelegate(PillDelegate(table))
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setShowGrid(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(36)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for row, item in enumerate(self.app.catalog):
            for column, value in enumerate((item["module"].upper(), item["method"], item["category"],
                                            item["source"], item["unverified"])):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.UserRole, MODULE_COLORS.get(item["module"], GRAY))
                table.setItem(row, column, cell)

        def apply(text):
            needle = text.lower()
            for row, item in enumerate(self.app.catalog):
                haystack = " ".join((item["module"], item["method"], item["category"], item["source"])).lower()
                table.setRowHidden(row, bool(needle) and needle not in haystack)

        search.textChanged.connect(apply)
        table.cellDoubleClicked.connect(lambda row, _: self.app.control._open_command(self.app.catalog[row]["method"]))
        bv.addWidget(table)
        layout.addWidget(box, 1)
        return page
