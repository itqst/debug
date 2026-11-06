"""消息追踪页：逐条/按请求/检测链路、后台搜索和翻页、证据链、问题备注、诊断包导出"""

import json
import sqlite3
import sys
import threading
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit,
    QPlainTextEdit, QSplitter, QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from .. import __version__
from ..core import (
    detection_chains, DETECTION_METHODS, DETECTION_STAGES, diagnose, diagnostic_zip, export_preview, requests,
)
from ..registry import MODULES
from ..trace_view import TraceView
from ..transport import simulate_detection
from ..ui import (
    button, card, ChainModel, COLUMN_WIDTHS, EventModel, fade_to, FAILED, GREEN, label, ORANGE, PAGE_SIZE,
    PillDelegate, RED, REQUEST_KINDS, RequestModel, Segmented, StageDelegate, Switch,
)


class MessagesPage:
    """消息追踪页：逐条、按请求、检测链路三种视图，证据链、备注和诊断包导出"""

    def __init__(self, app):
        self.app = app
        self.current_event = None
        self.current_request = None
        self.current_chain = None
        self.scope = None
        self.search, self.export_running = None, False
        self.requests_drawn = None
        self.preview, self.export_dialog = None, None
        # 逐条视图翻页：栈里存每页之前那页的最小行号，空栈表示最新一页
        self.pages, self.page_rows = [], []

    def _message_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.failures_only = Switch("只看失败")
        self.failures_only.toggled.connect(self._refresh)
        self.view_mode = Segmented(["按请求", "逐条消息", "检测链路"])
        self.view_mode.changed.connect(self._view_changed)
        self.app._page_header(layout, "消息追踪", "", self.view_mode, self.failures_only, button("导出诊断包", self._export))
        self.filter_text = QLineEdit()
        self.filter_text.setObjectName("search")
        self.filter_text.setPlaceholderText("搜索错误、tid、Topic 或任意内容")
        # 大库里文本搜索要全表扫描，停手 300 ms 再查
        self.search_timer = QTimer(self.app)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self._first_page)
        self.filter_text.textChanged.connect(self.search_timer.start)
        search = QHBoxLayout()
        search.addWidget(self.filter_text, 1)
        self.search_hint = label("搜索中…", "caption")
        self.search_hint.hide()
        search.addWidget(self.search_hint)
        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.filter_module = Segmented(["全部"] + [m.upper() for m in MODULES] + ["其他"])
        self.filter_kind = Segmented(["全部", "指令", "ACK", "状态", "事件", "日志"])
        self.filter_method = QComboBox()
        self.filter_method.addItems(["全部方法"] + sorted({e["method"] for e in self.app.catalog}))
        self.filter_method.setMinimumWidth(190)
        self.filter_module.changed.connect(self._refresh)
        self.filter_kind.changed.connect(self._refresh)
        self.filter_method.currentTextChanged.connect(self._refresh)
        search.addWidget(self.filter_method)
        layout.addLayout(search)
        filters.addWidget(self.filter_module)
        filters.addStretch()
        filters.addWidget(self.filter_kind)
        self.scope_chip = button("", lambda: self.set_scope(None), "chip")
        self.scope_chip.setToolTip("点击回到全部消息")
        self.scope_chip.hide()
        filters.insertWidget(1, self.scope_chip)
        self.chain_demo = button("沙盒演示检测链路", self._demo_detection, "link")
        self.chain_demo.hide()
        filters.addWidget(self.chain_demo)
        layout.addLayout(filters)
        layout.addSpacing(4)

        split = QSplitter()
        split.setHandleWidth(14)
        table_card, tv = card(0, 6)
        self.model = EventModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(PillDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(38)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.clicked.connect(self._inspect)
        self.request_model = RequestModel()
        self.request_table = QTableView()
        self.request_table.setModel(self.request_model)
        self.request_table.setItemDelegate(PillDelegate(self.request_table))
        self.request_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.request_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.request_table.setShowGrid(False)
        self.request_table.setWordWrap(False)
        self.request_table.verticalHeader().hide()
        self.request_table.verticalHeader().setDefaultSectionSize(40)
        header = self.request_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.request_table.clicked.connect(self._inspect_request)
        self.request_table.doubleClicked.connect(self._open_request)
        self.chain_model = ChainModel()
        self.chain_table = QTableView()
        self.chain_table.setModel(self.chain_model)
        self.chain_table.setItemDelegate(StageDelegate(self.chain_table))
        self.chain_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.chain_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.chain_table.setShowGrid(False)
        self.chain_table.verticalHeader().hide()
        self.chain_table.verticalHeader().setDefaultSectionSize(40)
        header = self.chain_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.chain_table.clicked.connect(self._inspect_chain)
        for table in (self.table, self.request_table, self.chain_table):
            QShortcut(QKeySequence.Copy, table, self._copy_selection, context=Qt.WidgetShortcut)
            # 列宽固定初值、可拖动，不按内容定宽：按内容定宽要问遍前 1000 行每一格的尺寸（走 Python 委托），
            # 消息多时每次刷新、换外观都要卡好几秒
            header = table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.Interactive)
            for column, width in enumerate(COLUMN_WIDTHS[table.model().__class__.__name__]):
                header.resizeSection(column, width)
        self.views = QStackedWidget()
        self.views.addWidget(self.request_table)
        self.views.addWidget(self.table)
        self.views.addWidget(self.chain_table)
        tv.addWidget(self.views)
        pager = QHBoxLayout()
        pager.setContentsMargins(10, 4, 10, 4)
        self.page_label = label("", "caption")
        self.newer_btn = button("‹ 较新", self._newer_page, "link")
        self.older_btn = button("更早 ›", self._older_page, "link")
        pager.addWidget(self.page_label)
        pager.addStretch()
        pager.addWidget(self.newer_btn)
        pager.addWidget(self.older_btn)
        self.pager = QWidget()
        self.pager.setLayout(pager)
        tv.addWidget(self.pager)
        split.addWidget(table_card)
        detail_card, dv = card(8, 16)
        head = QHBoxLayout()
        head.addStretch()
        self.note_btn = button("备注这次请求", self._edit_note, "link")
        self.note_btn.setEnabled(False)
        head.addWidget(self.note_btn)
        dv.addLayout(head)
        self.details = TraceView("点一行，看这次请求的完整证据链；失败的请求排在最前")
        self.details.previous = self.app.store.previous
        self.details.original.connect(self.app._original)
        self.details.store, self.details.nearby_logs = self.app.store, True
        self.details.switched.connect(self._details_switched)
        self.details.nearby.connect(lambda events: self.app.logs._nearby_logs(events, self.app.MESSAGES))
        dv.addWidget(self.details)
        split.addWidget(detail_card)
        split.setSizes([760, 520])
        layout.addWidget(split, 1)
        return page

    def _refresh(self):
        self.app.refresh_due, self.app.refreshed = False, time.monotonic()
        if self.app.stack.currentIndex() != self.app.MESSAGES:
            # 消息页不可见时不重建它的表格（切过去时 _page 会刷一次），只更新控制结果和状态页
            self.app._refresh_tail()
            return
        module_index = self.filter_module.index()
        module = "" if module_index == 0 else "unknown" if module_index > len(MODULES) else MODULES[module_index - 1]
        kind = "" if self.filter_kind.index() == 0 else ["", "指令", "ACK", "状态", "事件", "日志"][self.filter_kind.index()]
        method = "" if self.filter_method.currentIndex() == 0 else self.filter_method.currentText()
        before, text = (self.pages[-1] if self.pages else None), self.filter_text.text()
        wanted = ((module, method, kind, text, before, self._requests_visible())
                  if not self.scope and text and self.app.store.on_disk else None)
        # 条件变了（含清空搜索、进入请求作用域、换页、换视图），在途的旧搜索作废，迟到的结果不再落表
        if self.search and self.search["params"] != wanted:
            self._cancel_search()
        if self.scope:
            # 请求作用域下仍可叠加其他筛选，只是不再跨设备/会话
            needle = self.filter_text.text().lower()
            rows = [(0, e) for e in reversed(self.app.store.groups([self.scope]).get(self.scope, []))
                    if (not module or e.module == module) and (not method or e.method == method)
                    and (not kind or e.kind == kind)
                    and (not needle or needle in json.dumps(e.clean(), ensure_ascii=False).lower())]
            self._show_rows(rows, None)
        else:
            if wanted:
                # 文本搜索在大库上要全表扫描，放到后台只读连接里做，结果回来再更新表格
                self._search_async(wanted)
            else:
                rows = self.app.store.query(module, method, kind, text, PAGE_SIZE, before)
                # 请求列表只在有新的指令/ACK 等或筛选变了时重算；实时状态再多也不重算旧请求
                drawn = (module, method, kind, text, self.failures_only.isChecked(), self.app.store.request_version)
                request_rows = None
                if self._requests_visible() and drawn != self.requests_drawn:
                    self.requests_drawn = drawn
                    request_rows = self.app.store.query(module, method, kind or REQUEST_KINDS, text, 3000)
                self._show_rows(rows, request_rows)
        if self.views.currentIndex() == 2 and self.app.stack.currentIndex() == self.app.MESSAGES:
            self._refresh_chains()
        self.app._refresh_tail()

    def _requests_visible(self):
        return self.views.currentIndex() == 0 and self.app.stack.currentIndex() == self.app.MESSAGES

    def _show_rows(self, rows, request_rows):
        """把查询结果放进逐条表和请求表；request_rows 为 None 时请求表不动"""
        # 翻页按未过滤的整页判断还有没有更早的
        self._update_pager(rows)
        if self.failures_only.isChecked():
            rows = [(i, e) for i, e in rows if diagnose(e).status in FAILED | {"契约待核"}]
        keep = self.current_event.event_id if self.current_event else None
        if request_rows is not None:
            self._refresh_requests(request_rows)
        self.model.replace(rows)
        # 重建模型会清掉选中行，按事件 ID 恢复，避免实时消息刷新时选择跳走
        for row, (_, e, _) in enumerate(self.model.rows):
            if e.event_id == keep:
                self.table.selectRow(row)
                break

    def _search_async(self, params):
        if self.search and self.search["params"] == params:
            # 同样的条件已在查，不因实时刷新反复重来
            return
        self._cancel_search()
        reader = self.app.store.reader()
        token = object()
        self.search = {"params": params, "reader": reader, "token": token}
        self.search_hint.show()
        module, method, kind, text, before, want_requests = params

        def work():
            try:
                rows = reader.query(module, method, kind, text, PAGE_SIZE, before)
                request_rows = reader.query(module, method, kind or REQUEST_KINDS, text, 3000) if want_requests else None
                self.app.inbox.put(("search", token, rows, request_rows))
            except sqlite3.OperationalError:
                # 被新的搜索打断
                pass
            finally:
                reader.db.close()

        threading.Thread(target=work, daemon=True).start()

    def _cancel_search(self):
        if not self.search:
            return
        try:
            self.search["reader"].db.interrupt()
        except sqlite3.ProgrammingError:
            # 上一次已经查完并关闭了连接
            pass
        self.search = None
        self.search_hint.hide()

    def _search_done(self, token, rows, request_rows):
        if not self.search or self.search["token"] is not token:
            return
        self.search = None
        self.search_hint.hide()
        self._show_rows(rows, request_rows)

    def _refresh_chains(self):
        """检测链路视图：取最近的检测相关消息按关联键并链，失败的排前"""
        chains = detection_chains([e for _, e in reversed(self.app.store.query_methods(DETECTION_METHODS, 5000))])
        keep = self.current_chain
        self.chain_model.replace(chains[:500])
        for row, chain in enumerate(self.chain_model.rows):
            if chain["events"][0].event_id == keep:
                self.chain_table.selectRow(row)
                break

    def _copy_selection(self):
        """Ctrl+C：逐条视图复制所选消息的完整 JSON；请求和检测链路复制结论与 tid，便于贴到工单"""
        view = self.views.currentIndex()
        if view == 1:
            rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
            text = "\n".join(json.dumps(self.model.rows[r][1].clean(), ensure_ascii=False) for r in rows)
        elif view == 0:
            rows = sorted({i.row() for i in self.request_table.selectionModel().selectedRows()})
            text = "\n".join(f"{r.events[0].sn}\t{r.method}\t{r.status}\t{r.summary}\ttid={r.events[0].tid}"
                             for r in (self.request_model.rows[i] for i in rows))
        else:
            rows = sorted({i.row() for i in self.chain_table.selectionModel().selectedRows()})
            text = "\n".join(f"{c['file']}\t{c['verdict']}" for c in (self.chain_model.rows[i] for i in rows))
        if text:
            QApplication.clipboard().setText(text)
            self.app.notify(f"已复制 {len(rows)} 行")

    def _inspect_chain(self, index):
        if not index.isValid():
            return
        chain = self.chain_model.rows[index.row()]
        self.current_chain = chain["events"][0].event_id
        stage = next((s for s in DETECTION_STAGES if chain["stages"][s][0] == "失败"), None)
        focus = chain["stages"][stage][2][-1] if stage else chain["events"][-1]
        color = RED if chain["failed"] else GREEN if chain["verdict"] == "全链路已观测" else ORANGE
        detail = "　".join(f"{s} {chain['stages'][s][0]}" for s in DETECTION_STAGES)
        self.details.show_trace(focus, chain["events"], headline=(chain["verdict"], color, detail,
                                "“未观测”只表示没看到对应消息，可能是没订阅到或日志里没有，不等于没发生"))
        self.note_btn.setEnabled(False)

    def _demo_detection(self):
        session = "demo-detect-" + self.app.session
        for stop, fail in (("云端事件", ""), ("FSVR 登记", ""), ("上传终态", "上传终态")):
            for event in simulate_detection("TEST_DOG_001", session, stop, fail):
                self.app.store.add(event)
        self.app.store.flush()
        self._refresh()

    def _update_pager(self, rows):
        self.pager.setVisible(self.views.currentIndex() == 1 and not self.scope)
        self.page_label.setText(f"第 {len(self.pages) + 1} 页 · 本页 {len(rows)} 条" +
                                ("　（最新一页随新消息刷新）" if not self.pages else ""))
        self.newer_btn.setEnabled(bool(self.pages))
        self.older_btn.setEnabled(len(rows) >= PAGE_SIZE)
        self.page_rows = rows

    def _older_page(self):
        self.pages.append(min(i for i, _ in self.page_rows))
        self._refresh()

    def _newer_page(self):
        self.pages.pop()
        self._refresh()

    def _first_page(self):
        self.pages = []
        self._refresh()

    def _refresh_requests(self, rows):
        found = requests(self.app.store, rows)
        if self.failures_only.isChecked():
            found = [r for r in found if r.status in FAILED | {"契约待核", "无 ACK"}]
        keep = self.current_request
        self.request_model.noted = set(self.app.store.notes())
        self.request_model.replace(found[:500])
        for row, r in enumerate(self.request_model.rows):
            if r.key == keep:
                self.request_table.selectRow(row)
                break

    def _view_changed(self, index):
        fade_to(self.views, index)
        self.scope_chip.setVisible(bool(self.scope) and index == 1)
        self.chain_demo.setVisible(index == 2)
        self._refresh()

    def _inspect_request(self, index):
        if not index.isValid():
            return
        self._trace_request(self.request_model.rows[index.row()])

    def _trace_request(self, r):
        self.current_request = r.key
        # 结论卡优先落在失败那条，否则落在最后一条回复
        focus = next((e for e in r.events if diagnose(e).status in FAILED), r.events[-1])
        self.details.show_trace(focus, r.events, self.app.store.note(r.key))
        self.note_btn.setEnabled(True)

    def _details_switched(self, key):
        """证据链换到别的来源那组时，备注写给那一组"""
        self.current_request = key
        self.note_btn.setEnabled(True)

    def _edit_note(self):
        """给当前请求写问题备注：预期、实际、备注、是否已复现；绑定完整 (设备, 会话, tid)"""
        key = self.current_request
        if not key:
            return
        note = self.app.store.note(key) or {}
        dialog = QDialog(self.app)
        dialog.setWindowTitle("问题备注")
        dialog.setMinimumWidth(480)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.addWidget(label("问题备注", "heading"))
        layout.addWidget(label(f"{key[0]} · tid {key[2]}", "caption"))
        fields = {}
        for caption, name in (("预期", "expected"), ("实际", "actual"), ("备注", "remark")):
            layout.addWidget(label(caption, "groupHeader"))
            edit = QPlainTextEdit(note.get(name, ""))
            edit.setObjectName("code")
            edit.setFixedHeight(64 if name != "remark" else 96)
            layout.addWidget(edit)
            fields[name] = edit
        reproduced = Switch("已复现")
        reproduced.setChecked(bool(note.get("reproduced")))
        layout.addWidget(reproduced)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", dialog.reject))
        actions.addWidget(button("保存", dialog.accept, "primary"))
        layout.addLayout(actions)
        self.note_dialog = dialog
        if dialog.exec() != QDialog.Accepted:
            return
        self.app.store.set_note(key, {**{k: w.toPlainText().strip() for k, w in fields.items()},
                                  "reproduced": reproduced.isChecked()})
        self._refresh()
        row = next((i for i, r in enumerate(self.request_model.rows) if r.key == key), None)
        if row is not None and not self.details.origin:
            self._inspect_request(self.request_model.index(row, 0))
        else:
            self.details.refresh()
        self.app.notify("备注已保存，会随诊断包导出")

    def _open_request(self, index):
        """双击请求切到逐条视图，只看这次请求（同设备、同会话、同 tid）的原始消息"""
        self.set_scope(self.request_model.rows[index.row()].key)
        self.view_mode.set_index(1)

    def _show_request(self, r):
        """从别处跳到逐条视图，只看这次请求的消息并显示证据链；按 tid 搜索会混进别的设备或会话里同 tid 的请求"""
        self.filter_text.clear()
        self.app._go(self.app.MESSAGES)
        self.view_mode.set_index(1)
        self.set_scope(r.key)
        self._trace_request(r)

    def set_scope(self, key):
        """限定逐条视图只显示一个请求；None 回到全部消息"""
        self.scope = key
        if key:
            sn, session, tid = key
            self.scope_chip.setText(f"仅此请求 · {sn} · {tid[:8]}…  ✕")
        self.scope_chip.setVisible(bool(key))
        self._refresh()

    def _inspect(self, index):
        if not index.isValid():
            return
        self.current_event = self.model.rows[index.row()][1]
        self.details.show_trace(self.current_event, self.app.store.trace(self.current_event))

    def _fill_preview(self, dialog, info):
        dialog.loading.hide()
        devices = "、".join(info["devices"][:6]) + ("…" if len(info["devices"]) > 6 else "") or "无"
        sources = "、".join(f"{k or '未知'} {v}" for k, v in info["sessions"].items()) or "无"
        for caption, text in (("消息", f"{info['events']} 条，全部写入"), ("请求", f"{info['requests']} 个"),
                              ("设备", devices), ("会话来源", sources),
                              ("已脱敏", f"{info['redacted']} 条消息含 [REDACTED]，入库时已处理，包里不会有原值"),
                              ("问题备注", f"{info['notes']} 条"),
                              ("不包含", "导入的原始日志文件、MQTT 密码")):
            row = QHBoxLayout()
            name = label(caption, "caption")
            name.setFixedWidth(64)
            row.addWidget(name)
            row.addWidget(label(text, wrap=True), 1)
            dialog.body.addLayout(row)
        if info["redacted_topics"]:
            dialog.body.addWidget(label("脱敏样例 Topic：" + "，".join(t or "无 Topic" for t in info["redacted_topics"]), "caption", True))
        dialog.confirm.setEnabled(True)
        dialog.info = info

    def _export(self):
        """先给出导出概览（内容全部来自已脱敏的库），确认后再选保存位置

        库文件上的概览统计要扫全表，在后台只读连接里做，对话框先显示“正在统计…”；取消即中断统计
        """
        dialog = QDialog(self.app)
        self.export_dialog = dialog
        dialog.setWindowTitle("导出诊断包")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(8)
        layout.addWidget(label("导出诊断包", "heading"))
        dialog.body = QVBoxLayout()
        dialog.body.setSpacing(8)
        dialog.loading = label("正在统计本地数据…", "caption")
        dialog.body.addWidget(dialog.loading)
        layout.addLayout(dialog.body)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", dialog.reject))
        dialog.confirm = button("选择位置并导出", dialog.accept, "primary")
        dialog.confirm.setEnabled(False)
        actions.addWidget(dialog.confirm)
        layout.addSpacing(6)
        layout.addLayout(actions)
        if self.app.store.on_disk:
            self.app.store.flush()
            reader, token = self.app.store.reader(), object()
            self.preview = {"reader": reader, "token": token}

            def work():
                try:
                    self.app.inbox.put(("preview", token, export_preview(reader)))
                except sqlite3.OperationalError:
                    # 用户取消，统计被中断
                    pass
                finally:
                    reader.db.close()

            threading.Thread(target=work, daemon=True).start()
        else:
            self._fill_preview(dialog, export_preview(self.app.store))
        accepted = dialog.exec() == QDialog.Accepted
        if self.preview:
            try:
                self.preview["reader"].db.interrupt()
            except sqlite3.ProgrammingError:
                pass
        self.preview, self.export_dialog = None, None
        if not accepted:
            return
        name = time.strftime("dog-debug-%Y%m%d-%H%M%S.zip")
        path, _ = QFileDialog.getSaveFileName(self.app, "导出诊断包", name, "诊断包 (*.zip)")
        if not path:
            return
        if not self.app.store.on_disk:
            result = self.export_bundle(path)
            self.app.notify(f"诊断包已导出：{result['events']} 条消息，{result['listed']}/{result['requests']} 个请求")
            return
        # 大库导出要几分钟，后台只读连接里写，界面照常可用
        self.app.store.flush()
        environment, focus = self._environment(), self.current_request
        self.export_running = True
        self.app.notify("正在后台导出诊断包…")

        def work():
            reader = self.app.store.reader()
            try:
                result = diagnostic_zip(reader, path, environment, focus)
                self.app.inbox.put(("export_done", f"诊断包已导出：{result['events']} 条消息，{result['listed']}/{result['requests']} 个请求"))
            except Exception as error:
                self.app.inbox.put(("export_done", "导出失败：" + str(error)))
            finally:
                reader.db.close()

        threading.Thread(target=work, daemon=True).start()

    def export_bundle(self, path):
        return diagnostic_zip(self.app.store, path, self._environment(), self.current_request)

    def _environment(self):
        return {"version": __version__, "mode": self.app.mode, "platform": sys.platform,
                "broker": f"{self.app.mqtt.host}:{self.app.port.value()}" if self.app.mqtt.connected else "",
                "device_sn": self.app.mqtt.sn if self.app.mqtt.connected else self.app.sn.text().strip(),
                "model": self.app.scene.variant, "osd": self.app.scene.osd}

    def shown(self):
        self._refresh()

    def _preview_done(self, token, rows):
        # 导出窗口已经关了或又开了一次，旧的预览结果不再填
        if self.preview and self.preview["token"] is token and self.export_dialog:
            self._fill_preview(self.export_dialog, rows)

    def _export_done(self, note):
        self.export_running = False
        self.app.notify(note)
