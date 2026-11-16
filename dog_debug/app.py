"""Qt 整机调试界面的主窗口：各页面、连接和后台事件队列的分发；网络和日志后台输入统一交到事件队列"""

import argparse
import html
import json
import queue
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve, QEvent, QParallelAnimationGroup, QPoint, QPropertyAnimation, QSize, QStandardPaths, Qt, QTimer,
)
from PySide6.QtGui import QKeySequence, QPalette, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QPlainTextEdit, QPushButton, QSlider, QSpinBox, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import __version__, cad, robot as model_module
from .core import diagnose, Event, infer_module, Store
from .logs import detect_encoding
from .pointcloud import cloud_data
from .registry import entries, find, local_payload, make_command
from .robot import VARIANTS
from .scene import Scene
from .trace_view import Step
from .transport import MqttTransport, simulate
from .ui import (
    APP_NAME, apply_theme, button, DEVICES, dot, FAILED, glyph_icon, GRAY, guard_wheel, label, logo_icon, logo_image,
    MODES, NavDelegate, ORANGE, press_feedback, SCENARIOS, scroller, settings, setup_fonts, Switch, tint, Toast, tone,
)
from .pages.connection import ConnectionPanel
from .pages.control import ControlPage
from .pages.coverage import CoveragePage
from .pages.device_config import DeviceConfigPage
from .pages.logs import LogsPage
from .pages.messages import MessagesPage
from .pages.mission import MissionPage
from .pages.scene3d import ScenePanel
from .pages.status import StatusPage
from .pages.suite import SuitePage
from .pages.teleop import TeleopPage
from .pages.uav import UavPage


class Studio(QMainWindow):
    # 页面栈里的序号；左侧导航按连接卡里选的设备只列这种设备用得上的页面，名字和顺序各设备自己定
    REMOTE, CONTROL, MISSION, MESSAGES, STATUS, UAV, LOGS, SUITE, COVERAGE, UAV_MISSION, DEVICE_CONFIG = range(11)
    NAV = {"dog": [("遥控", REMOTE), ("控制调试", CONTROL), ("导航与文件", MISSION), ("消息追踪", MESSAGES), ("状态总览", STATUS),
                   ("日志排障", LOGS), ("设备配置", DEVICE_CONFIG), ("回归测试", SUITE), ("协议覆盖", COVERAGE)],
           "uav": [("控制调试", CONTROL), ("航线任务", UAV_MISSION), ("消息追踪", MESSAGES), ("状态总览", UAV), ("日志排障", LOGS)]}
    PAGES = [title for title, _ in NAV["dog"]]
    # 这些页旁边显示 3D 观察区；其余是分析页，整宽显示
    OBSERVED = (REMOTE, CONTROL)

    def __init__(self, db_path=None):
        super().__init__()
        self.connection = ConnectionPanel(self)
        self.control = ControlPage(self)
        self.teleop = TeleopPage(self)
        self.scene3d = ScenePanel(self)
        self.mission = MissionPage(self)
        self.messages = MessagesPage(self)
        self.status = StatusPage(self)
        self.logs = LogsPage(self)
        self.device_config = DeviceConfigPage(self)
        self.suite = SuitePage(self)
        self.coverage = CoveragePage(self)
        self.uav = UavPage(self)
        self.setWindowTitle(APP_NAME)
        self.resize(1480, 940)
        self.setMinimumSize(1180, 720)
        self.setAcceptDrops(True)
        if db_path is None:
            data = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
            data.mkdir(parents=True, exist_ok=True)
            db_path = data / "sessions.sqlite3"
        self.store = Store(db_path)
        self.inbox = queue.Queue(maxsize=20000)
        self.mqtt = MqttTransport(self.inbox)
        self.session = str(uuid.uuid4())
        self.import_cancel = threading.Event()
        self.catalog = entries()
        # 本次运行里各指令未发出的编辑内容，切回来接着改；切换连接模式时报文格式变了，一并清掉
        self.drafts = {}
        self.quick = None
        self.quick_views = []
        self.scene = Scene()
        self.refresh_due, self.refreshed = False, 0.0
        # 每个模块收到实时消息的次数，状态页详情据此判断要不要重画
        self.module_touched = {}
        self.demo_note = ""
        # 最近一次点云解析错误（同样的错误不重复提示）和点云标签上次按时效刷新的时刻
        self.dropped_shown = 0
        self.rate_count, self.rate_at = 0, time.monotonic()
        self._build()
        # 构建时建好的滑条、下拉框、数字框统一装上滚轮保护；之后动态加的（航点、关节）在创建处装
        for kind in (QSlider, QComboBox, QAbstractSpinBox):
            for widget in self.findChildren(kind):
                guard_wheel(widget)
        for widget in self.findChildren(QPushButton):
            press_feedback(widget)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain)
        self.timer.start(100)
        self.control._commands()
        self.connection._mode_changed()
        self.status._room_follow()
        self.sn.textChanged.connect(self.status._room_follow)
        self.teleop.remote.camera_sn.textChanged.connect(self.status._room_follow)
        self.messages._refresh()

    # ---- 布局 ----

    def _build(self):
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._sidebar())
        content = QWidget()
        content.setObjectName("content")
        inner = QVBoxLayout(content)
        inner.setContentsMargins(28, 22, 28, 20)
        observer = self.scene3d._observer()
        self.stack = QStackedWidget()
        for page in (self.teleop._remote_page(), self.control._control_page(), self.mission._mission_page(),
                     self.messages._message_page(), self.status._status_page(), self.uav._uav_page(),
                     self.logs._logs_page(), self.suite._suite_page(), self.coverage._coverage_page(), self.uav._mission_page(),
                     self.device_config._config_page()):
            self.stack.addWidget(page)
        # 观察区放在页面栈外，切页时模型、镜头和轨迹都不动；比例可拖
        self.workspace = QSplitter()
        self.workspace.setHandleWidth(14)
        self.workspace.setChildrenCollapsible(False)
        self.workspace.addWidget(self.stack)
        self.workspace.addWidget(observer)
        self.workspace.setStretchFactor(0, 3)
        self.workspace.setStretchFactor(1, 2)
        self.workspace.setSizes([600, 400])
        inner.addWidget(self.workspace)
        outer.addWidget(content, 1)
        self.setCentralWidget(root)
        self.toast = Toast(root)
        # 任何页面按 Ctrl+K 直接调出指令目录，选中后在控制调试页编辑
        QShortcut(QKeySequence("Ctrl+K"), self, self.control._quick_pick)
        # 上次保存的配置可能是无人机，开在当前设备导航的第一页
        self._go(self.NAV[DEVICES[self.device_box.currentText()]][0][1])

    def _page_header(self, layout, title, caption, *extra):
        row = QHBoxLayout()
        row.addWidget(label(title, "largeTitle"))
        row.addStretch()
        for widget in extra:
            row.addWidget(widget)
        layout.addLayout(row)
        if caption:
            layout.addWidget(label(caption, "caption"))
        layout.addSpacing(6)

    def _sidebar(self):
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(252)
        outer = QVBoxLayout(side)
        outer.setContentsMargins(0, 0, 0, 0)
        # 窗口矮时侧栏整体滚动，避免设备卡和连接卡压住导航
        body = QWidget()
        outer.addWidget(scroller(body))
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 22, 14, 16)
        layout.setSpacing(6)
        title = QHBoxLayout()
        title.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(QPixmap.fromImage(logo_image(64)).scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        title.addWidget(mark)
        title.addWidget(label(APP_NAME, "appTitle"))
        title.addStretch()
        theme = button("◐", self._toggle_theme, "link")
        # 符号在默认字号下小得像字母，单独放大
        theme.setStyleSheet("font-size:18px")
        theme.setToolTip("切换浅色 / 深色")
        title.addWidget(theme)
        layout.addLayout(title)
        layout.addWidget(label("设备调试工具  ·  v" + __version__, "appSub"))
        layout.addSpacing(14)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(22, 22))
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav.setItemDelegate(NavDelegate(self.nav))
        self.nav.setMouseTracking(True)
        self.nav.setSpacing(1)
        self._fill_nav("dog")
        self.nav.currentRowChanged.connect(lambda row: row >= 0 and self._page(self.nav.item(row).data(Qt.UserRole)))
        layout.addWidget(self.nav)
        layout.addStretch()

        layout.addWidget(label("当前设备", "groupHeader"))
        device = Step()
        device.setObjectName("sideCard")
        device.setCursor(Qt.PointingHandCursor)
        device.clicked.connect(lambda: self._go(self.UAV if self.device_box.currentText() == "无人机" else self.REMOTE))
        dl = QHBoxLayout(device)
        dl.setContentsMargins(12, 10, 12, 10)
        badge = QLabel()
        self.device_badge = badge
        badge.setPixmap(glyph_icon("L2", "#1C1C1E").pixmap(30, 30))
        dl.addWidget(badge)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.device_name = label("", "deviceName")
        self.device_meta = label("", "caption", True)
        text.addWidget(self.device_name)
        text.addWidget(self.device_meta)
        dl.addLayout(text, 1)
        self.device_dot = dot("#C7C7CC")
        dl.addWidget(self.device_dot)
        layout.addWidget(device)
        layout.addSpacing(8)

        layout.addWidget(label("连接", "groupHeader"))
        box = QFrame()
        box.setObjectName("sideCard")
        form = QVBoxLayout(box)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)
        # 保存的连接配置：选一个就填好下面各项，密码不保存
        saved = QHBoxLayout()
        saved.setSpacing(6)
        self.profile_box = QComboBox()
        self.profile_box.activated.connect(self.connection._profile_chosen)
        saved.addWidget(self.profile_box, 1)
        more = QPushButton("⋯")
        more.setObjectName("tile")
        more.setFixedWidth(40)
        # 去掉菜单箭头，窄按钮里只放“⋯”
        more.setStyleSheet("QPushButton{font-size:18px;padding:0 0 4px 0}QPushButton::menu-indicator{image:none;width:0px}")
        more.setToolTip("保存或删除连接配置")
        more.setCursor(Qt.PointingHandCursor)
        menu = QMenu(more)
        menu.addAction("把当前填写存为配置…", self.connection._save_profile)
        menu.addAction("删除选中的配置", self.connection._delete_profile)
        more.setMenu(menu)
        saved.addWidget(more)
        form.addLayout(saved)
        # 设备类型跟着连接走（随连接配置保存），左侧导航跟着换；连上后锁住，断开才能换
        self.device_box = QComboBox()
        self.device_box.addItems(DEVICES)
        self.device_box.currentTextChanged.connect(lambda _: self._device_changed())
        self.mode_box = QComboBox()
        self.mode_box.addItems(MODES)
        # 当前生效的模式；切换时要先按旧模式补发停止再更新，所以不直接读下拉框
        self.mode = MODES[self.mode_box.currentText()]
        self.mode_box.currentTextChanged.connect(self.connection._mode_changed)
        self.host = QLineEdit("127.0.0.1")
        self.host.setPlaceholderText("Broker 地址")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(1883)
        self.port.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.port.setFixedWidth(68)
        self.sn = QLineEdit("TEST_DOG_001")
        self.sn.setPlaceholderText("设备 SN")
        self.sn.textChanged.connect(self.control._update_topic)
        self.sn.textChanged.connect(self.connection._connection_hint)
        self.sn.textChanged.connect(self.scene3d._device_card)
        self.host.textChanged.connect(self.connection._connection_hint)
        self.user = QLineEdit()
        self.user.setPlaceholderText("用户名")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("密码")
        self.tls = Switch("TLS")
        # 远程：经 FRP 用 SSH 登到狗上，把狗上的 Broker 和 ZLM 端口转到本机，之后和直连一样用；取设备日志也用这组 SSH 账号
        stored = settings()
        self.frp = Switch("经远程（FRP）")
        self.frp.toggled.connect(self.connection._frp_toggled)
        self.ssh_user = QLineEdit(stored.value("ssh_user", ""))
        self.ssh_user.setPlaceholderText("SSH 用户名")
        self.ssh_password = QLineEdit(stored.value("ssh_password", ""))
        self.ssh_password.setEchoMode(QLineEdit.Password)
        self.ssh_password.setPlaceholderText("SSH 密码")
        # 本机自用，SSH 账号改完就记住
        self.ssh_user.editingFinished.connect(lambda: settings().setValue("ssh_user", self.ssh_user.text().strip()))
        self.ssh_password.editingFinished.connect(lambda: settings().setValue("ssh_password", self.ssh_password.text()))
        self.ssh, self.connection.ssh_zlm, self.connection.ssh_connecting = None, 0, False
        self.connect_btn = button("连接", self.connection._connect, "primary")
        broker = QHBoxLayout()
        broker.addWidget(self.host, 1)
        broker.addWidget(self.port)
        account = QHBoxLayout()
        account.addWidget(self.user)
        account.addWidget(self.password)
        form.addWidget(self.device_box)
        form.addWidget(self.mode_box)
        form.addLayout(broker)
        form.addWidget(self.sn)
        form.addLayout(account)
        self.ssh_row = QWidget()
        ssh = QVBoxLayout(self.ssh_row)
        ssh.setContentsMargins(0, 0, 0, 0)
        ssh.setSpacing(6)
        ssh.addWidget(self.frp)
        account = QHBoxLayout()
        account.addWidget(self.ssh_user)
        account.addWidget(self.ssh_password)
        ssh.addLayout(account)
        form.addWidget(self.ssh_row)
        # 无人机默认只读：打开才往 VFC、Bridge 的指令主题发，控制调试页和航线任务页都受它管；每次新连接自动关上
        self.uav_send = Switch("允许向无人机发送")
        self.uav_send.toggled.connect(lambda on: (setattr(self.mqtt, "uav_send", on), self.uav.render()))
        self.uav_send.hide()
        form.addWidget(self.uav_send)
        actions = QHBoxLayout()
        actions.addWidget(self.tls)
        actions.addStretch()
        actions.addWidget(self.connect_btn)
        form.addLayout(actions)
        layout.addWidget(box)
        state = QHBoxLayout()
        state.setContentsMargins(6, 6, 6, 0)
        self.link_dot = dot(GRAY)
        self.link = label("", "caption", True)
        state.addWidget(self.link_dot)
        state.addWidget(self.link, 1)
        self.rate_label = label("", "caption")
        state.addWidget(self.rate_label, 0, Qt.AlignTop)
        layout.addLayout(state)
        self.connection._load_profiles()
        return side

    @property
    def dark(self):
        return QApplication.instance().palette().color(QPalette.Window).lightness() < 128

    def _toggle_theme(self):
        """换外观要整窗重新套样式（约半秒）；先拿旧画面盖住，换好后让它淡出，看起来是平滑过渡"""
        dark = not self.dark
        cover = QLabel(self)
        cover.setPixmap(self.grab())
        cover.setGeometry(self.rect())
        cover.show()
        cover.repaint()
        apply_theme(QApplication.instance(), dark)
        settings().setValue("theme", "dark" if dark else "light")
        self.scene3d.set_dark(dark)
        effect = QGraphicsOpacityEffect(cover)
        cover.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", cover)
        fade.setDuration(260)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        fade.finished.connect(cover.deleteLater)
        fade.start()


    def _background(self, work, kind, *extra):
        """在只读连接上跑 work(reader)，结果以 (kind, 结果, *extra) 进 inbox；内存库没有文件，直接在当前线程跑"""
        reader = self.store.reader()

        def run():
            try:
                self.inbox.put((kind, work(reader), *extra))
            except Exception as error:
                # 后台线程里的异常没人接，转成提示，免得界面一直等
                self.inbox.put(("notice", f"读取失败：{error}"))
            finally:
                if reader is not self.store:
                    reader.db.close()

        if reader is self.store:
            run()
        else:
            threading.Thread(target=run, daemon=True).start()


    # ---- 消息入库 ----


    def _ingest(self, event):
        """所有消息都入库；只有当前活动连接、当前设备的 OSD 驱动 3D 和设备卡

        点云同样只认当前设备（沙盒模式下也显示沙盒的演示帧）；原始点只进 3D，库里存瘦身后的报文
        """
        data = cloud_data(event)
        if data is not None and (self._current_device(event) or (event.source == "sandbox" and self.mode == "sandbox")):
            self.scene3d.take_cloud(data)
        self.store.add(event)
        self.module_touched[event.module] = self.module_touched.get(event.module, 0) + 1
        self.mission.missions.observe(event)
        self.uav.observe(event)
        # 回放期间不让实时 OSD 把画面拉走
        if self._current_device(event) and not self.scene3d.replay:
            self.scene.telemetry(event)

    def _current_device(self, event):
        # 沙盒、导入日志和其他会话的消息只看不驱动实时状态；云端 OSD 的 Topic 里带 SN，要和活动连接一致
        if event.source != "mqtt" or not self.mqtt.connected or event.session != self.mqtt.session:
            return False
        match = re.match(r"tta/robotdog/([^/]+)/osd$", event.topic)
        # bridge 把 local/robotdog/vfc/osd 换成 tta/ 上云后，Topic 中间那段是 vfc 不是 SN，再看报文根上的 sn
        return not match or self.mqtt.sn in (match[1], event.payload.get("sn"))

    def _quick_view(self):
        """遥控类页面顶部的“最近一条指令结果”卡片，颜色随结果变，点一下到消息页看证据链；各页共用同一份状态"""
        chip = Step()
        chip.setObjectName("quickChip")
        chip.setCursor(Qt.PointingHandCursor)
        chip.setMaximumWidth(760)
        chip.clicked.connect(self._open_quick)
        chip.setStyleSheet(f"QFrame#quickChip{{background:{tint(GRAY, 26)};border-radius:12px}}")
        row = QHBoxLayout(chip)
        row.setContentsMargins(12, 6, 14, 6)
        row.setSpacing(8)
        marker = dot("#C7C7CC", 10)
        text = label("还没有发送")
        text.setTextFormat(Qt.RichText)
        text.setStyleSheet("font-size:14px")
        row.addWidget(marker)
        row.addWidget(text, 1)
        self.quick_views.append((chip, marker, text))
        return chip

    def _open_quick(self):
        if self.quick:
            self.messages.filter_text.setText(self.quick.tid)
            self._go(self.MESSAGES)


    # ---- 页面与状态 ----

    def _page(self, index):
        self._slide_to(index)
        page = {self.STATUS: self.status, self.MESSAGES: self.messages, self.LOGS: self.logs, self.REMOTE: self.teleop,
                self.UAV: self.uav, self.UAV_MISSION: self.uav}.get(index)
        if page:
            page.shown()
        self._show_observer(index)
        # 窗口还没显示时不加载，showEvent 再补；测试里窗口不显示，也就不加载 OpenGL
        if index in self.OBSERVED and self.isVisible():
            self.scene3d._ensure_viewer()

    def _show_observer(self, index):
        # 3D 观察区画的是狗的模型和遥测，设备选无人机时不显示
        self.scene3d.observer.setVisible(index in self.OBSERVED and DEVICES[self.device_box.currentText()] != "uav")

    def _slide_to(self, index):
        """切页：旧页截图淡出并往反方向滑走，新页从切换方向淡入滑到位（参照 host 的切页动画，260 ms）"""
        old, new = self.stack.currentWidget(), self.stack.widget(index)
        if new is old or not self.isVisible():
            self.stack.setCurrentIndex(index)
            return
        direction = 1 if index > self.stack.currentIndex() else -1
        ghost = QLabel(self.stack)
        ghost.setAttribute(Qt.WA_TransparentForMouseEvents)
        ghost.setPixmap(old.grab())
        ghost.setGeometry(old.geometry())
        self.stack.setCurrentIndex(index)
        ghost.show()
        ghost.raise_()
        home = new.pos()
        group = QParallelAnimationGroup(self)

        def animate(target, prop, start, end, ms):
            item = QPropertyAnimation(target, prop, group)
            item.setDuration(ms)
            item.setStartValue(start)
            item.setEndValue(end)
            item.setEasingCurve(QEasingCurve.OutCubic)

        ghost_fade = QGraphicsOpacityEffect(ghost)
        ghost.setGraphicsEffect(ghost_fade)
        page_fade = QGraphicsOpacityEffect(new)
        new.setGraphicsEffect(page_fade)
        animate(ghost_fade, b"opacity", 1.0, 0.0, 170)
        animate(ghost, b"pos", home, home - QPoint(direction * 28, 0), 260)
        animate(page_fade, b"opacity", 0.0, 1.0, 260)
        animate(new, b"pos", home + QPoint(direction * 28, 0), home, 260)

        def done():
            ghost.deleteLater()
            # 只撤掉本次加的效果：连续快速切页时，后一次动画可能已经换上自己的效果
            if new.graphicsEffect() is page_fade:
                new.setGraphicsEffect(None)
            new.move(home)

        group.finished.connect(done)
        group.start(QParallelAnimationGroup.DeleteWhenStopped)

    def _fill_nav(self, device):
        """左侧导航换成这种设备的页面；每行记着页面栈序号，不按行号对页面"""
        self.nav.blockSignals(True)
        self.nav.clear()
        for title, index in self.NAV[device]:
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, index)
            self.nav.addItem(item)
        # 行高 40 加上下各 1 px 间距；固定高度，窗口矮时由侧栏整体滚动
        self.nav.setFixedHeight(len(self.NAV[device]) * 42 + 4)
        self.nav.blockSignals(False)

    def _device_changed(self):
        """连接卡里换设备：左侧导航、指令目录、发送开关跟着换；当前页这种设备没有就切到第一页"""
        device = DEVICES[self.device_box.currentText()]
        current = self.stack.currentIndex()
        self._fill_nav(device)
        self.uav_send.setVisible(device == "uav")
        # 无人机的主题里不带 SN，也不走 FRP
        self.sn.setVisible(device == "dog")
        if device == "uav":
            self.frp.setChecked(False)
        self.ssh_row.setVisible(device == "dog" and self.mode == "direct")
        self.scene3d._device_card()
        self.control.device_changed()
        self.logs.device_changed(device)
        pages = [index for _, index in self.NAV[device]]
        self._go(current if current in pages else pages[0])
        self._show_observer(self.stack.currentIndex())

    def _go(self, index):
        row = next((r for r in range(self.nav.count()) if self.nav.item(r).data(Qt.UserRole) == index), None)
        if row is None:
            # 这种设备的导航里没有这一页（比如设备选狗时打开无人机页），直接切过去
            self._page(index)
        elif row == self.nav.currentRow():
            self._page(index)
        else:
            self.nav.setCurrentRow(row)

    def notify(self, text):
        self.toast.pop(text)


    # ---- 连接与发送 ----


    def _emit(self, payload, text, topic, module):
        """按当前模式发出并入库，返回代表本次发送的事件；发布失败提示原因并返回 None"""
        record = payload if isinstance(payload, dict) else {}
        if self.mode == "offline":
            self.notify("离线分析模式不发送")
            return None
        if self.mode == "sandbox":
            sn = self.sn.text().strip()
            if record:
                events = simulate(payload, sn, self.session, SCENARIOS[self.control.scenario.index()][1])
            else:
                events = [Event(topic, {}, "发送", "sandbox", sn, self.session, module, raw=text)]
            for event in events:
                self._ingest(event)
            sent = events[0]
        else:
            wire = payload
            try:
                self.mqtt.publish(topic, wire)
            except RuntimeError as error:
                self.notify(str(error))
                return None
            # 记账用当前活动连接的 SN，编辑框里改了但还没重连的 SN 不算
            sn = self.mqtt.sn
            # 模块按实际发往的 Topic / target 记，和 Broker 回显的那条一致；协议表里的归类（如文件类归 FSVR、target 是 gw）不算
            sent = Event(topic, wire if isinstance(wire, dict) else {}, "发送", "mqtt", sn, self.mqtt.session,
                         infer_module(topic, wire) if isinstance(wire, dict) else module, qos=self.mqtt.qos_for(topic), raw=text)
            self.store.add(sent)
            self.module_touched[sent.module] = self.module_touched.get(sent.module, 0) + 1
        self.store.flush()
        self.demo_note = self.scene.command(record.get("method"), record.get("data")) if record else ""
        return sent

    def _quick_send(self, method, data):
        """遥控面板的发送入口：取协议模板，覆盖 data 字段后按当前模式发出"""
        item = find(method)
        payload = make_command(item)
        payload["data"].update(data)
        # 本地指令的模板就是本地报文（pcd_switch 带 target），不再去掉 target
        if self.mode == "direct" and not item.get("local_only"):
            payload = local_payload(payload)
        sent = self._emit(payload, json.dumps(payload, ensure_ascii=False), self.control._topic_for(item), item["module"])
        if sent:
            self.quick = sent
            self.messages._refresh()
            if self.demo_note:
                self.notify(self.demo_note)
        return sent is not None

    def _quick_status(self):
        trace = self.store.trace(self.quick)
        acks = [e for e in trace if e.kind == "ACK"]
        # 优先看模块自己的 ACK，GW 转发的回复只在没有模块 ACK 时使用
        d = diagnose(next((e for e in acks if e.module != "gw"), acks[-1])) if acks else None
        status, color = (d.status, tone(d.status)) if d else ("等待 ACK", ORANGE)
        detail = d.summary if d and d.status in FAILED else ""
        item = find(self.quick.method)
        name = f"{item['title']} " if item else ""
        when = time.strftime("%H:%M:%S", time.localtime(self.quick.timestamp))
        # 失败原因可能很长，卡片里截一段，全文放悬停提示
        shown = detail if len(detail) <= 70 else detail[:68] + "…"
        text = (f"<b>{html.escape(name)}</b><span style='color:{GRAY}'>{html.escape(self.quick.method)}</span>　"
                f"<b style='color:{color}'>{html.escape(status)}</b>"
                + (f"　{html.escape(shown)}" if shown else "") + f"　<span style='color:{GRAY}'>{when}</span>")
        for chip, marker, caption in self.quick_views:
            chip.setStyleSheet(f"QFrame#quickChip{{background:{tint(color, 30)};border-radius:12px}}")
            chip.setToolTip(f"{self.quick.method} · {status}" + (f"\n{detail}" if detail else "") + "\n点一下到消息页看证据链")
            marker.setStyleSheet(f"background:{color};border-radius:5px")
            caption.setText(text)


    # ---- 刷新 ----


    def _refresh_tail(self):
        self.control.refresh_result()
        if self.quick:
            self._quick_status()
        if self.stack.currentIndex() == self.STATUS:
            self.status.refresh_if_changed()


    def _original(self, e):
        """打开原日志所选记录前后约 16 KB；按导入时同样的规则判断编码，光标按字节偏移换算到字符位置"""
        try:
            encoding = detect_encoding(e.file)
            start = max(0, e.offset - 4096)
            with Path(e.file).open("rb") as source:
                source.seek(start)
                chunk = source.read(16384)
        except (OSError, ValueError) as error:
            self.notify("原文打不开：" + str(error))
            return
        encoding = "utf-8" if encoding == "utf-8-sig" else encoding
        # 从中间截取时开头可能落在多字节字符中间，替换字符只影响截取边界
        text = chunk.decode(encoding, errors="replace")
        position = len(chunk[:e.offset - start].decode(encoding, errors="replace"))
        dialog = QDialog(self)
        dialog.setWindowTitle(f"原文附近 · 第 {e.line} 行 · {e.file}")
        dialog.resize(1000, 650)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        edit = QPlainTextEdit(text)
        edit.setObjectName("code")
        edit.setReadOnly(True)
        layout.addWidget(edit)
        # 光标定位到所选记录，打开就能看到它
        cursor = edit.textCursor()
        cursor.setPosition(min(position, len(text)))
        edit.setTextCursor(cursor)
        edit.centerCursor()
        dialog.exec()

    # ---- 拖入日志 ----

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.logs.start_import(paths)


    def _drain(self):
        now = time.monotonic()
        if now - self.rate_at >= 1:
            self.rate_label.setText(f"{self.rate_count / (now - self.rate_at):.0f} 条/秒" if self.mqtt.connected else "")
            self.rate_count, self.rate_at = 0, now
        self.scene3d.tick(now)
        if self.stack.currentIndex() == self.STATUS:
            self.status.tick(now)
        elif self.stack.currentIndex() == self.UAV:
            self.uav.tick(now)
        changed = False
        # 每次最多处理约 60 ms，剩下的留到下一轮，避免大批导入时界面无响应
        deadline = time.monotonic() + 0.06
        while time.monotonic() < deadline:
            try:
                kind, value, *extra = self.inbox.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                self._ingest(value)
                self.rate_count += 1
                changed = True
            elif kind == "rows":
                self.store.add_rows(value)
                self.logs._import_progress(*extra[0])
                changed = True
            elif kind == "search":
                self.messages._search_done(value, *extra)
            elif kind == "model":
                self.scene3d._import_done(value, extra[0])
            elif kind == "model_error":
                self.scene3d._import_failed(value, extra[0])
            elif kind == "preview":
                self.messages._preview_done(value, extra[0])
            elif kind == "replay_sessions":
                self.scene3d._replay_menu(value)
            elif kind == "replay_frames":
                self.scene3d._replay_start(value, *extra)
            elif kind == "zlm":
                self.status._zlm_done(value)
            elif kind == "config_list":
                self.device_config.list_done(value, *extra)
            elif kind == "config_file":
                self.device_config.file_done(value, *extra)
            elif kind == "media_cfg":
                self.status._media_cfg_done(value, *extra)
            elif kind == "notice":
                self.notify(value)
            elif kind == "export_done":
                self.messages._export_done(value)
            elif kind == "import_done":
                self.logs._import_finished()
            elif kind == "log_batches":
                self.logs._log_batches_done(value, *extra)
            elif kind == "log_rows":
                self.logs._log_rows_done(value, *extra)
            elif kind == "import":
                self.logs._import_note(value)
            elif kind == "connection":
                self.connection._connected(value)
            elif kind == "disconnect":
                self.connection._lost(value)
            elif kind == "ssh_ready":
                self.connection._ssh_ready(value, *extra[0])
            elif kind == "fetch":
                self.logs._fetch_progress(*value)
            elif kind == "fetch_done":
                self.logs._fetch_done(value, *extra)
            elif kind == "log_line":
                self.logs._live_line(*value)
                changed = True
            elif kind == "follow_error":
                self.logs._follow_failed(value)
            elif kind == "ssh_error":
                self.connection._ssh_failed(value)
            else:
                self.link.setToolTip(str(value))
        if changed:
            self.store.flush()
            self.refresh_due = True
        # 刷新会查库重建表格，实时消息再多也最多每 0.4 s 一次
        if self.refresh_due and time.monotonic() - self.refreshed >= 0.4:
            self.messages._refresh()
        if self.mqtt.dropped != self.dropped_shown:
            self.dropped_shown = self.mqtt.dropped
            self.notify(f"接收跟不上，已丢 {self.mqtt.dropped} 条")

    # ---- 窗口事件 ----


    def changeEvent(self, event):
        # 切到别的窗口时方向键的松开事件收不到，先停掉连续运动
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
            self.teleop.remote.halt()
        super().changeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.stack.currentIndex() in self.OBSERVED:
            self.scene3d._ensure_viewer()

    def closeEvent(self, event):
        # 连接和数据库关闭前收尾遥控，停止指令才能发出并入库；在途的模型导入一并终止（STEP 子进程不留后台）
        self.teleop.remote.halt()
        self.scene3d.cancel_import()
        self.import_cancel.set()
        self.mqtt.close()
        self.connection._close_ssh()
        self.timer.stop()
        self.store.close()
        event.accept()


def self_check(window, folder):
    """交付自检：逐项写 self_check.txt，3D 以真实渲染出的非空白画面为准，结束后退出；有任何一项失败退出码为 1"""
    import platform
    folder.mkdir(parents=True, exist_ok=True)
    failures = []
    lines = [f"{APP_NAME} {__version__}　{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"程序位置 {Path(sys.executable).resolve()}　冻结运行 {getattr(sys, 'frozen', False)}",
             f"Python {platform.python_version()}　{platform.platform()}"]

    def fail(text):
        failures.append(text)
        lines.append("失败：" + text)

    try:
        import numpy, OpenGL, pyqtgraph, PySide6
        lines.append(f"PySide6 {PySide6.__version__}　numpy {numpy.__version__}　PyOpenGL {OpenGL.__version__}　pyqtgraph {pyqtgraph.__version__}")
    except Exception as error:
        fail(f"依赖缺失：{error}")
    for name in VARIANTS:
        try:
            r = model_module.load(name)
            lines.append(f"内置模型 {name}：{len(r.links)} 连杆，{len(r.movable)} 可动关节，"
                         f"{sum(len(l.mesh) for l in r.links.values() if l.mesh is not None)} 面")
        except Exception as error:
            fail(f"内置模型 {name}：{error}")
    reason = cad.available()
    if reason:
        fail("STEP 后端：" + reason)
    else:
        try:
            job = cad.StepJob(cad.SAMPLE, folder)
            deadline, event = time.monotonic() + 120, None
            while time.monotonic() < deadline and (not event or event[0] not in ("done", "error")):
                event = job.poll() or event
                time.sleep(0.05)
            if event and event[0] == "done":
                r = job.result(model_module)
                low, high = model_module.bounds(r)
                lines.append(f"STEP 样件读取：{event[1]} 个部件，单位 {r.unit}，尺寸 " + "×".join(f"{v:.3f}" for v in high - low) + " m")
            else:
                job.cancel()
                fail(f"STEP 样件读取：{event}")
        except Exception as error:
            fail(f"STEP 样件读取：{error}")
    # 以隐藏方式启动时第一次 show 会沿用隐藏标志，这里再显式显示一次，OpenGL 才会真正出帧
    window.showNormal()
    window.raise_()
    # 3D 观察区只在设备选狗时显示，上次配置是无人机时先换过来
    window.device_box.setCurrentText("机器狗")
    window._go(window.REMOTE)
    deadline = time.monotonic() + 30
    # 窗口刚显示时布局还在调整，此时截图里 3D 区是空白的；视图尺寸和上一次检查相同才算稳定
    last_size = [None]

    def finish():
        (folder / "self_check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        window.close()
        QApplication.instance().exit(1 if failures else 0)

    def capture():
        viewer = window.scene3d.viewer
        ratio = _drawn_ratio(viewer.grabFramebuffer(), viewer.background) if viewer else 0.0
        size = viewer.size() if viewer else None
        steady, last_size[0] = size == last_size[0], size
        if (ratio < 0.01 or not steady) and time.monotonic() < deadline:
            QTimer.singleShot(300, capture)
            return
        window.grab().save(str(folder / "self_check_3d.png"))
        if ratio >= 0.01:
            lines.append(f"3D 渲染：有效画面，模型像素约占视图 {ratio * 100:.1f}%，截图 self_check_3d.png")
        else:
            fail(f"3D 渲染：30 秒内没有出现有效画面（{window.scene3d.stage_hint.text().replace(chr(10), ' ') if not viewer else '视图对象已创建但画面空白'}），"
                 "截图 self_check_3d.png；可能是显卡 OpenGL 驱动问题或窗口不可见")
        finish()

    QTimer.singleShot(500, capture)


def _drawn_ratio(image, background=(244, 245, 248)):
    """帧里和背景色明显不同的像素占比（隔点抽样）；用来区分真正画出模型和空白帧"""
    if image.isNull():
        return 0.0
    step = max(image.width(), image.height()) // 120 or 1
    total = drawn = 0
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            c = image.pixelColor(x, y)
            total += 1
            if abs(c.red() - background[0]) + abs(c.green() - background[1]) + abs(c.blue() - background[2]) > 40:
                drawn += 1
    return drawn / total


def main():
    # 打包成无控制台窗口的程序时没有 stderr；参数写错时 argparse 要输出提示，没有地方写会卡在看不见的错误框里，
    # 这里转到临时目录的日志文件，出错照常以退出码 2 结束
    import faulthandler
    import tempfile
    if sys.stderr is None or sys.stdout is None:
        log = open(Path(tempfile.gettempdir()) / "DogDebug-console.log", "a", encoding="utf-8")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    # 显卡驱动、OpenGL 这类原生崩溃不经过 Python 异常，进程直接没了；把各线程调用栈记到临时目录，事后能查
    crash = open(Path(tempfile.gettempdir()) / "KunKunDebug-crash.log", "a", encoding="utf-8")
    crash.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {__version__}\n")
    crash.flush()
    faulthandler.enable(crash, all_threads=True)
    parser = argparse.ArgumentParser(description=f"{APP_NAME}：设备调试工具")
    parser.add_argument("--demo", action="store_true", help="打开模拟故障，不连接真机")
    parser.add_argument("--db", help="指定 SQLite 文件，默认使用用户数据目录")
    parser.add_argument("--screenshot", help="保存界面截图后退出，用于界面验证")
    parser.add_argument("--self-check", metavar="目录", help="运行环境自检：依赖、内置模型、STEP 读取和 3D 截图，报告写入该目录后退出")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    # 程序名和组织名决定用户数据目录（会话库、连接配置）和外观设置的位置，改名后仍沿用旧值，已有数据不丢；对外显示用 APP_NAME
    app.setApplicationName("TTA RobotDog Debug Studio")
    app.setOrganizationName("TTA")
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(logo_icon())
    app.setStyle("Fusion")
    setup_fonts(app)
    apply_theme(app, settings().value("theme", "light") == "dark")
    window = Studio(args.db)
    window.connection.restore_connection()
    window.show()
    if args.demo:
        QTimer.singleShot(50, window.control._demo)
    if args.self_check:
        QTimer.singleShot(200, lambda: self_check(window, Path(args.self_check)))
    if args.screenshot:
        def capture():
            window.grab().save(args.screenshot)
            window.close()
            app.quit()
        QTimer.singleShot(800, capture)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
