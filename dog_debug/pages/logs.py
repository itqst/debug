"""日志排障页：导入本地日志、从设备取日志、实时跟随，按批次、时间、模块筛选并看详情"""

import json
import queue
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QDateTime, QStandardPaths, Qt, QTime, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QSplitter, QStackedWidget, QTableView,
    QVBoxLayout, QWidget,
)

from ..core import diagnose, occurred, Store, summarize
from ..logs import first_object, import_log, log_event
from ..registry import MODULES
from ..remote import LOG_DIRS, LOG_ROOT, pick_files
from ..trace_view import TraceView
from ..ui import (
    button, card, COLUMN_WIDTHS, FAILED, IMPORT_BATCH, label, LIVE_KEEP, LOG_PERIODS, LOG_RANGES, LOG_ROWS, LogModel,
    NEARBY, PillDelegate, Switch,
)


class LogsPage:
    """日志排障页：导入和取设备日志、实时跟随、按批次和时间筛、详情和原文附近"""

    def __init__(self, app):
        self.app = app
        self.import_thread = None

    def _logs_page(self):
        """导入后就在本页看：上面选批次和筛选，左边逐条日志，右边详情；只看导入的批次，不混实时和沙盒消息"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        # 设备日志经 SSH 直接取（远程走 FRP，局域网连狗的 IP），取下来留在本机一份并自动导入；实时跟随把新写的行逐条加进来
        self.live_follow = Switch("实时跟随")
        self.live_follow.toggled.connect(self._toggle_follow)
        self.fetch_logs = button("从设备取日志…", self._ask_device_logs)
        self.app._page_header(layout, "日志排障", "导入后在这里直接看：搜索、筛模块或只看错误，点一行看全文、原文附近和对应请求",
                          self.live_follow, self.fetch_logs, button("导出诊断包…", self.app.diagnostics.export),
                          button("选择文件…", self._import, "primary"))
        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.log_batch = QComboBox()
        # 批次名带文件名和条数，按内容定宽会把搜索框挤没；下拉列表里仍显示全名
        self.log_batch.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.log_batch.setMinimumContentsLength(18)
        self.log_batch.setPlaceholderText("还没有导入")
        self.log_search = QLineEdit()
        self.log_search.setObjectName("search")
        self.log_search.setPlaceholderText("搜索原文、tid")
        self.log_search.setClearButtonEnabled(True)
        self.log_module = QComboBox()
        self.log_module.addItem("全部模块", "")
        for module in MODULES:
            self.log_module.addItem(module.upper(), module)
        self.log_module.addItem("未识别", "unknown")
        self.log_errors = Switch("只看错误")
        self.log_count = label("", "caption")
        # 按日志里写的发生时间筛；“选时间段…”时下面出现起止两格
        self.log_period = QComboBox()
        self.log_period.addItems(list(LOG_RANGES))
        for widget in (self.log_batch, self.log_period, self.log_search, self.log_module, self.log_errors):
            tools.addWidget(widget, 1 if widget is self.log_search else 0)
        tools.addWidget(self.log_count)
        layout.addLayout(tools)
        self.log_custom = QWidget()
        custom = QHBoxLayout(self.log_custom)
        custom.setContentsMargins(0, 4, 0, 0)
        today = QDateTime.currentDateTime()
        self.log_start = QDateTimeEdit(QDateTime(today.date(), QTime(0, 0)))
        self.log_end = QDateTimeEdit(today)
        for edit in (self.log_start, self.log_end):
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            edit.setCalendarPopup(True)
            edit.dateTimeChanged.connect(lambda: self.log_timer.start())
        custom.addWidget(label("原文时间从", "caption"))
        custom.addWidget(self.log_start)
        custom.addWidget(label("到", "caption"))
        custom.addWidget(self.log_end)
        custom.addStretch()
        layout.addWidget(self.log_custom)
        self.log_custom.hide()
        # 从请求跳来看附近日志时出现：说明怎么圈出来的，可以回到原来的请求
        self.nearby_bar = QWidget()
        bar = QHBoxLayout(self.nearby_bar)
        bar.setContentsMargins(0, 4, 0, 0)
        self.nearby_text = label("", "caption", True)
        bar.addWidget(self.nearby_text, 1)
        bar.addWidget(button("从设备取日志…", lambda: self._ask_device_logs(*self.nearby_fetch), "link"))
        bar.addWidget(button("‹ 回到请求", self._nearby_back, "link"))
        bar.addWidget(button("✕", self.nearby_bar.hide, "link"))
        layout.addWidget(self.nearby_bar)
        self.nearby_bar.hide()
        self.nearby_page, self.nearby_fetch, self.log_want = None, ((), None), None
        self.log_period.currentTextChanged.connect(self._log_period_changed)
        # 搜索按停顿触发，不是每敲一个字就查一遍库
        self.log_timer = QTimer(self.app)
        self.log_timer.setSingleShot(True)
        self.log_timer.setInterval(300)
        self.log_timer.timeout.connect(self._log_refresh)
        self.log_search.textChanged.connect(self.log_timer.start)
        self.log_batch.currentIndexChanged.connect(self._log_refresh)
        self.log_module.currentIndexChanged.connect(self._log_refresh)
        self.log_errors.toggled.connect(self._log_refresh)
        self.import_bar = QProgressBar()
        self.import_bar.setRange(0, 1000)
        self.import_bar.setTextVisible(False)
        self.import_bar.setFixedHeight(4)
        self.import_state = label("", "caption")
        # 进度条、进度文字和停止按钮只在导入时出现
        self.import_row = QWidget()
        progress = QHBoxLayout(self.import_row)
        progress.setContentsMargins(0, 2, 0, 2)
        progress.addWidget(self.import_bar, 1)
        progress.addWidget(self.import_state)
        progress.addWidget(button("停止导入", self.app.import_cancel.set, "link"))
        layout.addWidget(self.import_row)
        self.import_row.hide()
        layout.addSpacing(4)

        # 没有导入批次时中间是拖入提示，有了就是日志表和详情
        self.log_views = QStackedWidget()
        drop, dv = card(10, 36)
        dv.addStretch()
        for widget in (label("把日志文件拖到窗口任意位置", "heading"),
                       label("支持 .log / .txt / JSONL、多行 JSON 和截断行；原文件不会被修改", "caption")):
            widget.setAlignment(Qt.AlignCenter)
            dv.addWidget(widget)
        dv.addStretch()
        self.log_views.addWidget(drop)
        split = QSplitter()
        split.setHandleWidth(14)
        table_card, tv = card(0, 6)
        self.log_model = LogModel()
        self.log_table = QTableView()
        self.log_table.setModel(self.log_model)
        self.log_table.setItemDelegate(PillDelegate(self.log_table))
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.log_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.log_table.setShowGrid(False)
        self.log_table.setWordWrap(False)
        self.log_table.verticalHeader().hide()
        self.log_table.verticalHeader().setDefaultSectionSize(34)
        header = self.log_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(QHeaderView.Interactive)
        for column, width in enumerate(COLUMN_WIDTHS["LogModel"]):
            header.resizeSection(column, width)
        self.log_table.clicked.connect(self._log_inspect)
        QShortcut(QKeySequence.Copy, self.log_table, self._copy_log, context=Qt.WidgetShortcut)
        tv.addWidget(self.log_table)
        split.addWidget(table_card)
        detail_card, dc = card(0, 6)
        self.log_detail = TraceView("点左边一行看完整原文；带 tid 的行会列出这次请求的所有消息")
        self.log_detail.original.connect(self.app._original)
        self.log_detail.store = self.app.store
        dc.addWidget(self.log_detail)
        split.addWidget(detail_card)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 3)
        self.log_views.addWidget(split)
        layout.addWidget(self.log_views, 1)

        # 导入记录平时收起，要看编码、sha256、耗时再展开
        foot = QHBoxLayout()
        self.history_btn = button("导入记录 ▸", self._toggle_history, "link")
        foot.addWidget(self.history_btn)
        foot.addStretch()
        self.store_size = label("", "caption")
        foot.addWidget(self.store_size)
        foot.addWidget(button("清空本地数据", self._clear_store, "link"))
        layout.addLayout(foot)
        self.import_info = QPlainTextEdit()
        self.import_info.setObjectName("plain")
        self.import_info.setReadOnly(True)
        self.import_info.setFixedHeight(120)
        self.import_info.setPlaceholderText("还没有导入。导入时的设备 SN 取左侧连接设置，混合设备的日志请分批导入")
        self.import_info.hide()
        layout.addWidget(self.import_info)
        self.log_version, self.log_selected, self.log_loaded, self.log_undated = 0, None, False, 0
        self.fetching, self.follow_stops, self.live_session, self.live_lines = False, [], "", {}
        return page

    def _ssh_for_logs(self):
        """后台线程里取一条 SSH：已经远程连着就用那条，否则按当前填写新开一条（用完由调用方关）"""
        if self.app.ssh and self.app.ssh.alive:
            return self.app.ssh, False
        return self.app.connection._open_ssh(self.app.frp.isChecked())[0], True

    def _ask_device_logs(self, modules=(), since=None):
        """选模块和时间段，从设备的 /userdata/gw_logs 取日志；modules 不为空时只预先勾这些模块，since 为时间戳时预选“这之后写过的文件”"""
        if self.fetching or self.import_thread and self.import_thread.is_alive():
            self.app.notify("上一批还在取或导入，可先停止")
            return
        dialog = QDialog(self.app)
        dialog.setWindowTitle("从设备取日志")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 16)
        target = f"经 FRP 连 {self.app.sn.text().strip()}" if self.app.ssh or self.app.frp.isChecked() else f"连 {self.app.host.text().strip()}:22"
        layout.addWidget(label(f"{target}，读 {LOG_ROOT} 下各模块的日志文件；只读，不改设备上的任何东西", "caption", True))
        boxes = {}
        row = QHBoxLayout()
        for folder, module in LOG_DIRS.items():
            boxes[folder] = QCheckBox(module.upper())
            boxes[folder].setChecked(not modules or module in modules)
            row.addWidget(boxes[folder])
        layout.addLayout(row)
        periods = {**({time.strftime("%m-%d %H:%M:%S 之后写过的文件", time.localtime(since)): since} if since else {}),
                   **{text: time.time() - seconds if seconds else None for text, seconds in LOG_PERIODS.items()}}
        period = QComboBox()
        period.addItems(list(periods))
        layout.addWidget(period)
        layout.addWidget(label("按文件修改时间挑整个文件，取到的会包含这段时间前后的其他记录", "caption", True))
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(button("取消", dialog.reject))
        buttons.addWidget(button("取日志", dialog.accept, "primary"))
        layout.addLayout(buttons)
        if dialog.exec() == QDialog.Accepted:
            folders = [f for f, box in boxes.items() if box.isChecked()]
            self.fetch_device_logs(folders, periods[period.currentText()])

    def fetch_device_logs(self, folders, since):
        """后台列文件、按时间段挑、SFTP 读到本机数据目录，读完作为一批导入；取消时已读完的照样导入"""
        self.fetching = True
        self.app.import_cancel.clear()
        sn = self.app.sn.text().strip()
        base = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / "设备日志" / sn

        def work():
            remote, own, paths = None, False, []
            try:
                remote, own = self._ssh_for_logs()
                jobs = [(folder, name, size) for folder in folders
                        for name, size, _ in pick_files(remote.list_logs(folder), since)]
                total, done = sum(size for *_, size in jobs) or 1, 0
                for folder, name, size in jobs:
                    if self.app.import_cancel.is_set():
                        break
                    path = base / folder / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    remote.download(folder, name, str(path), lambda got: self.app.inbox.put(("fetch", (done + got, total, name))),
                                    self.app.import_cancel.is_set)
                    done += size
                    paths.append(str(path))
                self.app.inbox.put(("fetch_done", paths, f"从设备取到 {len(paths)} 个日志文件（{done / 1e6:.1f} MB），存在 {base}"))
            except Exception as error:
                # 代理、网络、认证、目录不存在都可能，原样告诉用户；已经取下来的照样导入
                self.app.inbox.put(("fetch_done", paths, f"从设备取日志失败：{error}"))
            finally:
                if own and remote:
                    remote.close()

        threading.Thread(target=work, daemon=True).start()
        self.import_bar.setValue(0)
        self.import_row.show()
        self.import_state.setText("正在连接设备…")
        self.app._go(self.app.LOGS)

    def _fetch_done(self, paths, note):
        self.fetching = False
        self.import_info.appendPlainText(time.strftime("%H:%M:%S  ") + note)
        self.app.notify(note)
        if paths:
            self.start_import(paths)
        else:
            self.import_row.hide()

    def _toggle_follow(self, on):
        if not on:
            self._stop_follow()
            return
        self.live_session = "import-live-" + str(uuid.uuid4())
        self.live_lines = {}
        sn, session = self.app.sn.text().strip(), self.live_session

        def work():
            try:
                remote, own = self._ssh_for_logs()
                for folder in LOG_DIRS:
                    self.follow_stops.append(remote.follow(folder, lambda text, f=folder: self.app.inbox.put(("log_line", (f, text, sn, session)))))
                if own:
                    # 这条 SSH 只给实时跟随用，停的时候一起关
                    self.follow_stops.append(remote.close)
                self.app.inbox.put(("notice", "实时跟随已开始：设备上新写的日志会逐条出现在“实时跟随”这一批里"))
            except Exception as error:
                self.app.inbox.put(("follow_error", f"实时跟随没开起来：{error}"))

        threading.Thread(target=work, daemon=True).start()

    def _stop_follow(self):
        stops, self.follow_stops = self.follow_stops, []
        for stop in stops:
            stop()
        if self.live_follow.isChecked():
            self.live_follow.blockSignals(True)
            self.live_follow.setChecked(False)
            self.live_follow.blockSignals(False)
        # 停了以后这一批名字改成起止时间和条数
        if stops and self.log_loaded:
            self._log_batches()

    def _live_line(self, folder, text, sn, session):
        """实时跟随的一行入库；这一批第一次有内容时切过去，之后停在它上面就随新行刷新"""
        if session != self.live_session or not text.strip():
            return
        number = self.live_lines[folder] = self.live_lines.get(folder, 0) + 1
        value = first_object(text) if "{" in text else None
        event = log_event(text, value, f"{LOG_ROOT}/{folder}/tta.log", "live:" + folder, sn, session, number, 0)
        self.app.store.add(event)
        if sum(self.live_lines.values()) == 1:
            self.app.store.flush()
            self._log_batches(newest=True)
        elif (self.log_batch.currentData() or ("", "", 0))[:2] in ((sn, session), (sn, "")):
            self.log_timer.start()

    def _copy_log(self):
        """日志表 Ctrl+C：复制选中行的完整原文（没有原文时为 JSON）"""
        rows = sorted({i.row() for i in self.log_table.selectionModel().selectedRows()})
        text = "\n".join((e.raw or json.dumps(e.payload, ensure_ascii=False)).rstrip("\r\n")
                         for e in (self.log_model.rows[r][1] for r in rows))
        if text:
            QApplication.clipboard().setText(text)
            self.app.notify(f"已复制 {len(rows)} 行")

    def _toggle_history(self):
        shown = not self.import_info.isVisible()
        self.import_info.setVisible(shown)
        self.history_btn.setText("导入记录 ▾" if shown else "导入记录 ▸")

    def _log_batches(self, newest=False):
        """后台列出导入批次；newest 为真时切到最新一批（刚导入完）。超过 LIVE_KEEP 的实时跟随批次挑出来交给主线程删"""
        live = self.live_session if self.follow_stops else ""

        def work(reader):
            batches, expired = [], []
            for sn, session, count, first, last in reader.import_batches():
                head, tail = reader.event_at(first), reader.event_at(last)
                when = time.strftime("%m-%d %H:%M", time.localtime(head.timestamp))
                if session == live:
                    # 正在跟随的这一批一直在长，名字里不写条数
                    text = f"实时跟随中 · {when} 开始"
                elif session.startswith("import-live-"):
                    if time.time() - tail.timestamp > LIVE_KEEP:
                        expired.append((sn, session))
                        continue
                    text = f"实时跟随 · {when}–{time.strftime('%H:%M', time.localtime(tail.timestamp))} · {count} 条"
                else:
                    text = Path(head.file).name + (" 等" if tail.file != head.file else "") + f" · {count} 条 · {when} 导入"
                batches.append((text, (sn, session, count)))
            return batches, expired
        self.app._background(work, "log_batches", newest)

    def _log_batches_done(self, result, newest):
        batches, expired = result
        # 写库只在主线程这条连接上做，后台读连接只负责挑
        for sn, session in expired:
            self.app.store.drop_session(sn, session)
        current = self.log_batch.currentData()
        self.log_batch.blockSignals(True)
        self.log_batch.clear()
        # 第一项是这台设备导入过的全部日志，按时间段查故障时不用先猜在哪一批；从请求跳来时是那个请求的设备
        want, self.log_want = self.log_want, None
        sn = want[0] if want else self.app.sn.text().strip()
        mine = sum(key[2] for _, key in batches if key[0] == sn)
        if mine:
            batches = [(f"{sn} 全部日志 · {mine} 条", (sn, "", mine))] + batches
        for text, key in batches:
            self.log_batch.addItem(text, key)
        keys = [key[:2] for _, key in batches]
        keep = current and current[:2] in keys and not newest
        # 刚导入完切到新的那一批（在“全部日志”后面）
        fresh = 1 if mine and len(batches) > 1 else 0
        if want:
            # 这台设备没有日志时不选别的设备的批次，留空并提示去取
            self.log_batch.setCurrentIndex(keys.index(want) if want in keys else -1)
        else:
            self.log_batch.setCurrentIndex(keys.index(current[:2]) if keep else fresh if batches else -1)
        self.log_batch.blockSignals(False)
        self.log_views.setCurrentIndex(1 if batches else 0)
        self.log_loaded = True
        self._log_refresh()

    def _log_period_changed(self, text):
        self.log_custom.setVisible(LOG_RANGES[text] == "custom")
        self._log_refresh()

    def _log_range(self):
        """当前时间段的起止（"YYYY-MM-DD HH:MM:SS"），全部时间为两个空串"""
        choice = LOG_RANGES[self.log_period.currentText()]
        stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        if choice is None:
            return "", ""
        if choice == "custom":
            return (self.log_start.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
                    self.log_end.dateTime().toString("yyyy-MM-dd HH:mm:ss"))
        if choice == "today":
            return time.strftime("%Y-%m-%d 00:00:00"), stamp(time.time())
        return stamp(time.time() - choice), stamp(time.time())

    def _log_refresh(self):
        """按批次、时间段和筛选在后台读日志；批次和时间段在 SQL 里限定，错误在后台边读边筛，最多列 LOG_ROWS 条"""
        batch = self.log_batch.currentData()
        self.log_version += 1
        if not batch:
            self.log_model.replace([])
            self.log_count.setText("还没有这台设备的日志" if not self.nearby_bar.isHidden() else "")
            return
        sn, session, total = batch
        module, text, errors = self.log_module.currentData(), self.log_search.text().strip(), self.log_errors.isChecked()
        start, end = self._log_range()

        def work(reader):
            rows, more = [], False
            # 按时间筛时，原文没有日期的行算不出落在哪，告诉用户有多少没算进去
            self.log_undated = reader.undated_logs(sn, session, module, text) if start or end else 0
            for row_id, e in reader.log_rows(sn, session, module, text, start, end):
                d = diagnose(e)
                if errors and d.status not in FAILED:
                    continue
                if len(rows) == LOG_ROWS:
                    more = True
                    break
                rows.append((row_id, e, d))
            return rows, more, total
        self.app._background(work, "log_rows", self.log_version)

    def _log_rows_done(self, result, version):
        if version != self.log_version:
            return
        rows, more, total = result
        # 实时跟随那一批一直在长，批次列表里的条数是选它时的
        total = max(total, len(rows))
        self.log_model.replace(rows)
        filtered = (self.log_module.currentData() or self.log_search.text().strip() or self.log_errors.isChecked()
                    or any(self._log_range()))
        if not rows and not self.nearby_bar.isHidden():
            text = "这段时间没有日志"
        elif not rows:
            text = "当前筛选没有匹配的日志" if filtered else "这一批是空的"
        else:
            text = (f"显示前 {len(rows)} 条，用搜索或时间段缩小范围" if more
                    else f"{len(rows)} / {total} 条" if filtered else f"{total} 条")
        if self.log_undated:
            text += f"；另有 {self.log_undated} 条原文没有日期，按时间筛时没算进去"
        self.log_count.setText(text)
        # 筛选改了，原来选中的那行还在就继续选中
        ids = [row[0] for row in rows]
        if self.log_selected in ids:
            row = ids.index(self.log_selected)
            self.log_table.selectRow(row)
            self.log_table.scrollTo(self.log_model.index(row, 0))

    def _nearby_logs(self, events, page):
        """到日志排障看这组事件实际发生前后 NEARBY 秒、同模块的设备日志；只按时间圈出，不认定和请求有关

        时间用 occurred：导入的行用原文日期时间，实时收到的用收到时刻；原文只有时刻的行不参与圈时间
        """
        stamps = [t for t in map(occurred, events) if t is not None]
        first = events[0]
        module = summarize(events).module if any(e.kind in ("指令", "ACK") for e in events) else first.module
        start, end = min(stamps) - NEARBY, max(stamps) + NEARBY
        clock = lambda t: time.strftime("%H:%M:%S", time.localtime(t))
        span = clock(min(stamps)) + ("" if clock(max(stamps)) == clock(min(stamps)) else "–" + clock(max(stamps)))
        self.nearby_page = page
        self.nearby_fetch = ((module,), start)
        self.nearby_text.setText(f"{module.upper()} 请求 {span} 前后各 {NEARBY} 秒的 {module.upper()} 日志，按原文时间圈出，"
                                 "不一定和这次请求有关；设备时钟不准时可以放宽时间段")
        self.nearby_bar.show()
        # 先填好条件再统一刷新一次，不为每个控件各查一遍库
        for widget in (self.log_module, self.log_search, self.log_errors, self.log_period, self.log_start, self.log_end):
            widget.blockSignals(True)
        self.log_module.setCurrentIndex(max(0, self.log_module.findData(module)))
        self.log_search.clear()
        self.log_errors.setChecked(False)
        self.log_period.setCurrentText(next(text for text, choice in LOG_RANGES.items() if choice == "custom"))
        self.log_custom.show()
        self.log_start.setDateTime(QDateTime.fromSecsSinceEpoch(int(start)))
        self.log_end.setDateTime(QDateTime.fromSecsSinceEpoch(int(end) + 1))
        for widget in (self.log_module, self.log_search, self.log_errors, self.log_period, self.log_start, self.log_end):
            widget.blockSignals(False)
        self.log_want = (first.sn, "")
        # 第一次进日志页时切页本身就会列批次
        loaded = self.log_loaded
        self.app._go(self.app.LOGS)
        if loaded:
            self._log_batches()

    def _nearby_back(self):
        """回到跳来之前的页面；那边的列表、选中和证据链都没动过"""
        self.nearby_bar.hide()
        self.app._go(self.nearby_page)

    def _log_inspect(self, index):
        row_id, e, _ = self.log_model.rows[index.row()]
        self.log_selected = row_id
        self.log_detail.show_trace(e, self.app.store.trace(e))

    def _show_store_size(self):
        size = self.app.store.size()
        self.store_size.setText(f"本地 {self.app.store.count()} 条" + (f" · {size / 1e6:.0f} MB" if size else ""))

    def _clear_store(self):
        """清空库是唯一不可恢复的操作，保留一次确认"""
        if self.import_thread and self.import_thread.is_alive():
            self.app.notify("导入进行中，先停止再清空")
            return
        answer = QMessageBox.question(self.app, "清空本地数据", f"删除本地保存的 {self.app.store.count()} 条消息？\n"
                                      "导入的原始日志文件不受影响；需要留存请先导出诊断包")
        if answer != QMessageBox.Yes:
            return
        self.app.store.clear()
        self.app.control.watch, self.app.quick = None, None
        self.app._refresh()
        self._show_store_size()
        self._log_batches()
        self.app.notify("本地数据已清空")

    def _import_progress(self, done, total, count, started):
        rate = done / max(time.monotonic() - started, 0.001)
        left = (total - done) / rate if rate else 0
        self.import_bar.setValue(int(done * 1000 / total) if total else 0)
        self.import_state.setText(f"{done / 1e6:.0f} / {total / 1e6:.0f} MB　{count} 条　{rate / 1e6:.1f} MB/s　约剩 {left:.0f} 秒")

    def _import(self):
        paths, _ = QFileDialog.getOpenFileNames(self.app, "导入日志", "", "日志 (*.log *.txt *.jsonl);;所有文件 (*)")
        if paths:
            self.start_import(paths)

    def start_import(self, paths):
        if self.import_thread and self.import_thread.is_alive():
            self.app.notify("上一批还在导入，可先停止")
            return
        self.app.import_cancel.clear()
        session, sn = "import-" + str(uuid.uuid4()), self.app.sn.text()

        total = sum(Path(path).stat().st_size for path in paths)

        def offer(item):
            # 队列满时等主线程消费，取消后立即放弃，不会卡住线程
            while not self.app.import_cancel.is_set():
                try:
                    self.app.inbox.put(item, timeout=0.2)
                    return True
                except queue.Full:
                    continue
            return False

        def work():
            # 解析、脱敏、序列化都在这里做，主线程只按批写库，大文件导入时界面不卡
            count, done, started = 0, 0, time.monotonic()
            try:
                for path in paths:
                    stats, batch = {}, []
                    for event in import_log(path, sn, session, self.app.import_cancel.is_set, stats):
                        batch.append(Store.row(event))
                        if len(batch) >= IMPORT_BATCH:
                            count += len(batch)
                            if not offer(("rows", batch, (done + stats["bytes"], total, count, started))):
                                break
                            batch = []
                    if batch and not self.app.import_cancel.is_set():
                        count += len(batch)
                        offer(("rows", batch, (done + stats["bytes"], total, count, started)))
                    done += stats["bytes"]
                    if self.app.import_cancel.is_set():
                        break
                    bad = (f"；{stats['undecoded']} 行有无法按 {stats['encoding']} 解码的字节（行 "
                           f"{', '.join(map(str, stats['undecoded_lines']))}{' 等' if stats['undecoded'] > 10 else ''}），"
                           "已用替换字符保留，原文位置不变" if stats["undecoded"] else "")
                    self.app.inbox.put(("import", f"{Path(path).name}：编码 {stats['encoding']}，sha256 "
                                              f"{stats['sha256'][:16]}…{bad}"))
                cost = time.monotonic() - started
                verdict = "已停止，已导入的保留" if self.app.import_cancel.is_set() else "导入完成"
                self.app.inbox.put(("import", f"{verdict}：{count} 条，{done / 1e6:.1f} MB，用时 {cost:.1f} 秒"))
            except Exception as error:
                self.app.inbox.put(("import", "导入失败：" + str(error)))
            self.app.inbox.put(("import_done", None))

        self.import_thread = threading.Thread(target=work, daemon=True)
        self.import_thread.start()
        self.import_info.appendPlainText(time.strftime("%H:%M:%S  开始导入\n") + "\n".join(paths))
        self.import_bar.setValue(0)
        self.import_row.show()
        self.import_state.setText(f"准备读取 {total / 1e6:.1f} MB")
        self.app._go(self.app.LOGS)

    def device_changed(self, device):
        """取设备日志和实时跟随走狗的 SSH 和日志目录，无人机还没有对应的日志位置，先只留导入文件"""
        dog = device == "dog"
        if not dog:
            self.live_follow.setChecked(False)
        self.live_follow.setVisible(dog)
        self.fetch_logs.setVisible(dog)

    def shown(self):
        self._show_store_size()
        if not self.log_loaded:
            self._log_batches()

    def _import_finished(self):
        self.import_row.hide()
        self.import_state.setText("")
        self._show_store_size()
        # 后台用另一个只读连接查批次，先把这一批最后几行提交，否则可能查不到
        self.app.store.flush()
        self._log_batches(newest=True)

    def _import_note(self, note):
        self.import_info.appendPlainText(time.strftime("%H:%M:%S  ") + note)
        self.app.notify(note)

    def _fetch_progress(self, done, total, name):
        self.import_bar.setValue(int(done * 1000 / total))
        self.import_state.setText(f"从设备取 {name}　{done / 1e6:.1f} / {total / 1e6:.1f} MB")

    def _follow_failed(self, note):
        self._stop_follow()
        self.app.notify(note)
