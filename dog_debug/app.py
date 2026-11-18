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

from PySide6.QtCore import QEvent, QSize, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QKeySequence, QPalette, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
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
from .trace_view import Step, TraceView
from .transport import MqttTransport, simulate
from .uav import cloud_command, cloud_topic, VFC as UAV_VFC
from .ui import (
    APP_NAME, apply_theme, button, DEVICES, dot, FAILED, GRAY, GREEN, guard_wheel, label, logo_icon, logo_image,
    DeviceStack, MODES, NavDelegate, ORANGE, press_feedback, SCENARIOS, Segmented, settings, setup_fonts, Sheet, Switch, tint,
    Toast, tone,
)
from .pages.connection import ConnectionPanel
from .pages.control import ControlPage
from .pages.device_config import DeviceConfigPage
from .pages.diagnostics import Diagnostics
from .pages.logs import LogsPage
from .pages.mission import MissionPage
from .pages.scene3d import ScenePanel
from .pages.status import StatusPage
from .pages.suite import SuitePage
from .pages.teleop import TeleopPage
from .pages.uav import UavPage

# 无人机 VFC 本地指令主题；模拟云端时换成 tta/uav/{sn}/services
VFC_COMMAND = UAV_VFC + "command"


class Studio(QMainWindow):
    # 狗和无人机共用同样的 5 个页（页面栈里的序号）；换设备只换页里的数据和协议目录，不换页
    OVERVIEW, TELEOP, COMMANDS, MISSION, LOGS = range(5)
    # 旧名字：状态总览、无人机状态在总览页，遥控在遥控与载荷，控制调试在指令与链路，回归测试、航线任务在任务与文件，设备配置在日志与配置
    STATUS = UAV = OVERVIEW
    REMOTE = TELEOP
    CONTROL = COMMANDS
    SUITE = UAV_MISSION = MISSION
    DEVICE_CONFIG = LOGS
    TITLES = ("总览与遥测", "遥控与载荷", "指令与链路", "任务与文件", "日志与配置")
    NAV = dict.fromkeys(("dog", "uav"), list(zip(TITLES, range(5))))
    PAGES = list(TITLES)
    # 3D 观察区只在总览页左边（狗），其余页整宽
    # 显示 3D 观察区的页；先都不显示：模型姿态和真狗对不上，没实际用处（11-18 定）。观察区代码保留，要用时把页加回这里
    OBSERVED = ()

    def __init__(self, db_path=None):
        super().__init__()
        self.connection = ConnectionPanel(self)
        self.control = ControlPage(self)
        self.teleop = TeleopPage(self)
        self.scene3d = ScenePanel(self)
        self.mission = MissionPage(self)
        self.diagnostics = Diagnostics(self)
        self.status = StatusPage(self)
        self.logs = LogsPage(self)
        self.device_config = DeviceConfigPage(self)
        self.suite = SuitePage(self)
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
        self._refresh()

    # ---- 布局 ----

    def _build(self):
        """上面一条统一栏（设备、连接、最近指令、指令目录、外观），下面左侧栏加页面；按 tta_debug_studio_ios.html 的布局"""
        root = QWidget()
        root.setObjectName("shell")
        column = QVBoxLayout(root)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        # 连接设置面板要用到侧栏里建的控件，先建侧栏和连接面板，再建顶栏
        side = self._sidebar()
        column.addWidget(self._topbar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(side)
        content = QWidget()
        content.setObjectName("content")
        inner = QVBoxLayout(content)
        inner.setContentsMargins(26, 20, 26, 18)
        observer = self.scene3d._observer()
        self.stack = QStackedWidget()
        self.teleop_stack = DeviceStack(self.teleop._remote_page(), self.uav._teleop_page())
        self.mission_stack = DeviceStack(self._dog_mission(), self.uav._mission_page())
        for page in (self.status._status_page(), self.teleop_stack, self.control._control_page(), self.mission_stack,
                     self._logs_and_config()):
            self.stack.addWidget(page)
        # 观察区放在页面栈外、总览页的左边，切页时模型、镜头和轨迹都不动；比例可拖
        self.workspace = QSplitter()
        self.workspace.setHandleWidth(14)
        self.workspace.setChildrenCollapsible(False)
        self.workspace.addWidget(observer)
        self.workspace.addWidget(self.stack)
        self.workspace.setStretchFactor(0, 2)
        self.workspace.setStretchFactor(1, 3)
        self.workspace.setSizes([420, 700])
        inner.addWidget(self.workspace)
        body.addWidget(content, 1)
        column.addLayout(body, 1)
        self.setCentralWidget(root)
        self.connection_sheet = Sheet(root, self._connection_card())
        self.toast = Toast(root)
        # 任何页面按 Ctrl+K 直接调出指令目录，选中后在控制调试页编辑；Ctrl+1～5 切到侧栏第几页
        QShortcut(QKeySequence("Ctrl+K"), self, self.control._quick_pick)
        for number in range(1, 6):
            QShortcut(QKeySequence(f"Ctrl+{number}"), self, lambda row=number - 1: row < self.nav.count() and self.nav.setCurrentRow(row))
        # 上次保存的配置可能是无人机，开在当前设备导航的第一页
        self._go(self.NAV[DEVICES[self.device_box.currentText()]][0][1])

    def _dog_mission(self):
        """狗的任务与文件：导航与文件，下面是折成一行的模块契约回归测试"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.mission._mission_page(), 1)
        layout.addWidget(self.suite._suite_card())
        return page

    def _logs_and_config(self):
        """日志与配置：左边日志排障，右边经 SSH 只读看设备配置"""
        split = QSplitter()
        split.setHandleWidth(16)
        split.setChildrenCollapsible(False)
        split.addWidget(self.logs._logs_page())
        split.addWidget(self.device_config._config_card())
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        split.setSizes([800, 400])
        return split

    def _page_header(self, layout, title, caption, *extra):
        """页头：大标题、右边的按钮，下面一行说明；返回标题，换设备时页可以改标题"""
        row = QHBoxLayout()
        heading = label(title, "largeTitle")
        row.addWidget(heading)
        row.addStretch()
        for widget in extra:
            row.addWidget(widget)
        layout.addLayout(row)
        if caption:
            layout.addWidget(label(caption, "caption"))
        layout.addSpacing(6)
        return heading

    def _topbar(self):
        """统一栏：左边应用名和设备切换，中间连接胶囊（点开连接设置）和最近一条指令结果，右边无人机发送开关、指令目录、外观"""
        bar = QFrame()
        bar.setObjectName("topbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(12)
        mark = QLabel()
        mark.setPixmap(QPixmap.fromImage(logo_image(64)).scaled(30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        row.addWidget(mark)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QHBoxLayout()
        name.setSpacing(6)
        name.addWidget(label(APP_NAME, "appTitle"))
        name.addWidget(label("v" + __version__, "version"))
        name.addStretch()
        brand.addLayout(name)
        brand.addWidget(label("MQTT → GW → 模块 统一调试台", "appSub"))
        row.addLayout(brand)
        # 设备切换就是连接卡里设备的另一个入口：连上后锁住，断开才能换
        self.device_seg = Segmented(["机器狗 DOG", "无人机 UAV"])
        self.device_seg.set_index(list(DEVICES).index(self.device_box.currentText()))
        self.device_seg.changed.connect(lambda index: self.device_box.setCurrentText(list(DEVICES)[index]))
        row.addWidget(self.device_seg)
        row.addStretch()
        self.pill = Step()
        self.pill.setObjectName("pill")
        self.pill.setCursor(Qt.PointingHandCursor)
        self.pill.setToolTip("连接与通道设置")
        self.pill.clicked.connect(lambda: self.connection_sheet.open())
        pr = QHBoxLayout(self.pill)
        pr.setContentsMargins(14, 6, 12, 6)
        pr.setSpacing(8)
        self.pill_dot = dot(GRAY, 10)
        self.pill_text = label("")
        self.pill_text.setTextFormat(Qt.RichText)
        self.pill_rate = label("", "kbd")
        pr.addWidget(self.pill_dot)
        pr.addWidget(self.pill_text)
        pr.addWidget(self.pill_rate)
        pr.addWidget(label("⌄", "caption"))
        row.addWidget(self.pill)
        row.addWidget(self._quick_view())
        row.addStretch()
        # 无人机默认只读：打开才往 VFC、Bridge 的指令主题发，控制调试页和航线任务页都受它管；每次新连接自动关上
        self.uav_lock = QFrame()
        self.uav_lock.setObjectName("lockPill")
        lr = QHBoxLayout(self.uav_lock)
        lr.setContentsMargins(12, 2, 6, 2)
        self.uav_send = Switch("允许下发")
        self.uav_send.setToolTip("无人机默认只读，打开后才向 VFC / Bridge 的指令主题发送")
        self.uav_send.toggled.connect(lambda on: (setattr(self.mqtt, "uav_send", on), self.uav.render()))
        lr.addWidget(self.uav_send)
        self.uav_lock.hide()
        row.addWidget(self.uav_lock)
        palette_btn = button("搜指令  Ctrl+K", self.control._quick_pick)
        palette_btn.setToolTip("快速指令目录（任何页面按 Ctrl+K）")
        row.addWidget(palette_btn)
        theme = button("◐", self._toggle_theme)
        theme.setObjectName("icon")
        theme.setToolTip("切换浅色 / 深色")
        row.addWidget(theme)
        return bar

    def _sidebar(self):
        """左侧栏：统一工作台的页和快捷键，底下是当前设备档案和演示故障定位；连接的控件也在这里建，放进连接设置面板"""
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(224)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(12, 14, 12, 14)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(label("统一工作台", "groupHeader"))
        head.addStretch()
        head.addWidget(label("快捷键 Ctrl+", "groupHeader"))
        layout.addLayout(head)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(22, 22))
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav.setItemDelegate(NavDelegate(self.nav))
        self.nav.setMouseTracking(True)
        self.nav.setSpacing(1)
        self._fill_nav("dog")
        self.nav.currentRowChanged.connect(lambda row: row >= 0 and self._page(self.nav.item(row).data(Qt.UserRole)))
        layout.addWidget(self.nav)
        layout.addStretch()

        device = Step()
        device.setObjectName("sideCard")
        device.setCursor(Qt.PointingHandCursor)
        device.setToolTip("点开连接与通道设置")
        dl = QVBoxLayout(device)
        dl.setContentsMargins(12, 10, 12, 10)
        dl.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(label("当前设备档案", "groupHeader"))
        top.addStretch()
        self.device_dot = dot("#C7C7CC")
        top.addWidget(self.device_dot)
        self.device_badge = label("DOG 机器狗", "badge")
        top.addWidget(self.device_badge)
        dl.addLayout(top)
        self.device_name = label("", "deviceName")
        self.device_meta = label("", "caption", True)
        dl.addWidget(self.device_name)
        dl.addWidget(self.device_meta)
        # OSD 轨迹回放画在 3D 观察区里，观察区不显示时这一行也先藏起来
        self.replay_row = QWidget()
        replay = QHBoxLayout(self.replay_row)
        replay.setContentsMargins(0, 0, 0, 0)
        replay.addWidget(label("会话回放", "caption"))
        replay.addStretch()
        replay.addWidget(button("OSD 轨迹回放", lambda: self.scene3d._replay_scan(), "link"))
        self.replay_row.setVisible(bool(self.OBSERVED))
        dl.addWidget(self.replay_row)
        layout.addWidget(device)
        layout.addWidget(button("●  演示一次故障定位", lambda: self.control._demo()))

        # 下面这些控件放进连接设置面板（_connection_card），在这里建是因为别处构建时就要用到
        self.profile_box = QComboBox()
        self.profile_box.activated.connect(self.connection._profile_chosen)
        # 设备类型跟着连接走（随连接配置保存），左侧导航跟着换；连上后锁住，断开才能换；界面上用顶栏的分段切换
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
        self.port.setFixedWidth(90)
        self.sn = QLineEdit("TEST_DOG_001")
        self.sn.setPlaceholderText("设备 SN")
        self.sn.textChanged.connect(self.control._update_topic)
        self.sn.textChanged.connect(self.connection._connection_hint)
        self.sn.textChanged.connect(self.scene3d._device_card)
        self.sn.textChanged.connect(lambda _: self._update_pill())
        self.host.textChanged.connect(self.connection._connection_hint)
        self.host.textChanged.connect(lambda _: self._update_pill())
        self.user = QLineEdit()
        self.user.setPlaceholderText("用户名")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("密码")
        self.tls = Switch("")
        # 远程：经 FRP 用 SSH 登到狗上，把狗上的 Broker 和 ZLM 端口转到本机，之后和直连一样用；取设备日志也用这组 SSH 账号
        stored = settings()
        self.frp = Switch("")
        self.frp.toggled.connect(self.connection._frp_toggled)
        # 无人机模拟云端用的 SN 和是否替云端回上线、心跳；和狗的 SN 分开记
        self.uav_sn = QLineEdit(stored.value("uav_sn", "1209039909594066"))
        self.uav_sn.setPlaceholderText("无人机 SN")
        self.uav_sn.editingFinished.connect(lambda: settings().setValue("uav_sn", self.uav_sn.text().strip()))
        self.uav_sn.textChanged.connect(self.control._update_topic)
        self.cloud_answer = Switch("")
        self.cloud_answer.setChecked(stored.value("cloud_answer", True, type=bool))
        self.cloud_answer.toggled.connect(lambda on: settings().setValue("cloud_answer", on))
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
        self.link_dot = dot(GRAY)
        self.link = label("", "caption", True)
        self.rate_label = label("", "caption")
        device.clicked.connect(lambda: self.connection_sheet.open())
        self.connection._load_profiles()
        return side

    def _connection_card(self):
        """连接与通道设置：预设配置、接入方式、Broker、SN、账号、FRP、TLS，下面是连接状态和连接 / 断开"""
        sheet = QFrame()
        sheet.setObjectName("sheet")
        sheet.setFixedWidth(500)
        form = QVBoxLayout(sheet)
        form.setContentsMargins(22, 18, 22, 20)
        form.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label("连接与通道设置", "heading"))
        head.addStretch()
        head.addWidget(button("完成", lambda: self.connection_sheet.close()))
        form.addLayout(head)
        form.addWidget(label("预设连接配置（选一个就填好下面各项，密码不保存）", "caption"))
        saved = QHBoxLayout()
        saved.addWidget(self.profile_box, 1)
        more = QPushButton("⋯")
        more.setFixedWidth(42)
        # 去掉菜单箭头，窄按钮里只放“⋯”
        more.setStyleSheet("QPushButton{font-size:18px;padding:0 0 4px 0}QPushButton::menu-indicator{image:none;width:0px}")
        more.setToolTip("保存或删除连接配置")
        menu = QMenu(more)
        menu.addAction("把当前填写存为配置…", self.connection._save_profile)
        menu.addAction("删除选中的配置", self.connection._delete_profile)
        more.setMenu(menu)
        saved.addWidget(more)
        form.addLayout(saved)
        form.addWidget(label("接入模式", "caption"))
        # 分段只是接入方式下拉的另一个样子，切换逻辑还在下拉上
        self.mode_seg = Segmented(["沙盒模拟", "云端 → GW", "局域网直连", "离线分析"])
        self.mode_seg.set_index(self.mode_box.currentIndex())
        self.mode_seg.changed.connect(self.mode_box.setCurrentIndex)
        self.mode_box.currentIndexChanged.connect(lambda index: self.mode_seg.index() != index and self.mode_seg.set_index(index))
        form.addWidget(self.mode_seg)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.addWidget(label("Broker 地址", "caption"), 0, 0)
        grid.addWidget(label("端口", "caption"), 0, 1)
        grid.addWidget(self.host, 1, 0)
        grid.addWidget(self.port, 1, 1)
        form.addLayout(grid)
        self.sn_caption = label("设备 SN（Topic 路由键）", "caption")
        form.addWidget(self.sn_caption)
        form.addWidget(self.sn)
        # 无人机“云端 → GW”就是模拟云端：往 tta/uav/{sn}/services 发，收 GW 转上来的回执、状态和 OSD
        self.cloud_box = QWidget()
        cloud = QVBoxLayout(self.cloud_box)
        cloud.setContentsMargins(0, 0, 0, 0)
        cloud.setSpacing(4)
        cloud.addWidget(label("无人机 SN（板上 db/deviceInfo.cfg）", "caption"))
        cloud.addWidget(self.uav_sn)
        cloud.addWidget(label("Broker 要和板上 GW 云端那一侧的一致（db/accessServer.cfg），否则收不到上报", "caption", True))
        cloud.addLayout(self._switch_row(self.cloud_answer, "扮演云端应答上线和心跳", "不回的话 GW 一直显示离线"))
        form.addWidget(self.cloud_box)
        account = QHBoxLayout()
        account.addWidget(self.user)
        account.addWidget(self.password)
        form.addLayout(account)
        self.ssh_row = QWidget()
        ssh = QVBoxLayout(self.ssh_row)
        ssh.setContentsMargins(0, 4, 0, 0)
        ssh.setSpacing(6)
        ssh.addLayout(self._switch_row(self.frp, "经远程 FRP（SSH 端口转发）", "把狗上的 1883 和 ZLM 80 转到本机，之后和直连一样用"))
        account = QHBoxLayout()
        account.addWidget(self.ssh_user)
        account.addWidget(self.ssh_password)
        ssh.addLayout(account)
        form.addWidget(self.ssh_row)
        form.addLayout(self._switch_row(self.tls, "启用 TLS 加密传输", "云端生产 Broker 建议开启"))
        state = QHBoxLayout()
        state.addWidget(self.link_dot)
        state.addWidget(self.link, 1)
        state.addWidget(self.rate_label, 0, Qt.AlignTop)
        form.addLayout(state)
        self.connect_btn.setMinimumHeight(38)
        form.addWidget(self.connect_btn)
        return sheet

    @staticmethod
    def _switch_row(switch, title, note):
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(label(title))
        text.addWidget(label(note, "caption", True))
        row.addLayout(text, 1)
        row.addWidget(switch)
        return row

    def _update_pill(self):
        """顶栏连接胶囊：圆点颜色跟连接状态，文字是接入方式和 SN（无人机写 Broker 地址），后面是每秒消息数"""
        if not hasattr(self, "pill_text"):
            return
        mode = {"sandbox": "沙盒模拟", "gw": "云端 MQTT", "direct": "局域网直连", "offline": "离线分析"}[self.mode]
        uav = DEVICES[self.device_box.currentText()] == "uav"
        if uav:
            # 无人机：模拟云端写 SN，模拟 GW（本地主题）写 Broker 地址
            mode = {"gw": "模拟云端", "direct": "模拟 GW"}.get(self.mode, mode)
            target = (self.mqtt.cloud_sn or self.mqtt.host) if self.mqtt.connected else (
                self.uav_sn.text().strip() if self.mode == "gw" else self.host.text().strip())
        else:
            target = self.mqtt.sn if self.mqtt.connected else self.sn.text().strip()
        self.pill_text.setText(f"<b>{html.escape(mode)}</b>　<span style='color:#8E8E93'>·</span>　"
                               f"<span style='font-family:Consolas,monospace'>{html.escape(target or '—')}</span>")
        color = GREEN if self.mqtt.connected else ORANGE if self.mqtt.client or self.connection.ssh_connecting else GRAY
        self.pill_dot.setStyleSheet(f"background:{color};border-radius:5px")
        self.pill_rate.setText(self.rate_label.text())
        self.pill_rate.setVisible(bool(self.rate_label.text()))

    def uav_cloud(self):
        """设备是无人机、方式是“云端 → GW”：模拟云端，指令走 tta/uav/{sn}/services 经真 GW 转到 VFC"""
        return self.mode == "gw" and DEVICES[self.device_box.currentText()] == "uav"

    def uav_wire(self, payload, topic):
        """VFC 指令按当前方式定主题和外壳：模拟云端改发 services 并带 target，模拟 GW 照本地；Bridge 命令只有本地"""
        if self.uav_cloud() and topic == VFC_COMMAND:
            return cloud_topic(self.uav_sn.text().strip()), cloud_command(payload)
        return topic, payload

    def _lock_device(self, locked):
        """连着或正在登录时不能换设备：顶栏的分段和连接卡里的设备一起锁"""
        self.device_box.setEnabled(not locked)
        if hasattr(self, "device_seg"):
            self.device_seg.setEnabled(not locked)

    @property
    def dark(self):
        return QApplication.instance().palette().color(QPalette.Window).lightness() < 128

    def _toggle_theme(self):
        """换外观：整窗重新套样式；不再截图盖住做淡出，截图在带 3D 视图时很卡"""
        dark = not self.dark
        apply_theme(QApplication.instance(), dark)
        settings().setValue("theme", "dark" if dark else "light")
        self.scene3d.set_dark(dark)

    def keyPressEvent(self, event):
        """焦点不在输入框时直接按数字 1～5 切到侧栏第几页（输入框会先收下数字，不会误切）"""
        if event.modifiers() == Qt.NoModifier and Qt.Key_1 <= event.key() <= Qt.Key_5:
            row = event.key() - Qt.Key_1
            if row < self.nav.count():
                self.nav.setCurrentRow(row)
                return
        super().keyPressEvent(event)

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
        """遥控类页面顶部的“最近一条指令结果”卡片，颜色随结果变，点一下单独开窗口看证据链；各页共用同一份状态"""
        chip = Step()
        chip.setObjectName("quickChip")
        chip.setCursor(Qt.PointingHandCursor)
        chip.setMaximumWidth(380)
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
            self.open_request(self.quick)

    def open_request(self, event, events=None):
        """单独开一个窗口看这条消息所在的整条请求：结论、证据链、原始 JSON；events 不给时按 tid 从库里取"""
        self.store.flush()
        events = events or (self.store.trace(event) if event.tid else [event])
        focus = next((e for e in events if diagnose(e).status in ("失败", "错误线索")), event if event in events else events[-1])
        dialog = QDialog(self)
        dialog.setWindowTitle(f"请求 {event.method} · {event.tid or '无 tid'}")
        dialog.resize(980, 720)
        layout = QVBoxLayout(dialog)
        view = TraceView("")
        view.original.connect(self._original)
        view.store, view.nearby_logs = self.store, True
        view.nearby.connect(lambda chosen: self.logs._nearby_logs(chosen, self.stack.currentIndex()))
        view.show_trace(focus, events)
        layout.addWidget(view)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.show()
        return dialog


    # ---- 页面与状态 ----

    def _page(self, index):
        self._slide_to(index)
        uav = DEVICES[self.device_box.currentText()] == "uav"
        page = {self.OVERVIEW: self.status, self.LOGS: self.logs, self.TELEOP: None if uav else self.teleop,
                self.MISSION: self.uav if uav else None}.get(index)
        if page:
            page.shown()
        if uav and index == self.OVERVIEW:
            self.uav.shown()
        self._show_observer(index)
        # 窗口还没显示时不加载，showEvent 再补；测试里窗口不显示，也就不加载 OpenGL
        if index in self.OBSERVED and self.isVisible():
            self.scene3d._ensure_viewer()

    def _show_observer(self, index):
        # 3D 观察区画的是狗的模型和遥测，设备选无人机时不显示
        self.scene3d.observer.setVisible(index in self.OBSERVED and DEVICES[self.device_box.currentText()] != "uav")

    def _slide_to(self, index):
        """切页直接换页面栈；不截整窗做动画（带 OpenGL 视图和高频表格时 grab 很卡主线程）"""
        self.stack.setCurrentIndex(index)

    def _fill_nav(self, device):
        """左侧导航换成这种设备的页面；每行记着页面栈序号，不按行号对页面"""
        self.nav.blockSignals(True)
        self.nav.clear()
        for title, index in self.NAV[device]:
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, index)
            self.nav.addItem(item)
        # 行高 40 加上下各 1 px 间距；固定高度，窗口矮时由侧栏整体滚动
        self.nav.setFixedHeight(len(self.NAV[device]) * 44 + 8)
        self.nav.blockSignals(False)

    def _device_changed(self):
        """换设备（顶栏分段或连接配置）：5 个页不换，各页换成这种设备的内容和协议目录，顶栏和连接面板跟着换"""
        device = DEVICES[self.device_box.currentText()]
        self.teleop_stack.show_device(device)
        self.mission_stack.show_device(device)
        self.uav_lock.setVisible(device == "uav")
        if self.device_seg.index() != list(DEVICES.values()).index(device):
            self.device_seg.set_index(list(DEVICES.values()).index(device))
        # 无人机的主题里不带 SN，也不走 FRP
        self.sn.setVisible(device == "dog")
        self.sn_caption.setVisible(device == "dog")
        self.cloud_box.setVisible(device == "uav" and self.mode == "gw")
        if hasattr(self, "connection_sheet"):
            self.connection_sheet.refit()
        if device == "uav":
            self.frp.setChecked(False)
        self.ssh_row.setVisible(device == "dog" and self.mode == "direct")
        self.scene3d._device_card()
        self._update_pill()
        self.control.device_changed()
        self.logs.device_changed(device)
        self.status.device_changed()
        # 页不换，当前页按新设备重画
        self._page(self.stack.currentIndex())

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

    def _reply(self, topic, payload):
        """模拟云端时替云端回 GW 的上线、心跳，和指令一样入库，能在报文里对照"""
        try:
            self.mqtt.reply(topic, payload)
        except RuntimeError as error:
            self.notify(str(error))
            return
        text = json.dumps(payload, ensure_ascii=False)
        self.store.add(Event(topic, payload, "发送", "mqtt", self.mqtt.sn, self.mqtt.session, "gw", qos=0, raw=text))

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
            self._refresh()
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
            chip.setToolTip(f"{self.quick.method} · {status}" + (f"\n{detail}" if detail else "") + "\n点一下单独开窗口看证据链")
            marker.setStyleSheet(f"background:{color};border-radius:5px")
            caption.setText(text)


    # ---- 刷新 ----


    def _refresh(self):
        """有新消息时刷新控制结果、顶部最近指令和状态页；实时消息再多也由 _drain 限到最多每 0.4 s 一次"""
        self.refresh_due, self.refreshed = False, time.monotonic()
        self._refresh_tail()

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
            self.rate_label.setText(f"{self.rate_count / (now - self.rate_at):.0f} msg/s" if self.mqtt.connected else "")
            self.rate_count, self.rate_at = 0, now
            self._update_pill()
        self.scene3d.tick(now)
        if self.stack.currentIndex() == self.OVERVIEW:
            self.status.tick(now)
        if self.stack.currentIndex() in (self.OVERVIEW, self.MISSION) and DEVICES[self.device_box.currentText()] == "uav":
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
            elif kind == "model":
                self.scene3d._import_done(value, extra[0])
            elif kind == "model_error":
                self.scene3d._import_failed(value, extra[0])
            elif kind == "preview":
                self.diagnostics.preview_done(value, extra[0])
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
                self.diagnostics.export_done(value)
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
            self._refresh()
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
    window.device_box.setCurrentText("机器狗")
    window._go(window.OVERVIEW)
    if not window.OBSERVED:
        # 界面上不显示 3D 时不检查渲染，只截一张整窗图
        def skip():
            window.grab().save(str(folder / "self_check_window.png"))
            lines.append("3D 渲染：界面不显示 3D，跳过；整窗截图 self_check_window.png")
            (folder / "self_check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            window.close()
            QApplication.instance().exit(1 if failures else 0)
        QTimer.singleShot(1500, skip)
        return
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
