"""无人机的状态总览和航线任务两页：残留保留指令告警、VFC 和 Bridge 各领域状态、关键变化、指令时间线；航线编辑和常用 VFC 指令"""

import html
import json
import math
import time
import uuid

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QListWidget, QListWidgetItem, QSplitter,
    QStackedWidget, QTableView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import BRIDGE, payload_diff, requests, summarize
from ..trace_view import Step, TraceView
from ..registry import make_command
from ..uav import changes, domain, DOMAINS, freshness, link, LINK_SECONDS, outcome, quality, reading, retained_command, WAITING_NEXT
from ..ui import BLUE, button, card, FlowGrid, GRAY, GREEN, label, ModuleMessageModel, ORANGE, PillDelegate, RED, scroller, Segmented, tint

QUALITY_COLORS = {"正常": GREEN, "失败": RED, "未知": ORANGE, "不适用": GRAY}
RESULT_COLORS = {0: GREEN, 1: RED, 2: ORANGE}
# 页面上直接点的 VFC 指令（data 为空，带当前任务的 bid）
# 卡片详情“相关指令”列哪些：(模块, Bridge 动作前缀)；前缀为空就是这个模块的全部指令
RELATED = {"system": ("bridge", ("system.", "job.")), "localization": ("bridge", ("relocalization.",)),
           "relocalization": ("bridge", ("relocalization.",)), "navigation": ("bridge", ("navigation.",)),
           "mapping": ("bridge", ("mapping.",)), "flight": ("bridge", ("flight.",)), "online": ("bridge", ()), "pose": ("bridge", ()),
           "subsystem_status": ("vfc", ()), "nav_status": ("vfc", ()), "nav_status_progress": ("vfc", ())}
QUICK = (("pause_nav", "暂停"), ("continue_nav", "继续"), ("stop_nav", "结束航线"), ("start_nav_return_home", "返航"), ("land", "降落"))


class UavPage:
    """一个活动连接一份：换连接（新会话）时卡片和告警重来，历史仍在消息追踪里"""

    def __init__(self, app):
        self.app = app
        self.session, self.latest, self.alerts, self.log, self.since = "", {}, {}, [], 0.0
        # 这次连接里各领域收到的消息（新的在后），卡片详情里比对字段、列变化用
        self.history, self.detail_key, self.detail_drawn, self.detail_row = {}, None, None, None
        self.drawn_touch, self.drawn_at = None, 0.0

    def _uav_page(self):
        """设备选无人机时的“状态总览”"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.app._page_header(layout, "状态总览", "VFC 和 Bridge 的状态、指令和回执；时效按本机收到的时间算，板上时钟不准",
                              button("导出本次报文", self._export, "link"))
        self.banner = label("", wrap=True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setStyleSheet(f"background:{tint(RED, 40)};color:{RED};border-radius:10px;padding:10px 14px")
        self.banner.hide()
        layout.addWidget(self.banner)
        self.link_note = label("", "caption", True)
        layout.addWidget(self.link_note)
        self.cards = {}
        frames = []
        self.frames = {}
        for key, (title, _) in DOMAINS.items():
            # 整张卡片可点，右侧展开这个领域的详情，和狗的状态总览一样
            frame = Step()
            frame.setObjectName("card")
            frame.setCursor(Qt.PointingHandCursor)
            frame.clicked.connect(lambda k=key: self._open_detail(k))
            self.frames[key] = frame
            box = QVBoxLayout(frame)
            box.setContentsMargins(14, 12, 14, 12)
            box.setSpacing(4)
            head = label(title, "groupHeader")
            word = label("未观测")
            word.setStyleSheet(f"font-size:17px;font-weight:600;color:{GRAY}")
            msg, extra, age = label("", "caption", True), label("", "caption", True), label("", "caption", True)
            for widget in (head, word, msg, extra, age):
                box.addWidget(widget)
            box.addStretch()
            self.cards[key] = (word, msg, extra, age)
            frames.append(frame)
        # 卡片和时间线放进可滚动的区域：窗口矮时卡片保持原来的高度，不被时间线挤扁
        body = QWidget()
        inner = QVBoxLayout(body)
        inner.setContentsMargins(0, 0, 6, 0)
        self.grid = FlowGrid(frames, 4, 230)
        inner.addWidget(self.grid)
        inner.addSpacing(8)
        changes_card, cv = card(6, 12)
        cv.addWidget(label("关键变化", "sectionTitle"))
        cv.addWidget(label("状态词、航点进度、飞控解锁/落地/模式/起飞、VFC 任务说明的变化，新的在上；到点放行相关的标蓝加粗", "caption", True))
        self.changes = QListWidget()
        self.changes.setMinimumHeight(200)
        cv.addWidget(self.changes)
        inner.addWidget(changes_card)
        inner.addSpacing(8)
        table_card, tv = card(6, 12)
        tv.addWidget(label("指令时间线", "sectionTitle"))
        tv.addWidget(label("Bridge 命令不带 VFC 的 tid，按动作对应、时间就近配到 VFC 指令（候选，不代表一定是因果）；"
                           "VFC 的 ACK 里写了这条 Bridge 回执的状态和原因码时标“已对上”。双击一行在消息追踪里看全部报文", "caption", True))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["时间", "来源", "动作", "编号", "关联", "结果"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(32)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for column, width in enumerate((70, 64, 210, 190, 150)):
            header.resizeSection(column, width)
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.table.cellDoubleClicked.connect(self._open_row)
        self.table.setMinimumHeight(320)
        tv.addWidget(self.table, 1)
        inner.addWidget(table_card, 1)
        self.split = QSplitter()
        self.split.setHandleWidth(14)
        self.split.addWidget(scroller(body))
        self.split.addWidget(self._detail())
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        layout.addWidget(self.split, 1)
        self.rows = []
        self.render()
        return page

    def observe(self, event):
        """每条入库消息都过一遍：只有当前活动连接的无人机消息进卡片和告警"""
        if not event.topic.startswith("local/uav/") or event.source != "mqtt" or event.session != self.app.mqtt.session:
            return
        if event.session != self.session:
            self.session, self.latest, self.alerts, self.log = event.session, {}, {}, []
            self.since, self.history = event.timestamp, {}
        if retained_command(event):
            self.alerts[event.topic] = event
        key = domain(event)
        if key:
            # 保留消息是订阅时的旧快照，不当成这次连接里发生的变化
            if not event.retained:
                for text, loud in changes(self.latest.get(key), event):
                    self._note(event, text, loud)
            self.latest[key] = event
            kept = self.history.setdefault(key, [])
            kept.append(event)
            del kept[:-200]
        elif event.topic == BRIDGE + "events" and event.payload.get("type") == "waypoint_complete":
            info = event.payload.get("info") if isinstance(event.payload.get("info"), dict) else {}
            self._note(event, f"Bridge 到点事件：{info.get('data', '')}", True)
        elif event.topic == BRIDGE + "ack" and event.payload.get("duplicate"):
            self._note(event, f"Bridge 把 {event.payload.get('action')}（id {event.payload.get('id')}）当重复请求，回了旧的缓存结果，这次没有执行", True)
        elif event.topic == BRIDGE + "command" and event.payload.get("action") == "navigation.next":
            self._note(event, f"发出 navigation.next（{event.payload.get('client') or '未写 client'}，id {event.payload.get('id')}）", True)

    def _detail(self):
        """右侧领域详情，点卡片打开：状态上报（最新一条完整字段、和上一条比、这次连接里的变化）和相关指令"""
        self.detail_card, dv = card(8, 16)
        self.detail_card.setMinimumWidth(380)
        head = QHBoxLayout()
        self.detail_title = label("", "sectionTitle")
        head.addWidget(self.detail_title)
        head.addStretch()
        head.addWidget(button("收起", lambda: self._open_detail(None), "link"))
        dv.addLayout(head)
        self.detail_topic = label("", "caption", True)
        self.detail_topic.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dv.addWidget(self.detail_topic)
        self.detail_view = Segmented(["状态上报", "相关指令"])
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
        self.detail_model = ModuleMessageModel()
        self.detail_table = QTableView()
        self.detail_table.setModel(self.detail_model)
        self.detail_table.setItemDelegate(PillDelegate(self.detail_table))
        self.detail_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.detail_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.detail_table.setShowGrid(False)
        self.detail_table.verticalHeader().hide()
        self.detail_table.verticalHeader().setDefaultSectionSize(34)
        header = self.detail_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for column, width in enumerate((76, 190, 96)):
            header.resizeSection(column, width)
        self.detail_table.clicked.connect(self._detail_open)
        lv.addWidget(self.detail_table, 1)
        self.detail_empty = label("", "caption", True)
        lv.addWidget(self.detail_empty)
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
        self.detail_trace.store = self.app.store
        tv.addWidget(self.detail_trace, 1)
        self.detail_pages.addWidget(trace)
        dv.addWidget(self.detail_pages, 1)
        self.detail_card.hide()
        return self.detail_card

    def _open_detail(self, key):
        self.detail_key, self.detail_drawn = key, None
        for name, frame in self.frames.items():
            frame.setStyleSheet("QFrame#card{border:1.5px solid %s}" % BLUE if name == key else "")
        self.detail_card.setVisible(bool(key))
        # 详情一开始是隐藏的，第一次展开时分栏没给它留宽度
        sizes = self.split.sizes()
        if key and sizes[1] < 460:
            self.split.setSizes([max(sum(sizes) - 460, 300), 460])
        if key:
            self.detail_title.setText(DOMAINS[key][0] + " 详情")
            self.detail_pages.setCurrentIndex(min(self.detail_view.index(), 1))
            self._render_detail()

    def _detail_view_changed(self, index):
        self.detail_pages.setCurrentIndex(min(index, 1))
        self.detail_drawn = None
        self._render_detail()

    def _render_detail(self):
        """这个领域有新消息、相关指令有变化或换了分段才重画"""
        key = self.detail_key
        if not key:
            return
        module = RELATED[key][0]
        kept = self.history.get(key, [])
        stamp = (self.session, self.detail_view.index(), len(kept), kept[-1].event_id if kept else None,
                 self.app.module_touched.get(module, 0) if self.detail_view.index() else 0)
        if stamp == self.detail_drawn:
            return
        self.detail_drawn = stamp
        self.detail_topic.setText("Topic " + (kept[-1].topic if kept else "—"))
        if self.detail_view.index():
            self._render_related(key)
        else:
            self._render_reports(kept)

    def _render_reports(self, kept):
        while self.detail_box.count():
            item = self.detail_box.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        if not kept:
            self.detail_box.addWidget(label("这次连接还没收到这个领域的消息", "caption", True))
            self.detail_box.addStretch()
            return
        e = kept[-1]
        before = kept[-2] if len(kept) > 1 else None
        changed = {path: old for path, old, _ in payload_diff(before.payload, e.payload)} if before else {}
        lines = []
        for path, _, value in payload_diff({}, e.payload):
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if path in changed:
                lines.append(f"{html.escape(path)}　<b style='color:{ORANGE}'>{html.escape(text)}</b>"
                             f"<span style='color:{GRAY}'>　（原 {html.escape(json.dumps(changed[path], ensure_ascii=False))}）</span>")
            else:
                lines.append(f"{html.escape(path)}　{html.escape(text)}")
        source = "保留消息（订阅时 Broker 给的）" if e.retained else "实时"
        block = label(f"<b>最新一条</b><span style='color:{GRAY}'>　{time.strftime('%H:%M:%S', time.localtime(e.timestamp))} 收到 · {source}"
                      f"{' · 和上一条比，变了的标橙' if before else ''}</span><br>"
                      f"<span style='font-family:Consolas,monospace;font-size:12px'>{'<br>'.join(lines) or '（没有字段）'}</span>", wrap=True)
        block.setTextFormat(Qt.RichText)
        block.setTextInteractionFlags(Qt.TextSelectableByMouse)
        block.setObjectName("detailBlock")
        self.detail_box.addWidget(block)
        # 状态词等关键变化之外，每次报文里变了的字段都列出来，新的在上
        steps = []
        for old, new in zip(kept, kept[1:]):
            diff = [(path, a, b) for path, a, b in payload_diff(old.payload, new.payload) if path != "ts"]
            if diff:
                text = "；".join(f"{path} {json.dumps(a, ensure_ascii=False)} → {json.dumps(b, ensure_ascii=False)}" for path, a, b in diff[:6])
                steps.append(f"{time.strftime('%H:%M:%S', time.localtime(new.timestamp))}　{html.escape(text)}")
        self.detail_box.addWidget(label(f"这次连接里的变化 · 看最近收到的 {len(kept)} 条", "groupHeader"))
        changes_text = label("<br>".join(reversed(steps[-50:])) or "收到的这几条内容都一样（ts 除外）", "caption", True)
        changes_text.setTextFormat(Qt.RichText)
        changes_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail_box.addWidget(changes_text)
        self.detail_box.addStretch()

    def _render_related(self, key):
        """这个领域相关的指令和回执，一次请求一行，新的在前；只看这次连接"""
        module, prefixes = RELATED[key]
        # 领域在 SQL 里筛完再取条数，别的领域的命令再多也挤不掉这个领域的
        fetched = self.app.store.query(module=module, kind=("指令", "ACK"), limit=600, session=self.session, prefixes=prefixes) if self.session else []
        found = sorted(requests(self.app.store, fetched), key=lambda r: -r.events[-1].timestamp)[:150]
        rows = []
        for r in found:
            first = next((e for e in r.events if e.kind == "指令"), r.events[0])
            note = "本机发出" if any(e.direction == "发送" for e in r.events) else "观测到" if first.kind == "指令" else "仅观测到回复"
            rows.append((first, note, r.status, note))
        self.detail_model.replace(rows)
        for row, (e, *_) in enumerate(rows):
            if (e.sn, e.session, e.tid) == self.detail_row:
                self.detail_table.selectRow(row)
        what = "VFC 的指令" if module == "vfc" else "Bridge 命令" + (f"（{'、'.join(p.rstrip('.') + '.*' for p in prefixes)}）" if prefixes else "")
        self.detail_empty.setText(f"这次连接里列 {what}" + ("，还没有" if not rows else f"，{len(rows)} 次请求") + "；点一行看整条请求")
        if self.detail_pages.currentIndex() == 2:
            self.detail_trace.refresh()

    def _detail_open(self, index):
        e = self.detail_model.rows[index.row()][0]
        self.detail_row = (e.sn, e.session, e.tid)
        self.detail_trace.show_trace(e, self.app.store.trace(e))
        self.detail_pages.setCurrentIndex(2)

    def _detail_to_messages(self):
        e = self.detail_trace.selected
        if e and e.tid:
            self.app.messages._show_request(summarize(self.app.store.trace(e)))

    def _note(self, event, text, loud):
        self.log.insert(0, (event.timestamp, text, loud))
        del self.log[300:]

    def shown(self):
        self.drawn_touch = None
        self.render()

    def vfc_online(self):
        """这次连接里 VFC 还在报在线；VFC 在跑时直接给 Bridge 发命令会绕开它的状态机"""
        event = self.latest.get("subsystem_status")
        return bool(event) and freshness(event, time.time()) == "实时" and reading(event)[0] == "在线"

    def tick(self, now):
        """无人机页可见时事件队列每轮调用：卡片时效每秒更新，时间线在 VFC 或 Bridge 有新消息时重画"""
        touch = (self.app.module_touched.get("vfc", 0), self.app.module_touched.get("bridge", 0))
        if now - self.drawn_at >= 1 or touch != self.drawn_touch:
            self.render()

    def render(self):
        now = time.time()
        self.drawn_at = time.monotonic()
        live = self.app.mqtt.connected and self.app.mqtt.device == "uav" and self.session == self.app.mqtt.session
        if self.app.mqtt.device == "uav" and self.app.mqtt.client:
            sending = "已打开“允许向无人机发送”，控制调试页发的指令会真的发到板上" if self.app.mqtt.uav_send else "只读，不会发送任何消息"
            self.link_note.setText(f"{'已连接' if self.app.mqtt.connected else '连接中或已断开'} {self.app.mqtt.host}，{sending}")
            self.link_note.setStyleSheet(f"color:{RED}" if self.app.mqtt.uav_send else "")
        elif self.app.mqtt.client:
            self.link_note.setText("当前连接按机器狗订阅（只收 local/robotdog），收不到无人机的消息：先断开，设备选“无人机”再连接")
        else:
            self.link_note.setText("还没连无人机：左侧连接卡里设备选“无人机”、方式选“直连模块”，填板上 Broker 地址")
        self._render_alerts()
        self._render_changes()
        if hasattr(self, "task_line"):
            self._render_task()
        for key, (word, msg, extra, age) in self.cards.items():
            event = self.latest.get(key) if live else None
            if event is None:
                word.setText("未观测" if live or not self.latest.get(key) else "已断开")
                word.setStyleSheet(f"font-size:17px;font-weight:600;color:{GRAY}")
                msg.setText(self._missing(key, now) if live else "")
                extra.setText("")
                age.setText("")
                continue
            text, note, _, more = reading(event)
            state = quality(event)
            fresh = freshness(event, now)
            waiting = key == "navigation" and text == WAITING_NEXT
            word.setText("5 · 到点，等待放行" if waiting else text or "—")
            word.setStyleSheet(f"font-size:17px;font-weight:600;color:{BLUE if waiting else QUALITY_COLORS[state]}")
            msg.setText(note)
            extra.setText(more)
            seconds = now - event.timestamp
            stamp = f"{seconds:.0f} 秒前收到" if seconds < 120 else time.strftime("%H:%M:%S 收到", time.localtime(event.timestamp))
            # Bridge 在线只在上线时发一条保留消息，不定时重发；掉线时 Broker 按遗嘱改成 OFFLINE，所以保留的这条就是现在的状态
            online = key == "online" and fresh == "保留快照"
            age.setText("上线时发的保留消息，Bridge 掉线时 Broker 会改成 OFFLINE" if online else
                        {"保留快照": "保留快照（订阅时 Broker 给的缓存），等实时上报", "超时没收到": f"超时没收到新上报 · {stamp}"}.get(fresh, stamp)
                        + ("" if state == "正常" else f" · 数据{state}"))
            age.setStyleSheet(f"color:{ORANGE if fresh != '实时' and not online else GRAY}")
        touch = (self.app.module_touched.get("vfc", 0), self.app.module_touched.get("bridge", 0))
        if touch != self.drawn_touch:
            self.drawn_touch = touch
            self._render_timeline()
        if hasattr(self, "detail_card"):
            self._render_detail()

    def _missing(self, key, now):
        """连上后一直没收到的卡片写可能的原因，免得只看到“未观测”"""
        vfc = ("subsystem_status", "nav_status", "nav_status_progress")
        if key in vfc and not any(k in self.latest for k in vfc):
            return "连上后没收到 VFC 的任何消息，VFC 服务可能没在跑" if now - self.since > LINK_SECONDS else ""
        if key == "nav_status_progress":
            return "只在执行航线时上报"
        system = self.latest.get("system")
        detail = (system.payload.get("info") or {}).get("detail") if system else None
        if key == "pose" and isinstance(detail, dict) and detail.get("ros") == "DOWN":
            return "Bridge 报 ros DOWN，没有位姿可发"
        return ""

    def _render_changes(self):
        self.changes.clear()
        for at, text, loud in self.log[:200]:
            item = QListWidgetItem(time.strftime("%H:%M:%S", time.localtime(at)) + "　" + text)
            if loud:
                item.setForeground(QColor(BLUE))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.changes.addItem(item)

    def _task_bid(self):
        """当前任务的 bid：VFC 任务状态里的 flight_id；还没有任务时新生成一个"""
        state = self.latest.get("nav_status")
        info = state.payload.get("info", {}) if state else {}
        return (info.get("flight_id") if isinstance(info, dict) else "") or str(uuid.uuid4())

    def _command(self, method, data=None, bid=None):
        """按无人机目录生成一条 VFC 指令（新 tid、ts），模拟 GW 发出；开关没开时由 MQTT 出口拒绝并提示"""
        item = next(entry for entry in self.app.control.uav_catalog if entry["method"] == method)
        payload = make_command(item)
        payload["bid"] = bid or self._task_bid()
        if data is not None:
            payload["data"] = data
        return self.app._emit(payload, json.dumps(payload, ensure_ascii=False), item["topic"], "vfc")

    def _mission_page(self):
        """设备选无人机时的“航线任务”：常用 VFC 指令一键发，航线在表格里编，发出后到状态总览看进度"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.app._page_header(layout, "航线任务", "模拟 GW 给 VFC 发指令，暂停、继续带当前任务的 bid；左侧打开“允许向无人机发送”才会真的发")
        quick, qv = card(8, 16)
        qv.addWidget(label("当前任务", "sectionTitle"))
        self.task_line = label("还没有收到 VFC 的任务状态", "caption", True)
        qv.addWidget(self.task_line)
        row = QHBoxLayout()
        row.setSpacing(6)
        for method, text in QUICK:
            row.addWidget(button(text, lambda m=method: self._quick(m)))
        row.addStretch()
        qv.addLayout(row)
        layout.addWidget(quick)
        layout.addSpacing(10)
        route, rv = card(8, 16)
        head = QHBoxLayout()
        head.addWidget(label("编辑航线", "sectionTitle"))
        head.addStretch()
        head.addWidget(button("发送航线", self._send_route, "primary"))
        rv.addLayout(head)
        self.route = RouteEditor()
        rv.addWidget(self.route, 1)
        layout.addWidget(route, 1)
        return page

    def _quick(self, method):
        if self._command(method):
            self.app.notify(f"已发出 {method}，结果看状态总览的指令时间线")

    def _send_route(self):
        try:
            data, bid = self.route.route()
        except ValueError as error:
            self.route.error.setText(str(error))
            return
        self.route.error.setText("")
        if self._command("start_multi_nav_by_points", data, bid):
            self.app.notify(f"已发出航线（bid {bid}），进度看状态总览的 VFC 任务卡和关键变化")

    def _render_task(self):
        state, progress = self.latest.get("nav_status"), self.latest.get("nav_status_progress")
        if not state:
            self.task_line.setText("还没有收到 VFC 的任务状态（VFC 没在跑或没连上 Broker 时就是这样）")
            return
        word, msg, _, extra = reading(state)
        text = f"<b>{html.escape(word)}</b>　{html.escape(msg)}　{html.escape(extra)}"
        if progress:
            text += f"　航点 {html.escape(reading(progress)[0])}"
        self.task_line.setText(text)

    def _export(self):
        """把这次无人机连接收发的全部报文导出成 jsonl，一行一条：本机时间、Topic、retain、报文"""
        if not self.session:
            self.app.notify("这次还没有连无人机，没有报文可导出")
            return
        path, _ = QFileDialog.getSaveFileName(self.app, "导出本次报文", time.strftime("uav_%Y%m%d_%H%M%S.jsonl"), "JSONL (*.jsonl)")
        if not path:
            return
        count = 0
        with open(path, "w", encoding="utf-8") as out:
            for e in self.app.store.session_events(self.session):
                out.write(json.dumps({"t": round(e.timestamp, 3), "wall": time.strftime("%H:%M:%S", time.localtime(e.timestamp)),
                                      "direction": e.direction, "topic": e.topic, "retain": e.retained,
                                      "body": e.payload if e.payload else e.raw}, ensure_ascii=False) + "\n")
                count += 1
        self.app.notify(f"已导出 {count} 条到 {path}")

    def _render_alerts(self):
        if not self.alerts:
            self.banner.hide()
            return
        lines = []
        for topic, e in self.alerts.items():
            what = e.payload.get("method") or e.payload.get("action") or "（不是 JSON）"
            key = e.payload.get("tid") or e.payload.get("id") or "—"
            lines.append(f"<b>{html.escape(topic)}</b> 上有保留消息：{html.escape(str(what))}　编号 {html.escape(str(key))}"
                         f"　{time.strftime('%H:%M:%S', time.localtime(e.timestamp))} 本次连接看到")
        self.banner.setText("⚠ " + "<br>".join(lines) + "<br>执行方重启或重连时会再收到并执行一遍。"
                            "请往这个主题发一条空的保留消息清掉（MQTTX 里 Retain 打勾、内容留空）；没看到告警不代表 Broker 上一定没有残留")
        self.banner.show()

    def _render_timeline(self):
        # 只看当前这次无人机连接的；之前的连接到消息追踪里查
        events = []
        if self.session:
            for module in ("vfc", "bridge"):
                events += [e for _, e in self.app.store.query(module=module, kind=("指令", "ACK"), limit=400, session=self.session)
                           if e.topic.startswith("local/uav/")]
        rows = link(events)
        # 新的在前；配上的 Bridge 行紧跟在它的 VFC 指令下面
        ordered = []
        for row in sorted((r for r in rows if not r["parent"]), key=lambda r: -r["at"]):
            ordered.append(row)
            ordered += sorted((r for r in rows if r["parent"] == row["key"]), key=lambda r: r["at"])
        self.rows = ordered[:200]
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            text, result = outcome(row)
            related = row["origin"] + ("　已对上" if row["confirmed"] else "")
            cells = (time.strftime("%H:%M:%S", time.localtime(row["at"])), row["source"],
                     ("　└ " if row["parent"] else "") + row["action"], row["key"][2].removeprefix("bridge:"), related, text)
            for column, value in enumerate(cells):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 5:
                    item.setForeground(QColor(RESULT_COLORS.get(result, ORANGE)))
                self.table.setItem(index, column, item)

    def _open_row(self, index, _column):
        """双击：在消息追踪里只看这一请求的全部报文和证据链"""
        row = self.rows[index]
        self.app.messages._show_request(summarize(row["events"]))


def quaternion(yaw_deg):
    """绕 z 轴转 yaw（度）的四元数，VFC 用它算到点时的偏航角"""
    half = math.radians(yaw_deg) / 2
    return {"x": 0.0, "y": 0.0, "z": round(math.sin(half), 6), "w": round(math.cos(half), 6)}


def parse_actions(text):
    """“pause 5; take_photo; gimbal_action 1,1”→ 动作列表；动作名照写，不在这里判断 VFC 支不支持（VFC 不支持会整条拒绝）"""
    actions = []
    for index, part in enumerate(p.strip() for p in text.split(";") if p.strip()):
        name, _, rest = part.partition(" ")
        params = [json.loads(v) for v in rest.replace("，", ",").split(",") if v.strip()]
        actions.append({"id": f"a{index + 1}", "action": name, "param": params})
    return actions


class RouteEditor(QWidget):
    """编辑一条航线：每个航点填 x、y、z（米，z 相对起飞点）、偏航角（度）和动作，生成 start_multi_nav_by_points 的 data"""

    COLUMNS = ["x", "y", "z", "偏航角 °", "动作（; 分隔，如 pause 5; take_photo）"]

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.map_id, self.task_name = QLineEdit("20261023_142339"), QLineEdit("test_route")
        self.bid = QLineEdit(str(uuid.uuid4()))
        for text, field in (("map_id", self.map_id), ("task_name", self.task_name), ("bid（任务 ID）", self.bid)):
            top.addWidget(label(text, "caption"))
            top.addWidget(field, 1)
        top.addWidget(button("换新 bid", lambda: self.bid.setText(str(uuid.uuid4())), "link"))
        layout.addLayout(top)
        layout.addWidget(label("z 按 Bridge 只收 0.20～4.50 m；动作可用 take_photo、start_record、stop_record、pause、gimbal_action、start_ai、stop_ai，"
                               "VFC 不支持的动作会整条航线拒绝", "caption", True))
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(30)
        layout.addWidget(self.table, 1)
        self._add(("0", "1", "1.2", "90", "pause 3"))
        self.error = label("", "caption", True)
        self.error.setStyleSheet(f"color:{RED}")
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addWidget(button("加一个航点", lambda: self._add(("0", "0", "1.2", "0", ""))))
        buttons.addWidget(button("删掉选中的", lambda: self.table.removeRow(self.table.currentRow())))
        buttons.addStretch()
        layout.addLayout(buttons)

    def _add(self, values):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate(values):
            self.table.setItem(row, column, QTableWidgetItem(value))

    def route(self):
        """(data, bid)；填得不对时抛 ValueError，文字说明是哪一行"""
        points = []
        for row in range(self.table.rowCount()):
            cell = lambda c: (self.table.item(row, c).text() if self.table.item(row, c) else "").strip()
            try:
                x, y, z, yaw = (float(cell(c)) for c in range(4))
                actions = parse_actions(cell(4))
            except ValueError as error:
                raise ValueError(f"第 {row + 1} 个航点填得不对：{error}")
            points.append({"position": {"x": x, "y": y, "z": z}, "orientation": quaternion(yaw), "actions": actions})
        if not points:
            raise ValueError("至少要一个航点")
        return {"map_id": self.map_id.text().strip(), "task_name": self.task_name.text().strip(), "points": points}, self.bid.text().strip()
