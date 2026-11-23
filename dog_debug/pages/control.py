"""控制调试页：完整协议指令的目录、模板、参数表单、发送和本次结果"""

import json
import uuid

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QGridLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QRadioButton, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from .uav import PointsTable
from ..registry import find, local_payload, make_command, MODULES, refresh_ids, route, uav_entries
from ..trace_view import TraceView
from ..transport import MqttTransport, simulate
from ..uav import cloud_command, NEW_TASK

# 表单里“删掉这个字段”的取值：停止建图选“按默认”时 data 里不带 save
OMIT = object()
from ..ui import (
    button, card, CommandDelegate, DEVICES, GRAY, label, MODULE_COLORS, pill, RED, SCENARIOS, scroller, Segmented,
    Switch, tint,
)



class ControlPage:
    """控制调试页：指令目录、参数表单、发送和本次结果"""

    def __init__(self, app):
        self.app = app
        self.uav_catalog = uav_entries()
        self.form_key = "data"
        self.selected = None
        # 最近发过的按设备各记一份，换设备时换成那种设备的，不混着显示
        self.recent, self.recent_by, self.recent_device = [], {}, None
        self.form_syncing = False
        self.watch, self.watch_size = None, 0

    def _control_page(self):
        """三栏：左边协议目录（搜索、模块筛选、最近发过的），中间 Topic 和参数（快捷参数 / 原始 JSON 二选一），右边本次结果的证据链"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.new_tid_btn = button("换新 tid", self._new_tid)
        self.send_btn = button("发送", self._send, "primary")
        self.app._page_header(layout, "指令与链路", "协议目录、参数和 JSON 双向同步，发送后右边看 ACK 证据链；沙盒里可以选回复场景演示故障")
        columns = QSplitter()
        columns.setHandleWidth(14)
        columns.setChildrenCollapsible(False)

        catalog, lv = card(8, 14)
        self.command_search = QLineEdit()
        self.command_search.setObjectName("search")
        self.command_search.setPlaceholderText("搜索中文名、method 或模块，回车选第一条（Ctrl+K）")
        self.command_search.textChanged.connect(self._commands)
        self.command_search.returnPressed.connect(self._pick_first)
        lv.addWidget(self.command_search)
        # 模块筛选：全部加上当前设备目录里有的模块，换设备时重建
        self.module_chips = QGridLayout()
        self.module_chips.setSpacing(6)
        self.module_group = QButtonGroup(page)
        self.module_group.setExclusive(True)
        self.module_group.idClicked.connect(lambda _: self._commands())
        lv.addLayout(self.module_chips)
        self.commands = QListWidget()
        self.commands.setObjectName("commands")
        self.commands.setItemDelegate(CommandDelegate(self.commands))
        self.commands.currentItemChanged.connect(self._select_command)
        lv.addWidget(self.commands, 1)
        self.recent_row = QHBoxLayout()
        self.recent_row.setSpacing(6)
        recent = QWidget()
        recent.setLayout(self.recent_row)
        lv.addWidget(recent)
        # 侧栏（设备选择）先于本页建好，这里记下当前设备，换设备时才知道最近列表归谁
        self.recent_device = self.app.device_box.currentText()
        self._show_recent()
        catalog.setMinimumWidth(260)
        columns.addWidget(catalog)

        middle, mv = card(8, 18)
        head = QHBoxLayout()
        # 长 method（如 start_multi_nav_by_points）在窄屏下折到第二行，不截断
        self.command_heading = label("选择指令", "heading", True)
        self.command_module = pill("", GRAY)
        head.addWidget(self.command_heading, 1)
        head.addWidget(self.command_module, 0, Qt.AlignTop)
        mv.addLayout(head)
        self.contract = label("", "caption", True)
        # 航线这类说明很长，限高约四行、超出滚动，给下面的参数和航点表留地方
        contract = scroller(self.contract)
        contract.setMaximumHeight(68)
        mv.addWidget(contract)
        mv.addWidget(label("目标 Topic（跟着连接方式自动换）", "groupHeader"))
        self.topic = QLineEdit()
        topic_row = QHBoxLayout()
        topic_row.addWidget(self.topic, 1)
        # 模拟云端时云端指令要带 target，GW 按它转给哪个模块；默认 vfc，可以改成 fsvr 等看 GW 怎么转
        self.target_label = label("target", "caption")
        self.target_box = QComboBox()
        self.target_box.setEditable(True)
        self.target_box.addItems(["vfc", "fsvr", "gw", "ptz", "vision", "audio"])
        self.target_box.setToolTip("云端 services 路由目标：任务/建图/导航/自检默认 vfc，地图管理按协议默认 fsvr（旧版 GW 可按需切 vfc 或 gw）")
        self.target_box.setFixedWidth(96)
        self.target_box.currentTextChanged.connect(self._target_edited)
        topic_row.addWidget(self.target_label)
        topic_row.addWidget(self.target_box)
        mv.addLayout(topic_row)
        view_row = QHBoxLayout()
        self.edit_view = Segmented(["快捷参数", "原始 JSON"])
        view_row.addWidget(self.edit_view)
        self.form_note = label("", "caption")
        view_row.addWidget(self.form_note, 1)
        mv.addLayout(view_row)
        # 两种看法二选一：表单改了同步到 JSON，JSON 改了 0.3 s 后重建表单
        self.edit_pages = QStackedWidget()
        self.edit_view.changed.connect(self.edit_pages.setCurrentIndex)
        form_page = QWidget()
        fv = QVBoxLayout(form_page)
        fv.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        # 字段从上往下排，下面留白，不在中间悬着
        stack = QVBoxLayout(holder)
        stack.setContentsMargins(0, 0, 0, 0)
        self.form_grid = QGridLayout()
        stack.addLayout(self.form_grid)
        stack.addStretch()
        self.form_grid.setHorizontalSpacing(10)
        self.form_grid.setVerticalSpacing(6)
        self.form_area = scroller(holder)
        fv.addWidget(self.form_area, 1)
        # 无人机开始航线的 points 用航点表格编：只填航向角，不手填四元数
        self.points_table = PointsTable()
        self.points_table.setMinimumHeight(200)
        self.points_table.changed.connect(self._points_edited)
        self.points_table.hide()
        fv.addWidget(self.points_table, 2)
        self.edit_pages.addWidget(form_page)
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("code")
        self.editor.setMinimumHeight(110)
        self.form_timer = QTimer(self.app)
        self.form_timer.setSingleShot(True)
        self.form_timer.setInterval(300)
        self.form_timer.timeout.connect(self._rebuild_form)
        self.editor.textChanged.connect(lambda: None if self.form_syncing else self.form_timer.start())
        self.edit_pages.addWidget(self.editor)
        mv.addWidget(self.edit_pages, 1)
        self.scenario_row = QWidget()
        # 标题单独一行，窄屏下五个场景也放得下
        sr = QVBoxLayout(self.scenario_row)
        sr.setContentsMargins(0, 4, 0, 0)
        sr.setSpacing(4)
        sr.addWidget(label("沙盒回复场景", "caption"))
        self.scenario = Segmented([name for name, _ in SCENARIOS])
        sr.addWidget(self.scenario, 0, Qt.AlignLeft)
        mv.addWidget(self.scenario_row)
        # 发送放在参数下面靠右，改完参数顺手就点；单独一行，窄屏下也不和下面的按钮挤
        send_row = QHBoxLayout()
        send_row.addStretch()
        send_row.addWidget(self.new_tid_btn)
        self.send_btn.setMinimumWidth(110)
        send_row.addWidget(self.send_btn)
        mv.addLayout(send_row)
        actions = QHBoxLayout()
        actions.addWidget(button("恢复模板", lambda: self._template()))
        actions.addWidget(button("存为用例", self.app.suite._save_case))
        actions.addWidget(button("复制报文", lambda: QApplication.clipboard().setText(self.editor.toPlainText())))
        pub = button("复制命令", self._copy_pub_command)
        pub.setToolTip("复制一条 mosquitto_pub 命令，贴到板子终端里就能发同样的报文")
        actions.addWidget(pub)
        actions.addStretch()
        mv.addLayout(actions)
        columns.addWidget(middle)

        right, rv = card(8, 16)
        rh = QHBoxLayout()
        self.result_title = label("本次结果 · 证据链", "sectionTitle")
        rh.addWidget(self.result_title)
        rh.addStretch()
        rh.addWidget(button("单独窗口打开", self._open_watch, "link"))
        rv.addLayout(rh)
        self.result = TraceView("发送后在这里看 ACK、状态和失败原因")
        self.result.original.connect(self.app._original)
        rv.addWidget(self.result, 1)
        columns.addWidget(right)
        columns.setStretchFactor(0, 3)
        columns.setStretchFactor(1, 4)
        columns.setStretchFactor(2, 4)
        columns.setSizes([300, 420, 420])
        layout.addWidget(columns, 1)
        self._module_chips()
        return page

    def _module_chips(self):
        """模块筛选：全部 + 当前目录里有的模块；换设备时按新目录重建"""
        while self.module_chips.count():
            widget = self.module_chips.takeAt(0).widget()
            if widget:
                self.module_group.removeButton(widget)
                widget.hide()
                widget.deleteLater()
        modules = ["全部"] + list(dict.fromkeys(sorted((item["module"] for item in self._catalog()),
                                                      key=lambda m: MODULES.index(m) if m in MODULES else len(MODULES))))
        for index, module in enumerate(modules):
            chip = QPushButton(module.upper() if module != "全部" else module)
            chip.setObjectName("filterChip")
            chip.setCheckable(True)
            chip.setChecked(index == 0)
            chip.setProperty("module", "" if module == "全部" else module)
            self.module_group.addButton(chip, index)
            chip.setMinimumHeight(26)
            self.module_chips.addWidget(chip, index // 3, index % 3)

    def _quick_pick(self):
        """Ctrl+K：到指令与链路页，光标放进目录搜索框"""
        self.app._go(self.app.CONTROL)
        self.command_search.setFocus()
        self.command_search.selectAll()

    def _pick_first(self):
        item = next((self.commands.item(i) for i in range(self.commands.count()) if self.commands.item(i).data(Qt.UserRole)), None)
        if item:
            self.commands.setCurrentItem(item)

    def _commands(self):
        """按搜索词重建列表；不改变当前指令，启动时才默认选第一条"""
        self.commands.blockSignals(True)
        self.commands.clear()
        needle = self.command_search.text().lower()
        chip = self.module_group.checkedButton() if hasattr(self, "module_group") else None
        only = chip.property("module") if chip else ""
        first, group = None, None
        # 目录按文档顺序登记，同一模块的方法分散在多处；按模块归拢后分节，模块内保持文档顺序
        ordered = sorted(self._catalog(), key=lambda item: MODULES.index(item["module"]) if item["module"] in MODULES else len(MODULES))
        for item in ordered:
            available = item["category"] == "command" or (item["module"] == "gw" and item["method"] in {"oss_config_get", "online", "heartbeat"})
            words = " ".join((item["method"], item["module"], item["title"], item["summary"])).lower()
            if not available or (needle and needle not in words) or (only and item["module"] != only):
                continue
            if item["module"] != group:
                group = item["module"]
                title = item["label"].split(" · ")[0]
                # 目录里写的是本地方式（模拟 GW 发）；模拟云端时其实是云端经 GW 转
                header = QListWidgetItem(title.replace("模拟 GW 发", "模拟云端发，经 GW 转") if self.app.uav_cloud() else title)
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
        if not self._uav():
            return self.app.catalog
        # 模拟云端只能发 services 给 GW，Bridge 命令是本地的，不列
        return [e for e in self.uav_catalog if e["module"] != "bridge"] if self.app.uav_cloud() else self.uav_catalog

    def mode_changed(self):
        """无人机换方式（模拟 GW / 模拟云端）：目录、模块筛选和当前模板跟着换"""
        if self._uav():
            self.selected = None
            self._module_chips()
            self._commands()

    def _target_edited(self, text):
        """target 下拉改了写回 JSON 顶层"""
        if self.form_syncing:
            return
        try:
            payload = json.loads(self.editor.toPlainText())
        except ValueError:
            return
        if isinstance(payload, dict) and payload.get("target") != text:
            payload["target"] = text
            self.form_syncing = True
            self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            self.form_syncing = False

    def device_changed(self):
        """换设备类型：指令目录换成那种设备的，未发出的草稿作废（报文格式不同）"""
        self.selected = None
        self.app.drafts.clear()
        device = self.app.device_box.currentText()
        self.recent_by[self.recent_device] = self.recent
        self.recent, self.recent_device = self.recent_by.get(device, []), device
        self._show_recent()
        # 上一种设备的本次结果不留在新设备的页面上
        self.watch, self.watch_size = None, 0
        self.result_title.setText("本次结果 · 证据链")
        self.result.show_empty()
        self._module_chips()
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
        # 无人机的指令目录每条自带 Topic（VFC 或 Bridge 的指令主题）；模拟云端时 VFC 指令改发 tta/uav/{sn}/services
        if item.get("topic"):
            return self.app.uav_wire({}, item["topic"])[0]
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
        self.target_label.hide()
        self.target_box.hide()
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
        cloud = self.app.uav_cloud() and self.selected["module"] == "vfc"
        self.target_label.setVisible(cloud)
        self.target_box.setVisible(cloud)
        if cloud and isinstance(payload.get("target"), str) and payload["target"] != self.target_box.currentText():
            self.form_syncing = True
            self.target_box.setCurrentText(payload["target"])
            self.form_syncing = False
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
        items = list(data.items())
        if self._uav() and self.selected["method"] == "stop_mapping" and "save" not in data:
            # 不带 save 就是按 VFC 默认（保存）；选项照样列出来，表明这是选过的
            items.append(("save", OMIT))
        for index, (key, value) in enumerate(items):
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
        """停止建图的 save：“保存地图”“不保存”“按默认（保存）”三选一；模板里 save 是 null，不选不让发

        选“按默认”时 data 里不带 save，VFC 按保存处理（VFC 994c1ae）
        """
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        group = QButtonGroup(box)
        for text, choice in (("保存地图", True), ("不保存", False), ("按默认（保存）", OMIT)):
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
        if value is OMIT:
            payload[self.form_key].pop(key, None)
        else:
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
        self.command_heading.setText(f"{self.selected['title']} {self.selected['method']}")
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
                # 已知地图航线的 map_id 要等于当前选用的图，查过地图列表就用选用的那张
                chosen = self.app.uav.selected_map()
                data = payload.get("data")
                if self.selected["method"] == "start_record":
                    # 按《SLAM无人机-MQTT通信协议.md》：有图录制 data.map_id 为字符串；留空发送时自动省略即为无图录制
                    loaded = getattr(self.app.uav, "loaded_map", "")
                    data["map_id"] = loaded or ""
                elif chosen and isinstance(data, dict) and "map_id" in data:
                    data["map_id"] = chosen
                if self.app.uav_cloud():
                    cloud_command(payload)
            self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            self._update_topic()
        self._rebuild_form()

    def _send(self):
        """发送编辑框内容；非对象或非法 JSON 照原文发出，沙盒里只记录不模拟"""
        if self._uav() and self.app.mode not in ("direct", "gw"):
            self.app.notify("无人机要连上才能发：方式选“局域网直连”（模拟 GW）或“云端 → GW”（模拟云端）")
            return
        text, topic = self.editor.toPlainText(), self.topic.text().strip()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        data = payload.get("data") if isinstance(payload, dict) else None
        # save 必须选过：true / false，或者不带（按默认保存）；模板里的 null、手写的字符串都不发
        if self._uav() and isinstance(payload, dict) and payload.get("method") == "stop_mapping" \
                and not (isinstance(data, dict) and (isinstance(data.get("save"), bool) or "save" not in data)):
            self.app.notify("停止建图要先选“保存地图”“不保存”或“按默认（保存）”")
            return
        if self._uav() and isinstance(payload, dict):
            # VFC 和 Bridge 按 tid / id 认请求，同一个编号再发会被当成重复请求（Bridge 直接回旧的缓存结果、不执行），
            # 每次发送都换新编号和当前 ts；bid 是任务 ID，stop_nav 这类要带当前任务的，不换
            refresh_ids(payload, keep_bid=True)
            if self.selected["module"] == "vfc":
                payload["bid"] = self._vfc_bid(payload)
                if payload.get("method") == "start_record" and isinstance(payload.get("data"), dict):
                    if payload["data"].get("map_id") in ("", None):
                        payload["data"].pop("map_id", None)
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

    def _copy_pub_command(self):
        """拼一条 mosquitto_pub，Broker、端口、Topic、QoS 按当前填写，报文压成一行；密码不写进剪贴板，留占位自己填"""
        text = self.editor.toPlainText()
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
        except ValueError:
            # 故意发的异常报文按原文
            pass
        quote = lambda value: "'" + value.replace("'", "'\\''") + "'"
        app = self.app
        parts = ["mosquitto_pub", "-h", app.host.text().strip() or "127.0.0.1", "-p", str(app.port.value()),
                 "-q", str(MqttTransport.qos_for(self.topic.text().strip()))]
        if app.user.text().strip():
            parts += ["-u", quote(app.user.text().strip()), "-P", "'<密码>'"]
        if app.tls.isChecked():
            parts += ["--capath", "/etc/ssl/certs"]
        parts += ["-t", quote(self.topic.text().strip()), "-m", quote(text)]
        QApplication.clipboard().setText(" ".join(parts))
        app.notify("已复制 mosquitto_pub 命令" + ("，-P 后面换成密码" if app.user.text().strip() else ""))

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
