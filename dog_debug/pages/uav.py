"""无人机页：指令主题上残留的保留消息告警、VFC 和 Bridge 各领域状态、指令时间线；第一期只读"""

import html
import json
import math
import time
import uuid

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLineEdit, QListWidget, QListWidgetItem,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import BRIDGE, summarize
from ..registry import make_command
from ..uav import changes, domain, DOMAINS, freshness, link, outcome, quality, reading, retained_command, WAITING_NEXT
from ..ui import BLUE, button, card, FlowGrid, GRAY, GREEN, label, ORANGE, RED, scroller, Switch, tint

QUALITY_COLORS = {"正常": GREEN, "失败": RED, "未知": ORANGE, "不适用": GRAY}
RESULT_COLORS = {0: GREEN, 1: RED, 2: ORANGE}
# 页面上直接点的 VFC 指令（data 为空，带当前任务的 bid）
QUICK = (("pause_nav", "暂停"), ("continue_nav", "继续"), ("stop_nav", "结束航线"), ("start_nav_return_home", "返航"), ("land", "降落"))


class UavPage:
    """一个活动连接一份：换连接（新会话）时卡片和告警重来，历史仍在消息追踪里"""

    def __init__(self, app):
        self.app = app
        self.session, self.latest, self.alerts, self.log = "", {}, {}, []
        self.drawn_touch, self.drawn_at = None, 0.0

    def _uav_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.app._page_header(layout, "无人机", "订阅 local/uav/#：VFC 和 Bridge 的状态、指令和回执；默认只读，打开“允许向无人机发送”才发。"
                              "时效按本机收到的时间算，板上时钟不准，报文里的 ts 只作参考")
        self.banner = label("", wrap=True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setStyleSheet(f"background:{tint(RED, 40)};color:{RED};border-radius:10px;padding:10px 14px")
        self.banner.hide()
        layout.addWidget(self.banner)
        self.link_note = label("", "caption", True)
        layout.addWidget(self.link_note)
        # 测试时常用的几条 VFC 指令直接放这里；发不发由“允许向无人机发送”决定，和控制调试页是同一个开关
        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.send_switch = Switch("允许向无人机发送")
        self.send_switch.toggled.connect(lambda on: self.app.control.uav_send.setChecked(on))
        self.app.control.uav_send.toggled.connect(self._send_toggled)
        tools.addWidget(self.send_switch)
        tools.addSpacing(10)
        tools.addWidget(button("编辑航线…", self._edit_route, "primary"))
        for method, text in QUICK:
            tools.addWidget(button(text, lambda m=method: self._quick(m)))
        tools.addStretch()
        tools.addWidget(button("导出本次报文", self._export, "link"))
        layout.addLayout(tools)
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
        layout.addWidget(scroller(body), 1)
        self.rows = []
        self.render()
        return page

    def observe(self, event):
        """每条入库消息都过一遍：只有当前活动连接的无人机消息进卡片和告警"""
        if not event.topic.startswith("local/uav/") or event.source != "mqtt" or event.session != self.app.mqtt.session:
            return
        if event.session != self.session:
            self.session, self.latest, self.alerts, self.log = event.session, {}, {}, []
        if retained_command(event):
            self.alerts[event.topic] = event
        key = domain(event)
        if key:
            # 保留消息是订阅时的旧快照，不当成这次连接里发生的变化
            if not event.retained:
                for text, loud in changes(self.latest.get(key), event):
                    self._note(event, text, loud)
            self.latest[key] = event
        elif event.topic == BRIDGE + "events" and event.payload.get("type") == "waypoint_complete":
            info = event.payload.get("info") if isinstance(event.payload.get("info"), dict) else {}
            self._note(event, f"Bridge 到点事件：{info.get('data', '')}", True)
        elif event.topic == BRIDGE + "command" and event.payload.get("action") == "navigation.next":
            self._note(event, f"发出 navigation.next（{event.payload.get('client') or '未写 client'}，id {event.payload.get('id')}）", True)

    def _note(self, event, text, loud):
        self.log.insert(0, (event.timestamp, text, loud))
        del self.log[300:]

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
            sending = "已打开“允许向无人机发送”，控制调试页发的指令会真的发到板上" if self.app.mqtt.uav_send else "只读，不会发送任何消息"
            self.link_note.setText(f"{'已连接' if self.app.mqtt.connected else '连接中或已断开'} {self.app.mqtt.host}，{sending}")
            self.link_note.setStyleSheet(f"color:{RED}" if self.app.mqtt.uav_send else "")
        elif self.app.mqtt.client:
            self.link_note.setText("当前连接按机器狗订阅（只收 local/robotdog），收不到无人机的消息：先断开，设备选“无人机”再连接")
        else:
            self.link_note.setText("还没连无人机：左侧连接卡里设备选“无人机”、方式选“直连模块”，填板上 Broker 地址")
        self._render_alerts()
        self._render_changes()
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
            waiting = key == "navigation" and text == WAITING_NEXT
            word.setText("5 · 到点，等待放行" if waiting else text or "—")
            word.setStyleSheet(f"font-size:17px;font-weight:600;color:{BLUE if waiting else QUALITY_COLORS[state]}")
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

    def _send_toggled(self, on):
        self.send_switch.blockSignals(True)
        self.send_switch.setChecked(on)
        self.send_switch.blockSignals(False)
        self.render()

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

    def _quick(self, method):
        if self._command(method):
            self.app.notify(f"已发出 {method}，结果看下面的指令时间线")

    def _edit_route(self):
        dialog = RouteDialog(self.app)
        if dialog.exec() == QDialog.Accepted:
            data, bid = dialog.route()
            if self._command("start_multi_nav_by_points", data, bid):
                self.app.notify(f"已发出航线（bid {bid}），结果看下面的指令时间线和 VFC 任务卡")

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


class RouteDialog(QDialog):
    """编辑一条航线：每个航点填 x、y、z（米，z 相对起飞点）、偏航角（度）和动作，生成 start_multi_nav_by_points 的 data"""

    COLUMNS = ["x", "y", "z", "偏航角 °", "动作（; 分隔，如 pause 5; take_photo）"]

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("编辑航线")
        self.resize(820, 460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        top = QHBoxLayout()
        self.map_id, self.task_name = QLineEdit("20261023_142339"), QLineEdit("test_route")
        self.bid = QLineEdit(str(uuid.uuid4()))
        for text, field in (("map_id", self.map_id), ("task_name", self.task_name), ("bid（任务 ID）", self.bid)):
            top.addWidget(label(text, "caption"))
            top.addWidget(field, 1)
        layout.addLayout(top)
        layout.addWidget(label("z 按 Bridge 只收 0.20～4.50 m；动作可用 take_photo、start_record、stop_record、pause、gimbal_action、start_ai、stop_ai，"
                               "VFC 不支持的动作会整条航线拒绝", "caption", True))
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(30)
        layout.addWidget(self.table, 1)
        for row in (("0", "1", "1.2", "90", "pause 3"),):
            self._add(row)
        self.error = label("", "caption", True)
        self.error.setStyleSheet(f"color:{RED}")
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addWidget(button("加一个航点", lambda: self._add(("0", "0", "1.2", "0", ""))))
        buttons.addWidget(button("删掉选中的", lambda: self.table.removeRow(self.table.currentRow())))
        buttons.addStretch()
        buttons.addWidget(button("取消", self.reject))
        buttons.addWidget(button("发送航线", self._accept, "primary"))
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

    def _accept(self):
        try:
            self.route()
        except ValueError as error:
            self.error.setText(str(error))
            return
        self.accept()
