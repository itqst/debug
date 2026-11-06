"""无人机页：指令主题上残留的保留消息告警、VFC 和 Bridge 各领域状态、指令时间线；第一期只读"""

import html
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import summarize
from ..uav import domain, DOMAINS, freshness, link, outcome, quality, reading, retained_command
from ..ui import card, FlowGrid, GRAY, GREEN, label, ORANGE, RED, scroller, tint

QUALITY_COLORS = {"正常": GREEN, "失败": RED, "未知": ORANGE, "不适用": GRAY}
RESULT_COLORS = {0: GREEN, 1: RED, 2: ORANGE}


class UavPage:
    """一个活动连接一份：换连接（新会话）时卡片和告警重来，历史仍在消息追踪里"""

    def __init__(self, app):
        self.app = app
        self.session, self.latest, self.alerts = "", {}, {}
        self.drawn_touch, self.drawn_at = None, 0.0

    def _uav_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.app._page_header(layout, "无人机", "只读订阅 local/uav/#：VFC 和 Bridge 的状态、指令和回执。"
                              "时效按本机收到的时间算，板上时钟不准，报文里的 ts 只作参考")
        self.banner = label("", wrap=True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setStyleSheet(f"background:{tint(RED, 40)};color:{RED};border-radius:10px;padding:10px 14px")
        self.banner.hide()
        layout.addWidget(self.banner)
        self.link_note = label("", "caption", True)
        layout.addWidget(self.link_note)
        self.cards = {}
        frames = []
        for key, (title, _) in DOMAINS.items():
            frame = QFrame()
            frame.setObjectName("card")
            box = QVBoxLayout(frame)
            box.setContentsMargins(14, 12, 14, 12)
            box.setSpacing(4)
            head = label(title, "groupHeader")
            word = label("未观测")
            word.setStyleSheet(f"font-size:17px;font-weight:600;color:{GRAY}")
            msg, extra, age = label("", "caption", True), label("", "caption", True), label("", "caption")
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
        layout.addWidget(scroller(body), 1)
        self.rows = []
        self.render()
        return page

    def observe(self, event):
        """每条入库消息都过一遍：只有当前活动连接的无人机消息进卡片和告警"""
        if not event.topic.startswith("local/uav/") or event.source != "mqtt" or event.session != self.app.mqtt.session:
            return
        if event.session != self.session:
            self.session, self.latest, self.alerts = event.session, {}, {}
        if retained_command(event):
            self.alerts[event.topic] = event
        key = domain(event)
        if key:
            self.latest[key] = event

    def shown(self):
        self.drawn_touch = None
        self.render()

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
            self.link_note.setText(f"{'已连接' if self.app.mqtt.connected else '连接中或已断开'} {self.app.mqtt.host}，只读，不会发送任何消息")
        else:
            self.link_note.setText("还没连无人机：左侧连接卡里设备选“无人机”、方式选“直连模块”，填板上 Broker 地址")
        self._render_alerts()
        for key, (word, msg, extra, age) in self.cards.items():
            event = self.latest.get(key) if live else None
            if event is None:
                word.setText("未观测" if live or not self.latest.get(key) else "已断开")
                word.setStyleSheet(f"font-size:17px;font-weight:600;color:{GRAY}")
                msg.setText("")
                extra.setText("")
                age.setText("")
                continue
            text, note, _, more = reading(event)
            state = quality(event)
            fresh = freshness(event, now)
            word.setText(text or "—")
            word.setStyleSheet(f"font-size:17px;font-weight:600;color:{QUALITY_COLORS[state]}")
            msg.setText(note)
            extra.setText(more)
            seconds = now - event.timestamp
            stamp = f"{seconds:.0f} 秒前收到" if seconds < 120 else time.strftime("%H:%M:%S 收到", time.localtime(event.timestamp))
            age.setText({"保留快照": "保留快照（订阅时 Broker 给的缓存），等实时上报", "超时没收到": f"超时没收到新上报 · {stamp}"}.get(fresh, stamp)
                        + ("" if state == "正常" else f" · 数据{state}"))
            age.setStyleSheet(f"color:{ORANGE if fresh != '实时' else GRAY}")
        touch = (self.app.module_touched.get("vfc", 0), self.app.module_touched.get("bridge", 0))
        if touch != self.drawn_touch:
            self.drawn_touch = touch
            self._render_timeline()

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
        self.banner.setText("⚠ " + "<br>".join(lines) + "<br>执行方重启或重连时会再收到并执行一遍。第一期上位机不发送，"
                            "请在 MQTTX 里往这个主题发一条空的保留消息清掉；没看到告警不代表 Broker 上一定没有残留")
        self.banner.show()

    def _render_timeline(self):
        events = []
        for module in ("vfc", "bridge"):
            events += [e for _, e in self.app.store.query(module=module, kind=("指令", "ACK"), limit=400, live=True)
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
                     ("　└ " if row["parent"] else "") + row["action"], row["key"][2], related, text)
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
