"""无人机的状态总览和航线任务两页：残留保留指令告警、VFC 和 Bridge 各领域状态、关键变化、指令时间线；航线编辑和常用 VFC 指令"""

import html
import json
import math
import time
import uuid

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..core import BRIDGE
from ..registry import make_command
from ..uav import alarm_of, changes, domain, freshness, link, outcome, quality, reading, retained_command
from ..ui import BLUE, button, card, GRAY, GREEN, label, ORANGE, RED, scroller, Segmented, tint

QUALITY_COLORS = {"正常": GREEN, "失败": RED, "未知": ORANGE, "不适用": GRAY}
RESULT_COLORS = {0: GREEN, 1: RED, 2: ORANGE}
LEVEL_COLORS = {"critical": RED, "error": RED, "warning": ORANGE, "info": BLUE}
# 页面上直接点的 VFC 指令（data 为空，带当前任务的 bid）
QUICK = (("pause_nav", "暂停"), ("continue_nav", "继续"), ("stop_nav", "结束航线"), ("start_nav_return_home", "返航"), ("land", "降落"))


class UavPage:
    """一个活动连接一份：换连接（新会话）时卡片和告警重来，历史仍在库里，可导出诊断包"""

    def __init__(self, app):
        self.app = app
        self.session, self.latest, self.alerts, self.log, self.since = "", {}, {}, [], 0.0
        # 这次连接里各领域收到的消息（新的在后），卡片详情里比对字段、列变化用
        self.history, self.detail_key, self.detail_drawn, self.detail_row = {}, None, None, None
        # VFC 告警（码 → (最新一条, 是否还在)）、最近一次 get_map_list 的回执（(事件, 地图列表)）
        self.alarms, self.maps = {}, None
        self.drawn_touch, self.drawn_at = None, 0.0

    def banner_widget(self):
        """指令主题上的残留保留消息告警，放在状态总览顶上，只在无人机有残留时出现"""
        self.banner = label("", wrap=True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setStyleSheet(f"background:{tint(RED, 40)};color:{RED};border-radius:10px;padding:10px 14px")
        self.banner.hide()
        return self.banner

    def extras_widget(self):
        """状态总览卡片下面无人机多出来的：VFC 告警、关键变化、指令时间线三个分段，上面一行写连接和发送开关"""
        extras, ev = card(6, 14)
        head = QHBoxLayout()
        self.extras_view = Segmented(["告警", "关键变化", "指令时间线"])
        head.addWidget(self.extras_view)
        self.alarm_count = label("", "caption")
        head.addWidget(self.alarm_count)
        head.addStretch()
        head.addWidget(button("导出本次报文", self._export, "link"))
        ev.addLayout(head)
        self.link_note = label("", "caption", True)
        ev.addWidget(self.link_note)
        pages = QStackedWidget()
        self.extras_view.changed.connect(pages.setCurrentIndex)
        self.alarm_text = label("", "caption", True)
        self.alarm_text.setTextFormat(Qt.RichText)
        self.alarm_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.alarm_text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        pages.addWidget(scroller(self.alarm_text))
        changes = QWidget()
        cv = QVBoxLayout(changes)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addWidget(label("状态词、航点进度、飞控解锁/落地/模式/起飞、VFC 任务/建图/定位/地图和告警的变化，新的在上；到点放行、"
                           "地图保存结果、定位丢失、告警标蓝加粗", "caption", True))
        self.changes = QListWidget()
        cv.addWidget(self.changes, 1)
        pages.addWidget(changes)
        timeline = QWidget()
        tv = QVBoxLayout(timeline)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.addWidget(label("Bridge 命令不带 VFC 的 tid，按动作对应、时间就近配到 VFC 指令（候选，不代表一定是因果）；"
                           "VFC 的 ACK 里写了这条 Bridge 回执的状态和原因码时标“已对上”。双击一行单独开窗口看全部报文", "caption", True))
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
        tv.addWidget(self.table, 1)
        pages.addWidget(timeline)
        pages.setFixedHeight(140)
        ev.addWidget(pages)
        self.rows = []
        return extras

    def observe(self, event):
        """每条入库消息都过一遍：只有当前活动连接的无人机消息进卡片和告警"""
        if not event.topic.startswith("local/uav/") or event.source != "mqtt" or event.session != self.app.mqtt.session:
            return
        if event.session != self.session:
            self.session, self.latest, self.alerts, self.log = event.session, {}, {}, []
            self.since, self.history, self.alarms, self.maps = event.timestamp, {}, {}, None
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
        elif alarm_of(event):
            alarm = alarm_of(event)
            raised = alarm.get("state") != "clear"
            # 同一个码按最新一次算：raise 的留着，clear 的标成已解除，按时间新的在前
            self.alarms[alarm["code"]] = (event, raised)
            self._note(event, f"告警{'' if raised else '解除'} {alarm['code']} {alarm.get('title', '')}", True)
        elif event.topic == "local/uav/vfc/ack" and event.payload.get("method") == "get_map_list" and isinstance(event.payload.get("data"), list):
            self.maps = (event, event.payload["data"])
        elif event.topic == BRIDGE + "events" and event.payload.get("type") == "waypoint_complete":
            info = event.payload.get("info") if isinstance(event.payload.get("info"), dict) else {}
            self._note(event, f"Bridge 到点事件：{info.get('data', '')}", True)
        elif event.topic == BRIDGE + "ack" and event.payload.get("duplicate"):
            self._note(event, f"Bridge 把 {event.payload.get('action')}（id {event.payload.get('id')}）当重复请求，回了旧的缓存结果，这次没有执行", True)
        elif event.topic == BRIDGE + "command" and event.payload.get("action") == "navigation.next":
            self._note(event, f"发出 navigation.next（{event.payload.get('client') or '未写 client'}，id {event.payload.get('id')}）", True)

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
        """无人机状态总览或航线任务页可见时事件队列每轮调用：每秒更新一次，时间线在 VFC 或 Bridge 有新消息时重画"""
        touch = (self.app.module_touched.get("vfc", 0), self.app.module_touched.get("bridge", 0))
        if now - self.drawn_at >= 1 or touch != self.drawn_touch:
            self.render()

    def render(self):
        self.drawn_at = time.monotonic()
        if self.app.mqtt.device == "uav" and self.app.mqtt.client:
            sending = "已打开“允许向无人机发送”，控制调试页发的指令会真的发到板上" if self.app.mqtt.uav_send else "只读，不会发送任何消息"
            self.link_note.setText(f"{'已连接' if self.app.mqtt.connected else '连接中或已断开'} {self.app.mqtt.host}，{sending}")
            self.link_note.setStyleSheet(f"color:{RED}" if self.app.mqtt.uav_send else "")
        elif self.app.mqtt.client:
            self.link_note.setText("当前连接按机器狗订阅（只收 local/robotdog），收不到无人机的消息：先断开，设备选“无人机”再连接")
        else:
            self.link_note.setText("还没连无人机：左侧连接卡里设备选“无人机”、方式选“直连模块”，填板上 Broker 地址")
        self._render_alerts()
        self._render_alarms()
        self._render_changes()
        if hasattr(self, "task_line"):
            self._render_task()
            self._render_maps()
        touch = (self.app.module_touched.get("vfc", 0), self.app.module_touched.get("bridge", 0))
        if touch != self.drawn_touch:
            self.drawn_touch = touch
            self._render_timeline()

    def _render_alarms(self):
        """进行中的告警在前，已解除的在后，各自新的在前；告警码、级别、标题、建议，带坐标时写上"""
        if not hasattr(self, "alarm_text"):
            return
        items = sorted(self.alarms.values(), key=lambda pair: (not pair[1], -pair[0].timestamp))
        active = sum(1 for _, raised in items if raised)
        self.alarm_count.setText(f"告警 {active} 条进行中 · 已解除 {len(items) - active} 条" if items else "")
        lines = []
        for e, raised in items[:20]:
            info = alarm_of(e)
            color = LEVEL_COLORS.get(info.get("level"), ORANGE) if raised else GRAY
            where = f"　位置 x {info['x']} y {info['y']} z {info['z']}" if "x" in info else ""
            lines.append(f"<b style='color:{color}'>{html.escape(str(info['code']))}　{html.escape(str(info.get('title', '')))}</b>"
                         f"<span style='color:{GRAY}'>　{info.get('level', '')} · {'进行中' if raised else '已解除'} · "
                         f"{time.strftime('%H:%M:%S', time.localtime(e.timestamp))}{where}</span>"
                         + (f"<br>　　{html.escape(str(info['advice']))}" if info.get("advice") else ""))
        self.alarm_text.setText("<br>".join(lines) or "这次连接里 VFC 没报过告警（local/uav/vfc/events）")

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

    def current_task(self):
        """当前连接里 VFC 任务状态报的 flight_id（当前任务的 bid）；换了连接、新连接还没收到任务状态时为空，不沿用上一个连接的"""
        state = self.latest.get("nav_status")
        if not state or state.session != self.app.mqtt.session:
            return ""
        info = state.payload.get("info")
        return str(info.get("flight_id") or "") if isinstance(info, dict) else ""

    def _task_bid(self):
        """当前任务的 bid；还没有任务时新生成一个"""
        return self.current_task() or str(uuid.uuid4())

    def _command(self, method, data=None, bid=None, profile=None):
        """按无人机目录生成一条 VFC 指令（新 tid、ts），模拟 GW 发出；开关没开时由 MQTT 出口拒绝并提示"""
        item = next(entry for entry in self.app.control.uav_catalog if entry["method"] == method)
        payload = make_command(item)
        payload["bid"] = bid or self._task_bid()
        if data is not None:
            payload["data"] = data
        if profile:
            # 航线的环境放在指令顶层
            payload["profile"] = profile
        return self.app._emit(payload, json.dumps(payload, ensure_ascii=False), item["topic"], "vfc")

    def _teleop_page(self):
        """设备选无人机时的“遥控与载荷”：VFC 有的指令（暂停、继续、返航、降落、结束航线）照常发；实时摇杆、起飞、云台和拍照
        现在没有接口，先放着不可用，接口有了再接"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.app._page_header(layout, "无人机 · 遥控与载荷", "任务指令经 VFC 下发，带当前任务的 bid；实时摇杆、起飞和载荷还没有接口，先显示不执行")
        row = QHBoxLayout()
        row.setSpacing(16)
        flight, fv = card(10, 18)
        fv.addWidget(label("飞行控制（VFC）", "sectionTitle"))
        fv.addWidget(label("实时摇杆没有接口，方向键先不可用；飞控状态看总览里的 Bridge 卡片", "caption", True))
        pad = QGridLayout()
        pad.setSpacing(10)
        for index, text in enumerate(("↖", "↑ W", "↗", "← A", "停", "D →", "⟲ Q", "↓ S", "E ⟳")):
            key = QPushButton(text)
            key.setObjectName("padStop" if text == "停" else "pad")
            key.setFixedSize(68, 56)
            key.setEnabled(False)
            pad.addWidget(key, index // 3, index % 3)
        holder = QHBoxLayout()
        holder.addStretch()
        holder.addLayout(pad)
        holder.addStretch()
        fv.addLayout(holder)
        fv.addWidget(label("任务快捷指令", "groupHeader"))
        grid = QGridLayout()
        grid.setSpacing(8)
        actions = (("一键起飞（暂无接口）", None), ("降落 land", "land"), ("暂停 pause_nav", "pause_nav"),
                   ("继续 continue_nav", "continue_nav"), ("返航 return_home", "start_nav_return_home"), ("结束航线 stop_nav", "stop_nav"))
        for index, (text, method) in enumerate(actions):
            item = QPushButton(text)
            item.setObjectName("tile")
            item.setEnabled(bool(method))
            if method:
                item.clicked.connect(lambda _, m=method: self._quick(m))
            grid.addWidget(item, index // 3, index % 3)
        fv.addLayout(grid)
        fv.addStretch()
        row.addWidget(flight, 1)
        payload, pv = card(10, 18)
        pv.addWidget(label("云台与载荷", "sectionTitle"))
        pv.addWidget(label("无人机的 PTZ、Media 还没接入，下面先不可用；接入后和狗一样走 local/uav/ptz、media", "caption", True))
        screen = label("还没有视频流", "caption")
        screen.setAlignment(Qt.AlignCenter)
        screen.setMinimumHeight(200)
        screen.setStyleSheet("background:#111114;color:#8E8E93;border-radius:14px")
        pv.addWidget(screen)
        tools = QGridLayout()
        tools.setSpacing(8)
        for index, text in enumerate(("拍照", "录像", "识别抓拍", "云台回中", "可见光", "红外测温")):
            item = QPushButton(text)
            item.setObjectName("tile")
            item.setEnabled(False)
            tools.addWidget(item, index // 3, index % 3)
        pv.addLayout(tools)
        pv.addStretch()
        row.addWidget(payload, 1)
        layout.addLayout(row, 1)
        return page

    def _mission_page(self):
        """设备选无人机时的“航线任务”：常用 VFC 指令一键发，航线在表格里编，发出后到状态总览看进度"""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        self.app._page_header(outer, "无人机 · 航线任务与地图", "模拟 GW 给 VFC 发指令，暂停、继续带当前任务的 bid；顶栏打开“允许下发”才会真的发")
        # 四张卡片竖着排，窗口矮时整页滚动
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 6, 0)
        outer.addWidget(scroller(body), 1)
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
        # 建图：停止时保不保存地图由人明确选，两个按钮分开，不给默认
        mapping, mv = card(8, 16)
        mv.addWidget(label("建图", "sectionTitle"))
        status = QHBoxLayout()
        self.mapping_line = label("", "caption", True)
        self.mapping_line.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status.addWidget(self.mapping_line, 1)
        status.addWidget(button("复制 session_id", self._copy_mapping_session, "link"))
        mv.addLayout(status)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(button("开始建图", self._start_mapping))
        row.addSpacing(12)
        row.addWidget(button("停止并保存地图", lambda: self._stop_mapping(True)))
        row.addWidget(button("停止，不保存地图", lambda: self._stop_mapping(False)))
        row.addStretch()
        mv.addLayout(row)
        layout.addWidget(mapping)
        layout.addSpacing(10)
        layout.addWidget(self._maps_card())
        layout.addSpacing(10)
        route, rv = card(8, 16)
        head = QHBoxLayout()
        head.addWidget(label("编辑航线", "sectionTitle"))
        head.addStretch()
        head.addWidget(button("发送航线", self._send_route, "primary"))
        rv.addLayout(head)
        self.route = RouteEditor()
        self.route.setMinimumHeight(360)
        rv.addWidget(self.route, 1)
        layout.addWidget(route, 1)
        return page

    def _maps_card(self):
        """地图：查列表、选用定位地图、生成资产、改名、删除；VFC 照转 Bridge 的 map.*，能不能做由 Bridge 判"""
        maps, mv = card(8, 16)
        head = QHBoxLayout()
        head.addWidget(label("地图", "sectionTitle"))
        head.addStretch()
        head.addWidget(button("刷新地图列表", lambda: self._map_command("get_map_list", {})))
        mv.addLayout(head)
        self.map_line = label("", "caption", True)
        self.map_line.setTextFormat(Qt.RichText)
        self.map_line.setTextInteractionFlags(Qt.TextSelectableByMouse)
        mv.addWidget(self.map_line)
        self.map_table = QTableWidget(0, 6)
        self.map_table.setHorizontalHeaderLabels(["名称", "map_id", "大小 MB", "选用", "资产", "说明"])
        self.map_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.map_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.map_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.map_table.setShowGrid(False)
        self.map_table.verticalHeader().hide()
        self.map_table.verticalHeader().setDefaultSectionSize(30)
        self.map_table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((180, 220, 70, 50, 60)):
            self.map_table.horizontalHeader().resizeSection(column, width)
        self.map_table.setMinimumHeight(150)
        mv.addWidget(self.map_table)
        row = QHBoxLayout()
        row.setSpacing(6)
        for text, slot in (("选用为定位地图", self._select_map), ("生成地图资产", self._build_map), ("改名…", self._rename_map),
                           ("航线用这张图", self._route_map), ("删除…", self._delete_map)):
            row.addWidget(button(text, slot))
        row.addStretch()
        mv.addLayout(row)
        self.maps_drawn = None
        return maps

    def _map_command(self, method, data):
        # 地图指令不属于哪个任务，bid 每次新生成
        if self._command(method, data, str(uuid.uuid4())):
            self.app.notify(f"已发出 {method}，结果看指令时间线和地图状态")

    def _chosen_map(self):
        row = self.map_table.currentRow()
        maps = self.maps[1] if self.maps else []
        if row < 0 or row >= len(maps):
            self.app.notify("先点“刷新地图列表”，再选一张地图")
            return None
        return maps[row]

    def _select_map(self):
        chosen = self._chosen_map()
        if chosen:
            self._map_command("loc_load_map", {"map_id": chosen.get("map_id")})

    def _build_map(self):
        # build_map 按建图会话生成资产，会话号就是地图列表里的 map_id
        chosen = self._chosen_map()
        if chosen:
            self._map_command("build_map", {"session_id": chosen.get("map_id")})

    def _rename_map(self):
        chosen = self._chosen_map()
        if not chosen:
            return
        name, ok = QInputDialog.getText(self.app, "地图改名", f"{chosen.get('map_id')} 的新名称", text=str(chosen.get("name") or ""))
        if ok and name.strip():
            self._map_command("rename_map_name", {"map_id": chosen.get("map_id"), "new_name": name.strip()})

    def _delete_map(self):
        chosen = self._chosen_map()
        if not chosen:
            return
        # 删设备上的地图不能恢复，问一次
        answer = QMessageBox.question(self.app, "删除地图", f"删除设备上的地图 {chosen.get('name')}（{chosen.get('map_id')}）？删了不能恢复")
        if answer == QMessageBox.Yes:
            self._map_command("remove_map_by_id", {"map_ids": [chosen.get("map_id")]})

    def _route_map(self):
        chosen = self._chosen_map()
        if chosen:
            self.route.map_id.setText(str(chosen.get("map_id") or ""))
            self.route.profile.set_index(0)

    def _render_maps(self):
        """地图卡：VFC 的地图和定位状态一行，地图列表按最近一次 get_map_list 的回执"""
        parts = []
        for key in ("map_status", "loc_status"):
            e = self.latest.get(key)
            if e:
                word, note, _, extra = reading(e)
                color = QUALITY_COLORS[quality(e)]
                parts.append(f"<b>{'地图' if key == 'map_status' else '定位'}</b>　<b style='color:{color}'>{html.escape(word or '—')}</b>"
                             f"　{html.escape('　'.join(p for p in (note, extra) if p))}")
        listed = f"列表 {time.strftime('%H:%M:%S', time.localtime(self.maps[0].timestamp))} 查的，共 {len(self.maps[1])} 张" if self.maps else "还没查过地图列表"
        self.map_line.setText("<br>".join(parts + [f"<span style='color:{GRAY}'>{listed}</span>"]))
        if self.maps_drawn is self.maps:
            return
        self.maps_drawn = self.maps
        maps = self.maps[1] if self.maps else []
        self.map_table.setRowCount(len(maps))
        for index, item in enumerate(maps):
            item = item if isinstance(item, dict) else {}
            notes = [n for n in ("不完整" if item.get("incomplete") else "", f"错误 {item['error']}" if item.get("error") else "",
                                 "有原始数据" if item.get("has_raw") else "") if n]
            values = (item.get("name"), item.get("map_id"), item.get("size"), "✓" if item.get("selected") else "",
                      "有" if item.get("has_assets") else "没有", "，".join(notes))
            for column, value in enumerate(values):
                self.map_table.setItem(index, column, QTableWidgetItem("" if value is None else str(value)))

    def _quick(self, method):
        if self._command(method):
            self.app.notify(f"已发出 {method}，结果看状态总览的指令时间线")

    def _start_mapping(self):
        # 建图是新任务，bid 新生成，不借用上一个任务的
        bid = str(uuid.uuid4())
        if self._command("start_mapping", {}, bid):
            self.app.notify(f"已发出开始建图（bid {bid}），进度看 VFC 任务和 VFC 建图")

    def _stop_mapping(self, save):
        if self._command("stop_mapping", {"save": save}):
            self.app.notify("已发出停止建图（" + ("保存地图" if save else "不保存地图") + "），保没保存看 VFC 建图状态")

    def mapping_session(self):
        """当前连接里 VFC 建图状态报的 session_id，生成地图时要用；没有时为空"""
        state = self.latest.get("mapping_status")
        if not state or state.session != self.app.mqtt.session:
            return ""
        info = state.payload.get("info")
        return str(info.get("session_id") or "") if isinstance(info, dict) else ""

    def _copy_mapping_session(self):
        session = self.mapping_session()
        if not session:
            self.app.notify("还没有收到建图的 session_id")
            return
        QApplication.clipboard().setText(session)
        self.app.notify(f"已复制 session_id {session}")

    def _send_route(self):
        try:
            data, bid = self.route.route()
        except ValueError as error:
            self.route.error.setText(str(error))
            return
        self.route.error.setText("")
        if any(point["actions"] for point in data["points"]):
            self.app.notify("VFC 本期不执行航点动作：照飞，到点报 nav_action_progress，动作全部 skipped")
        if self._command("start_multi_nav_by_points", data, bid or str(uuid.uuid4()), self.route.profile_name()):
            self.app.notify(f"已发出航线（bid {bid}），进度看状态总览的 VFC 卡片和关键变化")

    def _render_task(self):
        mapping = self.latest.get("mapping_status")
        if mapping:
            word, msg, _, extra = reading(mapping)
            self.mapping_line.setText(f"<b>{html.escape(word)}</b>　{html.escape(msg)}　{html.escape(extra)}")
        else:
            self.mapping_line.setText("还没有收到 VFC 的建图状态（旧版 VFC 没有建图功能时也是这样）")
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
        # 只看当前这次无人机连接的
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
        """双击：单独开窗口看这一请求的全部报文和证据链"""
        row = self.rows[index]
        self.app.open_request(row["events"][0], row["events"])


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


def yaw_of(orientation):
    """四元数 → 绕 z 轴的航向角（度，保留两位）；读进来的航线在表格里只显示这一个角"""
    o = orientation if isinstance(orientation, dict) else {}
    x, y, z, w = (float(o.get(k, 0) or 0) for k in "xyzw")
    return round(math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))), 2)


def format_actions(actions):
    """动作列表 → “pause 5; take_photo”，和 parse_actions 互逆"""
    parts = []
    for action in actions if isinstance(actions, list) else []:
        params = ",".join(json.dumps(v, ensure_ascii=False) for v in action.get("param") or [])
        parts.append(f"{action.get('action', '')} {params}".strip())
    return "; ".join(parts)


class PointsTable(QWidget):
    """航点表格：每个航点填 x、y、z（米，z 相对起飞点）、航向角 yaw（度）和动作；不显示四元数，取值时换算。
    航线任务页和控制调试页的开始航线共用；表格改了发 changed"""

    COLUMNS = ["x", "y", "z", "航向角 yaw（°）", "动作（本期不执行）"]
    changed = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("z 按 Bridge 只收 0.20～4.50 m；航向角发出时换成四元数（x=0、y=0、z=sin(yaw/2)、w=cos(yaw/2)）。"
                               "动作写法如“pause 5; take_photo”；VFC 本期不执行动作，照飞，到点报 nav_action_progress 全部 skipped", "caption", True))
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.itemChanged.connect(lambda _: self.changed.emit())
        layout.addWidget(self.table, 1)
        self.error = label("", "caption", True)
        self.error.setStyleSheet(f"color:{RED}")
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addWidget(button("加一个航点", lambda: self._add(("0", "0", "1.2", "0", ""))))
        buttons.addWidget(button("删掉选中的", self._remove))
        buttons.addStretch()
        layout.addLayout(buttons)

    def _add(self, values):
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate(values):
            self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.blockSignals(False)
        self.changed.emit()

    def _remove(self):
        self.table.removeRow(self.table.currentRow())
        self.changed.emit()

    def load(self, points):
        """按报文里的 points 填表，四元数换成航向角；不发 changed。points 里有认不出的点时抛 ValueError，表格不动"""
        rows = []
        for index, point in enumerate(points):
            try:
                position = point.get("position") or {}
                values = [str(position.get(k, "")) for k in "xyz"]
                rows.append(values + [f"{yaw_of(point.get('orientation')):g}", format_actions(point.get("actions"))])
            except (AttributeError, TypeError, ValueError):
                raise ValueError(f"第 {index + 1} 个航点不是表格能认的格式")
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for values in rows:
                row = self.table.rowCount()
                self.table.insertRow(row)
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QTableWidgetItem(value))
        finally:
            self.table.blockSignals(False)
        self.error.setText("")

    def points(self):
        """表格 → points；填得不对时抛 ValueError，文字说明是哪一行"""
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
        return points


class RouteEditor(QWidget):
    """航线任务页上编辑一条航线：map_id、task_name、bid 和航点表格，生成 start_multi_nav_by_points 的 data"""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # 环境：已知地图（map_id 要是当前选用的地图，可以不填）或未知环境边建图边飞
        self.profile = Segmented(["已知地图 known_navigation", "未知环境 online_navigation"])
        env = QHBoxLayout()
        env.addWidget(label("环境", "caption"))
        env.addWidget(self.profile)
        env.addStretch()
        layout.addLayout(env)
        top = QHBoxLayout()
        self.map_id, self.task_name = QLineEdit(), QLineEdit("test_route")
        self.map_id.setPlaceholderText("不填就用当前选用的地图")
        self.bid = QLineEdit(str(uuid.uuid4()))
        for text, field in (("map_id", self.map_id), ("task_name", self.task_name), ("bid（任务 ID）", self.bid)):
            top.addWidget(label(text, "caption"))
            top.addWidget(field, 1)
        top.addWidget(button("换新 bid", lambda: self.bid.setText(str(uuid.uuid4())), "link"))
        layout.addLayout(top)
        self.points = PointsTable()
        self.points._add(("0", "1", "1.2", "0", ""))
        self.table, self.error, self._add = self.points.table, self.points.error, self.points._add
        layout.addWidget(self.points, 1)

    def profile_name(self):
        return ("known_navigation", "online_navigation")[self.profile.index()]

    def route(self):
        """(data, bid)；填得不对时抛 ValueError，文字说明是哪一行；map_id 空着不带，未知环境也不带"""
        data = {"task_name": self.task_name.text().strip(), "points": self.points.points()}
        if self.map_id.text().strip() and self.profile_name() == "known_navigation":
            data = {"map_id": self.map_id.text().strip(), **data}
        return data, self.bid.text().strip()
