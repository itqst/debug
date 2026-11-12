"""状态总览页：当前设备各模块的上报、详情里的指令与回复、ZLM 流、直播和拉流播放"""

import html
import json
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSplitter,
    QStackedWidget, QTableView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import Event, payload_diff, summarize
from ..player import hide_password, StreamWindow
from ..registry import find, MODULES
from ..trace_view import Step, TraceView
from ..ui import (
    BLUE, button, CAMERAS, card, COLUMN_WIDTHS, dot, FlowGrid, FRESH_SECONDS, glyph_icon, GRAY, GREEN, HTTP_PLAY,
    label, method_label, MODULE_COLORS, ModuleMessageModel, ORANGE, PillDelegate, RED, scroller, Segmented, settings,
    StayMenu, STREAM_CAMERAS, STREAM_CFGS, stream_kind, stream_presets, tint,
)


class StatusPage:
    """状态总览页：各模块卡片、模块详情（上报、指令与回复）、ZLM、直播和拉流"""

    def __init__(self, app):
        self.app = app
        self.status_seen, self.status_aged = {}, 0.0
        self.status_latest, self.stream_rows = {}, []
        self.status_drawn = None

    def _status_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        # 筛选只管本页显示：接收、入库照旧，模块的新鲜度和版本仍按完整状态算，隐藏心跳不会让模块显示成过期
        saved = settings()
        self.hidden_modules = set(json.loads(saved.value("status_hidden_modules", "[]")))
        self.hidden_types = set(json.loads(saved.value("status_hidden_types", "[]")))
        self.status_methods = set()
        self.module_filter = QPushButton()
        self.module_filter.setCursor(Qt.PointingHandCursor)
        menu = StayMenu(self.module_filter)
        self.module_actions = {}
        for module in MODULES:
            action = menu.addAction(module.upper())
            action.setCheckable(True)
            action.setChecked(module not in self.hidden_modules)
            action.toggled.connect(lambda on, m=module: self._toggle_status_filter(self.hidden_modules, m, on))
            self.module_actions[module] = action
        self.module_filter.setMenu(menu)
        self.type_filter = QPushButton()
        self.type_filter.setCursor(Qt.PointingHandCursor)
        menu = StayMenu(self.type_filter)
        menu.aboutToShow.connect(lambda: self._fill_type_menu(menu))
        self.type_filter.setMenu(menu)
        self.app._page_header(layout, "状态总览", f"当前设备各模块最近一次上报，{FRESH_SECONDS} 秒内有更新显示绿点；"
                          "点卡片看这个模块的上报、指令和 ACK。导入的日志在日志排障页看",
                          self.module_filter, self.type_filter, button("恢复全部", self._reset_status_filter, "link"))
        cards = []
        self.module_cards, self.module_frames = {}, {}
        for index, module in enumerate(MODULES):
            # 整张卡片可点，右侧展开这个模块的详情
            frame = Step()
            frame.setObjectName("card")
            frame.setCursor(Qt.PointingHandCursor)
            frame.clicked.connect(lambda m=module: self._open_module(m))
            cv = QVBoxLayout(frame)
            cv.setContentsMargins(16, 16, 16, 16)
            cv.setSpacing(8)
            head = QHBoxLayout()
            icon = QLabel()
            icon.setPixmap(glyph_icon(module[0].upper(), MODULE_COLORS[module]).pixmap(22, 22))
            head.addWidget(icon)
            head.addWidget(label(module.upper(), "sectionTitle"))
            head.addStretch()
            state = dot("#C7C7CC")
            when = label("未观测", "caption")
            head.addWidget(state)
            head.addWidget(when)
            cv.addLayout(head)
            content = label("", wrap=True)
            content.setTextFormat(Qt.RichText)
            content.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            content.setMinimumHeight(110)
            cv.addWidget(content, 1)
            self.module_cards[module] = (state, when, content)
            self.module_frames[module] = frame
            cards.append(frame)
        split = self.status_split = QSplitter()
        split.setHandleWidth(14)
        left = QWidget()
        layout_left = QVBoxLayout(left)
        layout_left.setContentsMargins(0, 0, 0, 0)
        self.status_grid = FlowGrid(cards, 3, 300)
        layout_left.addWidget(scroller(self.status_grid), 1)
        split.addWidget(left)
        split.addWidget(self._module_detail())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        layout = layout_left

        live, lv = self.live_card = card(6, 16)
        head = QHBoxLayout()
        head.addWidget(label("直播", "sectionTitle"))
        head.addWidget(label("推流是否在跑看 stream_status；room_state 只说明到哪个房间/用户拉流，不代表推流活跃", "caption"), 1)
        lv.addLayout(head)
        self.stream_table = QTableWidget(0, 7)
        self.stream_table.setHorizontalHeaderLabels(["index", "相机 SN", "类型", "推流", "房间", "userId", "观测"])
        self.stream_table.setItemDelegate(PillDelegate(self.stream_table))
        self.stream_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.stream_table.setShowGrid(False)
        self.stream_table.verticalHeader().hide()
        self.stream_table.verticalHeader().setDefaultSectionSize(34)
        self.stream_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        # 表头加三行：机体、云台可见光、云台红外
        self.stream_table.setFixedHeight(34 * 4 + 8)
        lv.addWidget(self.stream_table)
        layout.addSpacing(10)
        layout.addWidget(live)

        web, wv = self.web_card = card(8, 16)
        wv.addWidget(label("音视频预览", "sectionTitle"))
        row = QHBoxLayout()
        self.web_url = QLineEdit("https://124.126.230.115:5500/")
        # WebRTC 房间号一路相机一个：以设备 room_state 上报的为准（云端开流下发的地址里带了房间时设备用下发的），
        # 还没收到时按 `<整机SN>_<相机SN>` 算；跟着左侧 SN 和遥控页当前相机走
        self.room, self.room_auto = QLineEdit(), ""
        self.room.setPlaceholderText("房间号")
        row.addWidget(self.web_url, 2)
        row.addWidget(self.room, 1)
        row.addWidget(button("复制房间", lambda: QApplication.clipboard().setText(self.room.text())))
        row.addWidget(button("打开 WebRTC 网页", lambda: webbrowser.open(self.web_url.text()), "primary"))
        wv.addLayout(row)
        wv.addWidget(label("云台房间（相机 002）里有 visible（可见光）、infrared（红外）两个成员，网页里选拉哪个；机体房间（001）的成员是 001", "caption", True))
        # 拉流地址从设备拉流配置和 ZLM 流列表自动列出（相机直连、ZLM 原始、处理后的各种协议），也可以直接填别的地址
        pull = QHBoxLayout()
        self.pull_url = QComboBox()
        self.pull_url.setEditable(True)
        self.pull_url.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.pull_url.lineEdit().setPlaceholderText("选一路拉流，或直接填地址")
        self.pull_url.lineEdit().returnPressed.connect(self._play_selected)
        self.pull_url.currentIndexChanged.connect(lambda: self.pull_url.setToolTip(hide_password(self._selected_url())))
        pull.addWidget(self.pull_url, 1)
        pull.addWidget(button("拉流播放", self._play_selected, "primary"))
        pull.addWidget(button("复制地址", self._copy_selected))
        pull.addWidget(button("从设备读取", self._load_media_cfg))
        wv.addLayout(pull)
        self.pull_note = label("", "caption", True)
        wv.addWidget(self.pull_note)
        self.pull_history = json.loads(settings().value("direct_urls", "[]"))
        self._refresh_presets()
        layout.addSpacing(10)
        layout.addWidget(web)
        self._apply_status_filter()
        return page

    def _room_follow(self):
        """房间号自动填：当前相机在 room_state 里报的房间，没有时按规则算；手改过就不再跟"""
        camera = self.app.teleop.remote.camera_sn.text().strip()
        room = f"{self.app.sn.text().strip()}_{camera}"
        for e in getattr(self, "status_latest", {}).get("media", []):
            info = e.payload.get("info")
            if e.method == "room_state" and isinstance(info, dict):
                rooms = [item.get("room") for item in info.get("info") or [] if isinstance(item, dict) and str(item.get("sn")) == camera]
                room = str(next((r for r in rooms if r), room))
                break
        if self.room.text() in ("", self.room_auto):
            self.room.setText(room)
        self.room_auto = room

    def _toggle_status_filter(self, hidden, key, shown):
        if shown:
            hidden.discard(key)
        else:
            hidden.add(key)
        self._apply_status_filter()

    def _fill_type_menu(self, menu):
        """选项是已收到过的状态类型加上被隐藏的类型，中文名后附 method"""
        menu.clear()
        methods = sorted(self.status_methods | self.hidden_types)
        if not methods:
            menu.addAction("还没收到状态上报").setEnabled(False)
        for method in methods:
            title = (find(method) or {}).get("title", "")
            action = menu.addAction(f"{title}　{method}" if title else method or "（无 method）")
            action.setCheckable(True)
            action.setChecked(method not in self.hidden_types)
            action.toggled.connect(lambda on, m=method: self._toggle_status_filter(self.hidden_types, m, on))

    def _reset_status_filter(self):
        self.hidden_modules.clear()
        self.hidden_types.clear()
        for action in self.module_actions.values():
            action.blockSignals(True)
            action.setChecked(True)
            action.blockSignals(False)
        self._apply_status_filter()

    def _apply_status_filter(self):
        """记住筛选，重排卡片并立即重画卡片和详情，不等新消息"""
        store = settings()
        store.setValue("status_hidden_modules", json.dumps(sorted(self.hidden_modules)))
        store.setValue("status_hidden_types", json.dumps(sorted(self.hidden_types)))
        shown = [m for m in MODULES if m not in self.hidden_modules]
        self.module_filter.setText("模块：" + ("全部" if len(shown) == len(MODULES) else "、".join(m.upper() for m in shown) or "无"))
        self.type_filter.setText("状态类型：" + (f"隐藏 {len(self.hidden_types)} 类" if self.hidden_types else "全部"))
        self.status_grid.show_only([self.module_frames[m] for m in shown])
        # 直播和预览都是 Media 的内容，不看 Media 时一起收起
        self.live_card[0].setVisible("media" in shown)
        self.web_card[0].setVisible("media" in shown)
        if self.detail_module in self.hidden_modules:
            self._open_module(None)
        self._refresh_status()

    def _module_detail(self):
        """状态页右侧的模块详情，点卡片打开；内容随状态刷新，模块有新上报时才重画"""
        self.detail_card, dv = card(8, 16)
        self.detail_card.setMinimumWidth(380)
        head = QHBoxLayout()
        self.detail_icon = QLabel()
        self.detail_title = label("", "sectionTitle")
        head.addWidget(self.detail_icon)
        head.addWidget(self.detail_title)
        head.addStretch()
        head.addWidget(button("收起", lambda: self._open_module(None), "link"))
        dv.addLayout(head)
        # ZLM 不走 MQTT，靠它自己的 HTTP 接口（getMediaList），地址和 secret 由用户填，不扫描、不猜
        self.zlm_form = QWidget()
        zf = QHBoxLayout(self.zlm_form)
        zf.setContentsMargins(0, 0, 0, 0)
        self.zlm_host = QLineEdit()
        self.zlm_host.setPlaceholderText("ZLM 地址，直连模块连上后自动填狗的 IP:80")
        self.zlm_secret = QLineEdit()
        self.zlm_secret.setPlaceholderText("secret（ZLM config.ini 的 [api] 段）")
        self.zlm_secret.setEchoMode(QLineEdit.Password)
        # 本机自用的调试台，secret 和手填的地址改完就记住，下次自动填上并在打开状态页时自动查
        # 地址默认跟着直连的狗走（_zlm_follow）；只有手填了别的地址（如 ZLM 在独立的 NX 上）才固定不变
        saved = settings()
        self.zlm_auto, self.zlm_manual = "", saved.value("zlm_host_manual", "")
        self.zlm_host.setText(self.zlm_manual)
        self.zlm_secret.setText(saved.value("zlm_secret", ""))
        self.zlm_host.editingFinished.connect(self._zlm_host_edited)
        self.zlm_secret.editingFinished.connect(lambda: settings().setValue("zlm_secret", self.zlm_secret.text().strip()))
        zf.addWidget(self.zlm_host, 2)
        zf.addWidget(self.zlm_secret, 2)
        zf.addWidget(button("查询", self._zlm_poll, "primary"))
        dv.addWidget(self.zlm_form)
        # 模块上报、发往这个模块的指令、模块的回复分开看；点指令或 ACK 就在这里看整条请求，不跳到消息追踪
        self.detail_view = Segmented(["模块上报", "指令与回复"])
        self.detail_view.changed.connect(self._detail_view_changed)
        dv.addWidget(self.detail_view)
        self.detail_pages = QStackedWidget()
        holder = QWidget()
        self.detail_box = QVBoxLayout(holder)
        self.detail_box.setContentsMargins(0, 0, 6, 0)
        self.detail_box.setSpacing(10)
        self.detail_pages.addWidget(scroller(holder))
        listing = QWidget()
        lv = QVBoxLayout(listing)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)
        self.detail_filter = QLineEdit()
        self.detail_filter.setObjectName("search")
        self.detail_filter.setPlaceholderText("按方法筛选，如 拍照、take_photo")
        self.detail_filter.setClearButtonEnabled(True)
        self.detail_filter.textChanged.connect(lambda: self._render_detail())
        lv.addWidget(self.detail_filter)
        self.detail_model = ModuleMessageModel()
        self.detail_table = QTableView()
        self.detail_table.setModel(self.detail_model)
        self.detail_table.setItemDelegate(PillDelegate(self.detail_table))
        self.detail_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.detail_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.detail_table.setShowGrid(False)
        self.detail_table.setWordWrap(False)
        self.detail_table.verticalHeader().hide()
        self.detail_table.verticalHeader().setDefaultSectionSize(34)
        header = self.detail_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(QHeaderView.Interactive)
        for column, width in enumerate(COLUMN_WIDTHS["ModuleMessageModel"]):
            header.resizeSection(column, width)
        self.detail_table.clicked.connect(self._detail_open)
        lv.addWidget(self.detail_table, 1)
        foot = QHBoxLayout()
        self.detail_empty = label("", "caption", True)
        foot.addWidget(self.detail_empty, 1)
        foot.addWidget(button("这个模块的全部请求 ›", self._detail_all_requests, "link"))
        lv.addLayout(foot)
        self.detail_pages.addWidget(listing)
        trace = QWidget()
        tv = QVBoxLayout(trace)
        tv.setContentsMargins(0, 0, 0, 0)
        back = QHBoxLayout()
        back.addWidget(button("‹ 返回列表", lambda: self.detail_pages.setCurrentIndex(1), "link"))
        back.addStretch()
        back.addWidget(button("在消息追踪里看", self._detail_to_messages, "link"))
        tv.addLayout(back)
        self.detail_trace = TraceView("")
        self.detail_trace.original.connect(self.app._original)
        self.detail_trace.store, self.detail_trace.nearby_logs = self.app.store, True
        self.detail_trace.nearby.connect(lambda events: self.app.logs._nearby_logs(events, self.app.STATUS))
        tv.addWidget(self.detail_trace, 1)
        self.detail_pages.addWidget(trace)
        dv.addWidget(self.detail_pages, 1)
        # 每个分段上次画的依据，切回没变化的分段直接显示，不重画
        self.detail_module, self.detail_drawn, self.detail_key = None, {}, None
        self.zlm_state, self.zlm_busy = None, False
        # 各 ZLM 地址的 RTSP / RTMP 端口（getServerConfig 查一次记住），和远程时为拉流开好的隧道 {(狗能连到的主机, 端口): 本机端口}
        self.zlm_ports, self.app.connection.ssh_rtsp = {}, {}
        # 设备拉流配置（读到之前为 None）和读取结果说明：读到时是文件路径，没读到时是原因
        self.media_cfg, self.media_cfg_note, self.media_cfg_busy = None, "", False
        self.zlm_timer = QTimer(self.app)
        self.zlm_timer.setInterval(3000)
        self.zlm_timer.timeout.connect(self._zlm_poll)
        self.detail_card.hide()
        return self.detail_card

    def _open_module(self, module):
        self.detail_module, self.detail_drawn = module, {}
        for name, frame in self.module_frames.items():
            frame.setStyleSheet(self._card_style(name))
        self.detail_card.setVisible(bool(module))
        # 详情一开始是隐藏的，第一次展开时分栏没给它留宽度
        sizes = self.status_split.sizes()
        if module and sizes[1] < 460:
            self.status_split.setSizes([max(sum(sizes) - 460, 300), 460])
        if module:
            self.detail_icon.setPixmap(glyph_icon(module[0].upper(), MODULE_COLORS[module]).pixmap(22, 22))
            self.detail_title.setText(f"{module.upper()} 详情")
            self.zlm_form.setVisible(module == "zlm")
            # ZLM 只有 HTTP 接口的状态，没有 MQTT 指令和 ACK
            self.detail_view.setVisible(module != "zlm")
            if module == "zlm" and self.detail_view.index():
                self.detail_view.set_index(0)
            self._render_detail()

    def _card_style(self, module):
        return "QFrame#card{border:1.5px solid %s}" % BLUE if module == self.detail_module else ""

    def _clear_detail(self):
        while self.detail_box.count():
            item = self.detail_box.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()

    def _detail_view_changed(self, index):
        self.detail_pages.setCurrentIndex(min(index, 1))
        self._render_detail()

    def _render_detail(self):
        """按当前分段画详情；只在这个模块有新消息、ZLM 有新结果或筛选变了时重画，别的模块来消息不动"""
        module = self.detail_module
        view = self.detail_view.index()
        received = self.status_latest.get(module, [])
        stamp = (received[0].timestamp if received else 0, self.zlm_state and self.zlm_state["at"],
                 self.app.module_touched.get(module, 0), tuple(sorted(self.hidden_types)), self.detail_filter.text())
        if stamp == self.detail_drawn.get(view):
            return
        self.detail_drawn[view] = stamp
        if view:
            self._render_messages(module)
        else:
            self._render_reports(module, received)

    def _render_reports(self, module, received):
        """模块上报：概况、模块专属、各类状态的完整字段（和上一条比）"""
        events = [e for e in received if e.method not in self.hidden_types]
        self._clear_detail()
        seen = self.status_seen.get(module)
        if seen:
            age = time.time() - seen[0]
            overview = (f"最近上报 {time.strftime('%H:%M:%S', time.localtime(seen[0]))}（{age:.0f} 秒前）"
                        + (f"　版本 v{seen[1]}（{seen[2]}）" if seen[1] else ""))
        else:
            overview = "还没有收到这个模块的状态上报"
        self.detail_box.addWidget(label(overview, "caption", True))
        if module == "media":
            self._detail_media()
        elif module == "vision":
            self._detail_vision()
        elif module == "zlm":
            self._detail_zlm()
        if events:
            self.detail_box.addWidget(label("各类状态最新一条（模块上报）", "groupHeader"))
        elif received:
            self.detail_box.addWidget(label(f"当前筛选无匹配状态（收到 {len(received)} 类，都被“状态类型”隐藏了）", "caption", True))
        for e in events[:10]:
            self.detail_box.addWidget(self._status_block(e))
        self.detail_box.addStretch()

    # 本机发送记录和 Broker 回显是同一次请求，合成一行；按这个顺序列出看到它的位置
    ORIGINS = ("本机发出", "观测到云端下发", "观测到发往模块", "模块回复", "GW 回复云端")

    @staticmethod
    def _origin(e):
        """消息是在哪看到的：Broker 上观测到发往模块的指令不等于模块已经处理"""
        if e.direction == "发送":
            return "本机发出"
        local = e.topic.startswith("local/")
        if e.kind == "指令":
            return "观测到发往模块" if local else "观测到云端下发"
        return "模块回复" if local else "GW 回复云端"

    def _render_messages(self, module):
        """这个模块的指令和回复，一次请求一行，新的在前；结果按整条请求算。点开看指令原文、对应 ACK 和完整证据

        只收到回复、没观测到指令的请求也列出来，备注写明。在哪看到的（本机发出、Broker 回显、各段回复）放提示里，不占主列
        """
        needle = self.detail_filter.text().strip().lower()
        # 类型和方法都在查询里筛，状态报得再多也挤不掉指令和回复；方法按目录里的中文名和 method 匹配
        methods = [item["method"] for item in self.app.catalog if needle in (item["method"] + item["title"]).lower()] if needle else None
        fetched = self.app.store.query(module=module, kind=("指令", "ACK"), limit=600, sn=self._status_sn(), live=True, methods=methods,
                                       dog_only=True)
        merged = {}
        for _, e in fetched:
            key = self._detail_row_key(e)
            first, origins = merged.setdefault(key, (e, set()))
            origins.add(self._origin(e))
            # 行上显示指令那条（列表新的在前，后遇到的指令更早）
            if e.kind == "指令" and first.kind != "指令":
                merged[key] = (e, origins)
        shown = list(merged.values())[:150]
        # 各行的整条请求一次取齐，不逐行查
        traces = self.app.store.groups([self._detail_row_key(e) for e, _ in shown if e.tid])
        rows = []
        for e, origins in shown:
            note = "仅观测到回复" if e.kind == "ACK" else ""
            if e.source == "sandbox":
                note = (note + " · " if note else "") + "沙盒"
            trace = traces.get(self._detail_row_key(e)) or [e]
            rows.append((e, note, summarize(trace).status, " · ".join(o for o in self.ORIGINS if o in origins)))
        position = self.detail_table.verticalScrollBar().value()
        self.detail_model.replace(rows)
        # 新消息进来重建列表时，还停在原来那行和原来的位置
        for row, (e, *_) in enumerate(rows):
            if self._detail_row_key(e) == self.detail_key:
                self.detail_table.selectRow(row)
        self.detail_table.verticalScrollBar().setValue(position)
        if not rows:
            self.detail_empty.setText("当前设备还没有这个模块的指令或回复" + ("（按方法筛选后）" if needle else ""))
        else:
            # 只列最近的，更早的不是没有，到消息追踪里看
            more = len(fetched) == 600 or len(merged) > len(shown)
            self.detail_empty.setText(f"只列出最近 {len(rows)} 次请求，更早的点右边看" if more else "")
        if self.detail_pages.currentIndex() == 2:
            self.detail_trace.refresh()

    @staticmethod
    def _detail_row_key(e):
        return (e.sn, e.session, e.tid) if e.tid else e.event_id

    def _detail_open(self, index):
        e = self.detail_model.rows[index.row()][0]
        self.detail_key = self._detail_row_key(e)
        self.detail_trace.show_trace(e, self.app.store.trace(e))
        self.detail_pages.setCurrentIndex(2)

    def _detail_all_requests(self):
        """到消息追踪的请求视图，只看这个模块；失败在前，搜索 tid 或方法、逐条视图往前翻页都能找到更早的"""
        module = self.detail_module
        # 消息页上次留下的类型、方法、只看失败、翻页都清掉，否则会以为这个模块没有历史
        self.app.messages.pages = []
        self.app.messages.set_scope(None)
        self.app.messages.filter_text.clear()
        self.app.messages.filter_kind.set_index(0)
        self.app.messages.filter_method.setCurrentIndex(0)
        self.app.messages.failures_only.setChecked(False)
        self.app._go(self.app.MESSAGES)
        self.app.messages.filter_module.set_index(MODULES.index(module) + 1)
        self.app.messages.view_mode.set_index(0)

    def _detail_to_messages(self):
        e = self.detail_trace.selected
        if e and e.tid:
            self.app.messages._show_request(summarize(self.app.store.trace(e)))

    def _status_sn(self):
        """状态页只看当前设备：连着就是连接时的 SN，没连就是左侧填的"""
        return self.app.mqtt.sn if self.app.mqtt.client else self.app.sn.text().strip()

    def _status_block(self, e):
        """一类状态的完整字段；和上一条同类消息相比变了的字段标橙色并附旧值。云端汇总里拆出来的不比对"""
        def content(event):
            body = event.payload.get("info", event.payload.get("data", event.payload))
            return body if isinstance(body, dict) else {"值": body}

        body = content(e)
        fields = payload_diff({}, body)
        # 分路上报（info.index）只和同一路的上一条比，否则会拿另一路的值当“原值”
        before = None if getattr(e, "summary_of", None) else self.app.store.previous(e)
        for _ in range(20):
            if not before or content(before).get("index") == body.get("index"):
                break
            before = self.app.store.previous(before)
        if before and content(before).get("index") != body.get("index"):
            before = None
        changed = {path: old for path, old, _ in payload_diff(content(before), body)} if before else {}
        lines = []
        for path, _, value in fields:
            text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            if path in changed:
                old = changed[path]
                lines.append(f"{html.escape(path)}　<b style='color:{ORANGE}'>{html.escape(text)}</b>"
                             f"<span style='color:{GRAY}'>　（原 {html.escape(json.dumps(old, ensure_ascii=False))}）</span>")
            else:
                lines.append(f"{html.escape(path)}　{html.escape(text)}")
        source = e.source.split(":")[0] + ("　· 来自云端 subsystem_status 汇总" if getattr(e, "summary_of", None) else "")
        block = label(f"<b>{html.escape(method_label(e.method))}</b><span style='color:{GRAY}'>　"
                      f"{time.strftime('%H:%M:%S', time.localtime(e.timestamp))} · {html.escape(source)}</span><br>"
                      f"<span style='font-family:Consolas,monospace;font-size:12px'>{'<br>'.join(lines) or '（没有字段）'}</span>", wrap=True)
        block.setTextFormat(Qt.RichText)
        block.setTextInteractionFlags(Qt.TextSelectableByMouse)
        block.setObjectName("detailBlock")
        return block

    def _detail_media(self):
        rows = self.stream_rows
        if not rows:
            return
        self.detail_box.addWidget(label("推流（stream_status 看推流在不在跑；room_state 只说明房间和 userId）", "groupHeader"))
        for index, sn, kind, state, color, room, user, seen in rows:
            line = label(f"<b>index {html.escape(index)}</b>　相机 {html.escape(sn)}　{html.escape(kind)}　"
                         f"<b style='color:{color}'>{html.escape(state)}</b>　房间 {html.escape(room)}　userId {html.escape(user)}"
                         f"<span style='color:{GRAY}'>　{seen}</span>", wrap=True)
            line.setTextFormat(Qt.RichText)
            self.detail_box.addWidget(line)

    def _detail_vision(self):
        events = self.app.store.query(method="detect_result_event", limit=5)
        self.detail_box.addWidget(label(f"最近检测事件（{len(events)} 条，最多显示 5 条）" if events else "还没有检测事件", "groupHeader"))
        for _, e in events:
            data = e.payload.get("data") if isinstance(e.payload.get("data"), dict) else {}
            brief = "　".join(f"{k}={v}" for k, v in data.items() if isinstance(v, (str, int, float)))[:160]
            self.detail_box.addWidget(label(f"{time.strftime('%H:%M:%S', time.localtime(e.timestamp))}　{brief}", "caption", True))

    def _detail_zlm(self):
        state = self.zlm_state
        if not state:
            self.detail_box.addWidget(label("填 ZLM 地址和 secret 后点“查询”，停在本页时每 3 秒刷新一次", "caption", True))
            return
        when = time.strftime("%H:%M:%S", time.localtime(state["at"]))
        if state["error"]:
            line = label(f"<b style='color:{RED}'>接口不可用</b>　{html.escape(state['error'])}<span style='color:{GRAY}'>　{when}</span>", wrap=True)
            line.setTextFormat(Qt.RichText)
            self.detail_box.addWidget(line)
            return
        streams = state["streams"]
        self.detail_box.addWidget(label(f"接口正常 · {len(streams)} 路流　{when}", "groupHeader"))
        for name, info in streams.items():
            # __input_<流> 是 Media 在 ZLM 上建的隐藏输入（相机原始画面），不对外，和公开流分开看
            # 流名去掉 __input_ 前缀和 _av 这类后缀后是 video0 / video1，对应机体 / 云台相机
            stream = name.split("/")[-1]
            raw = stream.startswith("__input_")
            camera = STREAM_CAMERAS.get(stream.removeprefix("__input_").split("_")[0], "")
            note = f"{camera}，Media 内部用" if raw and camera else "Media 内部用" if raw else camera
            hint = f"　<span style='color:{GRAY}'>{note}</span>" if note else ""
            kind = stream_kind(stream, info["origin"])
            tag = f"<b style='color:{ORANGE if kind == 'AI 处理后' else BLUE}'>{kind}</b>　" if kind else ""
            row = QHBoxLayout()
            line = label(f"{tag}<b>{html.escape(name)}</b>{hint}<br><span style='color:{GRAY};font-size:12px'>"
                         f"{html.escape(' / '.join(info['schemas']))} · 观看 {info['readers']} 人 · {info['speed'] * 8 / 1000:.0f} kbps · "
                         f"已运行 {info['alive']} 秒 · 来源 {html.escape(info['origin'])}</span>", wrap=True)
            line.setTextFormat(Qt.RichText)
            row.addWidget(line, 1)
            row.addWidget(button("播放", lambda _=False, n=name, i=info: self._play(n, i), "link"), 0, Qt.AlignTop)
            row.addWidget(button("复制地址", lambda _=False, n=name, i=info: self._copy_play_url(n, i), "link"), 0, Qt.AlignTop)
            self.detail_box.addLayout(row)

    def _play_url(self, name, info):
        """本机拉这路流用的地址；HTTP 格式走 ZLM 地址本身（远程时就是 80 端口那条隧道），只有 RTSP 时用 RTSP 端口

        远程时 RTSP 端口第一次用到才开隧道，之后复用；拿不到 RTSP 端口时抛异常说明原因
        """
        address = self.zlm_state["address"]
        for schema, suffix in HTTP_PLAY:
            if schema in info["schemas"]:
                return f"http://{address}/{name}{suffix}"
        port = self.zlm_ports.get(address, {}).get("rtsp")
        if not port:
            raise RuntimeError("这路流只有 RTSP，没查到 ZLM 的 RTSP 端口（getServerConfig 没返回 rtsp.port）")
        if self.app.ssh:
            return f"rtsp://127.0.0.1:{self._tunnel('127.0.0.1', port)}/{name}"
        return f"rtsp://{address.rsplit(':', 1)[0]}:{port}/{name}"

    def _tunnel(self, host, port):
        """远程时经 SSH 转 host:port（从狗那边连）到本机，同一个目标只开一次，返回本机端口"""
        if (host, port) not in self.app.connection.ssh_rtsp:
            self.app.connection.ssh_rtsp[(host, port)] = self.app.ssh.forward(port, host)
        return self.app.connection.ssh_rtsp[(host, port)]

    def _reachable(self, url):
        """本机实际拉的地址：远程时把地址里的主机端口（从狗上看的）换成经狗转过来的本机隧道，直连时原样用"""
        if not self.app.ssh:
            return url
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            raise RuntimeError(f"地址里没有主机：{url}")
        port = parts.port or {"rtsp": 554, "rtmp": 1935, "http": 80}.get(parts.scheme, 554)
        login = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
        return urllib.parse.urlunsplit(parts._replace(netloc=f"{login}127.0.0.1:{self._tunnel(parts.hostname, port)}"))

    def _presets(self):
        """当前的候选地址：狗的主机按“从狗上看”——远程时是狗自己 127.0.0.1，直连时是 ZLM 地址的主机"""
        address = self.zlm_host.text().strip()
        host, _, http = address.partition(":") if not self.app.ssh else ("127.0.0.1", "", "80")
        known = self.zlm_ports.get(address, {})
        rtsp = known.get("rtsp") or (self.media_cfg or {}).get("zlm", {}).get("rtsp_port") or 554
        ports = {"http": http or "80", "rtsp": rtsp, "rtmp": known.get("rtmp") or 1935}
        streams = (self.zlm_state or {}).get("streams") or {}
        return stream_presets(self.media_cfg, streams, host or "127.0.0.1", ports, self.app.sn.text().strip()) if host or self.media_cfg else []

    def _refresh_presets(self):
        """重列候选：设备配置、ZLM 流列表、自己填过的地址（最近 8 个）；正在看的那项或正在填的文字不变"""
        text, chosen = self.pull_url.currentText(), self._selected_url()
        items = self._presets() + [(url, url) for url in self.pull_history]
        self.pull_url.blockSignals(True)
        self.pull_url.clear()
        for title, url in items:
            self.pull_url.addItem(title, url)
        index = self.pull_url.findData(chosen) if chosen else -1
        self.pull_url.setCurrentIndex(index)
        if index < 0:
            self.pull_url.setEditText(text)
        self.pull_url.blockSignals(False)
        cameras = len(((self.media_cfg or {}).get("streams") or {}))
        streams = len(((self.zlm_state or {}).get("streams") or {}))
        self.pull_note.setText("　".join(filter(None, (
            f"设备 {Path(self.media_cfg_note).name}：{cameras} 路相机" if self.media_cfg else self.media_cfg_note or "连上狗后自动从设备配置读相机地址（要填 SSH 账号）",
            f"ZLM：{streams} 路流" if self.zlm_state and not self.zlm_state["error"] else "",
            f"共 {len(items)} 个地址" if items else ""))))

    def _selected_url(self):
        """下拉选中的那项的地址；改过文字（自己填的）就用填的文字"""
        index = self.pull_url.currentIndex()
        if index >= 0 and self.pull_url.itemText(index) == self.pull_url.currentText():
            return self.pull_url.itemData(index)
        return self.pull_url.currentText().strip()

    def _play_selected(self):
        """拉选中的那路；自己填的地址记进下拉，最新的在前"""
        url = self._selected_url()
        if not url:
            return
        title = self.pull_url.currentText() if self.pull_url.currentText() != url else hide_password(url)
        if self.pull_url.findData(url) < 0 or url in self.pull_history:
            self.pull_history = ([url] + [u for u in self.pull_history if u != url])[:8]
            settings().setValue("direct_urls", json.dumps(self.pull_history))
            self._refresh_presets()
        StreamWindow("拉流 · " + title, lambda: self._reachable(url), self.app).show()

    def _copy_selected(self):
        url = self._selected_url()
        if not url:
            return
        try:
            url = self._reachable(url)
        except (RuntimeError, OSError) as error:
            self.app.notify(str(error))
            return
        QApplication.clipboard().setText(url)
        self.app.notify("已复制 " + hide_password(url) + ("（经远程隧道的本机地址，断开后失效）" if self.app.ssh else ""))

    def _load_media_cfg(self):
        """后台经 SSH 读设备的拉流配置（STREAM_CFGS 里第一个读得到的）；已经远程连着就用那条，否则按填的账号临时连一次"""
        if self.media_cfg_busy or not self.app.ssh_user.text().strip():
            if not self.app.ssh_user.text().strip():
                self.app.notify("要先在左侧连接设置里填 SSH 账号")
            return
        self.media_cfg_busy = True

        def work():
            remote, own = None, False
            try:
                remote, own = self.app.logs._ssh_for_logs()
                errors = []
                for path in STREAM_CFGS:
                    try:
                        self.app.inbox.put(("media_cfg", json.loads(remote.read_file(path)), path))
                        return
                    except (OSError, ValueError) as error:
                        errors.append(f"{path}：{error}")
                self.app.inbox.put(("media_cfg", None, "读不了设备的拉流配置；" + "；".join(errors)))
            except Exception as error:
                # 连不上、认证失败等，原样说明
                self.app.inbox.put(("media_cfg", None, f"读不了设备的拉流配置：{error}"))
            finally:
                if own and remote:
                    remote.close()

        threading.Thread(target=work, daemon=True).start()

    def _media_cfg_done(self, cfg, note):
        """cfg 读到时 note 是文件路径，没读到时是原因"""
        self.media_cfg_busy = False
        self.media_cfg_note = note
        if cfg is not None:
            self.media_cfg = cfg
            # ZLM secret 也在 media.cfg 里，没填过就顺手填上
            secret = str((cfg.get("zlm") or {}).get("api_secret") or "")
            if secret and not self.zlm_secret.text().strip():
                self.zlm_secret.setText(secret)
                settings().setValue("zlm_secret", secret)
                self._zlm_poll()
        self._refresh_presets()

    def _play(self, name, info):
        """开一个窗口拉流播放；可以同时开几路对比原始和处理后的画面"""
        kind = stream_kind(name.split("/")[-1], info["origin"])
        window = StreamWindow(f"拉流 · {name}" + (f" · {kind}" if kind else ""), lambda: self._play_url(name, info), self.app)
        window.show()

    def _copy_play_url(self, name, info):
        try:
            url = self._play_url(name, info)
        except RuntimeError as error:
            self.app.notify(str(error))
            return
        QApplication.clipboard().setText(url)
        self.app.notify("已复制 " + url + ("（经远程隧道的本机地址，断开后失效）" if self.app.ssh else ""))

    def _zlm_host_edited(self):
        text = self.zlm_host.text().strip()
        self.zlm_manual = text if text != self.zlm_auto else ""
        settings().setValue("zlm_host_manual", self.zlm_manual)

    def _zlm_follow(self):
        """直连模块连上后，没手填过地址就把 ZLM 地址换成这只狗的 IP:80，并清掉旧地址的查询结果。
        云端 GW 模式的 Broker 是云端接入点，不是狗上的 ZLM，不跟"""
        if self.app.mode != "direct":
            return
        # 拉流候选要用这只狗的 media.cfg；换了狗先清掉上一只的，免得列出别的狗的地址
        self.media_cfg, self.media_cfg_note = None, ""
        if self.app.ssh_user.text().strip():
            self._load_media_cfg()
        # 经远程时 ZLM 走它自己的那条隧道，不是 MQTT 的本机端口
        self.zlm_auto = f"127.0.0.1:{self.app.connection.ssh_zlm}" if self.app.ssh else f"{self.app.mqtt.host}:80"
        if self.zlm_manual or self.zlm_host.text().strip() == self.zlm_auto:
            return
        self.zlm_host.setText(self.zlm_auto)
        self.zlm_state = None
        self._zlm_poll()

    def _zlm_poll(self):
        """后台问一次 ZLM 的 getMediaList；结果进 inbox，上一次还没回来就不重复发"""
        address, secret = self.zlm_host.text().strip(), self.zlm_secret.text().strip()
        if not address or not secret or self.zlm_busy or self.app.mode in {"sandbox", "offline"}:
            return
        self.zlm_busy = True
        self.zlm_timer.start()
        url = f"http://{address}/index/api/getMediaList?" + urllib.parse.urlencode({"secret": secret})
        known = address in self.zlm_ports
        # 经 FRP 隧道每次新连接要 1~3 s（实测），局域网 2.5 s 不够
        timeout = 10 if self.app.ssh else 2.5

        def work():
            state = {"at": time.time(), "error": "", "streams": {}, "address": address, "ports": {}}
            if not known:
                # 拉 RTSP / RTMP 时要用；查不到不影响流列表，播放时再说明
                config = f"http://{address}/index/api/getServerConfig?" + urllib.parse.urlencode({"secret": secret})
                try:
                    with urllib.request.urlopen(config, timeout=timeout) as reply:
                        values = (json.loads(reply.read().decode("utf-8", "replace")).get("data") or [{}])[0]
                    state["ports"] = {key: int(values[f"{key}.port"]) for key in ("rtsp", "rtmp") if values.get(f"{key}.port")}
                except (OSError, ValueError, IndexError, AttributeError):
                    pass
            try:
                with urllib.request.urlopen(url, timeout=timeout) as reply:
                    body = json.loads(reply.read().decode("utf-8", "replace"))
                if body.get("code") != 0:
                    state["error"] = f"ZLM 返回 code={body.get('code')}：{body.get('msg', '')}"
                for item in body.get("data") or []:
                    name = f"{item.get('app', '')}/{item.get('stream', '')}"
                    info = state["streams"].setdefault(name, {"schemas": [], "readers": 0, "speed": 0, "alive": 0, "origin": ""})
                    info["schemas"].append(str(item.get("schema", "")))
                    info["readers"] += int(item.get("readerCount") or 0)
                    info["speed"] = max(info["speed"], int(item.get("bytesSpeed") or 0))
                    info["alive"] = max(info["alive"], int(item.get("aliveSecond") or 0))
                    info["origin"] = str(item.get("originTypeStr") or item.get("originType") or "")
            except (OSError, ValueError) as error:
                state["error"] = f"连不上 {address}：{error}"
            self.app.inbox.put(("zlm", state))

        threading.Thread(target=work, daemon=True).start()

    def _zlm_done(self, state):
        self.zlm_busy = False
        # 查询途中换了地址，旧地址的结果不能显示成新地址的
        if state["address"] != self.zlm_host.text().strip():
            self._zlm_poll()
            return
        self.zlm_state = state
        if state["ports"]:
            self.zlm_ports[state["address"]] = state["ports"]
        self._refresh_presets()
        if self.app.stack.currentIndex() != self.app.STATUS:
            self.zlm_timer.stop()
        self._refresh_status()

    def _status_ages(self):
        """只按已记下的最后上报时间更新卡片时效，不查库；状态页可见时每秒调用"""
        now = time.time()
        for module, (seen, version, _) in self.status_seen.items():
            state, when, _ = self.module_cards[module]
            # 有过上报但超时是“已过期”，从没收到是“未观测”，两者都不等于模块离线
            age = now - seen
            fresh = age < FRESH_SECONDS
            state.setStyleSheet(f"background:{GREEN if fresh else ORANGE};border-radius:4px")
            stamp = time.strftime("%H:%M:%S", time.localtime(seen))
            when.setText((f"v{version} · " if version else "") + (stamp if fresh else f"已过期 {age:.0f} 秒 · {stamp}"))
        self.status_aged = time.monotonic()

    def _refresh_status(self):
        self.status_drawn = (self.app.store.status_version, self._status_sn())
        before, self.status_seen = self.status_seen, {}
        latest = {}
        for _, e in self.app.store.query(kind="状态", limit=3000, sn=self._status_sn(), live=True, dog_only=True):
            info = e.payload.get("info", {})
            index = str(info.get("index", "")) if isinstance(info, dict) else ""
            latest.setdefault((e.module, e.method, index), e)
            # 云端 subsystem_status 的 data 是各模块一项的数组：只在展示里拆给对应模块卡，仍指回原事件，不当成多条消息入库
            data = e.payload.get("data")
            if e.method == "subsystem_status" and isinstance(data, list):
                for item in data:
                    name = str(item.get("name", "")).lower() if isinstance(item, dict) else ""
                    if name in MODULES and (name, "subsystem_status", "") not in latest:
                        part = Event(e.topic, {"method": "subsystem_status", "data": item}, e.direction, e.source, e.sn,
                                     e.session, name, e.timestamp, raw=e.raw, event_id=e.event_id)
                        part.summary_of = e
                        latest[(name, "subsystem_status", "")] = part
        self.status_latest = {}
        now = time.time()
        for module, (state, when, content) in self.module_cards.items():
            events = sorted((e for (m, _, _), e in latest.items() if m == module), key=lambda e: -e.timestamp)
            self.status_latest[module] = events
            if module == "zlm" and self.zlm_state:
                self._zlm_card(state, when, content)
                continue
            if not events:
                state.setStyleSheet("background:#C7C7CC;border-radius:4px")
                when.setText("未观测")
                content.setText(f"<span style='color:{GRAY}'>还没有收到 {module.upper()} 的状态上报</span>")
                continue
            # 版本多数在 data.version，实机 VFC 的状态报文放在根上
            reported, version = None, ""
            for e in events:
                data = e.payload.get("data") if isinstance(e.payload.get("data"), dict) else {}
                if data.get("version") or e.payload.get("version"):
                    reported, version = e, str(data.get("version") or e.payload.get("version"))
                    break
            source = f"{reported.method} · {reported.source.split(':')[0]}" if reported else ""
            self.status_seen[module] = (events[0].timestamp, version, source)
            self.status_methods.update(e.method for e in events)
            # 这一刷比上一刷有更新的上报：卡片短暂带上模块色，一眼看出谁刚报了状态
            if module in before and events[0].timestamp > before[module][0]:
                self._flash_card(self.module_frames[module], MODULE_COLORS[module])
            rows = []
            shown = [e for e in events if e.method not in self.hidden_types]
            if not shown:
                rows.append(f"<span style='color:{GRAY}'>当前筛选无匹配状态（收到 {len(events)} 类，都被“状态类型”隐藏了）</span>")
            for e in shown[:8]:
                rows.append(f"<p style='margin:0 0 10px 0'><b>{html.escape(e.method)}</b>"
                            f"<span style='color:{GRAY}'>　{time.strftime('%H:%M:%S', time.localtime(e.timestamp))}"
                            f" · {html.escape(e.source.split(':')[0])}</span><br>"
                            f"<span style='font-family:Consolas,monospace;font-size:12px'>"
                            f"{html.escape(self._brief(e.payload)).replace(chr(10), '<br>')}</span></p>")
            content.setText("".join(rows))
        self._refresh_streams()
        self._room_follow()
        self._status_ages()
        if self.detail_module:
            self._render_detail()

    def _zlm_card(self, state, when, content):
        z = self.zlm_state
        state.setStyleSheet(f"background:{RED if z['error'] else GREEN};border-radius:4px")
        when.setText("接口 " + time.strftime("%H:%M:%S", time.localtime(z["at"])))
        if z["error"]:
            content.setText(f"<span style='color:{RED}'>{html.escape(z['error'])}</span>")
        else:
            names = "<br>".join(f"{html.escape(n)}　观看 {i['readers']}" for n, i in list(z["streams"].items())[:6])
            content.setText(f"<b>{len(z['streams'])} 路流</b><br><span style='font-size:12px'>{names or '当前没有流'}</span>")

    def _flash_card(self, frame, color):
        frame.setStyleSheet(f"QFrame#card{{border:1px solid {color};background:{tint(color, 24)}}}")
        module = next(m for m, f in self.module_frames.items() if f is frame)
        QTimer.singleShot(700, lambda: frame.setStyleSheet(self._card_style(module)))

    def _refresh_streams(self):
        rows = self.stream_rows = self._stream_rows()
        self.stream_table.setRowCount(len(rows))
        for row, (index, sn, kind, state, color, room, user, seen) in enumerate(rows):
            for column, value in enumerate((index, sn, kind, state, room, user, seen)):
                cell = QTableWidgetItem(value)
                if column == 3:
                    cell.setData(Qt.UserRole, color)
                self.stream_table.setItem(row, column, cell)

    def _stream_rows(self):
        """按 index 合并最近的 stream_status（推流活跃）和 room_state（拉流寻址）：[(index, 相机 SN, 类型, 推流, 颜色, 房间, userId, 观测时间)]"""
        streams, rooms = {}, {}
        # 用 _refresh_status 已经取好的各类最新状态（stream_status 每路一条），不再另查库
        for e in self.status_latest.get("media", []):
            info = e.payload.get("info")
            if e.method == "stream_status" and isinstance(info, dict):
                streams.setdefault(str(info.get("index", "")), (e, info))
            elif e.method == "room_state" and isinstance(info, dict) and not rooms:
                for item in info.get("info") or []:
                    if isinstance(item, dict):
                        rooms[str(item.get("index", ""))] = (e, item)
        indexes = sorted(set(streams) | set(rooms), key=lambda v: (len(v), v))
        out = []
        for index in indexes:
            stream, room = streams.get(index), rooms.get(index)
            info = stream[1] if stream else {}
            if stream:
                active = info.get("result")
                state, color = {1: ("活跃", GREEN), 0: ("未活跃", ORANGE)}.get(active, (f"未知 {active}", GRAY))
            else:
                state, color = "未观测", GRAY
            seen = max((x[0].timestamp for x in (stream, room) if x), default=0)
            sn = str(info.get("sn") or (room[1].get("sn") if room else "") or "—")
            out.append((index, f"{sn}（{CAMERAS[sn]}）" if sn in CAMERAS else sn, str(info.get("type") or "—"), state,
                        color, str(room[1].get("room", "—")) if room else "—", str(room[1].get("userId", "—")) if room else "—",
                        time.strftime("%H:%M:%S", time.localtime(seen)) if seen else "—"))
        return out

    @staticmethod
    def _brief(payload):
        """状态卡片里的一行摘要：取 info/data 的顶层字段"""
        body = payload.get("info", payload.get("data", payload))
        if not isinstance(body, dict):
            return json.dumps(body, ensure_ascii=False)[:180]
        def shown(value):
            if isinstance(value, (str, int, float)):
                return str(value)
            # 对象列表（如 room_state 的各路房间）一项一行，比整段 JSON 好读
            if isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
                return "".join("\n　· " + "  ".join(f"{k}={v}" for k, v in i.items()) for i in value)
            return json.dumps(value, ensure_ascii=False)
        text = "  ".join(f"{k}={shown(v)}" for k, v in body.items())
        return text if len(text) <= 300 else text[:297] + "…"

    def shown(self):
        self._refresh_status()
        self._zlm_poll()

    def tick(self, now):
        """状态页可见时事件队列每轮调用，卡片时效每秒更新一次"""
        if now - self.status_aged >= 1:
            self._status_ages()

    def refresh_if_changed(self):
        """没有新状态、设备没换就不重读状态；详情里的指令与回复由它自己按模块消息数判断"""
        if (self.app.store.status_version, self._status_sn()) != self.status_drawn:
            self._refresh_status()
        elif self.detail_module:
            self._render_detail()
