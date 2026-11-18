"""控制调试页：完整协议指令的目录、模板、参数表单、发送和本次结果"""

import json
import uuid

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup, QGridLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QRadioButton, QSplitter,
    QVBoxLayout, QWidget,
)

from .uav import PointsTable
from ..registry import find, local_payload, make_command, MODULES, refresh_ids, route, uav_entries
from ..trace_view import TraceView
from ..transport import simulate
from ..ui import (
    button, card, CommandDelegate, DEVICES, GRAY, label, MODULE_COLORS, pill, RED, SCENARIOS, scroller, Segmented,
    Switch, tint,
)

# 无人机开始新任务的 VFC 指令：bid 新生成，按钮是“换新 bid”；其他 VFC 指令带当前任务的 bid
NEW_TASK = ("start_multi_nav_by_points", "start_mapping")


class ControlPage:
    """控制调试页：指令目录、参数表单、发送和本次结果"""

    def __init__(self, app):
        self.app = app
        self.uav_catalog = uav_entries()
        self.form_key = "data"
        self.selected = None
        self.recent = []
        self.form_syncing = False
        self.watch, self.watch_size = None, 0

    def _control_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.app._page_header(layout, "指令与链路", "协议目录、参数和 JSON 双向同步、发送后看 ACK 证据链；沙盒里可以注入故障", button("演示一次故障定位", self._demo, "link"))
        # 全部指令收在弹出列表里，让出宽度给编辑和结果；最近发送过的点一下回到它的草稿接着改，不会自动发送
        picker = QHBoxLayout()
        picker.setSpacing(6)
        self.picker_btn = button("全部指令 ▾", self._open_picker, "primary")
        self.picker_btn.setToolTip("任何页面按 Ctrl+K 也能打开")
        picker.addWidget(self.picker_btn)
        picker.addSpacing(8)
        self.recent_row = QHBoxLayout()
        self.recent_row.setSpacing(6)
        picker.addLayout(self.recent_row, 1)
        layout.addLayout(picker)
        self._show_recent()

        # 弹出层是独立窗口：外层透明，里面一张卡片，才能露出圆角
        self.picker = QWidget(self.app, Qt.Popup)
        self.picker.setAttribute(Qt.WA_TranslucentBackground)
        self.picker.resize(400, 540)
        sheet, lv = card(8, 12)
        outer_box = QVBoxLayout(self.picker)
        outer_box.setContentsMargins(0, 0, 0, 0)
        outer_box.addWidget(sheet)
        self.command_search = QLineEdit()
        self.command_search.setObjectName("search")
        self.command_search.setPlaceholderText("搜索中文名、method 或模块，回车选第一条")
        self.command_search.textChanged.connect(self._commands)
        self.command_search.returnPressed.connect(self._pick_first)
        self.commands = QListWidget()
        self.commands.setObjectName("commands")
        self.commands.setItemDelegate(CommandDelegate(self.commands))
        self.commands.currentItemChanged.connect(self._select_command)
        self.commands.itemClicked.connect(lambda item: item.data(Qt.UserRole) and self.picker.hide())
        lv.addWidget(self.command_search)
        lv.addWidget(self.commands)

        middle, mv = card(8, 18)
        head = QHBoxLayout()
        self.command_heading = label("选择指令", "heading")
        self.command_module = pill("", GRAY)
        head.addWidget(self.command_heading)
        head.addWidget(self.command_module)
        head.addStretch()
        mv.addLayout(head)
        self.contract = label("", "caption", True)
        mv.addWidget(self.contract)
        mv.addSpacing(4)
        mv.addWidget(label("Topic", "groupHeader"))
        self.topic = QLineEdit()
        mv.addWidget(self.topic)
        form_head = QHBoxLayout()
        form_head.addWidget(label("参数", "groupHeader"))
        self.form_note = label("", "caption")
        form_head.addWidget(self.form_note, 1)
        mv.addLayout(form_head)
        holder = QWidget()
        self.form_grid = QGridLayout(holder)
        self.form_grid.setContentsMargins(0, 0, 0, 0)
        self.form_grid.setHorizontalSpacing(10)
        self.form_grid.setVerticalSpacing(6)
        self.form_area = scroller(holder)
        self.form_area.setMaximumHeight(120)
        mv.addWidget(self.form_area)
        # 无人机开始航线的 points 用航点表格编：只填航向角，不手填四元数
        self.points_table = PointsTable()
        self.points_table.setMinimumHeight(200)
        self.points_table.changed.connect(self._points_edited)
        self.points_table.hide()
        mv.addWidget(self.points_table)
        mv.addWidget(label("Payload", "groupHeader"))
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("code")
        self.editor.setMinimumHeight(110)
        self.form_timer = QTimer(self.app)
        self.form_timer.setSingleShot(True)
        self.form_timer.setInterval(300)
        self.form_timer.timeout.connect(self._rebuild_form)
        self.editor.textChanged.connect(lambda: None if self.form_syncing else self.form_timer.start())
        mv.addWidget(self.editor, 1)
        self.scenario_row = QWidget()
        sr = QHBoxLayout(self.scenario_row)
        sr.setContentsMargins(0, 4, 0, 0)
        sr.addWidget(label("沙盒结果", "caption"))
        self.scenario = Segmented([name for name, _ in SCENARIOS])
        sr.addWidget(self.scenario)
        sr.addStretch()
        mv.addWidget(self.scenario_row)
        actions = QHBoxLayout()
        self.new_tid_btn = button("换新 tid", self._new_tid)
        actions.addWidget(self.new_tid_btn)
        actions.addWidget(button("恢复模板", lambda: self._template()))
        actions.addWidget(button("存为用例", self.app.suite._save_case))
        actions.addStretch()
        self.send_btn = button("发送", self._send, "primary")
        actions.addWidget(self.send_btn)
        mv.addLayout(actions)
        # 编辑在上、回复在下，发送后不用转头找结果
        work = QSplitter(Qt.Vertical)
        work.setHandleWidth(12)
        # 开始航线多了航点表格，窗口矮时编辑卡整体滚动，不把表格、Payload 和按钮挤到一起
        work.addWidget(scroller(middle))

        right, rv = card(8, 16)
        rh = QHBoxLayout()
        self.result_title = label("本次结果", "sectionTitle")
        rh.addWidget(self.result_title)
        rh.addStretch()
        rh.addWidget(button("单独窗口打开", self._open_watch, "link"))
        rv.addLayout(rh)
        self.result = TraceView("发送后在这里看 ACK、状态和失败原因")
        self.result.original.connect(self.app._original)
        rv.addWidget(self.result, 1)
        work.addWidget(right)
        work.setSizes([560, 400])
        layout.addWidget(work, 1)
        return page

    def _quick_pick(self):
        self.app._go(self.app.CONTROL)
        self._open_picker()

    def _open_picker(self):
        self.picker.move(self.picker_btn.mapToGlobal(self.picker_btn.rect().bottomLeft()))
        # 弹出层是独立窗口，用窗口透明度淡入
        self.picker.setWindowOpacity(0.0)
        self.picker.show()
        pop = QPropertyAnimation(self.picker, b"windowOpacity", self.picker)
        pop.setDuration(150)
        pop.setStartValue(0.0)
        pop.setEndValue(1.0)
        pop.setEasingCurve(QEasingCurve.OutCubic)
        pop.start(QPropertyAnimation.DeleteWhenStopped)
        self.command_search.setFocus()
        self.command_search.selectAll()

    def _pick_first(self):
        item = next((self.commands.item(i) for i in range(self.commands.count()) if self.commands.item(i).data(Qt.UserRole)), None)
        if item:
            self.commands.setCurrentItem(item)
            self.picker.hide()

    def _commands(self):
        """按搜索词重建列表；不改变当前指令，启动时才默认选第一条"""
        self.commands.blockSignals(True)
        self.commands.clear()
        needle = self.command_search.text().lower()
        first, group = None, None
        # 目录按文档顺序登记，同一模块的方法分散在多处；按模块归拢后分节，模块内保持文档顺序
        ordered = sorted(self._catalog(), key=lambda item: MODULES.index(item["module"]) if item["module"] in MODULES else len(MODULES))
        for item in ordered:
            available = item["category"] == "command" or (item["module"] == "gw" and item["method"] in {"oss_config_get", "online", "heartbeat"})
            words = " ".join((item["method"], item["module"], item["title"], item["summary"])).lower()
            if not available or (needle and needle not in words):
                continue
            if item["module"] != group:
                group = item["module"]
                header = QListWidgetItem(item["label"].split(" · ")[0])
                header.setFlags(Qt.NoItemFlags)
                header.setForeground(QColor(GRAY))
                font = header.font()
                font.setPixelSize(11)
                font.setBold(True)
                header.setFont(font)
                self.commands.addItem(header)
            entry = QListWidgetItem(item["title"])
            entry.setData(Qt.UserRole, item)
            entry.setToolTip(f"{item['method']}：{item['summary']}")
            self.commands.addItem(entry)
            first = first or entry
            if self.selected and item["method"] == self.selected["method"]:
                self.commands.setCurrentItem(entry)
        self.commands.blockSignals(False)
        if first and not self.selected:
            self.commands.setCurrentItem(first)

    def _uav(self):
        """连接卡里选的设备是不是无人机：编辑指令跟着选的设备走，实际能不能发看活动连接"""
        return DEVICES[self.app.device_box.currentText()] == "uav"

    def _catalog(self):
        return self.uav_catalog if self._uav() else self.app.catalog

    def device_changed(self):
        """换设备类型：指令目录换成那种设备的，未发出的草稿作废（报文格式不同）"""
        self.selected = None
        self.app.drafts.clear()
        self._commands()

    def _select_command(self, current, previous=None):
        if current and current.data(Qt.UserRole):
            if self.selected:
                self.app.drafts[self.selected["method"]] = (self.topic.text(), self.editor.toPlainText())
            self.selected = current.data(Qt.UserRole)
            self._template(self.app.drafts.pop(self.selected["method"], None))

    def _open_command(self, method):
        self.command_search.clear()
        for row in range(self.commands.count()):
            item = self.commands.item(row).data(Qt.UserRole)
            if item and item["method"] == method:
                self.commands.setCurrentRow(row)
                self.app._go(self.app.CONTROL)
                return
        self.app.notify(method + " 是上报类条目，不在控制列表")

    def _topic_for(self, item):
        """直连时按 GW 对云端模板的路由选模块 Topic，例如喊话器目录归 MEDIA，GW 实际转给 audio"""
        # 无人机的指令目录每条自带 Topic（VFC 或 Bridge 的指令主题）
        if item.get("topic"):
            return item["topic"]
        # pcd_switch 这类本地指令云端没有，哪种模式都发本地 Topic
        if item.get("local_only"):
            return f"local/robotdog/{item['module']}/command"
        if self.app.mode in {"sandbox", "gw", "offline"}:
            return f"tta/robotdog/{self.app.sn.text().strip()}/services"
        return f"local/robotdog/{route(item['template'])}/command"

    def _update_topic(self):
        self.topic.setText(self._topic_for(self.selected))

    def _rebuild_form(self):
        """按编辑框里 JSON 的 data 生成参数表单；JSON 无效时表单暂停，不改用户的原文"""
        while self.form_grid.count():
            widget = self.form_grid.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        self.points_table.hide()
        try:
            payload = json.loads(self.editor.toPlainText())
        except ValueError:
            self.form_note.setText("JSON 暂时无效，表单暂停同步；照原文仍可发送")
            self.form_area.hide()
            return
        # Bridge 的命令参数在 args 里，其他协议在 data 里
        self.form_key = "args" if isinstance(payload, dict) and "args" in payload and "data" not in payload else "data"
        data = payload.get(self.form_key) if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            self.form_note.setText("没有 data 对象，直接编辑下方 JSON")
            self.form_area.hide()
            return
        self.form_note.setText("改这里会同步到 JSON；列表和对象请在 JSON 里改" if data else "data 为空")
        self.form_area.setVisible(bool(data))
        # 狗也有 VFC 模块，航点表格只给无人机的开始航线
        route = self._uav() and self.selected["module"] == "vfc" and isinstance(data.get("points"), list)
        if route:
            try:
                self.points_table.load(data["points"])
                data = {k: v for k, v in data.items() if k != "points"}
            except ValueError as error:
                # 手改成表格认不出的 points 时照原文留在 JSON 里，表格先收起，改回能认的再显示
                self.form_note.setText(f"航点表格暂停：{error}；直接编辑下方 JSON")
                route = False
        self.points_table.setVisible(route)
        for index, (key, value) in enumerate(data.items()):
            row, column = divmod(index, 2)
            self.form_grid.addWidget(label(key, "caption"), row, column * 2)
            if key == "save" and self._uav() and self.selected["method"] == "stop_mapping":
                field = self._save_choice(value)
            elif isinstance(value, bool):
                field = Switch()
                field.setChecked(value)
                field.toggled.connect(lambda on, key=key: self._form_edited(key, on))
            elif isinstance(value, (int, float, str)) or value is None:
                field = QLineEdit("" if value is None else str(value))
                field.textEdited.connect(lambda text, key=key, kind=type(value), w=field: self._form_text(key, kind, text, w))
            else:
                field = QLineEdit(json.dumps(value, ensure_ascii=False))
                field.setReadOnly(True)
                field.setToolTip("列表/对象请在 JSON 里编辑")
            self.form_grid.addWidget(field, row, column * 2 + 1)

    def _save_choice(self, value):
        """停止建图的 save：“保存地图”“不保存”两个选项，模板里不选，必须由人选一个"""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        group = QButtonGroup(box)
        for text, choice in (("保存地图", True), ("不保存", False)):
            option = QRadioButton(text)
            option.setChecked(value is choice)
            option.toggled.connect(lambda on, choice=choice: on and self._form_edited("save", choice))
            group.addButton(option)
            row.addWidget(option)
        row.addStretch()
        return box

    def _points_edited(self):
        try:
            points = self.points_table.points()
        except ValueError as error:
            # 填错的格子不写回 JSON，改对了再同步
            self.points_table.error.setText(str(error))
            return
        self.points_table.error.setText("")
        self._form_edited("points", points)

    def _form_text(self, key, kind, text, widget):
        """按原字段类型解析输入；数字字段整数小数都收（模板示例 0 不代表只能填整数），解析不了只标红，不写回 JSON"""
        try:
            if kind in (int, float):
                value = json.loads(text)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(text)
            else:
                value = None if kind is type(None) and not text else text
        except ValueError:
            widget.setStyleSheet(f"border:1px solid {RED}")
            return
        widget.setStyleSheet("")
        self._form_edited(key, value)

    def _form_edited(self, key, value):
        """只改 data（Bridge 为 args）里这一个字段，其他字段（包括模板里没有的）原样保留"""
        try:
            payload = json.loads(self.editor.toPlainText())
        except ValueError:
            return
        payload[self.form_key][key] = value
        self.form_syncing = True
        cursor = self.editor.textCursor().position()
        self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
        cursor_obj = self.editor.textCursor()
        cursor_obj.setPosition(min(cursor, len(self.editor.toPlainText())))
        self.editor.setTextCursor(cursor_obj)
        self.form_syncing = False

    def _new_tid(self):
        """只换 tid、ts（有 bid 也换），参数和其他字段保持；用于同样参数再发一次。无人机开始航线只换 bid（新任务）"""
        try:
            payload = json.loads(self.editor.toPlainText())
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            self.app.notify("不是 JSON 对象，没法换 tid，原文保持不变")
            return
        if self._uav():
            payload["bid"] = str(uuid.uuid4())
        else:
            refresh_ids(payload)
        self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    def _vfc_bid(self, payload):
        """VFC 指令的 bid（任务 ID，必填）：开始航线用填了的，没填就新生成；暂停、返航、降落这些带当前任务的，还没有任务时保留填的"""
        bid = str(payload.get("bid") or "")
        if payload.get("method") in NEW_TASK:
            return bid or str(uuid.uuid4())
        return self.app.uav.current_task() or bid or str(uuid.uuid4())

    def _template(self, draft=None):
        """显示当前指令；draft 为之前没发出的 (Topic, 正文)，没有时按目录生成新模板（新 tid）"""
        module = self.selected["module"]
        self.command_heading.setText(f"{self.selected['title']}　{self.selected['method']}")
        self.command_module.setText(module.upper())
        color = MODULE_COLORS.get(module, GRAY)
        self.command_module.setStyleSheet(f"background:{tint(color, 34)};color:{color};border-radius:9px;"
                                          "padding:2px 9px;font-size:11px;font-weight:600")
        note = self.selected["unverified"]
        if module == "gw":
            note = "设备发起的上线/心跳/配置请求，沙盒里演示云端应答"
        self.contract.setText(self.selected["summary"] + ("　·　" + note if note else ""))
        # 无人机每次发送都自动换 tid / id，不用手换；开始航线要换的是 bid（新任务），其他指令的 bid 跟着当前任务
        uav, route = self._uav(), self.selected["method"] in NEW_TASK
        self.new_tid_btn.setText("换新 bid" if uav and route else "换新 tid")
        self.new_tid_btn.setVisible(not uav or route)
        self.contract.setToolTip(self.selected["label"].split(" · ")[0] + "　·　" + self.selected["source"])
        if draft:
            self.editor.setPlainText(draft[1])
            self.topic.setText(draft[0])
        else:
            payload = make_command(self.selected)
            if self.app.mode == "direct":
                payload = local_payload(payload)
            if module == "vfc" and self._uav():
                payload["bid"] = self._vfc_bid(payload)
            self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            self._update_topic()
        self._rebuild_form()

    def _send(self):
        """发送编辑框内容；非对象或非法 JSON 照原文发出，沙盒里只记录不模拟"""
        if self._uav() and self.app.mode != "direct":
            self.app.notify("无人机只能直连：连接方式选“直连模块”")
            return
        text, topic = self.editor.toPlainText(), self.topic.text().strip()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        data = payload.get("data") if isinstance(payload, dict) else None
        if self._uav() and isinstance(payload, dict) and payload.get("method") == "stop_mapping" \
                and not (isinstance(data, dict) and isinstance(data.get("save"), bool)):
            self.app.notify("停止建图要先选“保存地图”还是“不保存”")
            return
        if self._uav() and isinstance(payload, dict):
            # VFC 和 Bridge 按 tid / id 认请求，同一个编号再发会被当成重复请求（Bridge 直接回旧的缓存结果、不执行），
            # 每次发送都换新编号和当前 ts；bid 是任务 ID，stop_nav 这类要带当前任务的，不换
            refresh_ids(payload, keep_bid=True)
            if self.selected["module"] == "vfc":
                payload["bid"] = self._vfc_bid(payload)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            self.editor.setPlainText(text)
        record = payload if isinstance(payload, dict) else {}
        sent = self.app._emit(payload, text, topic, self.selected["module"])
        self._flash_send("✓ 已发出" if sent else "✗ 没发出")
        if not sent:
            return
        self.watch, self.watch_size = sent, 0
        self.result_title.setText(f"本次结果 · {self.selected['title']}")
        self._remember(self.selected)
        self.app._refresh()
        if not record:
            self.app.notify("不是 JSON 对象，无法按对象模拟，已记录原文" if self.app.mode == "sandbox" else "已按原文发送")
        else:
            text = "沙盒已模拟完整链路" if self.app.mode == "sandbox" else "已发送，等待 ACK"
            if self.selected["module"] == "bridge" and self.app.uav.vfc_online():
                text += "；VFC 在线，直接发 Bridge 会绕开 VFC"
            self.app.notify(text + ("；" + self.app.demo_note if self.app.demo_note else ""))

    def _remember(self, item):
        self.recent = [item] + [r for r in self.recent if r["method"] != item["method"]][:5]
        self._show_recent()

    def _show_recent(self):
        while self.recent_row.count():
            widget = self.recent_row.takeAt(0).widget()
            if widget:
                # deleteLater 要等下一轮事件循环才释放，先隐藏，免得这一帧和新内容叠着画
                widget.hide()
                widget.deleteLater()
        self.recent_row.addWidget(label("最近", "caption"))
        for item in self.recent:
            chip = button(item["title"], lambda checked=False, m=item["method"]: self._open_command(m), "chip")
            chip.setToolTip(item["method"])
            self.recent_row.addWidget(chip)
        if not self.recent:
            self.recent_row.addWidget(label("发送过的指令会出现在这里，点一下回到它接着改", "caption"))
        self.recent_row.addStretch()

    def _flash_send(self, text):
        """发送按钮上短暂显示这次有没有发出去，1.2 s 后恢复"""
        self.send_btn.setText(text)
        QTimer.singleShot(1200, lambda: self.send_btn.setText("模拟发送" if self.app.mode == "sandbox" else "发送"))

    def _open_watch(self):
        if not self.watch:
            self.app.notify("还没有发送过指令")
            return
        self.app.open_request(self.watch)

    def _demo(self):
        payload = make_command(find("take_photo"))
        events = simulate(payload, "TEST_DOG_001", "demo-" + self.app.session, "failure")
        for event in events:
            self.app.store.add(event)
        # 演示的失败请求单独开窗口看：结论卡、证据链和原始 JSON
        self.app.open_request(events[0])

    def refresh_result(self):
        """本次结果有新消息时重画，还停在用户点开的那条上"""
        if not self.watch:
            return
        trace = self.app.store.trace(self.watch)
        if len(trace) != self.watch_size:
            self.watch_size = len(trace)
            chosen = self.result.selected
            self.result.show_trace(next((e for e in trace if chosen and e.event_id == chosen.event_id), self.watch), trace)
