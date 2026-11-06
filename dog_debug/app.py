"""Qt 整机调试界面（iOS 风格），网络和日志后台输入统一交到事件队列"""

import argparse
import html
import json
import math
import queue
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QDateTime, QEasingCurve, QEvent, QModelIndex, QObject, QParallelAnimationGroup, QPoint, QPointF,
                            QPropertyAnimation, QRectF, QSettings, QSize, QStandardPaths, Qt, QTime, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QIcon, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QShortcut,
                           QPalette, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QButtonGroup, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDoubleSpinBox,
    QFileDialog, QFrame, QGraphicsItem, QGraphicsOpacityEffect, QInputDialog, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter,
    QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableView, QTableWidget,
    QSizePolicy, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from . import __version__
from .core import (DETECTION_METHODS, DETECTION_STAGES, Event, Store, detection_chains, diagnose, diagnostic_zip,
                   export_preview, infer_module, occurred, payload_diff, requests, summarize, trace_report)
from .logs import detect_encoding, first_object, import_log, log_event
from .pointcloud import CloudError, cloud_data, decode as decode_cloud, summary as cloud_summary
from .registry import MODULES, entries, find, local_payload, make_command, refresh_ids, route
from . import cad
from . import robot as model_module
from .robot import POSES, VARIANTS
from .scene import LIVE_SECONDS, Scene, osd_fields
from .player import StreamWindow, hide_password
from .remote import LOG_DIRS, LOG_ROOT, Remote, pick_files
from .transport import MqttTransport, simulate, simulate_detection

DATA = Path(__file__).parent / "data"
APP_NAME = "坤坤调试台"
STYLE = (DATA / "style.qss").read_text(encoding="utf-8").replace("@DATA", DATA.as_posix())
MODES = {"沙盒 · 全链路模拟": "sandbox", "云端 → 真实 GW": "gw",
         "直连模块": "direct", "离线日志分析": "offline"}
SCENARIOS = [("正常", "success"), ("执行失败", "failure"), ("无 ACK", "no_ack"),
             ("嵌套失败", "nested_failure"), ("重复回复", "duplicate")]

BLUE, GREEN, ORANGE, RED, GRAY = "#007AFF", "#34C759", "#FF9500", "#FF3B30", "#8E8E93"
PURPLE = "#AF52DE"
MODULE_COLORS = {"gw": "#5856D6", "vfc": BLUE, "ptz": ORANGE, "media": "#FF2D55", "vision": "#AF52DE", "fsvr": GREEN,
                 "ros2bridge": "#A2845E", "zlm": "#30B0C7"}
FAILED = {"失败", "错误线索"}
WARNING = {"契约待核", "未活跃", "待核", "无 ACK", "保留消息"}
PROGRESS = {"已受理/上传中", "阶段待判"}
SUCCESS = {"操作成功", "完成", "活跃", "JPEG 已生成", "已开始", "请求已处理", "导航已受理"}
STAGES_ROLE = Qt.UserRole + 1
# 逐条视图每页条数；日志导入每批交给主线程写库的条数
PAGE_SIZE = 1000
# 请求视图只按指令和 ACK 找请求；不限类型取最近的消息时，高频状态会把旧请求挤出窗口
REQUEST_KINDS = ("指令", "ACK")
# 两路相机固定：机体相机 index 0 / sn 001 / ZLM 流 video0，云台相机 index 1 / sn 002 / video1（用户确认）
CAMERAS = {"001": "机体相机", "002": "云台相机"}
STREAM_CAMERAS = {"video0": "机体相机", "video1": "云台相机"}
# 设备上记着相机源地址、ZLM 的 app / RTSP 端口 / secret、各路推流模式的配置，按顺序找第一个读得到的：
# RK 上是 Media 的 media.cfg（root 0600，读不了时试 sudo -n），NX 上是 vision 的 vision.cfg，两者 streams / zlm 结构相同
STREAM_CFGS = ("/oem/ttaviation/gateway/db/media.cfg", "/oem/ttaviation/gateway/db/vision.cfg")
# GW 连的本地 Broker 写在这里（local_host / local_port）；FRP 登进去的板子上不一定就是 Broker 本身（如 NX 登录、Broker 在 192.168.234.1）
BROKER_CFG = "/oem/ttaviation/gateway/db/mqttBroker.cfg"
# 拉流配置里 livestream_mode 对应公开流 live/<流> 的画面；off 时没有公开流
PUBLIC_MODES = {"direct": "原画转发", "processed": "处理后"}
# ZLM 的 HTTP 端口上能直接拉的格式（getMediaList 的 schema → 地址后缀），按顺序挑第一个有的；都没有才走 RTSP 端口
HTTP_PLAY = (("rtmp", ".live.flv"), ("ts", ".live.ts"), ("fmp4", ".live.mp4"))


def broker_target(remote):
    """经 FRP 时隧道要转到的 Broker（从登录的板子上看）：按设备 mqttBroker.cfg 的 local_host / local_port，
    读不到或没写时是板子自己的 127.0.0.1:1883。在后台线程里调用"""
    try:
        cfg = json.loads(remote.read_file(BROKER_CFG))
    except (OSError, ValueError):
        return "127.0.0.1", 1883
    host = str(cfg.get("local_host") or "127.0.0.1")
    return ("127.0.0.1" if host == "0.0.0.0" else host), int(cfg.get("local_port") or 1883)


def stream_kind(stream, origin):
    """按流名和 ZLM 的来源类型说是哪种画面：__input_ 是相机原始；公开流 direct 模式是拉原始转发（pull），
    processed 模式是 RK 处理后经 RTP 推回来（rtp_push）。认不出的返回空串"""
    if stream.startswith("__input_"):
        return "相机原始"
    if "rtp" in origin:
        return "AI 处理后"
    return "原画转发" if origin == "pull" else ""


def stream_presets(cfg, streams, host, ports, sn):
    """拉流地址候选 [(名称, 地址)]，按相机分组：相机直连、ZLM 相机原始、ZLM 公开流的各种协议、云端 RTMP，最后是 ZLM 上别的流

    cfg 是设备的拉流配置 media.cfg / vision.cfg（没读到为 None），streams 是 ZLM getMediaList 整理后的 {app/流: 信息}（没查到为空）。
    地址都按“从狗上看”写：host 是狗访问 ZLM 用的主机，ports 为 {"http", "rtsp", "rtmp"}；远程时播放前再换成隧道
    """
    out = []

    def zlm(title, name, schemas):
        for schema in schemas:
            if schema == "rtsp":
                out.append((f"{title} · RTSP", f"rtsp://{host}:{ports['rtsp']}/{name}"))
            elif schema == "rtmp":
                out.append((f"{title} · RTMP", f"rtmp://{host}:{ports['rtmp']}/{name}"))
                out.append((f"{title} · HTTP-FLV", f"http://{host}:{ports['http']}/{name}.live.flv"))
            elif schema == "ts":
                out.append((f"{title} · HTTP-TS", f"http://{host}:{ports['http']}/{name}.live.ts"))
            elif schema == "fmp4":
                out.append((f"{title} · HTTP-fMP4", f"http://{host}:{ports['http']}/{name}.live.mp4"))
            elif schema == "hls":
                out.append((f"{title} · HLS", f"http://{host}:{ports['http']}/{name}/hls.m3u8"))

    covered = set()
    config = cfg or {}
    app = (config.get("zlm") or {}).get("app") or "live"
    protocols = (config.get("zlm") or {}).get("push_protocol") or {}
    template = (config.get("zlm") or {}).get("rtmp_push_url_template") or ""
    for sid, item in (config.get("streams") or {}).items():
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        camera = f"{STREAM_CAMERAS.get(sid, sid)} {item.get('camera_sn', '')}".strip()
        source = (item.get("zlm") or {}).get("source_url")
        if source:
            out.append((f"{camera} · 相机直连 · {source.split(':')[0].upper()}", source))
        raw = f"{app}/__input_{sid}"
        covered.add(raw)
        # ZLM 上实际有哪些协议以查到的为准；没查到时按 Media 建流时打开的协议（原始输入只开 RTSP，公开流开 RTSP 和 RTMP）
        zlm(f"{camera} · ZLM 相机原始", raw, (streams.get(raw) or {}).get("schemas") or ["rtsp"])
        mode = item.get("livestream_mode")
        if mode in PUBLIC_MODES:
            public = f"{app}/{sid}"
            covered.add(public)
            title = PUBLIC_MODES[mode] + ("（AI）" if mode == "processed" and item.get("ai_enabled") else "")
            zlm(f"{camera} · ZLM {title}", public, (streams.get(public) or {}).get("schemas") or ["rtsp", "rtmp"])
        # 云端 RTMP 只在这路推流协议配成 rtmp 时有；实际地址可能被 GatewayOnlineInfo.cfg 换掉，这里按模板拼
        if protocols.get(sid) == "rtmp" and template.count("%s") == 2:
            out.append((f"{camera} · 云端 RTMP（按配置里的模板）", template.replace("%s", sn, 1).replace("%s", str(item.get("camera_sn", "")), 1)))
    for name, info in streams.items():
        if name not in covered:
            kind = stream_kind(name.split("/")[-1], info.get("origin", ""))
            zlm(f"ZLM {name}" + (f" · {kind}" if kind else ""), name, info.get("schemas") or [])
    return out
# 消息页三张表和日志表的初始列宽（最后一列自动撑满）
COLUMN_WIDTHS = {"EventModel": (84, 90, 70, 210, 100, 130), "RequestModel": (84, 90, 210, 100, 70, 300),
                 "ChainModel": (84, 230, 190), "LogModel": (96, 48, 76, 68),
                 "ModuleMessageModel": (72, 136, 78)}
# 日志排障页一次最多列出的行数，再多用搜索缩小
LOG_ROWS = 5000
IMPORT_BATCH = 500
# 从设备取日志的时间段：名称 → 往前多少秒，0 为设备上保留的全部
# 日志排障页按原文时间筛的时间段：名称 → 往前多少秒，None 为不按时间筛，"today" 为今天 0 点起，"custom" 为自己选起止
LOG_RANGES = {"全部时间": None, "最近 30 分钟": 1800, "最近 2 小时": 7200, "今天": "today", "选时间段…": "custom"}
# 实时跟随的批次最后一行超过这么久就删掉：内容只在本机库里，要找回可以从设备按时间段再取
LIVE_KEEP = 48 * 3600
# 从请求看附近日志时，在请求首尾各往外放的秒数；直播等指令从下发到 ACK 一般在几秒内
NEARBY = 30
LOG_PERIODS = {"最近 30 分钟": 1800, "最近 2 小时": 7200, "最近 12 小时": 43200, "设备上保留的全部": 0}
# 超过这个时间没有新状态，状态页的模块圆点变灰
FRESH_SECONDS = 10
# 带走势线的遥测字段和线色
SPARK_COLORS = {"batteryRemain": GREEN, "speed": BLUE, "heading": ORANGE, "temperature": RED}


def tone(status):
    """诊断状态对应的系统色，未登记的状态显示灰色"""
    if status in FAILED:
        return RED
    if status in WARNING:
        return ORANGE
    if status in PROGRESS:
        return BLUE
    return GREEN if status in SUCCESS else GRAY


def tint(color, alpha):
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def label(text="", name="", wrap=False):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def pill(text, color):
    widget = QLabel(text)
    widget.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    widget.setStyleSheet(f"background:{tint(color, 34)};color:{color};border-radius:9px;"
                         "padding:2px 9px;font-size:11px;font-weight:600")
    return widget


def dot(color, size=8):
    widget = QLabel()
    widget.setFixedSize(size, size)
    widget.setStyleSheet(f"background:{color};border-radius:{size // 2}px")
    return widget


def method_label(method):
    """表格里显示的方法名：目录里有中文名时写成“中文名 method”"""
    item = find(method)
    return f"{item['title']}　{method}" if item else method


def card(spacing=10, margins=16):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return frame, layout


def button(text, slot, name=""):
    widget = QPushButton(text)
    widget.setCursor(Qt.PointingHandCursor)
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(slot)
    return press_feedback(widget)


def logo_image(size):
    """程序标志（data/logo.png，由 tools/make_icon.py 从选定原图生成），缩到 size×size 返回 QImage

    一次缩太多会发糊，先按一半一半缩到目标的两倍以内，再缩到目标尺寸
    """
    image = QImage(str(DATA / "logo.png"))
    while image.width() >= size * 2:
        image = image.scaled(image.width() // 2, image.height() // 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return image.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def logo_icon():
    """窗口和任务栏图标：多个尺寸一起放进 QIcon，让系统按需取用"""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(QPixmap.fromImage(logo_image(size)))
    return icon


def glyph_icon(char, color):
    """iOS 设置页式的圆角色块图标，按 2 倍像素绘制保证高分屏清晰"""
    pixmap = QPixmap(44, 44)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(QRectF(0, 0, 44, 44), 10, 10)
    font = QFont(QApplication.font())
    font.setPixelSize(22 if len(char) == 1 else 16)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(QRectF(0, 0, 44, 44), Qt.AlignCenter, char)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    icon = QIcon(pixmap)
    # 选中行不让 Qt 给图标叠加高亮色
    icon.addPixmap(pixmap, QIcon.Selected)
    return icon


class Segmented(QFrame):
    """iOS 分段控件，`changed` 发出新选中的序号"""

    changed = Signal(int)

    def __init__(self, items):
        super().__init__()
        self.setObjectName("seg")
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(2)
        # 选中块是按钮后面的一块底色，换选中项时滑过去，不是瞬间跳
        self.thumb = QFrame(self)
        self.thumb.setObjectName("segThumb")
        self.slide = QPropertyAnimation(self.thumb, b"geometry", self)
        self.slide.setDuration(260)
        self.slide.setEasingCurve(QEasingCurve.OutCubic)
        self.group = QButtonGroup(self)
        for index, text in enumerate(items):
            item = QPushButton(text)
            item.setObjectName("segBtn")
            item.setCheckable(True)
            item.setCursor(Qt.PointingHandCursor)
            # 窄窗口下宁可整体换行或滚动，也不把按钮文字挤成半个字
            item.setMinimumWidth(item.sizeHint().width())
            self.group.addButton(item, index)
            row.addWidget(item)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(self.changed)
        self.group.idToggled.connect(lambda index, on: on and self._move_thumb(True))

    def _move_thumb(self, animate):
        target = self.group.checkedButton().geometry()
        self.slide.stop()
        if animate and self.isVisible() and self.thumb.width():
            self.slide.setStartValue(self.thumb.geometry())
            self.slide.setEndValue(target)
            self.slide.start()
        else:
            self.thumb.setGeometry(target)
        self.thumb.lower()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 按钮位置要等布局排好才有效，尺寸变了直接贴到选中项
        QTimer.singleShot(0, lambda: self._move_thumb(False))

    def index(self):
        return self.group.checkedId()

    def set_index(self, index):
        self.group.button(index).setChecked(True)
        self.changed.emit(index)


class Switch(QCheckBox):
    """iOS 开关外观的 QCheckBox，信号和状态接口不变"""

    def __init__(self, text=""):
        super().__init__(text)
        self.setCursor(Qt.PointingHandCursor)

    def sizeHint(self):
        extra = self.fontMetrics().horizontalAdvance(self.text()) + 10 if self.text() else 0
        return QSize(42 + extra, 26)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        track = QRectF(0, (self.height() - 24) / 2, 40, 24)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(GREEN if self.isChecked() else "#E1E1E6"))
        painter.drawRoundedRect(track, 12, 12)
        knob = track.right() - 22 if self.isChecked() else track.left() + 2
        painter.setBrush(QColor("white"))
        painter.drawEllipse(QRectF(knob, track.top() + 2, 20, 20))
        if self.text():
            painter.setPen(self.palette().color(QPalette.WindowText))
            painter.drawText(QRectF(50, 0, self.width() - 50, self.height()), Qt.AlignVCenter, self.text())


class WheelGuard(QObject):
    """滚动区里的滑条、下拉框、数字框没获得焦点时，滚轮只滚页面，不偷偷改值"""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and isinstance(obj, (QSlider, QComboBox, QAbstractSpinBox)) and not obj.hasFocus():
            area = obj.parentWidget()
            while area and not isinstance(area, QScrollArea):
                area = area.parentWidget()
            if area:
                bar = area.verticalScrollBar()
                bar.setValue(bar.value() - event.angleDelta().y() // 120 * bar.singleStep() * 3)
            return True
        return False


class PressFeedback(QObject):
    """按钮按下时快速变淡、松开后淡回（参照 host 的 PressFeedback：60 ms 按下、260 ms 恢复，透明度调到 72%）；只加动画，不拦截事件"""

    def eventFilter(self, button, event):
        kind = event.type()
        if kind == QEvent.MouseButtonPress and button.isEnabled() and event.button() == Qt.LeftButton:
            # host 用 45%，在浅色按钮上像被禁用了，调轻一些
            self._fade(button, 0.72, 60)
        elif kind in (QEvent.MouseButtonRelease, QEvent.Leave, QEvent.Hide, QEvent.EnabledChange) and button.graphicsEffect():
            self._fade(button, 1.0, 260)
        return False

    @staticmethod
    def _fade(button, target, ms):
        effect = button.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            if target >= 1.0:
                return
            effect = QGraphicsOpacityEffect(button)
            button.setGraphicsEffect(effect)
            effect.fade = QPropertyAnimation(effect, b"opacity", effect)
            # 回到不透明后撤掉效果，按钮恢复正常绘制；要等下一轮事件循环，finished 里直接撤会删掉正在发信号的动画
            effect.fade.finished.connect(lambda: effect.opacity() >= 0.999 and QTimer.singleShot(
                0, lambda: button.graphicsEffect() is effect and button.setGraphicsEffect(None)))
        effect.fade.stop()
        effect.fade.setDuration(ms)
        effect.fade.setEasingCurve(QEasingCurve.OutQuad if target < 1.0 else QEasingCurve.OutCubic)
        effect.fade.setStartValue(effect.opacity())
        effect.fade.setEndValue(target)
        effect.fade.start()


_PRESS_FEEDBACK = []


def press_feedback(widget):
    """给按钮装上按下反馈，返回 widget；遥控方向键按住时本身整块变色，不装。和滚轮保护一样只装在按钮自己身上"""
    if widget.objectName() in ("pad", "padStop"):
        return widget
    if not _PRESS_FEEDBACK:
        _PRESS_FEEDBACK.append(PressFeedback())
    widget.installEventFilter(_PRESS_FEEDBACK[0])
    return widget


_WHEEL_GUARD = []


def guard_wheel(widget):
    """给滑条、下拉框、数字框装上 WheelGuard，返回 widget

    只装在这些控件自己身上，不装成应用级过滤器：应用级过滤器让程序里每个事件都先过一遍 Python，
    一次换外观就有三万多次，平时鼠标移动、重绘也一直在付这笔开销
    """
    if not _WHEEL_GUARD:
        _WHEEL_GUARD.append(WheelGuard())
    widget.installEventFilter(_WHEEL_GUARD[0])
    return widget


class FlowGrid(QWidget):
    """卡片网格：按可用宽度自动在 1 到 columns 列之间切换，窄窗口不出现横向滚动"""

    def __init__(self, cards, columns=2, min_width=520):
        super().__init__()
        self.every, self.cards, self.max_columns, self.min_width = list(cards), list(cards), columns, min_width
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 4, 0)
        self.grid.setSpacing(14)
        self.columns = None
        self._place(columns)

    def resizeEvent(self, event):
        columns = max(1, min(self.max_columns, self.width() // self.min_width))
        if columns != self.columns:
            self._place(columns)
        super().resizeEvent(event)

    def show_only(self, cards):
        """只显示并重排 cards，其余卡片隐藏且不占位"""
        for widget in self.every:
            widget.setVisible(widget in cards)
        self.cards = [w for w in self.every if w in cards]
        self._place(self.columns)

    def _place(self, columns):
        for widget in self.every:
            self.grid.removeWidget(widget)
        for column in range(self.max_columns):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, widget in enumerate(self.cards):
            self.grid.addWidget(widget, index // columns, index % columns)
        rows = (len(self.cards) + columns - 1) // columns
        for row in range(len(self.cards) + 1):
            self.grid.setRowStretch(row, 1 if row == rows else 0)
        self.columns = columns


class StayMenu(QMenu):
    """多选菜单：点可勾选的项只切换勾选，菜单不收起"""

    def mouseReleaseEvent(self, event):
        action = self.activeAction()
        if action and action.isCheckable() and action.isEnabled():
            action.trigger()
            return
        super().mouseReleaseEvent(event)


def fade_to(stack, index, ms=200):
    """切到 stack 的第 index 页，新页从透明淡入；窗口没显示时直接切"""
    stack.setCurrentIndex(index)
    page = stack.currentWidget()
    if not page.isVisible():
        return
    effect = QGraphicsOpacityEffect(page)
    page.setGraphicsEffect(effect)
    fade = QPropertyAnimation(effect, b"opacity", page)
    fade.setDuration(ms)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setEasingCurve(QEasingCurve.OutCubic)
    # 只撤本次加的效果，连续快速切换时后一次可能已经换上自己的
    fade.finished.connect(lambda: page.graphicsEffect() is effect and page.setGraphicsEffect(None))
    fade.start(QPropertyAnimation.DeleteWhenStopped)


def scroller(widget):
    """只允许竖向滚动的滚动区，内容宽度跟随视口"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setWidget(widget)
    return area


class PillDelegate(QStyledItemDelegate):
    """把带 UserRole 颜色的单元格画成圆角标签"""

    def paint(self, painter, option, index):
        color = index.data(Qt.UserRole)
        if not color:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        opt.widget.style().drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont(option.font)
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(text) + 18
        rect = QRectF(option.rect.x() + 6, option.rect.center().y() - 10 + 1, width, 20)
        painter.setPen(Qt.NoPen)
        fill = QColor(color)
        fill.setAlpha(34)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(QColor(color))
        painter.drawText(rect, Qt.AlignCenter, text)
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width() + 24, size.height())


class Sparkline(QWidget):
    """遥测卡里最近 60 s 的走势小线；纵轴按这段时间的最小最大值自适应，少于两个点不画"""

    WINDOW = 60

    def __init__(self, color):
        super().__init__()
        self.color, self.points = QColor(color), []
        self.setFixedHeight(18)

    def add(self, t, value):
        self.points.append((t, value))
        self.points = [p for p in self.points if p[0] >= t - self.WINDOW]
        self.update()

    def paintEvent(self, event):
        if len(self.points) < 2:
            return
        t0, t1 = self.points[0][0], self.points[-1][0]
        low, high = min(v for _, v in self.points), max(v for _, v in self.points)
        span, width, height = (high - low) or 1.0, self.width() - 2, self.height() - 3
        line = QPainterPath()
        for i, (t, v) in enumerate(self.points):
            x = 1 + width * (t - t0) / ((t1 - t0) or 1)
            # 数值不变时画在中间，免得贴底像没数据
            y = 1 + height * (0.5 if high == low else 1 - (v - low) / span)
            line.lineTo(x, y) if i else line.moveTo(x, y)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        fill = QPainterPath(line)
        fill.lineTo(line.currentPosition().x(), self.height())
        fill.lineTo(1, self.height())
        gradient = QLinearGradient(0, 0, 0, self.height())
        top = QColor(self.color)
        top.setAlpha(70)
        gradient.setColorAt(0, top)
        gradient.setColorAt(1, QColor(0, 0, 0, 0))
        painter.fillPath(fill, gradient)
        painter.setPen(QPen(self.color, 1.6))
        painter.drawPath(line)


class Compass(QWidget):
    """叠在 3D 视图左下角的航向表和速度；按场景约定 0° 对齐 x 轴、逆时针为正（待真机核对），不标东南西北

    挂在视图上，跟随父控件尺寸贴在左下角；鼠标事件穿透，不挡拖动旋转
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedSize(92, 112)
        self.heading, self.speed, self.state = 0.0, 0.0, "演示"
        self.setToolTip("航向按场景约定：0° 对齐 x 轴、逆时针为正，待真机核对")
        parent.installEventFilter(self)
        self._place()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            self._place()
        return False

    def _place(self):
        self.move(14, self.parent().height() - self.height() - 14)

    def set(self, heading, speed, state):
        """state 为“OSD 实时”“回放”或“演示”"""
        if (heading, speed, state) != (self.heading, self.speed, self.state):
            self.heading, self.speed, self.state = heading, speed, state
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center, radius = QPointF(46, 46), 40
        painter.setPen(QPen(QColor(128, 128, 128, 40), 1))
        face = self.palette().color(QPalette.Base)
        face.setAlpha(215)
        painter.setBrush(face)
        painter.drawEllipse(center, radius, radius)
        font = QFont(self.font())
        font.setPixelSize(9)
        painter.setFont(font)
        painter.setPen(QColor(GRAY))
        for angle in range(0, 360, 30):
            painter.save()
            painter.translate(center)
            painter.rotate(-angle)
            painter.drawLine(QPointF(0, -radius + 3), QPointF(0, -radius + (8 if angle % 90 == 0 else 5)))
            painter.restore()
        # 左右两个数字往里收，免得贴着刻度线看成负号
        for angle, (dx, dy) in ((0, (0, -27)), (90, (-22, 0)), (180, (0, 27)), (270, (22, 0))):
            painter.drawText(QRectF(center.x() + dx - 12, center.y() + dy - 6, 24, 12), Qt.AlignCenter, str(angle))
        painter.save()
        painter.translate(center)
        painter.rotate(-self.heading)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor({"OSD 实时": GREEN, "回放": PURPLE}.get(self.state, BLUE)))
        painter.drawPolygon(QPolygonF([QPointF(0, -20), QPointF(7, 8), QPointF(0, 3), QPointF(-7, 8)]))
        painter.restore()
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(self.palette().color(QPalette.WindowText))
        painter.drawText(QRectF(0, 90, 92, 12), Qt.AlignCenter, f"{self.heading:.0f}°　{self.speed:.2f} m/s")
        font.setBold(False)
        font.setPixelSize(9)
        painter.setFont(font)
        painter.setPen(QColor(GRAY))
        painter.drawText(QRectF(0, 101, 92, 11), Qt.AlignCenter, self.state)


def nav_icon(painter, name, x, y, color):
    """侧栏的线形图标，画在 (x, y) 起的约 20 px 方框里；name 为页面名"""
    p = painter
    p.save()
    p.translate(x, y)
    p.scale(0.85, 0.85)
    p.setPen(QPen(QColor(color), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    line = lambda *points: p.drawPolyline(QPolygonF([QPointF(*pt) for pt in points]))
    if name == "遥控":
        p.drawRoundedRect(QRectF(2, 6, 20, 13), 5, 5)
        line((6, 12), (11, 12))
        line((8.5, 9.5), (8.5, 14.5))
        p.drawEllipse(QPointF(16, 10.5), 0.6, 0.6)
        p.drawEllipse(QPointF(18, 13.5), 0.6, 0.6)
    elif name == "控制调试":
        line((8, 6), (3, 12), (8, 18))
        line((16, 6), (21, 12), (16, 18))
        line((14, 4), (10, 20))
    elif name == "导航与文件":
        line((3, 6), (9, 3), (15, 6), (21, 3), (21, 18), (15, 21), (9, 18), (3, 21), (3, 6))
        line((9, 3), (9, 18))
        line((15, 6), (15, 21))
    elif name == "消息追踪":
        p.drawRoundedRect(QRectF(3, 3, 18, 15), 4, 4)
        line((6, 18), (6, 22), (11, 18))
        line((7, 8), (17, 8))
        line((7, 12), (14, 12))
    elif name == "状态总览":
        line((2, 12), (6, 12), (9, 5), (13, 20), (16, 9), (19, 12), (22, 12))
    elif name == "日志排障":
        p.drawRoundedRect(QRectF(4, 2, 14, 19), 3, 3)
        line((8, 6), (14, 6))
        p.drawEllipse(QPointF(11, 12), 3, 3)
        line((13.5, 14.5), (20, 21))
    elif name == "回归测试":
        line((8, 2), (16, 2))
        line((10, 2), (10, 9), (4, 19), (5, 21), (19, 21), (20, 19), (14, 9), (14, 2))
        line((8, 15), (16, 15))
    else:
        for left, top in ((3, 3), (14, 3), (3, 14), (14, 14)):
            p.drawRoundedRect(QRectF(left, top, 7, 7), 2, 2)
    p.restore()


class NavDelegate(QStyledItemDelegate):
    """侧栏一行：线形图标加页名；选中是白底细边加左侧蓝色短标记，悬停浅底，深浅色按调色板取"""

    def paint(self, painter, option, index):
        # 看程序的调色板：套了样式表的列表自己的调色板窗口色和实际外观不一致
        dark = QApplication.palette().color(QPalette.Window).lightness() < 128
        selected = bool(option.state & QStyle.State_Selected)
        rect = QRectF(option.rect).adjusted(1.5, 1.5, -1.5, -1.5)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if selected:
            fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            fill.setColorAt(0, QColor("#2C2C2E" if dark else "#FFFFFF"))
            fill.setColorAt(1, QColor("#242426" if dark else "#F8FAFE"))
            painter.setBrush(fill)
            painter.setPen(QPen(QColor("#3A3A3C" if dark else "#D8E1EF"), 1))
            painter.drawRoundedRect(rect, 10, 10)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(BLUE))
            painter.drawRoundedRect(QRectF(rect.x() + 5, rect.center().y() - 7.5, 3, 15), 1.5, 1.5)
        elif option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#2A2A2C" if dark else "#E9EEF7"))
            painter.drawRoundedRect(rect, 10, 10)
        name = index.data(Qt.DisplayRole)
        accent = ("#64A8FF" if dark else "#2469D8") if selected else ("#98989F" if dark else "#697C99")
        nav_icon(painter, name, rect.x() + 16, rect.center().y() - 10, accent)
        font = QFont(option.font)
        font.setWeight(QFont.DemiBold if selected else QFont.Medium)
        painter.setFont(font)
        painter.setPen(QColor(("#64A8FF" if dark else "#1F5FC5") if selected else ("#E5E5EA" if dark else "#35445B")))
        painter.drawText(rect.adjusted(46, 0, -8, 0), Qt.AlignVCenter | Qt.AlignLeft, name)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 40)


class CommandDelegate(QStyledItemDelegate):
    """指令列表一行：左边中文名，右边灰色 method；分组标题按普通文字画"""

    def paint(self, painter, option, index):
        item = index.data(Qt.UserRole)
        if not item:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.widget.style().drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        selected = bool(option.state & QStyle.State_Selected)
        rect = option.rect.adjusted(12, 0, -10, 0)
        painter.save()
        font = QFont(option.font)
        font.setPixelSize(11)
        painter.setFont(font)
        method = painter.fontMetrics().elidedText(item["method"], Qt.ElideMiddle, rect.width() // 2)
        painter.setPen(QColor(255, 255, 255, 200) if selected else QColor(GRAY))
        painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, method)
        used = painter.fontMetrics().horizontalAdvance(method) + 10
        painter.setFont(option.font)
        painter.setPen(QColor("white") if selected else option.palette.color(QPalette.Text))
        title = painter.fontMetrics().elidedText(item["title"], Qt.ElideRight, rect.width() - used)
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, title)
        painter.restore()


class EventModel(QAbstractTableModel):
    headers = ["时间", "模块", "类型", "方法", "结果", "来源", "tid · Topic"]

    def __init__(self):
        super().__init__()
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        _, e, d = self.rows[index.row()]
        column = index.column()
        if role == Qt.DisplayRole:
            return [time.strftime("%H:%M:%S", time.localtime(e.timestamp)), e.module.upper(), e.kind,
                    method_label(e.method), d.status, e.source.split(":")[0] + " · " + e.direction, e.tid or e.topic][column]
        if role == Qt.UserRole:
            return MODULE_COLORS.get(e.module, GRAY) if column == 1 else tone(d.status) if column == 4 else None
        if role == Qt.ForegroundRole and column in (0, 5, 6):
            return QColor(GRAY)
        if role == Qt.ToolTipRole:
            return e.topic + ("\n" + d.summary if d.summary else "")

    def replace(self, rows):
        """rows 为 (行号, 事件)，诊断在此一次算好，避免绘制时反复计算"""
        self.beginResetModel()
        self.rows = [(row_id, e, diagnose(e)) for row_id, e in rows]
        self.endResetModel()


class LogModel(QAbstractTableModel):
    """日志排障页的逐条日志：原文时间和原文内容为主，没解析出协议字段的普通文本行也能看懂"""

    headers = ["原文时间", "行", "模块", "结果", "内容"]

    def __init__(self):
        super().__init__()
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]

    @staticmethod
    def content(e):
        """原文压成一行；开头的时间已经单独成列，去掉"""
        text = " ".join((e.raw or json.dumps(e.payload, ensure_ascii=False))[:2000].split())
        return text[len(e.source_time):].lstrip() if e.source_time and text.startswith(e.source_time) else text

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        _, e, d = self.rows[index.row()]
        column = index.column()
        if role == Qt.DisplayRole:
            # 导入时的本机时间不是故障发生时间，原文里没找到时间就空着
            # 日期太占宽度，列里只放时刻，完整时间在提示里；普通文本行没有判定结果，空着不占眼
            return [e.source_time.split()[-1] if e.source_time else "—", e.line, e.module.upper(),
                    "" if d.status == "未判定" else d.status, self.content(e)[:300]][column]
        if role == Qt.UserRole:
            return (MODULE_COLORS.get(e.module, GRAY) if column == 2 else
                    tone(d.status) if column == 3 and d.status != "未判定" else None)
        if role == Qt.ForegroundRole and column in (0, 1):
            return QColor(GRAY)
        if role == Qt.ToolTipRole:
            return (self.content(e)[:1500] + f"\n\n{Path(e.file).name} 第 {e.line} 行　原文时间 {e.source_time or '无'}"
                    + time.strftime("　导入于 %m-%d %H:%M:%S", time.localtime(e.timestamp)) + (f"\ntid {e.tid}" if e.tid else ""))

    def replace(self, rows):
        """rows 为后台已算好诊断的 (行号, 事件, 诊断)"""
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class ModuleMessageModel(QAbstractTableModel):
    """状态页模块详情里的指令与回复，一行一次请求：[(事件, 备注, 结果, 看到它的位置)]"""

    headers = ["时间", "方法", "结果", "备注"]

    def __init__(self):
        super().__init__()
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e, note, status, origin = self.rows[index.row()]
        column = index.column()
        if role == Qt.DisplayRole:
            return [time.strftime("%H:%M:%S", time.localtime(e.timestamp)), method_label(e.method), status, note][column]
        if role == Qt.UserRole and column == 2:
            return tone(status)
        if role == Qt.ForegroundRole and column in (0, 3):
            return QColor(GRAY)
        if role == Qt.ToolTipRole:
            return f"看到它的位置：{origin}\n{e.topic}\ntid {e.tid or '无'}\n点开看指令原文和对应 ACK"

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class RequestModel(QAbstractTableModel):
    """按请求聚合的列表，一行一个 (设备, 会话, tid)"""

    # 链路很长放最后一列，宽度不够时省略，不挤掉结论
    headers = ["时间", "模块", "方法", "结果", "耗时", "结论", "链路"]

    def __init__(self):
        super().__init__()
        self.rows, self.noted = [], set()

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, column = self.rows[index.row()], index.column()
        if role == Qt.DisplayRole:
            latency = "—" if r.latency_ms is None else f"{r.latency_ms:.0f} ms"
            mark = "【备注】" if r.key in self.noted else ""
            return [time.strftime("%H:%M:%S", time.localtime(r.events[0].timestamp)), r.module.upper(), method_label(r.method),
                    r.status, latency, mark + r.summary, r.chain][column]
        if role == Qt.UserRole:
            return MODULE_COLORS.get(r.module, GRAY) if column == 1 else tone(r.status) if column == 3 else None
        if role == Qt.ForegroundRole and column in (0, 4, 6):
            return QColor(GRAY)
        if role == Qt.ToolTipRole:
            return "\n".join(filter(None, [f"{r.events[0].sn} · tid {r.events[0].tid}", r.summary, r.chain, r.note,
                                            f"{len(r.events)} 条消息，双击看逐条"]))

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class ChainModel(QAbstractTableModel):
    """检测链路列表：一行一条按关联键并好的链"""

    headers = ["时间", "文件", "阶段", "结论"]
    COLORS = {"通过": GREEN, "失败": RED, "进行中": BLUE, "待核": ORANGE, "未观测": "#D1D1D6"}

    def __init__(self):
        super().__init__()
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        chain, column = self.rows[index.row()], index.column()
        if role == Qt.DisplayRole:
            return [time.strftime("%H:%M:%S", time.localtime(chain["events"][0].timestamp)), chain["file"] or "—",
                    "", chain["verdict"]][column]
        if role == STAGES_ROLE and column == 2:
            return [self.COLORS[chain["stages"][s][0]] for s in DETECTION_STAGES]
        if role == Qt.ForegroundRole and column == 3 and chain["failed"]:
            return QColor(RED)
        if role == Qt.ToolTipRole and column == 2:
            return "\n".join(f"{s}：{chain['stages'][s][0]}" + (f"（{chain['stages'][s][1]}）" if chain["stages"][s][1] else "")
                             for s in DETECTION_STAGES)

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class StageDelegate(PillDelegate):
    """阶段列画成一排圆点，颜色对应通过/失败/进行中/未观测"""

    def paint(self, painter, option, index):
        colors = index.data(STAGES_ROLE)
        if not colors:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.widget.style().drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        y = option.rect.center().y()
        for i, color in enumerate(colors):
            painter.setBrush(QColor(color))
            painter.drawEllipse(QRectF(option.rect.x() + 10 + i * 18, y - 5, 10, 10))
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(10 + 18 * len(DETECTION_STAGES) + 10, 36)


class Step(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event):
        self.clicked.emit()


class TraceView(QScrollArea):
    """一次请求的证据链：顶部结论卡、时间线、所选消息 JSON；点时间线切换所选消息

    设了 store 后列出其他来源中相同 tid 的记录，点了原地换成那一组，可以回到原来那组；切换后发 switched(请求键)。
    nearby_logs 为真时多一个“这段时间的设备日志”，点了发 nearby(当前这组事件)
    """

    original = Signal(object)
    switched = Signal(object)
    nearby = Signal(object)

    def __init__(self, placeholder):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.placeholder = placeholder
        self.events, self.selected, self.lines, self.note, self.headline = [], None, [], None, None
        # previous(event) 返回上一条同类消息时，所选消息下方列出和它相比变了哪些字段
        self.previous = None
        self.store, self.nearby_logs, self.origin = None, False, None
        self.show_empty()

    def text(self):
        """当前展示的全部文字，供测试和复制使用"""
        return "\n".join(self.lines)

    def _reset(self):
        """新建一棵空控件树，填完后由 _attach 换上"""
        self.lines = []
        self.body = QWidget()
        self.box = QVBoxLayout(self.body)
        self.box.setContentsMargins(2, 2, 6, 2)
        self.box.setSpacing(8)

    def _attach(self, position=0):
        """换上填好的控件树并滚到 position

        整棵树填好再挂上去，滚动范围当场就能算出来；先挂空树再往里加，子控件要等之后的事件才显示，设回的位置会被截成 0
        """
        # 点证据链里的某一步会在它自己的鼠标事件里重建整棵控件；旧控件必须延迟释放，立即删除会访问已释放内存而崩溃
        old = self.takeWidget()
        if old:
            old.deleteLater()
        self.setWidget(self.body)
        self.verticalScrollBar().setValue(position)

    def _add(self, widget):
        if isinstance(widget, QLabel):
            self.lines.append(widget.text())
        self.box.addWidget(widget)
        return widget

    def show_empty(self, text=None):
        self.events, self.selected = [], None
        self._reset()
        self.box.addStretch()
        hint = self._add(label(text or self.placeholder, "caption", True))
        hint.setAlignment(Qt.AlignCenter)
        self.box.addStretch()
        self._attach()

    def show_trace(self, selected, events, note=None, headline=None, origin=None):
        """headline 为 (标题, 颜色, 摘要, 建议) 时用它做结论卡，否则按消息诊断生成；origin 是切到别的来源前所选的消息"""
        # 同一请求里切换所选消息或来了新消息时保留滚动位置；换了请求才回到顶部
        same = self.selected is not None and any(e.event_id == self.selected.event_id for e in events)
        position = self.verticalScrollBar().value()
        # 备注绑定完整请求键，切到别的来源时跟着换
        if note is None and self.store and selected.tid and not headline:
            note = self.store.note((selected.sn, selected.session, selected.tid))
        self.events, self.selected, self.note, self.headline, self.origin = events, selected, note, headline, origin
        self._reset()
        if origin:
            back = QHBoxLayout()
            back.addWidget(button("‹ 回到原来那组", lambda: self._switch(origin, None), "link"))
            back.addStretch()
            self.box.addLayout(back)
        diagnosed = [(e, diagnose(e)) for e in events]
        failed = [(e, d) for e, d in diagnosed if d.status in FAILED]
        waiting = any(e.kind == "指令" for e in events) and not any(e.kind == "ACK" for e in events)
        if headline:
            title, color, summary, suggestion = headline
        elif failed:
            focus, d = next(((e, d) for e, d in failed if e.module != "gw"), failed[0])
            title, color = focus.module.upper() + " 返回失败", RED
            summary, suggestion = d.summary, d.suggestion
        elif waiting:
            title, color = "等待 ACK", ORANGE
            summary, suggestion = "指令已发出，还没收到回复", "一直没有 ACK 就查 GW 转发和目标模块日志"
        elif any(e.kind == "ACK" for e in events):
            # 有回复时结论是整个请求的，和消息页按请求聚合同一套判定；不看当前选中的是哪一条（选中指令本身只会是“待判”）
            r = summarize(events)
            title, color, summary, suggestion = r.status, tone(r.status), "；".join(filter(None, (r.summary, r.note))), ""
        else:
            d = diagnose(selected)
            title, color, summary, suggestion = d.status, tone(d.status), d.summary, d.suggestion
        self._hero(title, color, summary, suggestion, selected)
        if note:
            self._note(note)

        head = QHBoxLayout()
        title = label(f"证据链 · {len(events)} 条", "sectionTitle")
        head.addWidget(title)
        self.lines.append(title.text())
        head.addStretch()
        if self.nearby_logs and any(occurred(e) is not None for e in events):
            head.addWidget(button("这段时间的设备日志", lambda: self.nearby.emit(self.events), "link"))
        self.box.addLayout(head)
        for e, d in diagnosed:
            self._step(e, d)
        if self.store and not headline:
            self._others(selected)
        self._detail(selected)
        self.box.addStretch()
        self._attach(position if same else 0)

    def _note(self, note):
        box = QFrame()
        box.setObjectName("noteCard")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(3)
        header = label("问题备注" + ("　· 已复现" if note.get("reproduced") else ""), "groupHeader")
        layout.addWidget(header)
        self.lines.append(header.text())
        for caption, key in (("预期", "expected"), ("实际", "actual"), ("备注", "remark")):
            if note.get(key):
                line = label(f"{caption}　{note[key]}", wrap=True)
                layout.addWidget(line)
                self.lines.append(line.text())
        self.box.addWidget(box)

    def _hero(self, title, color, summary, suggestion, selected):
        hero = QFrame()
        hero.setObjectName("hero")
        hero.setStyleSheet(f"QFrame#hero{{background:{tint(color, 26)};border-radius:14px}}")
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)
        meta = label(f"{selected.sn or '未知设备'}  ·  {selected.source.split(':')[0]}", "caption")
        heading = label(title)
        heading.setStyleSheet(f"font-size:20px;font-weight:700;color:{color}")
        layout.addWidget(meta)
        layout.addWidget(heading)
        self.lines += [meta.text(), title]
        if summary:
            reason = label(summary, wrap=True)
            reason.setStyleSheet("font-size:14px")
            reason.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(reason)
            self.lines.append(summary)
        if suggestion:
            advice = label("下一步　" + suggestion, wrap=True)
            advice.setStyleSheet("color:palette(window-text)")
            advice.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(advice)
            self.lines.append(advice.text())
        self.box.addWidget(hero)

    def _step(self, e, d):
        step = Step()
        step.setObjectName("stepSelected" if e is self.selected else "step")
        step.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(step)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        marker = dot(tone(d.status), 10)
        row.addWidget(marker, 0, Qt.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(8)
        # 日志文件里的一行（原文没写 Topic）和收到的 MQTT 消息分开说，免得像多了一条空消息
        logged = e.kind == "日志" and e.file
        kind = "日志记录" if logged else f"{e.kind} · {e.direction}"
        name = label(f"<b>{html.escape(e.module.upper())}</b>　{html.escape(kind)}")
        head.addWidget(name)
        if not (logged and d.status == "未判定"):
            head.addWidget(pill(d.status, tone(d.status)))
        head.addStretch()
        # 日志来的消息显示原文里写的时间；实时收到的显示收到时刻
        when = e.source_time.split()[-1].split("T")[-1] if e.source_time else time.strftime("%H:%M:%S", time.localtime(e.timestamp))
        head.addWidget(label(when, "caption"))
        column.addLayout(head)
        topic = label(e.topic or (f"原文未记录 Topic · {Path(e.file).name} 第 {e.line} 行" if logged else "无 Topic"), "caption")
        topic.setStyleSheet("font-family:'Cascadia Mono','Consolas',monospace;font-size:11px")
        column.addWidget(topic)
        self.lines += [f"[{e.module} · {e.kind} · {d.status}] {e.topic}"]
        if d.summary and e.kind != "指令":
            summary = label(d.summary, wrap=True)
            column.addWidget(summary)
            self.lines.append(d.summary)
        row.addLayout(column, 1)
        step.clicked.connect(lambda e=e: self.show_trace(e, self.events, self.note, self.headline, self.origin))
        self.box.addWidget(step)

    def refresh(self):
        """按库里现在的消息重画当前这组，所选消息和“回到原来那组”不变"""
        if self.selected:
            self.show_trace(self.selected, self.store.trace(self.selected), headline=self.headline, origin=self.origin)

    def _switch(self, e, origin):
        """原地换成 e 所在的那组 (设备, 会话, tid)；结论卡落在失败那条，否则落在 e"""
        events = self.store.trace(e)
        focus = next((x for x in events if diagnose(x).status in FAILED), e)
        self.show_trace(focus, events, origin=origin)
        self.switched.emit((e.sn, e.session, e.tid))

    def _others(self, selected):
        groups = self.store.other_sources(selected)
        if not groups:
            return
        self._add(label("其他来源中相同 tid 的记录", "sectionTitle"))
        self._add(label("同一台设备、同一个 tid，不一定是同一次操作；点“查看”单独看那一组，不和上面拼在一起", "caption", True))
        for events in groups:
            row = QHBoxLayout()
            line = label(f"{self._source_name(events)} · {len(events)} 条 · {self._span(events)}", wrap=True)
            self.lines.append(line.text())
            row.addWidget(line, 1)
            row.addWidget(button("查看", lambda _=False, e=events[0]: self._switch(e, self.origin or self.selected), "link"), 0, Qt.AlignTop)
            self.box.addLayout(row)

    @staticmethod
    def _source_name(events):
        session = events[0].session
        if session.startswith("import-live-"):
            return "实时跟随"
        if session.startswith("import-"):
            names = list(dict.fromkeys(Path(e.file).name for e in events if e.file))
            return "设备日志 · " + (names[0] + (" 等" if len(names) > 1 else "") if names else "导入")
        return "沙盒" if events[0].source == "sandbox" else "实时 MQTT"

    @staticmethod
    def _span(events):
        """各条实际发生时间的起止；原文没写日期的照原文写时刻"""
        def when(e):
            stamp = occurred(e)
            return time.strftime("%m-%d %H:%M:%S", time.localtime(stamp)) if stamp is not None else e.source_time or "时间未知"
        first, last = when(events[0]), when(events[-1])
        return first if first == last else f"{first} – {last.split()[-1]}"

    def _detail(self, e):
        text = json.dumps(e.payload, ensure_ascii=False, indent=2) if e.payload else e.raw.strip()
        head = QHBoxLayout()
        head.addWidget(label("所选消息", "sectionTitle"))
        head.addStretch()
        head.addWidget(self._copy_button("复制消息", text))
        if e.file:
            head.addWidget(button("原文附近", lambda: self.original.emit(e), "link"))
        self.box.addLayout(head)
        where = e.topic or (f"日志记录，原文未记录 Topic · {e.file} 第 {e.line} 行" if e.file else "")
        if where:
            row = QHBoxLayout()
            line = label(where, "caption", True)
            self.lines.append(where)
            line.setStyleSheet("font-family:'Cascadia Mono','Consolas',monospace;font-size:11px")
            line.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.addWidget(line, 1)
            if e.topic:
                row.addWidget(self._copy_button("复制", e.topic), 0, Qt.AlignTop)
            self.box.addLayout(row)
        code = QPlainTextEdit(text)
        code.setObjectName("code")
        code.setReadOnly(True)
        code.setFixedHeight(min(26 + 18 * (text.count("\n") + 1), 380))
        self.lines.append(text)
        self.box.addWidget(code)
        before = self.previous(e) if self.previous and e.payload else None
        if before:
            self._diff(before, e)

    @staticmethod
    def _copy_button(text, content):
        """点了把 content 放进剪贴板，按钮短暂显示“已复制”"""
        def copy():
            QApplication.clipboard().setText(content)
            widget.setText("已复制 ✓")
            QTimer.singleShot(1200, lambda: widget.setText(text))

        widget = button(text, copy, "link")
        return widget

    def _diff(self, before, e):
        changes = payload_diff(before.payload, e.payload)
        when = time.strftime("%H:%M:%S", time.localtime(before.timestamp))
        self._add(label(f"和上一条同类消息相比（{when}）" + ("" if changes else "：内容相同"), "groupHeader"))
        shown = lambda v: "（无）" if v is None else json.dumps(v, ensure_ascii=False)[:60]
        for path, old, new in changes[:12]:
            line = label(f"<span style='font-family:Consolas,monospace'>{html.escape(path)}</span>　"
                         f"<span style='color:{GRAY}'>{html.escape(shown(old))}</span> → "
                         f"<b style='color:{ORANGE}'>{html.escape(shown(new))}</b>", wrap=True)
            self._add(line)
        if len(changes) > 12:
            self._add(label(f"另有 {len(changes) - 12} 个字段变化，看上方完整报文", "caption"))


class RemotePanel(QWidget):
    """常用指令遥控面板；send(method, data) 发出一条指令，成功返回 True

    运动和云台按住时立即发一次，之后按 REPEAT_MS 连续发送；变焦按住发持续放大/缩小。
    松手或 halt()（面板隐藏、窗口失焦、断开、关闭窗口）时尝试补发一次零速/回中/停止变焦；
    任一次发送失败只结束本地连续发送，不重试、不排队
    """

    # 协议未规定连续控制频率，5 Hz 是待实机核对的工程初值，没有设备端速度超时依据
    REPEAT_MS = 200
    MOTION_KEYS = {Qt.Key_W: "fwd", Qt.Key_S: "back", Qt.Key_A: "left", Qt.Key_D: "right",
                   Qt.Key_Q: "turn_l", Qt.Key_E: "turn_r"}
    GIMBAL_KEYS = {Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right"}

    def __init__(self, send):
        super().__init__()
        self.send = send
        self.setFocusPolicy(Qt.StrongFocus)
        self.motion, self.gimbal = set(), set()
        # 正在持续变焦时保存按下那一刻的相机目标，停止变焦发给同一台相机
        self.zooming = None
        self.motion_timer, self.gimbal_timer = QTimer(self), QTimer(self)
        self.motion_timer.timeout.connect(self._motion_tick)
        self.gimbal_timer.timeout.connect(self._gimbal_tick)
        self.pads = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(FlowGrid([self._motion_card(), self._gimbal_card(), self._action_card(), self._media_card()]))

    # ---- 构建 ----

    def _slider(self, layout, caption, low, high, value, scale, unit):
        """滑条用整数刻度，scale 把刻度换算成协议单位"""
        row = QHBoxLayout()
        row.addWidget(label(caption, "caption"))
        slider = QSlider(Qt.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        shown = label("", "caption")
        shown.setFixedWidth(78)
        shown.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        update = lambda v: shown.setText(f"{v * scale:g} {unit}")
        slider.valueChanged.connect(update)
        update(value)
        row.addWidget(slider, 1)
        row.addWidget(shown)
        layout.addLayout(row)
        slider.scale = scale
        return slider

    def _pad(self, text, group, key):
        widget = QPushButton(text)
        widget.setObjectName("pad")
        widget.setCursor(Qt.PointingHandCursor)
        widget.setFixedSize(58, 58)
        widget.pressed.connect(lambda: self.press(group, key))
        widget.released.connect(lambda: self.release(group, key))
        self.pads[(group, key)] = widget
        return widget

    def _tile(self, text, method, data=None):
        widget = button(text, lambda: self.send(method, data or {}), "tile")
        widget.setToolTip(method)
        return widget

    def _motion_card(self):
        frame, layout = card(10, 18)
        head = QHBoxLayout()
        head.addWidget(label("运动", "sectionTitle"))
        head.addWidget(pill("VFC", MODULE_COLORS["vfc"]))
        head.addStretch()
        layout.addLayout(head)
        owner = Segmented(["SDK", "手柄", "建图"])
        owner.changed.connect(lambda i: self.send("controllor", {"owner": i + 1}))
        mode = Segmented(["巡航", "普通 SDK"])
        mode.changed.connect(lambda i: self.send("ctrl_mode", {"mode": (1, 3)[i]}))
        for caption, widget in (("控制权", owner), ("模式", mode)):
            row = QHBoxLayout()
            name = label(caption, "caption")
            name.setFixedWidth(42)
            row.addWidget(name)
            row.addWidget(widget)
            row.addStretch()
            layout.addLayout(row)
        pad = QGridLayout()
        pad.setSpacing(10)
        for (r, c), (text, key) in {(0, 0): ("↺", "turn_l"), (0, 1): ("↑", "fwd"), (0, 2): ("↻", "turn_r"),
                                    (1, 0): ("←", "left"), (1, 2): ("→", "right"), (2, 1): ("↓", "back")}.items():
            pad.addWidget(self._pad(text, "motion", key), r, c)
        stop = button("停", self.halt, "padStop")
        stop.setFixedSize(58, 58)
        pad.addWidget(stop, 1, 1)
        holder = QHBoxLayout()
        holder.addStretch()
        holder.addLayout(pad)
        holder.addStretch()
        layout.addLayout(holder)
        self.vx = self._slider(layout, "前后", 1, 20, 6, 0.05, "m/s")
        self.vy = self._slider(layout, "平移", 2, 20, 6, 0.05, "m/s")
        self.yaw = self._slider(layout, "转向", 1, 50, 15, 0.02, "rad/s")
        layout.addWidget(label("按住发送，5 Hz；松手、切走页面或窗口、断线即停。键盘 WASD 移动、QE 转向、空格急停", "caption", True))
        return frame

    def _gimbal_card(self):
        frame, layout = card(10, 18)
        head = QHBoxLayout()
        head.addWidget(label("云台与镜头", "sectionTitle"))
        head.addWidget(pill("PTZ", MODULE_COLORS["ptz"]))
        head.addStretch()
        layout.addLayout(head)
        head = QHBoxLayout()
        head.addWidget(label("相机 index", "caption"))
        self.camera_index = QSpinBox()
        self.camera_index.setRange(0, 9)
        self.camera_index.setValue(1)
        self.camera_index.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.camera_index.setFixedWidth(44)
        head.addWidget(self.camera_index)
        head.addWidget(label("SN", "caption"))
        self.camera_sn = QLineEdit("002")
        self.camera_sn.setFixedWidth(96)
        head.addWidget(self.camera_sn)
        head.addStretch()
        layout.addLayout(head)
        pad = QGridLayout()
        pad.setSpacing(10)
        for (r, c), (text, key) in {(0, 1): ("↑", "up"), (1, 0): ("←", "left"),
                                    (1, 2): ("→", "right"), (2, 1): ("↓", "down")}.items():
            pad.addWidget(self._pad(text, "gimbal", key), r, c)
        center = button("复位", lambda: self.send("gimbal_reset", self.camera()), "padStop")
        center.setFixedSize(58, 58)
        pad.addWidget(center, 1, 1)
        holder = QHBoxLayout()
        holder.addStretch()
        holder.addLayout(pad)
        holder.addStretch()
        layout.addLayout(holder)
        self.gimbal_speed = self._slider(layout, "速度", 1, 10, 4, 50, "")
        self.lens = Segmented(["可见光", "红外", "拼接"])
        self.lens.changed.connect(self._lens)
        row = QHBoxLayout()
        row.addWidget(label("镜头", "caption"))
        row.addWidget(self.lens)
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        self.zoom_buttons = []
        for text, kind in (("放大", 1), ("缩小", 2)):
            zoom = QPushButton(text)
            zoom.setObjectName("tile")
            zoom.setCursor(Qt.PointingHandCursor)
            zoom.pressed.connect(lambda kind=kind: self.zoom(kind))
            zoom.released.connect(self.stop_zoom)
            self.zoom_buttons.append(zoom)
        self.zoom_buttons.append(button("1×", lambda: self.send("camera_focal_length_set", {**self.camera(), "type": 4}), "tile"))
        for zoom in self.zoom_buttons:
            row.addWidget(zoom)
        row.addWidget(button("一键向下", lambda: self.send("gimbal_down", self.camera()), "tile"))
        layout.addLayout(row)
        layout.addWidget(label("方向键按住转动，松手回中；方向键盘 ↑↓←→ 同样可用", "caption", True))
        return frame

    def _action_card(self):
        frame, layout = card(10, 18)
        layout.addWidget(label("姿态动作", "sectionTitle"))
        grid = QGridLayout()
        grid.setSpacing(10)
        actions = [("站立", "stand_up"), ("半蹲", "lie_down"), ("趴下", "passive"), ("跳跃", "jump"),
                   ("前跳", "front_jump"), ("后空翻", "back_flip"), ("打招呼", "shake_hand"),
                   ("双腿站立", "two_leg_stand"), ("退出双腿站立", "cancel_two_leg_stand")]
        for index, (text, method) in enumerate(actions):
            grid.addWidget(self._tile(text, method), index // 3, index % 3)
        layout.addLayout(grid)
        layout.addStretch()
        return frame

    def _media_card(self):
        frame, layout = card(10, 18)
        head = QHBoxLayout()
        head.addWidget(label("音视频与 AI", "sectionTitle"))
        head.addWidget(pill("MEDIA", MODULE_COLORS["media"]))
        head.addWidget(pill("VISION", MODULE_COLORS["vision"]))
        head.addStretch()
        layout.addLayout(head)
        row = QHBoxLayout()
        row.addWidget(button("拍照", lambda: self.send("take_photo", self.camera()), "tile"))
        row.addWidget(button("开始录像", lambda: self.send("camera_recording", {**self.camera(), "mode": 1}), "tile"))
        row.addWidget(button("停止录像", lambda: self.send("camera_recording", {**self.camera(), "mode": 0}), "tile"))
        layout.addLayout(row)
        live = QHBoxLayout()
        live.addWidget(label("直播", "caption"))
        self.live_mode = Segmented(["音频", "视频", "音视频"])
        self.live_mode.group.button(1).setChecked(True)
        live.addWidget(self.live_mode)
        live.addStretch()
        layout.addLayout(live)
        live = QHBoxLayout()
        live.addStretch()
        live.addWidget(button("开启直播", lambda: self._live(1), "tile"))
        live.addWidget(button("关闭直播", lambda: self._live(2), "tile"))
        layout.addLayout(live)
        ai = QHBoxLayout()
        ai.addWidget(label("AI 识别", "caption"))
        ai.addStretch()
        ai.addWidget(self._tile("开启", "start_ai", {"filters": [1]}))
        ai.addWidget(self._tile("关闭", "stop_ai"))
        layout.addLayout(ai)
        self.volume = self._slider(layout, "音量", 0, 100, 70, 1, "")
        volume = QHBoxLayout()
        volume.addStretch()
        volume.addWidget(button("设置音量", lambda: self.send("speaker_volume_set", {"volume": self.volume.value()}), "tile"))
        layout.addLayout(volume)
        layout.addStretch()
        return frame

    # ---- 指令 ----

    def camera(self):
        return {"index": self.camera_index.value(), "sn": self.camera_sn.text().strip()}

    def _lens(self, index):
        """发 camera_switch 切镜头，type 1 可见光（变焦）、2 红外、3 拼接；红外不能变焦，发出后禁用变焦按钮"""
        if self.send("camera_switch", {**self.camera(), "type": index + 1}):
            for zoom in self.zoom_buttons:
                zoom.setEnabled(index != 1)

    def _live(self, state):
        self.send("camera_livestream", {**self.camera(), "state": state, "stream_type": "webrtc",
                                        "mode": self.live_mode.index() + 1})

    def press(self, group, key):
        keys, timer, tick = ((self.motion, self.motion_timer, self._motion_tick) if group == "motion"
                             else (self.gimbal, self.gimbal_timer, self._gimbal_tick))
        keys.add(key)
        self.pads[(group, key)].setDown(True)
        tick()
        if keys and not timer.isActive():
            timer.start(self.REPEAT_MS)

    def release(self, group, key):
        keys = self.motion if group == "motion" else self.gimbal
        keys.discard(key)
        self.pads[(group, key)].setDown(False)
        if keys:
            return
        if group == "motion" and self.motion_timer.isActive():
            self.motion_timer.stop()
            self.send("move", {"vx": 0, "vy": 0, "yaw_rate": 0})
        elif group == "gimbal" and self.gimbal_timer.isActive():
            self.gimbal_timer.stop()
            self.send("gimbal_control", {**self.camera(), "vx": 1500, "vy": 1500})

    def zoom(self, kind):
        """kind 为 1 持续放大、2 持续缩小；发送失败不进入变焦状态"""
        target = self.camera()
        if self.send("camera_focal_length_set", {**target, "type": kind}):
            self.zooming = target

    def stop_zoom(self):
        target, self.zooming = self.zooming, None
        if target:
            self.send("camera_focal_length_set", {**target, "type": 3})

    def halt(self):
        """松开全部方向键、停止变焦，并各补发一次停止"""
        self.stop_zoom()
        for group, key in list(self.pads):
            if key in (self.motion if group == "motion" else self.gimbal):
                self.release(group, key)

    def _abort(self, group):
        """发送失败：停掉连续发送，不再补发停止"""
        keys, timer = (self.motion, self.motion_timer) if group == "motion" else (self.gimbal, self.gimbal_timer)
        timer.stop()
        for key in keys:
            self.pads[(group, key)].setDown(False)
        keys.clear()

    def _motion_tick(self):
        k = self.motion
        data = {"vx": round((("fwd" in k) - ("back" in k)) * self.vx.value() * self.vx.scale, 2),
                "vy": round((("left" in k) - ("right" in k)) * self.vy.value() * self.vy.scale, 2),
                "yaw_rate": round((("turn_l" in k) - ("turn_r" in k)) * self.yaw.value() * self.yaw.scale, 2)}
        if not self.send("move", data):
            self._abort("motion")

    def _gimbal_tick(self):
        k, offset = self.gimbal, self.gimbal_speed.value() * self.gimbal_speed.scale
        data = {**self.camera(), "vx": 1500 + (("up" in k) - ("down" in k)) * offset,
                "vy": 1500 + (("right" in k) - ("left" in k)) * offset}
        if not self.send("gimbal_control", data):
            self._abort("gimbal")

    # ---- 键盘与焦点 ----

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        if event.key() == Qt.Key_Space:
            self.halt()
        elif event.key() in self.MOTION_KEYS:
            self.press("motion", self.MOTION_KEYS[event.key()])
        elif event.key() in self.GIMBAL_KEYS:
            self.press("gimbal", self.GIMBAL_KEYS[event.key()])
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        if event.key() in self.MOTION_KEYS:
            self.release("motion", self.MOTION_KEYS[event.key()])
        elif event.key() in self.GIMBAL_KEYS:
            self.release("gimbal", self.GIMBAL_KEYS[event.key()])
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        # 焦点进到面板内的输入框时键盘松开事件收不到，统一按松手处理
        self.halt()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self.halt()
        super().hideEvent(event)


def spin(low, high, value, step=1.0, decimals=2, width=80):
    widget = QDoubleSpinBox()
    widget.setRange(low, high)
    widget.setDecimals(decimals)
    widget.setSingleStep(step)
    widget.setValue(value)
    widget.setButtonSymbols(QAbstractSpinBox.NoButtons)
    # width 为 0 时随容器伸缩，用在表格单元格里
    if width:
        widget.setFixedWidth(width)
    return guard_wheel(widget)


def pose(x, y, yaw_deg):
    """平面坐标加朝向角转成协议的 position/orientation 四元数"""
    half = math.radians(yaw_deg) / 2
    return {"position": {"x": x, "y": y, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": round(math.sin(half), 6), "w": round(math.cos(half), 6)}}


class MapView(QGraphicsView):
    """栅格地图预览：显示 PGM、航点和机身位置，左键点选发出 picked(x, y) 世界坐标

    坐标换算按 ROS map_server 约定：origin = (x, y, yaw) 是左下角像素在世界系中的位姿；
    像素 (u, v) 左上为原点，先得到地图系 (u·res, (height - v)·res)，再按 yaw 旋转并平移
    """

    picked = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setMinimumHeight(320)
        self.setStyleSheet("QGraphicsView{background:#F5F5F8;border:0;border-radius:10px}")
        self.image, self.resolution, self.origin = None, 0.05, (0.0, 0.0, 0.0)
        self.markers, self.robot = [], None
        self.press, self.zoomed = None, False

    def set_map(self, image, resolution, origin):
        self.scene().clear()
        self.markers, self.robot = [], None
        self.image, self.resolution, self.origin = image, resolution, origin
        self.scene().addPixmap(QPixmap.fromImage(image))
        self.setSceneRect(QRectF(0, 0, image.width(), image.height()))
        self.zoomed = False
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def to_pixel(self, x, y):
        ox, oy, yaw = self.origin
        c, s = math.cos(yaw), math.sin(yaw)
        dx, dy = x - ox, y - oy
        mx, my = c * dx + s * dy, -s * dx + c * dy
        return mx / self.resolution, self.image.height() - my / self.resolution

    def to_world(self, u, v):
        ox, oy, yaw = self.origin
        c, s = math.cos(yaw), math.sin(yaw)
        mx, my = u * self.resolution, (self.image.height() - v) * self.resolution
        return round(ox + c * mx - s * my, 3), round(oy + s * mx + c * my, 3)

    def set_points(self, points):
        for item in self.markers:
            self.scene().removeItem(item)
        self.markers = []
        if not self.image:
            return
        pixels = [self.to_pixel(x, y) for x, y in points]
        line = QPen(QColor(BLUE), 2)
        line.setCosmetic(True)
        for (u0, v0), (u1, v1) in zip(pixels, pixels[1:]):
            self.markers.append(self.scene().addLine(u0, v0, u1, v1, line))
        for index, (u, v) in enumerate(pixels, 1):
            marker = self._fixed(self.scene().addEllipse(-10, -10, 20, 20, QPen(QColor("white"), 2), QColor(BLUE)), u, v)
            text = QGraphicsSimpleTextItem(str(index), marker)
            text.setBrush(QColor("white"))
            font = text.font()
            font.setPixelSize(11)
            font.setBold(True)
            text.setFont(font)
            box = text.boundingRect()
            text.setPos(-box.width() / 2, -box.height() / 2)
            self.markers.append(marker)

    def set_robot(self, x, y, heading):
        if not self.image:
            return
        if self.robot is None:
            shape = QPolygonF([QPointF(14, 0), QPointF(-10, 8), QPointF(-5, 0), QPointF(-10, -8)])
            self.robot = self._fixed(self.scene().addPolygon(shape, QPen(QColor("white"), 1.5), QColor("#1C1C1E")), 0, 0)
            self.robot.setZValue(10)
        u, v = self.to_pixel(x, y)
        self.robot.setPos(u, v)
        # 先换到地图系再取反：图像 y 轴向下
        self.robot.setRotation(-math.degrees(heading - self.origin[2]))

    @staticmethod
    def _fixed(item, u, v):
        """标记不随地图缩放，始终保持屏幕上的固定大小"""
        item.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        item.setPos(u, v)
        item.setZValue(5)
        return item

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.image and not self.zoomed:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.zoomed = True
        self.scale(factor, factor)

    def mousePressEvent(self, event):
        self.press = event.position()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        # 拖动是平移，只有原地点击才算选点
        if self.image and event.button() == Qt.LeftButton and self.press is not None \
                and (event.position() - self.press).manhattanLength() < 4:
            point = self.mapToScene(event.position().toPoint())
            if self.sceneRect().contains(point):
                self.picked.emit(*self.to_world(point.x(), point.y()))
        self.press = None


STALE_LIDAR = "3 s 内没收到雷达帧，查 NX 雷达和 zenoh 连接"


class MissionPanel(QWidget):
    """建图定位、导航、文件同步、红外测温和喊话表单

    send(method, data) 发出一条指令；edit(method, data) 把报文带到控制页继续手改；
    camera() 取遥控面板的相机 index/SN；observe(event) 用设备 ACK 回填地图、文件和温度
    """

    # 航点动作：显示名、协议 action、参数占位提示。文档 param/param1 混用，按注释用 param
    ACTIONS = [("无", "", ""), ("拍照", "take_photo", ""), ("开始录像", "start_record", ""),
               ("停止录像", "stop_record", ""), ("停留", "pause", "秒，如 10"),
               ("云台动作", "gimbal_action", "如 1,1"), ("开启 AI", "start_ai", "如 1,2,3"), ("关闭 AI", "stop_ai", "")]

    def __init__(self, send, edit, camera, robot_pose=None):
        super().__init__()
        self.send, self.edit, self.camera, self.robot_pose = send, edit, camera, robot_pose
        # 地图 ID → 栅格参数，来自 get_all_compressed_pgm_map 应答
        self.map_info = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # 导航卡一行有坐标、朝向和按钮，每列不足 560 时改单列，避免右侧被裁
        layout.addWidget(FlowGrid([self._map_card(), self._cloud_card(), self._nav_card(), self._grid_card(), self._file_card(),
                                   self._ir_card(), self._speaker_card()], min_width=560))
        self.add_point()

    @staticmethod
    def _head(title, *modules):
        head = QHBoxLayout()
        head.addWidget(label(title, "sectionTitle"))
        for module in modules:
            head.addWidget(pill(module.upper(), MODULE_COLORS[module]))
        head.addStretch()
        return head

    def _do(self, text, method, data=lambda: {}, name="tile"):
        """data 为无参函数，点击时才取表单当前值"""
        widget = button(text, lambda: self.send(method, data()), name)
        widget.setToolTip(method)
        return widget

    # ---- 地图与定位 ----

    def _map_card(self):
        frame, layout = card(10, 18)
        layout.addLayout(self._head("建图与定位", "vfc"))
        row = QHBoxLayout()
        for text, method in (("开始建图", "start_mapping"), ("停止建图", "stop_mapping"), ("重置定位", "loc_reset")):
            row.addWidget(self._do(text, method))
        layout.addLayout(row)
        maps = QHBoxLayout()
        self.map_box = QComboBox()
        self.map_box.setEditable(True)
        self.map_box.lineEdit().setPlaceholderText("地图 ID，可手填或点获取")
        maps.addWidget(self.map_box, 1)
        maps.addWidget(self._do("获取地图", "get_map_list", name=""))
        layout.addLayout(maps)
        row = QHBoxLayout()
        row.addWidget(self._do("加载定位", "loc_load_map", lambda: {"map_id": self.map_id()}))
        row.addWidget(self._do("删除地图", "remove_map_by_id", lambda: {"map_ids": [self.map_id()]}))
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("新名称")
        row.addWidget(self.new_name, 1)
        row.addWidget(self._do("重命名", "rename_map_name",
                               lambda: {"map_id": self.map_id(), "new_name": self.new_name.text().strip()}))
        layout.addLayout(row)
        layout.addStretch()
        return frame

    def map_id(self):
        return self.map_box.currentText().strip()

    # ---- 雷达点云 ----

    def _cloud_card(self):
        frame, layout = card(10, 18)
        layout.addLayout(self._head("雷达点云", "ros2bridge"))
        row = QHBoxLayout()
        row.addWidget(self._do("开始采集", "pcd_switch", lambda: {"enable": True}, "primary"))
        row.addWidget(self._do("停止采集", "pcd_switch", lambda: {"enable": False}))
        layout.addLayout(row)
        self.cloud_state = label("还没收到 ros2bridge 心跳", wrap=True)
        layout.addWidget(self.cloud_state)
        self.cloud_ack = label("", "caption", True)
        layout.addWidget(self.cloud_ack)
        layout.addWidget(label("pcd_switch 是本地指令，哪种连接方式都发到 local/robotdog/ros2bridge/command。"
                               "平时不用手动开：GW 在建图中（mapping_status=5）自己打开、建图结束关掉；不建图时手动打开，"
                               "GW 最快 5 秒后会按心跳关回去，只想看点云可以先停 GW 或在 ros2bridge.cfg 设 start_enabled。"
                               "点云显示在遥控页和控制调试页右侧的 3D 区", "caption", True))
        layout.addStretch()
        return frame

    def _cloud_heartbeat(self, data):
        on = data.get("enabled")
        if on is None:
            self.cloud_state.setText("心跳里没有 enabled（3.1.0 之前的 ros2bridge 不认 pcd_switch）")
            return
        if not on:
            self.cloud_state.setText(f"<b style='color:{GRAY}'>未采集</b>　来源 {html.escape(str(data.get('source', '')))}")
            return
        ok = data.get("ros_ok")
        rate = data.get("out_bytes_per_sec")
        self.cloud_state.setText(
            f"<b style='color:{GREEN}'>采集中</b>　雷达 {data.get('in_hz', 0):.1f} Hz → 输出 {data.get('out_hz', 0):.1f} Hz"
            + (f"　{rate / 1024:.0f} KB/s" if isinstance(rate, (int, float)) else "")
            + f"　每帧 {data.get('out_count', '—')} 点"
            + ("" if ok else f"　<b style='color:{ORANGE}'>{STALE_LIDAR}</b>"))

    # ---- 导航 ----

    def _nav_card(self):
        frame, layout = card(10, 18)
        layout.addLayout(self._head("导航", "vfc"))
        single = QHBoxLayout()
        self.goal = [spin(-1000, 1000, 0), spin(-1000, 1000, 0), spin(-180, 180, 0, 15, 1)]
        for caption, widget in zip(("x", "y", "朝向°"), self.goal):
            single.addWidget(label(caption, "caption"))
            single.addWidget(widget)
        single.addStretch()
        single.addWidget(self._do("单点前往", "start_nav", lambda: pose(*(w.value() for w in self.goal)), "primary"))
        layout.addLayout(single)

        task = QHBoxLayout()
        task.addWidget(label("多点任务", "caption"))
        self.task_name = QLineEdit("task_" + time.strftime("%Y%m%d"))
        task.addWidget(self.task_name, 1)
        task.addWidget(button("添加航点", self.add_point))
        task.addWidget(button("删除所选", self._remove_point))
        layout.addLayout(task)
        self.points = QTableWidget(0, 5)
        self.points.setHorizontalHeaderLabels(["x", "y", "朝向°", "动作", "参数"])
        self.points.verticalHeader().setDefaultSectionSize(40)
        self.points.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.points.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.points.setShowGrid(False)
        self.points.setMinimumHeight(170)
        layout.addWidget(self.points)
        run = QHBoxLayout()
        run.addWidget(button("在控制页编辑", lambda: self.edit("start_multi_nav_by_points", self.multi_nav())))
        run.addStretch()
        run.addWidget(self._do("开始多点导航", "start_multi_nav_by_points", self.multi_nav, "primary"))
        layout.addLayout(run)
        control = QHBoxLayout()
        for text, method in (("暂停", "pause_nav"), ("继续", "continue_nav"), ("停止", "stop_nav"), ("返航", "start_nav_return_home")):
            control.addWidget(self._do(text, method))
        layout.addLayout(control)
        return frame

    def add_point(self, x=0.0, y=0.0, yaw=0.0, action=0, param=""):
        row = self.points.rowCount()
        self.points.insertRow(row)
        for column, value in enumerate((x, y, yaw)):
            cell = spin(-1000, 1000, value, 1, 2, 0)
            cell.valueChanged.connect(self._sync_map)
            self.points.setCellWidget(row, column, cell)
        kind = QComboBox()
        kind.addItems([name for name, _, _ in self.ACTIONS])
        kind.setCurrentIndex(action)
        extra = QLineEdit(param)
        kind.currentIndexChanged.connect(lambda i: extra.setPlaceholderText(self.ACTIONS[i][2]))
        self.points.setCellWidget(row, 3, kind)
        self.points.setCellWidget(row, 4, extra)
        self._sync_map()

    def _remove_point(self):
        for row in sorted({i.row() for i in self.points.selectedIndexes()}, reverse=True):
            self.points.removeRow(row)
        self._sync_map()

    def multi_nav(self):
        points = []
        for row in range(self.points.rowCount()):
            x, y, yaw = (self.points.cellWidget(row, c).value() for c in range(3))
            point = pose(x, y, yaw)
            _, action, _ = self.ACTIONS[self.points.cellWidget(row, 3).currentIndex()]
            text = self.points.cellWidget(row, 4).text().replace("，", ",")
            params = [float(v) if "." in v else int(v) for v in text.split(",") if v.strip()]
            point["actions"] = [{"id": "", "action": action, "param": params or None}] if action else []
            points.append(point)
        return {"map_id": self.map_id(), "task_name": self.task_name.text().strip(), "points": points}

    # ---- 地图 ----

    def _grid_card(self):
        frame, layout = card(10, 18)
        head = self._head("地图与航点", "vfc")
        head.addWidget(button("打开 PGM 地图…", self.open_map))
        layout.addLayout(head)
        row = QHBoxLayout()
        self.resolution = spin(0.001, 10, 0.05, 0.01, 3, 70)
        self.origin = [spin(-10000, 10000, 0, 1, 3, 80), spin(-10000, 10000, 0, 1, 3, 80), spin(-7, 7, 0, 0.1, 6, 90)]
        for caption, widget in (("分辨率 m/px", self.resolution), ("原点 x", self.origin[0]), ("y", self.origin[1]),
                                ("yaw rad", self.origin[2])):
            row.addWidget(label(caption, "caption"))
            row.addWidget(widget)
            widget.editingFinished.connect(self._reproject)
        row.addStretch()
        layout.addLayout(row)
        self.map_view = MapView()
        self.map_view.picked.connect(lambda x, y: self.add_point(x, y))
        layout.addWidget(self.map_view, 1)
        self.map_hint = label("打开地图目录里的 .pgm，同名 .yaml 自动读分辨率和原点；没有 yaml 时用“获取地图”应答里的信息。"
                              "左键点地图添加航点，拖动平移，滚轮缩放", "caption", True)
        layout.addWidget(self.map_hint)
        return frame

    def open_map(self, path=None):
        """读取 PGM；分辨率和原点优先取同名 yaml，其次取地图列表应答，再次保留当前输入"""
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "打开栅格地图", "", "地图 (*.pgm *.png);;所有文件 (*)")
            if not path:
                return
        image = QImage(path)
        if image.isNull():
            self.map_hint.setText("图片读不出来：" + path)
            return
        source = "当前输入"
        meta = Path(path).with_suffix(".yaml")
        info = self.map_info.get(Path(path).stem) or self.map_info.get(self.map_id())
        if meta.exists():
            import yaml
            data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
            if "resolution" in data:
                self.resolution.setValue(float(data["resolution"]))
            if isinstance(data.get("origin"), list) and len(data["origin"]) >= 2:
                values = [float(v) for v in data["origin"][:3]] + [0.0]
                for widget, value in zip(self.origin, values):
                    widget.setValue(value)
            source = meta.name
        elif info:
            # 应答里的 origin.orientation 编码未确认，yaw 保持当前输入
            self.resolution.setValue(info["resolution"])
            self.origin[0].setValue(info["x"])
            self.origin[1].setValue(info["y"])
            source = "地图列表应答（yaw 未读）"
        self.map_view.set_map(image, self.resolution.value(), tuple(w.value() for w in self.origin))
        self.map_hint.setText(f"{Path(path).name}　{image.width()}×{image.height()} px　参数来自{source}；左键添加航点")
        self._sync_map()

    def _reproject(self):
        if self.map_view.image:
            self.map_view.set_map(self.map_view.image, self.resolution.value(), tuple(w.value() for w in self.origin))
            self._sync_map()

    def _sync_map(self):
        points = [(self.points.cellWidget(r, 0).value(), self.points.cellWidget(r, 1).value()) for r in range(self.points.rowCount())]
        self.map_view.set_points(points)
        self.update_robot()

    def update_robot(self):
        if self.robot_pose:
            self.map_view.set_robot(*self.robot_pose())

    # ---- 文件同步 ----

    def _file_card(self):
        frame, layout = card(10, 18)
        layout.addLayout(self._head("文件同步", "fsvr"))
        self.upload_path = QLineEdit()
        self.upload_path.setPlaceholderText("上传地址 upload_path")
        layout.addWidget(self.upload_path)
        row = QHBoxLayout()
        row.addWidget(label("媒体文件", "caption"))
        row.addStretch()
        row.addWidget(self._do("获取列表", "get_media_filelist", name=""))
        layout.addLayout(row)
        self.files = QListWidget()
        self.files.setObjectName("commands")
        self.files.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.files.setMinimumHeight(150)
        layout.addWidget(self.files)
        row = QHBoxLayout()
        row.addWidget(self._do("同步所选媒体", "sync_media_file", lambda: {
            "file_path": [i.data(Qt.UserRole) for i in self.files.selectedItems()], "upload_path": self.upload_path.text().strip()}))
        row.addWidget(self._do("同步当前地图点云", "sync_map_file", lambda: {
            "map_id": [self.map_id()], "upload_path": self.upload_path.text().strip()}))
        layout.addLayout(row)
        self.event_report = Switch("检测结果上报")
        self.event_report.clicked.connect(lambda on: self.send("event_switch", {
            "mode": int(on), "upload_path": self.upload_path.text().strip()}))
        layout.addWidget(self.event_report)
        return frame

    # ---- 红外测温 ----

    def _ir_card(self):
        frame, layout = card(10, 18)
        layout.addLayout(self._head("红外测温", "ptz"))
        mode = QHBoxLayout()
        mode.addWidget(label("video_type", "caption"))
        self.video_type = spin(0, 9, 0, 1, 0, 50)
        mode.addWidget(self.video_type)
        mode.addStretch()
        mode.addWidget(self._do("设置测温模式", "ir_metering_mode_set",
                                lambda: {**self.camera(), "video_type": int(self.video_type.value())}))
        layout.addLayout(mode)
        point = QHBoxLayout()
        self.ir_point = [spin(0, 1, 0.5, 0.05), spin(0, 1, 0.5, 0.05)]
        for caption, widget in zip(("x", "y"), self.ir_point):
            point.addWidget(label(caption, "caption"))
            point.addWidget(widget)
        point.addStretch()
        point.addWidget(self._do("点测温", "ir_metering_point_set", lambda: {
            **self.camera(), "x": self.ir_point[0].value(), "y": self.ir_point[1].value()}))
        layout.addLayout(point)
        area = QHBoxLayout()
        self.ir_area = [spin(0, 1, 0.1, 0.05), spin(0, 1, 0.1, 0.05), spin(0, 1, 0.8, 0.05), spin(0, 1, 0.8, 0.05)]
        for caption, widget in zip(("x", "y", "宽", "高"), self.ir_area):
            area.addWidget(label(caption, "caption"))
            area.addWidget(widget)
        area.addStretch()
        area.addWidget(self._do("区域测温", "ir_metering_area_set", lambda: {
            **self.camera(), **dict(zip(("x", "y", "width", "height"), (w.value() for w in self.ir_area)))}))
        layout.addLayout(area)
        self.temperature = label("测温结果会显示在这里", "caption", True)
        layout.addWidget(self.temperature)
        layout.addStretch()
        return frame

    # ---- 喊话 ----

    def _speaker_card(self):
        frame, layout = card(10, 18)
        layout.addLayout(self._head("喊话播放", "media"))
        self.mp3_url = QLineEdit()
        self.mp3_url.setPlaceholderText("MP3 地址 url")
        layout.addWidget(self.mp3_url)
        row = QHBoxLayout()
        self.mp3_md5 = QLineEdit()
        self.mp3_md5.setPlaceholderText("md5（32 位小写）")
        self.mp3_size = spin(0, 1e9, 0, 1024, 0, 90)
        self.mp3_duration = spin(0, 1e7, 0, 100, 0, 80)
        row.addWidget(self.mp3_md5, 1)
        row.addWidget(label("字节", "caption"))
        row.addWidget(self.mp3_size)
        row.addWidget(label("毫秒", "caption"))
        row.addWidget(self.mp3_duration)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.preempt = Switch("打断正在播放")
        self.preempt.setChecked(True)
        row.addWidget(self.preempt)
        row.addStretch()
        row.addWidget(self._do("播放", "speaker_play_start", self.play_request, "primary"))
        layout.addLayout(row)
        layout.addStretch()
        return frame

    def play_request(self):
        return {"play_id": "rec-" + uuid.uuid4().hex[:8], "url": self.mp3_url.text().strip(),
                "md5": self.mp3_md5.text().strip(), "format": "mp3", "size": int(self.mp3_size.value()),
                "duration_ms": int(self.mp3_duration.value()), "preempt": int(self.preempt.isChecked())}

    # ---- ACK 回填 ----

    def observe(self, event):
        """设备 ACK 带回的列表和测温结果回填表单；同一回复经 GW 转发再来一次也只是重复覆盖

        ros2bridge 的心跳和 pcd_switch 回执更新雷达点云卡片
        """
        if event.module == "ros2bridge":
            data = event.payload.get("data")
            if event.method == "heartbeat" and isinstance(data, dict):
                self._cloud_heartbeat(data)
            elif event.method == "pcd_switch" and event.kind == "ACK":
                d = diagnose(event)
                self.cloud_ack.setText(f"最近一次 pcd_switch：{d.status}" + (f"（{d.summary}）" if d.status in FAILED else "")
                                       + (f"，实际 enable={json.dumps(data.get('enable'))}" if isinstance(data, dict) else ""))
        if event.kind != "ACK":
            return
        body = event.payload.get("data")
        inner = body.get("data") if isinstance(body, dict) else None
        method = event.method
        if method == "get_map_list" and isinstance(inner, list):
            self._fill_maps([m.get("map_id", "") for m in inner if isinstance(m, dict)])
        elif method == "get_all_compressed_pgm_map" and isinstance(inner, dict):
            self._fill_maps(list(inner))
            for map_id, value in inner.items():
                meta = value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else {}
                info = meta.get("info", {})
                position = info.get("origin", {}).get("position", {})
                if "resolution" in info:
                    self.map_info[map_id] = {"resolution": float(info["resolution"]), "x": float(position.get("x", 0)),
                                             "y": float(position.get("y", 0))}
        elif method == "get_media_filelist" and isinstance(inner, list):
            self.files.clear()
            for path, size in self._walk(inner, ""):
                item = QListWidgetItem(f"{path}　　{size / 1024:.0f} KB")
                item.setData(Qt.UserRole, path)
                self.files.addItem(item)
        elif method.startswith("ir_metering_") and isinstance(inner, dict):
            # 文档里区域测温的应答 method 写成了 point_set，按字段区分
            if "aver_temperature" in inner:
                high, low = inner.get("max_temperature_point", {}), inner.get("min_temperature_point", {})
                self.temperature.setText(f"区域平均 {inner['aver_temperature']} ℃　最高 {high.get('temperature')} ℃"
                                         f"　最低 {low.get('temperature')} ℃")
            elif "temperature" in inner:
                self.temperature.setText(f"点 ({inner.get('x')}, {inner.get('y')})　{inner['temperature']} ℃")

    def _fill_maps(self, ids):
        current = self.map_id()
        self.map_box.clear()
        self.map_box.addItems([i for i in ids if i])
        if current:
            self.map_box.setCurrentText(current)

    def _walk(self, nodes, prefix):
        """把文件树展开成 (目录/文件名, 字节数)，只保留文件节点"""
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            path = prefix + str(node.get("file_name", ""))
            if node.get("node_type") == 1:
                yield from self._walk(node.get("children"), path + "/")
            else:
                yield path, node.get("file_size") or 0


class Toast(QLabel):
    """内容区底部居中的短暂提示，淡入后停 2.6 s 再淡出；放底部是为了不挡页头的指令结果"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("toast")
        self.hide()
        self.effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.effect)
        self.fade = QPropertyAnimation(self.effect, b"opacity", self)
        self.fade.setEasingCurve(QEasingCurve.OutCubic)
        self.fade.finished.connect(lambda: self.effect.opacity() == 0 and self.hide())
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self._fade_to(0.0, 220))

    def _fade_to(self, value, ms):
        self.fade.stop()
        self.fade.setDuration(ms)
        self.fade.setStartValue(self.effect.opacity())
        self.fade.setEndValue(value)
        self.fade.start()

    def pop(self, text):
        self.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        # 126 为侧栏宽度的一半，让提示落在内容区中间
        self.move((parent.width() - self.width()) // 2 + 126, parent.height() - self.height() - 28)
        self.raise_()
        if not self.isVisible():
            self.effect.setOpacity(0.0)
        self.show()
        self._fade_to(1.0, 160)
        self.timer.start(2600)


class Studio(QMainWindow):
    PAGES = ["遥控", "控制调试", "导航与文件", "消息追踪", "状态总览", "日志排障", "回归测试", "协议覆盖"]
    REMOTE, CONTROL, MISSION, MESSAGES, STATUS, LOGS, SUITE, COVERAGE = range(8)
    # 这些页旁边显示 3D 观察区；其余是分析页，整宽显示
    OBSERVED = (REMOTE, CONTROL)

    def __init__(self, db_path=None):
        super().__init__()
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
        self.import_thread = None
        self.catalog = entries()
        self.selected = None
        # 本次运行里各指令未发出的编辑内容，切回来接着改；切换连接模式时报文格式变了，一并清掉
        self.drafts = {}
        self.recent = []
        self.form_syncing = False
        self.current_event = None
        self.current_request = None
        self.current_chain = None
        self.scope = None
        self.watch, self.watch_size = None, 0
        self.quick = None
        self.quick_views = []
        self.scene = Scene()
        self.joint_sliders = {}
        self.importing = None
        self.step_timer = QTimer(self)
        self.step_timer.timeout.connect(self._poll_step)
        self.refresh_due, self.refreshed = False, 0.0
        self.search, self.export_running = None, False
        self.status_seen, self.status_aged = {}, 0.0
        self.status_latest, self.stream_rows = {}, []
        self.status_drawn, self.requests_drawn = None, None
        # 每个模块收到实时消息的次数，状态页详情据此判断要不要重画
        self.module_touched = {}
        self.preview, self.export_dialog = None, None
        # 逐条视图翻页：栈里存每页之前那页的最小行号，空栈表示最新一页
        self.pages, self.page_rows = [], []
        self.shown_live, self.demo_note = False, ""
        # None 未加载，False 加载中，否则为 RobotView
        self.viewer = None
        # 最近一次点云解析错误（同样的错误不重复提示）和点云标签上次按时效刷新的时刻
        self.cloud_error, self.cloud_aged = "", 0.0
        self.dropped_shown = 0
        self.rate_count, self.rate_at = 0, time.monotonic()
        self.spark_at, self.compass = 0.0, None
        self.replay = None
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
        self._commands()
        self._mode_changed()
        self._room_follow()
        self.sn.textChanged.connect(self._room_follow)
        self.remote.camera_sn.textChanged.connect(self._room_follow)
        self._refresh()

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
        observer = self._observer()
        self.stack = QStackedWidget()
        for page in (self._remote_page(), self._control_page(), self._mission_page(),
                     self._message_page(), self._status_page(),
                     self._logs_page(), self._suite_page(), self._coverage_page()):
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
        QShortcut(QKeySequence("Ctrl+K"), self, self._quick_pick)
        self.nav.setCurrentRow(0)

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
        for title in self.PAGES:
            self.nav.addItem(QListWidgetItem(title))
        # 行高 40 加上下各 1 px 间距；固定高度，窗口矮时由侧栏整体滚动
        self.nav.setFixedHeight(len(self.PAGES) * 42 + 4)
        self.nav.currentRowChanged.connect(self._page)
        layout.addWidget(self.nav)
        layout.addStretch()

        layout.addWidget(label("当前设备", "groupHeader"))
        device = Step()
        device.setObjectName("sideCard")
        device.setCursor(Qt.PointingHandCursor)
        device.clicked.connect(lambda: self._go(self.REMOTE))
        dl = QHBoxLayout(device)
        dl.setContentsMargins(12, 10, 12, 10)
        badge = QLabel()
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
        self.profile_box.activated.connect(self._profile_chosen)
        saved.addWidget(self.profile_box, 1)
        more = QPushButton("⋯")
        more.setObjectName("tile")
        more.setFixedWidth(40)
        # 去掉菜单箭头，窄按钮里只放“⋯”
        more.setStyleSheet("QPushButton{font-size:18px;padding:0 0 4px 0}QPushButton::menu-indicator{image:none;width:0px}")
        more.setToolTip("保存或删除连接配置")
        more.setCursor(Qt.PointingHandCursor)
        menu = QMenu(more)
        menu.addAction("把当前填写存为配置…", self._save_profile)
        menu.addAction("删除选中的配置", self._delete_profile)
        more.setMenu(menu)
        saved.addWidget(more)
        form.addLayout(saved)
        self.mode_box = QComboBox()
        self.mode_box.addItems(MODES)
        # 当前生效的模式；切换时要先按旧模式补发停止再更新，所以不直接读下拉框
        self.mode = MODES[self.mode_box.currentText()]
        self.mode_box.currentTextChanged.connect(self._mode_changed)
        self.host = QLineEdit("127.0.0.1")
        self.host.setPlaceholderText("Broker 地址")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(1883)
        self.port.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.port.setFixedWidth(68)
        self.sn = QLineEdit("TEST_DOG_001")
        self.sn.setPlaceholderText("设备 SN")
        self.sn.textChanged.connect(self._update_topic)
        self.sn.textChanged.connect(self._connection_hint)
        self.sn.textChanged.connect(self._device_card)
        self.host.textChanged.connect(self._connection_hint)
        self.user = QLineEdit()
        self.user.setPlaceholderText("用户名")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("密码")
        self.tls = Switch("TLS")
        # 远程：经 FRP 用 SSH 登到狗上，把狗上的 Broker 和 ZLM 端口转到本机，之后和直连一样用；取设备日志也用这组 SSH 账号
        stored = settings()
        self.frp = Switch("经远程（FRP）")
        self.frp.toggled.connect(self._frp_toggled)
        self.ssh_user = QLineEdit(stored.value("ssh_user", ""))
        self.ssh_user.setPlaceholderText("SSH 用户名")
        self.ssh_password = QLineEdit(stored.value("ssh_password", ""))
        self.ssh_password.setEchoMode(QLineEdit.Password)
        self.ssh_password.setPlaceholderText("SSH 密码")
        # 本机自用，SSH 账号改完就记住
        self.ssh_user.editingFinished.connect(lambda: settings().setValue("ssh_user", self.ssh_user.text().strip()))
        self.ssh_password.editingFinished.connect(lambda: settings().setValue("ssh_password", self.ssh_password.text()))
        self.ssh, self.ssh_zlm, self.ssh_connecting = None, 0, False
        self.connect_btn = button("连接", self._connect, "primary")
        broker = QHBoxLayout()
        broker.addWidget(self.host, 1)
        broker.addWidget(self.port)
        account = QHBoxLayout()
        account.addWidget(self.user)
        account.addWidget(self.password)
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
        self._load_profiles()
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
        if self.viewer:
            self.viewer.set_dark(dark)
        effect = QGraphicsOpacityEffect(cover)
        cover.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", cover)
        fade.setDuration(260)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        fade.finished.connect(cover.deleteLater)
        fade.start()

    def restore_connection(self):
        """填回上次点“连接”时的连接参数；只填不连，没记过就保持默认的沙盒"""
        saved = settings().value("connection", "")
        if not saved:
            return
        config = json.loads(saved)
        self._profile_chosen_config(config)
        self.password.setText(config.get("password", ""))

    def _load_profiles(self, select=None):
        self.profile_box.clear()
        self.profile_box.addItem("手动填写", None)
        for name, config in self.store.connections().items():
            self.profile_box.addItem(name, config)
        if select:
            self.profile_box.setCurrentText(select)

    def _profile_chosen(self, index):
        config = self.profile_box.itemData(index)
        if config:
            self._profile_chosen_config(config)

    def _profile_chosen_config(self, config):
        # 旧版的“模拟 GW → 真实模块”发出的内容和直连一样，已合并到“直连模块”
        mode = "direct" if config["mode"] == "mock_gw" else config["mode"]
        self.mode_box.setCurrentText(next(k for k, v in MODES.items() if v == mode))
        self.host.setText(config["host"])
        self.port.setValue(config["port"])
        self.sn.setText(config["sn"])
        self.user.setText(config["user"])
        self.tls.setChecked(config["tls"])
        self.frp.setChecked(config.get("frp", False))
        self.password.clear()

    def _save_profile(self):
        default = self.profile_box.currentText() if self.profile_box.currentData() else f"{self.host.text().strip()} · {self.sn.text().strip()}"
        name, ok = QInputDialog.getText(self, "保存连接配置", "名称（密码不保存）", text=default)
        if not ok or not name.strip():
            return
        self.store.save_connection(name.strip(), {"mode": self.mode, "host": self.host.text().strip(), "port": self.port.value(),
                                                  "sn": self.sn.text().strip(), "user": self.user.text().strip(),
                                                  "tls": self.tls.isChecked()})
        self._load_profiles(name.strip())
        self.notify(f"已保存连接配置：{name.strip()}")

    def _delete_profile(self):
        if self.profile_box.currentData():
            self.store.delete_connection(self.profile_box.currentText())
            self._load_profiles()

    def _control_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._page_header(layout, "控制调试", "", button("演示一次故障定位", self._demo, "link"))
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
        self.picker = QWidget(self, Qt.Popup)
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
        mv.addWidget(label("Payload", "groupHeader"))
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("code")
        self.form_timer = QTimer(self)
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
        actions.addWidget(button("换新 tid", self._new_tid))
        actions.addWidget(button("恢复模板", lambda: self._template()))
        actions.addWidget(button("存为用例", self._save_case))
        actions.addStretch()
        self.send_btn = button("发送", self._send, "primary")
        actions.addWidget(self.send_btn)
        mv.addLayout(actions)
        # 编辑在上、回复在下，发送后不用转头找结果
        work = QSplitter(Qt.Vertical)
        work.setHandleWidth(12)
        work.addWidget(middle)

        right, rv = card(8, 16)
        rh = QHBoxLayout()
        self.result_title = label("本次结果", "sectionTitle")
        rh.addWidget(self.result_title)
        rh.addStretch()
        rh.addWidget(button("在消息页打开", self._open_watch, "link"))
        rv.addLayout(rh)
        self.result = TraceView("发送后在这里看 ACK、状态和失败原因")
        self.result.original.connect(self._original)
        rv.addWidget(self.result, 1)
        work.addWidget(right)
        work.setSizes([560, 400])
        layout.addWidget(work, 1)
        return page

    def _observer(self):
        """3D 观察区：模型选择、视图和遥测；遥控页和控制调试页共用同一个"""
        self.observer = QWidget()
        mv = QVBoxLayout(self.observer)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(12)
        self.model_box = QComboBox()
        for name in VARIANTS:
            self.model_box.addItem(name, name)
        self.model_box.activated.connect(self._model_chosen)
        importer = QPushButton("导入模型")
        importer.setObjectName("primary")
        importer.setCursor(Qt.PointingHandCursor)
        menu = QMenu(importer)
        for text, kind in (("URDF 文件…", "urdf"), ("URDF 模型目录…", "urdf_dir"), ("URDF 压缩包（zip）…", "urdf_zip"),
                           ("STEP / STP 文件…", "step"), ("STL 文件…", "stl")):
            menu.addAction(text, lambda kind=kind: self.import_model_dialog(kind))
        importer.setMenu(menu)
        row = QHBoxLayout()
        row.addWidget(self.model_box, 1)
        self.replay_btn = button("回放日志", self._replay_scan)
        self.replay_btn.setToolTip("按已入库的 OSD 回放某次会话：模型位置和当时的指令、ACK 一起看")
        row.addWidget(self.replay_btn)
        row.addWidget(importer)
        mv.addLayout(row)
        stage, self.stage = card(8, 12)
        top = QHBoxLayout()
        self.position_pill = pill("位置 · 演示", GRAY)
        self.joint_pill = pill("关节 · 演示", GRAY)
        top.addWidget(self.position_pill)
        top.addWidget(self.joint_pill)
        top.addStretch()
        self.view_bar = Segmented(["适合", "前", "侧", "顶", "等轴"])
        self.view_bar.changed.connect(lambda i: self.viewer and self.viewer.view(["等轴", "前", "侧", "顶", "等轴"][i]) if i else
                                      self.viewer and self.viewer.fit())
        top.addWidget(self.view_bar)
        self.stage.addLayout(top)
        self.model_info = label("", "caption", True)
        self.stage.addWidget(self.model_info)
        self.stage.addLayout(self._cloud_bar())
        # 导入进行中 / 失败时出现在视图上方，原场景保持可见可操作
        self.import_banner = QFrame()
        self.import_banner.setObjectName("banner")
        bl = QHBoxLayout(self.import_banner)
        bl.setContentsMargins(14, 10, 14, 10)
        self.import_spinner = QProgressBar()
        self.import_spinner.setRange(0, 0)
        self.import_spinner.setFixedSize(90, 6)
        self.import_text = label("", wrap=True)
        bl.addWidget(self.import_spinner)
        bl.addWidget(self.import_text, 1)
        self.banner_actions = QHBoxLayout()
        bl.addLayout(self.banner_actions)
        self.import_banner.hide()
        self.stage.addWidget(self.import_banner)
        self.stage_hint = label("窗口显示后加载模型", "caption")
        self.stage_hint.setAlignment(Qt.AlignCenter)
        self.stage.addWidget(self.stage_hint, 1)
        hint = label("左键拖动旋转 · 滚轮缩放 · 中键或 Ctrl+左键平移", "caption")
        hint.setAlignment(Qt.AlignRight)
        self.stage.addWidget(hint)
        mv.addWidget(stage, 1)
        mv.addWidget(self._replay_bar())

        stats, tv = card(8, 14)
        grid = QGridLayout()
        grid.setSpacing(8)
        self.stats, self.sparks = {}, {}
        for index, (key, caption) in enumerate((("batteryRemain", "电量"), ("speed", "速度"), ("heading", "航向"),
                                                ("position", "位置"), ("functionMode", "模式"), ("controller", "控制权"),
                                                ("temperature", "温度"), ("flyingTime", "运行"))):
            tile = QFrame()
            tile.setObjectName("stat")
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(10, 6, 10, 6)
            tl.setSpacing(0)
            value = label("—", "statValue")
            tl.addWidget(value)
            tl.addWidget(label(caption, "caption"))
            self.stats[key] = value
            # 数值类字段带走势线；其他格子留同样高度，四列对齐
            spark = Sparkline(SPARK_COLORS[key]) if key in SPARK_COLORS else QWidget()
            spark.setFixedHeight(18)
            if key in SPARK_COLORS:
                self.sparks[key] = spark
            tl.addWidget(spark)
            grid.addWidget(tile, index // 4, index % 4)
        tv.addLayout(grid)
        self.osd_note = label("还没收到 OSD，位置按已发的 move 指令演示", "caption", True)
        tv.addWidget(self.osd_note)
        mv.addWidget(stats)
        self.scene.changed.connect(self._device_changed)
        self.scene.posed.connect(self._sync_joints)
        return self.observer


    def _cloud_bar(self):
        """3D 区的点云一行：是否在收、显示开关、着色、雷达离地高度；下面一行是最近一帧的点数、频率和多久前"""
        box = QVBoxLayout()
        box.setSpacing(2)
        row = QHBoxLayout()
        self.cloud_pill = pill("点云 · 未收到", GRAY)
        row.addWidget(self.cloud_pill)
        row.addStretch()
        self.cloud_show = Switch("显示")
        self.cloud_show.setChecked(True)
        self.cloud_show.toggled.connect(self._cloud_options)
        row.addWidget(self.cloud_show)
        # 新格式（原点坐标系）的点云逐帧累加成地图，看建图效果；旧格式没有地图
        self.cloud_map_show = Switch("地图")
        self.cloud_map_show.setChecked(True)
        self.cloud_map_show.setToolTip("把每帧点云按 10 cm 体素累加成地图，并画出雷达走过的轨迹（红线）。点是稀疏后的，只看效果，不是 SLAM 的 map.pcd")
        self.cloud_map_show.toggled.connect(self._cloud_options)
        row.addWidget(self.cloud_map_show)
        self.cloud_mode = Segmented(["高度", "强度"])
        self.cloud_mode.changed.connect(self._cloud_options)
        row.addWidget(self.cloud_mode)
        row.addWidget(label("离地", "caption"))
        self.lidar_height = spin(-2, 3, 0.45, 0.05, 2, 54)
        self.lidar_height.setToolTip("雷达原点离地高度（米），只影响显示。安装位置协议没给，按室内实测地面约在雷达下方 0.45 m 估的")
        self.lidar_height.valueChanged.connect(self._cloud_options)
        row.addWidget(self.lidar_height)
        row.addWidget(button("清除", self.scene.clear_cloud, "link"))
        box.addLayout(row)
        self.cloud_info = label("ros2bridge 采集时（GW 建图中，或导航与文件页手动打开）收到的点云挂在机身位置上显示", "caption", True)
        box.addWidget(self.cloud_info)
        self.scene.clouded.connect(self._cloud_status)
        return box

    def _cloud_options(self, *_):
        if self.viewer:
            self.viewer.set_cloud_options(self.cloud_show.isChecked(), ["高度", "强度"][self.cloud_mode.index()],
                                          self.lidar_height.value(), self.cloud_map_show.isChecked())

    def _cloud_status(self):
        """点云标签：实时（3 s 内有新帧）/ 已停 / 未收到；每秒和每次换帧时更新"""
        scene, cloud = self.scene, self.scene.cloud
        live = scene.cloud_live
        text, color = ("点云 · 实时", GREEN) if live else ("点云 · 已停", ORANGE) if cloud else ("点云 · 未收到", GRAY)
        self.cloud_pill.setText(text)
        self.cloud_pill.setStyleSheet(f"background:{tint(color, 34)};color:{color};border-radius:9px;"
                                      "padding:2px 9px;font-size:11px;font-weight:600")
        if cloud:
            text = cloud_summary(cloud, scene.cloud_rate if live else None)
            acc = scene.cloud_map
            if cloud.in_map:
                text += f" · 地图 {len(acc.xyz)} 点" + ("（已满，不再累加）" if acc.full else "") + f"、走了 {acc.distance:.1f} m"
            else:
                text += f" · {cloud.meta.get('frame_id', '')} 坐标系，跟着机身位置画"
            self.cloud_info.setText(text)
        # 停发后点变淡，最后一帧留着
        if self.viewer and self.viewer.cloud.visible() and self.viewer.cloud_stale == live:
            self.viewer.sync_cloud()

    def _remote_page(self):
        """遥控和模型设置两栏切换；切到“模型”时遥控面板隐藏，按住的方向会按松手处理"""
        page = QWidget()
        sv = QVBoxLayout(page)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(12)
        self._page_header(sv, "遥控", "", self._quick_view())
        self.side_tabs = Segmented(["遥控", "模型"])
        sv.addWidget(self.side_tabs)
        self.side_stack = QStackedWidget()
        self.side_tabs.changed.connect(lambda i: fade_to(self.side_stack, i))
        self.remote = RemotePanel(self._quick_send)
        self.side_stack.addWidget(scroller(self.remote))
        model_side = QWidget()
        ms = QVBoxLayout(model_side)
        ms.setContentsMargins(0, 0, 0, 0)
        ms.setSpacing(14)
        self.side_stack.addWidget(model_side)
        sv.addWidget(self.side_stack, 1)

        self.pose_card, pv = card(10, 16)
        pv.addWidget(label("演示姿态", "sectionTitle"))
        row = QGridLayout()
        row.setSpacing(8)
        for index, name in enumerate(POSES):
            row.addWidget(button(name, lambda checked=False, name=name: self.scene.pose(name), "tile"), 0, index)
        pv.addLayout(row)
        pv.addWidget(label("只改 3D 演示，不发指令。遥控发站立、趴下、跳跃、打招呼、move 时模型同步演示", "caption", True))
        ms.addWidget(self.pose_card)

        joints, jv = card(8, 16)
        head = QHBoxLayout()
        self.joint_title = label("关节", "sectionTitle")
        head.addWidget(self.joint_title)
        head.addStretch()
        self.follow = Switch("镜头跟随")
        self.follow.setChecked(True)
        self.follow.toggled.connect(lambda on: setattr(self.viewer, "follow", on) if self.viewer else None)
        head.addWidget(self.follow)
        jv.addLayout(head)
        holder = QWidget()
        self.joint_box = QVBoxLayout(holder)
        self.joint_box.setContentsMargins(0, 0, 6, 0)
        self.joint_box.setSpacing(4)
        jv.addWidget(scroller(holder), 1)
        actions = QHBoxLayout()
        actions.addWidget(button("关节归零", lambda: self.scene.pose("零位")))
        actions.addWidget(button("清除轨迹", self.scene.clear_trail))
        jv.addLayout(actions)
        ms.addWidget(joints, 1)
        return page

    def _replay_bar(self):
        """回放条：拖动或播放逐帧显示历史 OSD，下面列出到这一帧为止最近的消息"""
        self.replay_bar, rb = card(6, 12)
        row = QHBoxLayout()
        self.replay_play = button("▶", self._replay_toggle, "tile")
        self.replay_play.setFixedWidth(44)
        row.addWidget(self.replay_play)
        self.replay_slider = QSlider(Qt.Horizontal)
        self.replay_slider.valueChanged.connect(self._replay_seek)
        row.addWidget(self.replay_slider, 1)
        self.replay_label = label("", "caption")
        row.addWidget(self.replay_label)
        row.addWidget(button("退出回放", self._replay_exit, "link"))
        rb.addLayout(row)
        rb.addWidget(label("到这一帧为止最近的消息，粗体是上一帧之后新出现的；双击看证据链", "caption"))
        self.replay_list = QListWidget()
        self.replay_list.setObjectName("commands")
        self.replay_list.setFixedHeight(130)
        self.replay_list.itemDoubleClicked.connect(self._replay_open)
        rb.addWidget(self.replay_list)
        self.replay_timer = QTimer(self)
        self.replay_timer.setInterval(100)
        self.replay_timer.timeout.connect(self._replay_tick)
        self.replay_bar.hide()
        return self.replay_bar

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

    def _replay_scan(self):
        self.store.flush()
        self.notify("正在查找带 OSD 的会话…")
        self._background(lambda reader: reader.osd_sessions(), "replay_sessions")

    def _replay_menu(self, sessions):
        if not sessions:
            self.notify("库里还没有 OSD 消息：先连上设备，或在日志排障页导入带 OSD 的日志")
            return
        menu = QMenu(self)
        for sn, session, count in sessions:
            menu.addAction(f"{sn or '未知设备'} · 会话 {session[:8]} · {count} 条 OSD",
                           lambda sn=sn, session=session: self._background(lambda r: r.osd_frames(sn, session),
                                                                           "replay_frames", sn, session))
        menu.popup(self.replay_btn.mapToGlobal(self.replay_btn.rect().bottomLeft()))

    def _replay_start(self, rows, sn, session):
        frames = [(row_id, e, fields) for row_id, e in rows if (fields := osd_fields(e))]
        if not frames:
            self.notify("这个会话的 OSD 里没有位置或遥测字段")
            return
        # 轨迹按帧累积；某帧没带 x/y 时沿用上一帧的位置
        trail, x, y = [], 0.0, 0.0
        for _, _, fields in frames:
            x, y = float(fields.get("x", x)), float(fields.get("y", y))
            trail.append((x, y))
        self.replay = {"frames": frames, "trail": trail, "sn": sn, "session": session}
        self.replay_bar.show()
        self.replay_slider.blockSignals(True)
        self.replay_slider.setRange(0, len(frames) - 1)
        self.replay_slider.setValue(0)
        self.replay_slider.blockSignals(False)
        self._replay_seek(0)

    def _replay_seek(self, index):
        frames = self.replay["frames"]
        row_id, e, fields = frames[index]
        self.scene.replay(fields, self.replay["trail"][:index + 1])
        stamp = lambda m: m.source_time or time.strftime("%H:%M:%S", time.localtime(m.timestamp))
        self.replay_label.setText(f"第 {index + 1} / {len(frames)} 帧　{stamp(e)}")
        after = frames[index - 1][0] if index else 0
        self.replay_list.clear()
        for found_id, m in self.store.recent_before(self.replay["sn"], self.replay["session"], row_id):
            item = QListWidgetItem(f"{stamp(m)}　{m.module.upper()} {m.kind} · {method_label(m.method)} · {diagnose(m).status}")
            item.setData(Qt.UserRole, m)
            if found_id > after:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.replay_list.addItem(item)
        if not self.replay_list.count():
            self.replay_list.addItem("这一帧之前没有其他消息")

    def _replay_toggle(self):
        if self.replay_timer.isActive():
            self.replay_timer.stop()
        else:
            if self.replay_slider.value() == self.replay_slider.maximum():
                self.replay_slider.setValue(0)
            self.replay_timer.start()
        self.replay_play.setText("⏸" if self.replay_timer.isActive() else "▶")

    def _replay_tick(self):
        if self.replay_slider.value() >= self.replay_slider.maximum():
            self._replay_toggle()
        else:
            self.replay_slider.setValue(self.replay_slider.value() + 1)

    def _replay_open(self, item):
        event = item.data(Qt.UserRole)
        if event and event.tid:
            self.filter_text.setText(event.tid)
            self._go(self.MESSAGES)

    def _replay_exit(self):
        self.replay_timer.stop()
        self.replay_play.setText("▶")
        self.replay = None
        self.replay_bar.hide()
        self.scene.reset_telemetry()
        self.scene.clear_trail()

    def _ensure_viewer(self):
        """观察区首次显示才加载模型和 OpenGL，先显示加载中，避免拖慢启动"""
        if self.viewer is not None:
            return
        self.viewer = False
        self.stage_hint.setText("正在加载模型…")
        QTimer.singleShot(40, self._load_viewer)

    def _load_viewer(self):
        try:
            from .viewer import RobotView
        except ImportError as error:
            self.stage_hint.setText("3D 需要安装 numpy、PyOpenGL、pyqtgraph（见 requirements.txt）\n" + str(error))
            self.viewer = None
            return
        if not self.scene.robot:
            self.scene.set_variant(self.model_box.currentData())
        self.viewer = RobotView(self.scene)
        self.viewer.follow = self.follow.isChecked()
        self.compass = Compass(self.viewer)
        self.viewer.set_dark(self.dark)
        self._cloud_options()
        self.stage_hint.hide()
        self.stage.insertWidget(self.stage.indexOf(self.stage_hint), self.viewer, 1)
        self._model_loaded()

    def _model_chosen(self, index):
        """下拉框里选内置模型或本次导入过的模型；内置模型按名字现读，导入的直接复用"""
        data = self.model_box.itemData(index)
        robot = model_module.load(data) if isinstance(data, str) else data
        self.scene.set_robot(robot)
        self._model_loaded()

    def _model_loaded(self):
        """模型切换后刷新侧栏：L2 显示演示姿态和按腿分组的关节；其他 URDF 按自身关节；STEP/STL 列部件"""
        robot = self.scene.robot
        self.pose_card.setVisible(robot.kind == "L2")
        self.joint_pill.setText("关节 · 演示" if robot.kind in ("L2", "URDF") else "结构 · 无关节")
        triangles = sum(len(l.mesh) for l in robot.links.values() if l.mesh is not None)
        low, high = model_module.bounds(robot)
        size = "×".join(f"{v:.2f}" for v in (high - low))
        kinds = {"L2": "内置 URDF", "URDF": "导入 URDF", "STEP": "STEP 读取", "STL": "STL 网格"}
        unit = {"STEP": f"文件单位 {robot.unit}，已换算为 m", "STL": f"按 {robot.unit} 导入"}.get(robot.kind, "单位 m（URDF 约定）")
        parts = [kinds.get(robot.kind, robot.kind), robot.name]
        if robot.kind in ("L2", "URDF"):
            parts.append(f"{len(robot.links)} 连杆 · {len(robot.movable)} 可动关节")
        else:
            parts.append(f"{sum(1 for l in robot.links.values() if l.mesh is not None)} 个部件")
        parts += [f"{triangles / 1000:.0f}k 面" if triangles >= 1000 else f"{triangles} 面", f"{size} m", unit]
        if robot.missing:
            parts.append(f"缺 {len(robot.missing)} 个网格未显示")
        self.model_info.setText("　".join(parts))
        self.model_info.setToolTip(robot.source)
        self._build_joints()
        self._device_card()
        if self.viewer:
            self.viewer.fit()

    def _build_joints(self):
        while self.joint_box.count():
            item = self.joint_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.joint_sliders = {}
        robot = self.scene.robot
        if robot.kind in ("STEP", "STL"):
            self.joint_title.setText("部件")
            note = "STEP 是结构外形，文件里没有关节定义，这里只做结构查看" if robot.kind == "STEP" else "STL 只有网格，没有部件和关节信息"
            self.joint_box.addWidget(label(note, "caption", True))
            parts = QListWidget()
            parts.setObjectName("commands")
            for name, link in robot.links.items():
                if link.mesh is not None:
                    item = QListWidgetItem(f"{name.split(' ', 1)[-1]}　　{len(link.mesh)} 面")
                    item.setData(Qt.UserRole, name)
                    parts.addItem(item)
            # 点部件在 3D 里高亮
            parts.currentItemChanged.connect(lambda item, _: self.viewer and self.viewer.highlight(item.data(Qt.UserRole) if item else None))
            self.joint_box.addWidget(parts, 1)
            self.part_list = parts
            return
        self.joint_title.setText("关节")
        if not robot.movable:
            self.joint_box.addWidget(label("这个模型没有可动关节", "caption"))
        l2 = robot.kind == "L2"
        legs = {"FBL": "左前", "FAR": "右前", "RBL": "左后", "RAR": "右后"}
        parts = {"ABAD": "侧摆", "HIP": "髋", "KNEE": "膝", "FOOT": "轮"}
        current = None
        for joint in robot.movable:
            if l2:
                leg, part = joint.name[:3], joint.name.split("_")[-2]
                if leg != current:
                    current = leg
                    self.joint_box.addWidget(label(legs.get(leg, leg) + "腿", "groupHeader"))
                caption = parts.get(part, part)
            else:
                caption = joint.name
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            name_label = label(caption, "caption")
            name_label.setToolTip(f"{joint.name}　{joint.kind}　{joint.lower:.3f} ~ {joint.upper:.3f}"
                                  + (" m" if joint.kind == "prismatic" else " rad"))
            if not l2:
                name_label.setFixedWidth(110)
            rl.addWidget(name_label)
            slider = guard_wheel(QSlider(Qt.Horizontal))
            # 转动关节 1 格 0.01 rad，移动关节 1 格 1 mm
            scale = 1000 if joint.kind == "prismatic" else 100
            slider.setRange(round(joint.lower * scale), round(joint.upper * scale))
            value = label("", "caption")
            value.setFixedWidth(52)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            slider.valueChanged.connect(lambda v, name=joint.name, shown=value, s=scale, j=joint: (
                shown.setText(self._joint_text(j, v / s)), self.scene.set_joint(name, v / s)))
            # 拖动时把对应连杆染成强调色，松手恢复
            slider.sliderPressed.connect(lambda child=joint.child: self.viewer and self.viewer.highlight(child))
            slider.sliderReleased.connect(lambda: self.viewer and self.viewer.highlight(None))
            rl.addWidget(slider, 1)
            rl.addWidget(value)
            self.joint_box.addWidget(row)
            self.joint_sliders[joint.name] = (slider, value, scale, joint)
        self.joint_box.addStretch()
        self._sync_joints()

    @staticmethod
    def _joint_text(joint, value):
        return f"{value * 1000:.0f} mm" if joint.kind == "prismatic" else f"{math.degrees(value):.0f}°"

    def _sync_joints(self):
        for name, (slider, value, scale, joint) in self.joint_sliders.items():
            angle = self.scene.angles.get(name, 0.0)
            slider.blockSignals(True)
            slider.setValue(round(angle * scale))
            slider.blockSignals(False)
            value.setText(self._joint_text(joint, angle))

    # ---- 模型导入 ----

    def import_model_dialog(self, kind):
        filters = {"urdf": ("选择 URDF 文件", "URDF (*.urdf *.xml)"), "urdf_zip": ("选择 URDF 压缩包", "压缩包 (*.zip)"),
                   "step": ("选择 STEP 文件", "STEP (*.step *.stp *.STEP *.STP)"), "stl": ("选择 STL 文件", "STL (*.stl *.STL)")}
        if kind == "urdf_dir":
            path = QFileDialog.getExistingDirectory(self, "选择包含 URDF 的模型目录")
        else:
            path, _ = QFileDialog.getOpenFileName(self, *filters[kind])
        if not path:
            return
        unit = "m"
        if kind == "stl":
            answer = QMessageBox.question(self, "STL 单位", "STL 文件本身不带单位。按毫米导入？\n选“否”按米导入",
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            unit = "mm" if answer == QMessageBox.Yes else "m"
        self.import_model("step" if kind == "step" else "stl" if kind == "stl" else "urdf", path, unit=unit)

    def import_model(self, kind, path, resource_dirs=(), allow_missing=False, unit="m"):
        """后台导入模型：URDF/STL 在线程里解析，STEP 在子进程里用 OpenCASCADE 网格化；成功才切场景，失败保留原场景"""
        self.cancel_import()
        token = object()
        self.importing = {"token": token, "kind": kind, "path": path, "dirs": list(resource_dirs), "cancel": threading.Event()}
        self._banner(f"正在导入 {Path(path).name}…", busy=True, actions=[("取消", self.cancel_import)])
        if kind == "step":
            reason = cad.available()
            if reason:
                self._import_failed(token, reason)
                return
            self.importing["job"] = cad.StepJob(path, QStandardPaths.writableLocation(QStandardPaths.TempLocation))
            self.step_timer.start(100)
            return
        cancel = self.importing["cancel"]

        def work():
            try:
                robot = (model_module.load_stl(path, unit) if kind == "stl" else
                         model_module.load_urdf(path, resource_dirs, allow_missing, cancel.is_set))
                self.inbox.put(("model", token, robot))
            except model_module.ModelError as error:
                self.inbox.put(("model_error", token, error))
            except Exception as error:
                self.inbox.put(("model_error", token, model_module.ModelError(f"{type(error).__name__}: {error}")))

        threading.Thread(target=work, daemon=True).start()

    def _poll_step(self):
        job = self.importing["job"]
        while (event := job.poll()) is not None:
            kind, value = event
            if kind == "stage":
                self.import_text.setText(f"{value}　（{time.monotonic() - job.started:.0f} s）")
            elif kind == "done":
                self.step_timer.stop()
                try:
                    self._import_done(self.importing["token"], job.result(model_module))
                except Exception as error:
                    self._import_failed(self.importing["token"], f"读取网格结果失败：{error}")
                return
            elif kind == "error":
                self.step_timer.stop()
                job.cancel()
                self._import_failed(self.importing["token"], value)
                return

    def cancel_import(self):
        """取消在途导入：URDF 线程按标志位停，STEP 子进程直接终止；当前场景不受影响"""
        if not self.importing:
            return
        self.importing["cancel"].set()
        if self.importing.get("job"):
            self.importing["job"].cancel()
            self.step_timer.stop()
        self.importing = None
        self._banner("")

    def _import_done(self, token, robot):
        if not self.importing or self.importing["token"] is not token:
            return
        self.importing = None
        self._banner("")
        self.model_box.addItem(f"{robot.name}（{robot.kind}）", robot)
        self.model_box.setCurrentIndex(self.model_box.count() - 1)
        self.scene.set_robot(robot)
        if self.observer.isVisible():
            self._ensure_viewer()
        self._model_loaded()
        self.notify(f"已导入 {robot.name}" + (f"，{len(robot.missing)} 个网格缺失未显示" if robot.missing else ""))

    def _import_failed(self, token, error):
        if not self.importing or self.importing["token"] is not token:
            return
        job = dict(self.importing)
        self.importing = None
        missing = getattr(error, "missing", [])
        text = str(error)
        if missing:
            lines = [f"· {link}：{filename}" for link, filename, _ in missing[:6]]
            if len(missing) > 6:
                lines.append(f"· 还有 {len(missing) - 6} 个")
            text += "\n" + "\n".join(lines)
            self.import_text.setToolTip("\n\n".join(f"{link}：{filename}\n尝试过：\n" + "\n".join(tried[:8])
                                                     for link, filename, tried in missing[:10]))
        actions = [("关闭", lambda: self._banner(""))]
        if missing:
            actions = [("选择资源目录重试", lambda: self._retry_with_dir(job)),
                       ("忽略缺失继续打开", lambda: self.import_model(job["kind"], job["path"], job["dirs"], True))] + actions
        self._banner("导入失败，当前场景未改变：" + text, error=True, actions=actions)

    def _retry_with_dir(self, job):
        directory = QFileDialog.getExistingDirectory(self, "选择网格所在的资源目录（例如 meshes 的上级或 ROS 包目录）")
        if directory:
            self.import_model(job["kind"], job["path"], job["dirs"] + [directory])

    def _banner(self, text, busy=False, error=False, actions=()):
        """视图上方的导入状态条；text 为空时隐藏"""
        while self.banner_actions.count():
            widget = self.banner_actions.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        if not text:
            self.import_banner.hide()
            return
        self.import_banner.setProperty("error", error)
        self.import_banner.style().polish(self.import_banner)
        self.import_spinner.setVisible(busy)
        self.import_text.setText(text)
        for caption, slot in actions:
            self.banner_actions.addWidget(button(caption, slot, "link"))
        self.import_banner.show()

    def _device_changed(self):
        """刷新遥测卡和侧栏设备卡；动画期间每帧都会调用，只做文字更新"""
        scene, osd = self.scene, self.scene.osd
        live = self.shown_live = scene.live
        state = "回放" if self.replay else "OSD 实时" if live else "演示"
        self.position_pill.setText("位置 · " + state)
        color = PURPLE if self.replay else GREEN if live else GRAY
        self.position_pill.setStyleSheet(f"background:{tint(color, 34)};color:{color};border-radius:9px;"
                                         "padding:2px 9px;font-size:11px;font-weight:600")
        modes = {1: "SDK 巡航", 2: "手柄遥控", 3: "普通 SDK"}
        owners = {1: "SDK", 2: "手柄", 3: "建图"}
        values = {"batteryRemain": f"{osd['batteryRemain']:.0f}%" if "batteryRemain" in osd else "—",
                  "speed": f"{osd['speed']:.2f} m/s" if "speed" in osd else "—",
                  "heading": f"{osd['heading']:.0f}°" if "heading" in osd else "—",
                  "position": f"{scene.x:.2f}, {scene.y:.2f}",
                  "functionMode": modes.get(osd.get("functionMode"), "—"),
                  "controller": owners.get(osd.get("controller"), "—"),
                  "temperature": f"{osd['temperature']:.0f} ℃" if "temperature" in osd else "—",
                  "flyingTime": f"{int(osd['flyingTime']) // 60} 分 {int(osd['flyingTime']) % 60} 秒" if "flyingTime" in osd else "—"}
        for key, text in values.items():
            self.stats[key].setText(text)
        # 动画期间每帧都会进来，走势线每 0.5 s 采一次，只采真实 OSD
        now = time.monotonic()
        if live and now - self.spark_at >= 0.5:
            self.spark_at = now
            for key, spark in self.sparks.items():
                if isinstance(osd.get(key), (int, float)):
                    spark.add(now, float(osd[key]))
        if self.compass:
            speed = osd.get("speed", 0.0) if live or self.replay else math.hypot(*scene.velocity[:2])
            self.compass.set(math.degrees(scene.heading) % 360, float(speed), state)
        if self.replay:
            self.osd_note.setText("回放中：显示历史 OSD，实时 OSD 暂不驱动模型")
        elif osd:
            self.osd_note.setText("OSD 实时，位置跟随设备" if live else f"OSD 已超过 {LIVE_SECONDS:.0f} 秒没更新，显示最后一次数值")
        self._device_card()

    def _device_card(self):
        osd, live = self.scene.osd, self.scene.live
        sn = self.mqtt.sn if self.mqtt.connected else self.sn.text().strip()
        self.device_name.setText(sn or "未设置 SN")
        robot = self.scene.robot
        parts = [(self.scene.variant or list(VARIANTS)[0]).split(" ")[0] if not robot or robot.kind == "L2" else robot.name]
        if "batteryRemain" in osd:
            parts.append(f"电量 {osd['batteryRemain']:.0f}%")
        parts.append("OSD 实时" if live else "未收到 OSD" if not osd else "OSD 已停")
        self.device_meta.setText("  ·  ".join(parts))
        self.device_dot.setStyleSheet(f"background:{GREEN if live else '#C7C7CC'};border-radius:4px")

    def _mission_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._page_header(layout, "导航与文件", "建图定位、航点导航、文件同步、红外测温和喊话；设备回复的地图、文件和温度会自动回填",
                          self._quick_view())
        self.missions = MissionPanel(self._quick_send, self._edit_payload, lambda: self.remote.camera(),
                                     lambda: (self.scene.x, self.scene.y, self.scene.heading))
        self.scene.changed.connect(self.missions.update_robot)
        layout.addWidget(scroller(self.missions), 1)
        return page

    def _edit_payload(self, method, data):
        """把表单组好的报文带到控制页，继续手改后再发"""
        self._open_command(method)
        payload = make_command(find(method))
        payload["data"] = data
        if self.mode == "direct":
            payload = local_payload(payload)
        self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    def _ingest(self, event):
        """所有消息都入库；只有当前活动连接、当前设备的 OSD 驱动 3D 和设备卡

        点云同样只认当前设备（沙盒模式下也显示沙盒的演示帧）；原始点只进 3D，库里存瘦身后的报文
        """
        data = cloud_data(event)
        if data is not None and (self._current_device(event) or (event.source == "sandbox" and self.mode == "sandbox")):
            try:
                self.scene.set_cloud(decode_cloud(data))
                self.cloud_error = ""
            except CloudError as error:
                # 同一个错误连着来只提示一次，中间解出过好帧再出错会再提示
                if str(error) != self.cloud_error:
                    self.cloud_error = str(error)
                    self.notify("点云解析失败：" + self.cloud_error)
                    self.cloud_info.setText("最近一帧没解出来：" + self.cloud_error)
        self.store.add(event)
        self.module_touched[event.module] = self.module_touched.get(event.module, 0) + 1
        self.missions.observe(event)
        # 回放期间不让实时 OSD 把画面拉走
        if self._current_device(event) and not self.replay:
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
            self.filter_text.setText(self.quick.tid)
            self._go(self.MESSAGES)

    def _message_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.failures_only = Switch("只看失败")
        self.failures_only.toggled.connect(self._refresh)
        self.view_mode = Segmented(["按请求", "逐条消息", "检测链路"])
        self.view_mode.changed.connect(self._view_changed)
        self._page_header(layout, "消息追踪", "", self.view_mode, self.failures_only, button("导出诊断包", self._export))
        self.filter_text = QLineEdit()
        self.filter_text.setObjectName("search")
        self.filter_text.setPlaceholderText("搜索错误、tid、Topic 或任意内容")
        # 大库里文本搜索要全表扫描，停手 300 ms 再查
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self._first_page)
        self.filter_text.textChanged.connect(self.search_timer.start)
        search = QHBoxLayout()
        search.addWidget(self.filter_text, 1)
        self.search_hint = label("搜索中…", "caption")
        self.search_hint.hide()
        search.addWidget(self.search_hint)
        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.filter_module = Segmented(["全部"] + [m.upper() for m in MODULES] + ["其他"])
        self.filter_kind = Segmented(["全部", "指令", "ACK", "状态", "事件", "日志"])
        self.filter_method = QComboBox()
        self.filter_method.addItems(["全部方法"] + sorted({e["method"] for e in self.catalog}))
        self.filter_method.setMinimumWidth(190)
        self.filter_module.changed.connect(self._refresh)
        self.filter_kind.changed.connect(self._refresh)
        self.filter_method.currentTextChanged.connect(self._refresh)
        search.addWidget(self.filter_method)
        layout.addLayout(search)
        filters.addWidget(self.filter_module)
        filters.addStretch()
        filters.addWidget(self.filter_kind)
        self.scope_chip = button("", lambda: self.set_scope(None), "chip")
        self.scope_chip.setToolTip("点击回到全部消息")
        self.scope_chip.hide()
        filters.insertWidget(1, self.scope_chip)
        self.chain_demo = button("沙盒演示检测链路", self._demo_detection, "link")
        self.chain_demo.hide()
        filters.addWidget(self.chain_demo)
        layout.addLayout(filters)
        layout.addSpacing(4)

        split = QSplitter()
        split.setHandleWidth(14)
        table_card, tv = card(0, 6)
        self.model = EventModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(PillDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(38)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.clicked.connect(self._inspect)
        self.request_model = RequestModel()
        self.request_table = QTableView()
        self.request_table.setModel(self.request_model)
        self.request_table.setItemDelegate(PillDelegate(self.request_table))
        self.request_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.request_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.request_table.setShowGrid(False)
        self.request_table.setWordWrap(False)
        self.request_table.verticalHeader().hide()
        self.request_table.verticalHeader().setDefaultSectionSize(40)
        header = self.request_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.request_table.clicked.connect(self._inspect_request)
        self.request_table.doubleClicked.connect(self._open_request)
        self.chain_model = ChainModel()
        self.chain_table = QTableView()
        self.chain_table.setModel(self.chain_model)
        self.chain_table.setItemDelegate(StageDelegate(self.chain_table))
        self.chain_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.chain_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.chain_table.setShowGrid(False)
        self.chain_table.verticalHeader().hide()
        self.chain_table.verticalHeader().setDefaultSectionSize(40)
        header = self.chain_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.chain_table.clicked.connect(self._inspect_chain)
        for table in (self.table, self.request_table, self.chain_table):
            QShortcut(QKeySequence.Copy, table, self._copy_selection, context=Qt.WidgetShortcut)
            # 列宽固定初值、可拖动，不按内容定宽：按内容定宽要问遍前 1000 行每一格的尺寸（走 Python 委托），
            # 消息多时每次刷新、换外观都要卡好几秒
            header = table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.Interactive)
            for column, width in enumerate(COLUMN_WIDTHS[table.model().__class__.__name__]):
                header.resizeSection(column, width)
        self.views = QStackedWidget()
        self.views.addWidget(self.request_table)
        self.views.addWidget(self.table)
        self.views.addWidget(self.chain_table)
        tv.addWidget(self.views)
        pager = QHBoxLayout()
        pager.setContentsMargins(10, 4, 10, 4)
        self.page_label = label("", "caption")
        self.newer_btn = button("‹ 较新", self._newer_page, "link")
        self.older_btn = button("更早 ›", self._older_page, "link")
        pager.addWidget(self.page_label)
        pager.addStretch()
        pager.addWidget(self.newer_btn)
        pager.addWidget(self.older_btn)
        self.pager = QWidget()
        self.pager.setLayout(pager)
        tv.addWidget(self.pager)
        split.addWidget(table_card)
        detail_card, dv = card(8, 16)
        head = QHBoxLayout()
        head.addStretch()
        self.note_btn = button("备注这次请求", self._edit_note, "link")
        self.note_btn.setEnabled(False)
        head.addWidget(self.note_btn)
        dv.addLayout(head)
        self.details = TraceView("点一行，看这次请求的完整证据链；失败的请求排在最前")
        self.details.previous = self.store.previous
        self.details.original.connect(self._original)
        self.details.store, self.details.nearby_logs = self.store, True
        self.details.switched.connect(self._details_switched)
        self.details.nearby.connect(lambda events: self._nearby_logs(events, self.MESSAGES))
        dv.addWidget(self.details)
        split.addWidget(detail_card)
        split.setSizes([760, 520])
        layout.addWidget(split, 1)
        return page

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
        self._page_header(layout, "状态总览", f"当前设备各模块最近一次上报，{FRESH_SECONDS} 秒内有更新显示绿点；"
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
        head.addWidget(label("直播两路", "sectionTitle"))
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
        self.stream_table.setFixedHeight(34 * 3 + 8)
        lv.addWidget(self.stream_table)
        layout.addSpacing(10)
        layout.addWidget(live)

        web, wv = self.web_card = card(8, 16)
        wv.addWidget(label("音视频预览", "sectionTitle"))
        row = QHBoxLayout()
        self.web_url = QLineEdit("https://124.126.230.115:5500/")
        # WebRTC 房间号是“整机SN_相机SN”（用户 11-02 确认，一个相机一个房间），跟着左侧 SN 和遥控页当前相机走
        self.room = QLineEdit()
        self.room.setPlaceholderText("房间号")
        row.addWidget(self.web_url, 2)
        row.addWidget(self.room, 1)
        row.addWidget(button("复制房间", lambda: QApplication.clipboard().setText(self.room.text())))
        row.addWidget(button("打开 WebRTC 网页", lambda: webbrowser.open(self.web_url.text()), "primary"))
        wv.addLayout(row)
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
        self.room.setText(f"{self.sn.text().strip()}_{self.remote.camera_sn.text().strip()}")

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
        # 直播两路和预览都是 Media 的内容，不看 Media 时一起收起
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
        self.detail_trace.original.connect(self._original)
        self.detail_trace.store, self.detail_trace.nearby_logs = self.store, True
        self.detail_trace.nearby.connect(lambda events: self._nearby_logs(events, self.STATUS))
        tv.addWidget(self.detail_trace, 1)
        self.detail_pages.addWidget(trace)
        dv.addWidget(self.detail_pages, 1)
        # 每个分段上次画的依据，切回没变化的分段直接显示，不重画
        self.detail_module, self.detail_drawn, self.detail_key = None, {}, None
        self.zlm_state, self.zlm_busy = None, False
        # 各 ZLM 地址的 RTSP / RTMP 端口（getServerConfig 查一次记住），和远程时为拉流开好的隧道 {(狗能连到的主机, 端口): 本机端口}
        self.zlm_ports, self.ssh_rtsp = {}, {}
        # 设备拉流配置（读到之前为 None）和读取结果说明：读到时是文件路径，没读到时是原因
        self.media_cfg, self.media_cfg_note, self.media_cfg_busy = None, "", False
        self.zlm_timer = QTimer(self)
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
                 self.module_touched.get(module, 0), tuple(sorted(self.hidden_types)), self.detail_filter.text())
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
        methods = [item["method"] for item in self.catalog if needle in (item["method"] + item["title"]).lower()] if needle else None
        fetched = self.store.query(module=module, kind=("指令", "ACK"), limit=600, sn=self._status_sn(), live=True, methods=methods)
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
        traces = self.store.groups([self._detail_row_key(e) for e, _ in shown if e.tid])
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
        self.detail_trace.show_trace(e, self.store.trace(e))
        self.detail_pages.setCurrentIndex(2)

    def _detail_all_requests(self):
        """到消息追踪的请求视图，只看这个模块；失败在前，搜索 tid 或方法、逐条视图往前翻页都能找到更早的"""
        module = self.detail_module
        # 消息页上次留下的类型、方法、只看失败、翻页都清掉，否则会以为这个模块没有历史
        self.pages = []
        self.set_scope(None)
        self.filter_text.clear()
        self.filter_kind.set_index(0)
        self.filter_method.setCurrentIndex(0)
        self.failures_only.setChecked(False)
        self._go(self.MESSAGES)
        self.filter_module.set_index(MODULES.index(module) + 1)
        self.view_mode.set_index(0)

    def _detail_to_messages(self):
        e = self.detail_trace.selected
        if e and e.tid:
            self._show_request(summarize(self.store.trace(e)))

    def _status_sn(self):
        """状态页只看当前设备：连着就是连接时的 SN，没连就是左侧填的"""
        return self.mqtt.sn if self.mqtt.client else self.sn.text().strip()

    def _status_block(self, e):
        """一类状态的完整字段；和上一条同类消息相比变了的字段标橙色并附旧值。云端汇总里拆出来的不比对"""
        def content(event):
            body = event.payload.get("info", event.payload.get("data", event.payload))
            return body if isinstance(body, dict) else {"值": body}

        body = content(e)
        fields = payload_diff({}, body)
        # 分路上报（info.index）只和同一路的上一条比，否则会拿另一路的值当“原值”
        before = None if getattr(e, "summary_of", None) else self.store.previous(e)
        for _ in range(20):
            if not before or content(before).get("index") == body.get("index"):
                break
            before = self.store.previous(before)
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
        events = self.store.query(method="detect_result_event", limit=5)
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
        if self.ssh:
            return f"rtsp://127.0.0.1:{self._tunnel('127.0.0.1', port)}/{name}"
        return f"rtsp://{address.rsplit(':', 1)[0]}:{port}/{name}"

    def _tunnel(self, host, port):
        """远程时经 SSH 转 host:port（从狗那边连）到本机，同一个目标只开一次，返回本机端口"""
        if (host, port) not in self.ssh_rtsp:
            self.ssh_rtsp[(host, port)] = self.ssh.forward(port, host)
        return self.ssh_rtsp[(host, port)]

    def _reachable(self, url):
        """本机实际拉的地址：远程时把地址里的主机端口（从狗上看的）换成经狗转过来的本机隧道，直连时原样用"""
        if not self.ssh:
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
        host, _, http = address.partition(":") if not self.ssh else ("127.0.0.1", "", "80")
        known = self.zlm_ports.get(address, {})
        rtsp = known.get("rtsp") or (self.media_cfg or {}).get("zlm", {}).get("rtsp_port") or 554
        ports = {"http": http or "80", "rtsp": rtsp, "rtmp": known.get("rtmp") or 1935}
        streams = (self.zlm_state or {}).get("streams") or {}
        return stream_presets(self.media_cfg, streams, host or "127.0.0.1", ports, self.sn.text().strip()) if host or self.media_cfg else []

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
        StreamWindow("拉流 · " + title, lambda: self._reachable(url), self).show()

    def _copy_selected(self):
        url = self._selected_url()
        if not url:
            return
        try:
            url = self._reachable(url)
        except (RuntimeError, OSError) as error:
            self.notify(str(error))
            return
        QApplication.clipboard().setText(url)
        self.notify("已复制 " + hide_password(url) + ("（经远程隧道的本机地址，断开后失效）" if self.ssh else ""))

    def _load_media_cfg(self):
        """后台经 SSH 读设备的拉流配置（STREAM_CFGS 里第一个读得到的）；已经远程连着就用那条，否则按填的账号临时连一次"""
        if self.media_cfg_busy or not self.ssh_user.text().strip():
            if not self.ssh_user.text().strip():
                self.notify("要先在左侧连接设置里填 SSH 账号")
            return
        self.media_cfg_busy = True

        def work():
            remote, own = None, False
            try:
                remote, own = self._ssh_for_logs()
                errors = []
                for path in STREAM_CFGS:
                    try:
                        self.inbox.put(("media_cfg", json.loads(remote.read_file(path)), path))
                        return
                    except (OSError, ValueError) as error:
                        errors.append(f"{path}：{error}")
                self.inbox.put(("media_cfg", None, "读不了设备的拉流配置；" + "；".join(errors)))
            except Exception as error:
                # 连不上、认证失败等，原样说明
                self.inbox.put(("media_cfg", None, f"读不了设备的拉流配置：{error}"))
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
        window = StreamWindow(f"拉流 · {name}" + (f" · {kind}" if kind else ""), lambda: self._play_url(name, info), self)
        window.show()

    def _copy_play_url(self, name, info):
        try:
            url = self._play_url(name, info)
        except RuntimeError as error:
            self.notify(str(error))
            return
        QApplication.clipboard().setText(url)
        self.notify("已复制 " + url + ("（经远程隧道的本机地址，断开后失效）" if self.ssh else ""))

    def _zlm_host_edited(self):
        text = self.zlm_host.text().strip()
        self.zlm_manual = text if text != self.zlm_auto else ""
        settings().setValue("zlm_host_manual", self.zlm_manual)

    def _zlm_follow(self):
        """直连模块连上后，没手填过地址就把 ZLM 地址换成这只狗的 IP:80，并清掉旧地址的查询结果。
        云端 GW 模式的 Broker 是云端接入点，不是狗上的 ZLM，不跟"""
        if self.mode != "direct":
            return
        # 拉流候选要用这只狗的 media.cfg；换了狗先清掉上一只的，免得列出别的狗的地址
        self.media_cfg, self.media_cfg_note = None, ""
        if self.ssh_user.text().strip():
            self._load_media_cfg()
        # 经远程时 ZLM 走它自己的那条隧道，不是 MQTT 的本机端口
        self.zlm_auto = f"127.0.0.1:{self.ssh_zlm}" if self.ssh else f"{self.mqtt.host}:80"
        if self.zlm_manual or self.zlm_host.text().strip() == self.zlm_auto:
            return
        self.zlm_host.setText(self.zlm_auto)
        self.zlm_state = None
        self._zlm_poll()

    def _zlm_poll(self):
        """后台问一次 ZLM 的 getMediaList；结果进 inbox，上一次还没回来就不重复发"""
        address, secret = self.zlm_host.text().strip(), self.zlm_secret.text().strip()
        if not address or not secret or self.zlm_busy or self.mode in {"sandbox", "offline"}:
            return
        self.zlm_busy = True
        self.zlm_timer.start()
        url = f"http://{address}/index/api/getMediaList?" + urllib.parse.urlencode({"secret": secret})
        known = address in self.zlm_ports
        # 经 FRP 隧道每次新连接要 1~3 s（实测），局域网 2.5 s 不够
        timeout = 10 if self.ssh else 2.5

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
            self.inbox.put(("zlm", state))

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
        if self.stack.currentIndex() != self.STATUS:
            self.zlm_timer.stop()
        self._refresh_status()

    def _logs_page(self):
        """导入后就在本页看：上面选批次和筛选，左边逐条日志，右边详情；只看导入的批次，不混实时和沙盒消息"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        # 设备日志经 SSH 直接取（远程走 FRP，局域网连狗的 IP），取下来留在本机一份并自动导入；实时跟随把新写的行逐条加进来
        self.live_follow = Switch("实时跟随")
        self.live_follow.toggled.connect(self._toggle_follow)
        self._page_header(layout, "日志排障", "导入后在这里直接看：搜索、筛模块或只看错误，点一行看全文、原文附近和对应请求",
                          self.live_follow, button("从设备取日志…", self._ask_device_logs),
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
        self.log_timer = QTimer(self)
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
        progress.addWidget(button("停止导入", self.import_cancel.set, "link"))
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
        self.log_detail.original.connect(self._original)
        self.log_detail.store = self.store
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

    # ---- 设备日志 ----

    def _ssh_for_logs(self):
        """后台线程里取一条 SSH：已经远程连着就用那条，否则按当前填写新开一条（用完由调用方关）"""
        if self.ssh and self.ssh.alive:
            return self.ssh, False
        return self._open_ssh(self.frp.isChecked())[0], True

    def _ask_device_logs(self, modules=(), since=None):
        """选模块和时间段，从设备的 /userdata/gw_logs 取日志；modules 不为空时只预先勾这些模块，since 为时间戳时预选“这之后写过的文件”"""
        if self.fetching or self.import_thread and self.import_thread.is_alive():
            self.notify("上一批还在取或导入，可先停止")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("从设备取日志")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 16)
        target = f"经 FRP 连 {self.sn.text().strip()}" if self.ssh or self.frp.isChecked() else f"连 {self.host.text().strip()}:22"
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
        self.import_cancel.clear()
        sn = self.sn.text().strip()
        base = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / "设备日志" / sn

        def work():
            remote, own, paths = None, False, []
            try:
                remote, own = self._ssh_for_logs()
                jobs = [(folder, name, size) for folder in folders
                        for name, size, _ in pick_files(remote.list_logs(folder), since)]
                total, done = sum(size for *_, size in jobs) or 1, 0
                for folder, name, size in jobs:
                    if self.import_cancel.is_set():
                        break
                    path = base / folder / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    remote.download(folder, name, str(path), lambda got: self.inbox.put(("fetch", (done + got, total, name))),
                                    self.import_cancel.is_set)
                    done += size
                    paths.append(str(path))
                self.inbox.put(("fetch_done", paths, f"从设备取到 {len(paths)} 个日志文件（{done / 1e6:.1f} MB），存在 {base}"))
            except Exception as error:
                # 代理、网络、认证、目录不存在都可能，原样告诉用户；已经取下来的照样导入
                self.inbox.put(("fetch_done", paths, f"从设备取日志失败：{error}"))
            finally:
                if own and remote:
                    remote.close()

        threading.Thread(target=work, daemon=True).start()
        self.import_bar.setValue(0)
        self.import_row.show()
        self.import_state.setText("正在连接设备…")
        self._go(self.LOGS)

    def _fetch_done(self, paths, note):
        self.fetching = False
        self.import_info.appendPlainText(time.strftime("%H:%M:%S  ") + note)
        self.notify(note)
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
        sn, session = self.sn.text().strip(), self.live_session

        def work():
            try:
                remote, own = self._ssh_for_logs()
                for folder in LOG_DIRS:
                    self.follow_stops.append(remote.follow(folder, lambda text, f=folder: self.inbox.put(("log_line", (f, text, sn, session)))))
                if own:
                    # 这条 SSH 只给实时跟随用，停的时候一起关
                    self.follow_stops.append(remote.close)
                self.inbox.put(("notice", "实时跟随已开始：设备上新写的日志会逐条出现在“实时跟随”这一批里"))
            except Exception as error:
                self.inbox.put(("follow_error", f"实时跟随没开起来：{error}"))

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
        self.store.add(event)
        if sum(self.live_lines.values()) == 1:
            self.store.flush()
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
            self.notify(f"已复制 {len(rows)} 行")

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
        self._background(work, "log_batches", newest)

    def _log_batches_done(self, result, newest):
        batches, expired = result
        # 写库只在主线程这条连接上做，后台读连接只负责挑
        for sn, session in expired:
            self.store.drop_session(sn, session)
        current = self.log_batch.currentData()
        self.log_batch.blockSignals(True)
        self.log_batch.clear()
        # 第一项是这台设备导入过的全部日志，按时间段查故障时不用先猜在哪一批；从请求跳来时是那个请求的设备
        want, self.log_want = self.log_want, None
        sn = want[0] if want else self.sn.text().strip()
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
        self._background(work, "log_rows", self.log_version)

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
        self._go(self.LOGS)
        if loaded:
            self._log_batches()

    def _nearby_back(self):
        """回到跳来之前的页面；那边的列表、选中和证据链都没动过"""
        self.nearby_bar.hide()
        self._go(self.nearby_page)

    def _log_inspect(self, index):
        row_id, e, _ = self.log_model.rows[index.row()]
        self.log_selected = row_id
        self.log_detail.show_trace(e, self.store.trace(e))

    def _suite_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._page_header(layout, "回归测试", "内置九组沙盒用例覆盖六个模块；也可以加载控制页保存的 YAML 用例",
                          button("加载 YAML 用例", self._load_case), button("运行内置回归", self._run_suite, "primary"))
        result, rv = card(8, 18)
        self.suite_info = QTextBrowser()
        self.suite_info.setPlaceholderText("点右上角“运行内置回归”")
        rv.addWidget(self.suite_info)
        layout.addWidget(result, 1)
        return page

    def _coverage_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        counts = "  ·  ".join(f"{m.upper()} {sum(x['module'] == m for x in self.catalog)}" for m in MODULES)
        self._page_header(layout, "协议覆盖", f"共 {len(self.catalog)} 条：{counts}　　双击控制指令可直接打开")
        search = QLineEdit()
        search.setObjectName("search")
        search.setPlaceholderText("搜索方法、模块或来源")
        layout.addWidget(search)
        layout.addSpacing(4)
        box, bv = card(0, 6)
        table = QTableWidget(len(self.catalog), 5)
        table.setHorizontalHeaderLabels(["模块", "方法", "类型", "来源", "待核差异"])
        table.setItemDelegate(PillDelegate(table))
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setShowGrid(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(36)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for row, item in enumerate(self.catalog):
            for column, value in enumerate((item["module"].upper(), item["method"], item["category"],
                                            item["source"], item["unverified"])):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.UserRole, MODULE_COLORS.get(item["module"], GRAY))
                table.setItem(row, column, cell)

        def apply(text):
            needle = text.lower()
            for row, item in enumerate(self.catalog):
                haystack = " ".join((item["module"], item["method"], item["category"], item["source"])).lower()
                table.setRowHidden(row, bool(needle) and needle not in haystack)

        search.textChanged.connect(apply)
        table.cellDoubleClicked.connect(lambda row, _: self._open_command(self.catalog[row]["method"]))
        bv.addWidget(table)
        layout.addWidget(box, 1)
        return page

    # ---- 页面与状态 ----

    def _page(self, index):
        self._slide_to(index)
        if index == self.STATUS:
            self._refresh_status()
            self._zlm_poll()
        elif index == self.MESSAGES:
            self._refresh()
        elif index == self.LOGS:
            self._show_store_size()
            if not self.log_loaded:
                self._log_batches()
        elif index == self.REMOTE:
            self.remote.setFocus()
        self.observer.setVisible(index in self.OBSERVED)
        # 窗口还没显示时不加载，showEvent 再补；测试里窗口不显示，也就不加载 OpenGL
        if index in self.OBSERVED and self.isVisible():
            self._ensure_viewer()

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

    def _go(self, index):
        self.nav.setCurrentRow(index)

    def notify(self, text):
        self.toast.pop(text)

    def _set_link(self, text, color):
        self.link.setText(text)
        self.link_dot.setStyleSheet(f"background:{color};border-radius:4px")
        self.connect_btn.setText("断开" if self.mqtt.client else "连接")
        self.connect_btn.setObjectName("danger" if self.mqtt.client else "primary")
        self.connect_btn.style().polish(self.connect_btn)
        self._device_card()

    def _connection_hint(self):
        """连接中修改了 Broker/SN 时提示仍按旧连接收发，重连后才切换"""
        if not self.mqtt.connected:
            return
        if self.ssh:
            active = f"已经远程连接 {self.ssh.host} · {self.mqtt.sn}"
            edited = self.sn.text().strip() != self.mqtt.sn
        else:
            active = f"已连接 {self.mqtt.host} · {self.mqtt.sn}"
            edited = (self.host.text().strip(), self.sn.text().strip()) != (self.mqtt.host, self.mqtt.sn)
        self._set_link(active + ("\n左侧修改在重连后生效" if edited else ""), GREEN)

    def _mode_changed(self):
        mode = MODES[self.mode_box.currentText()]
        # 沙盒和离线不接实时数据：断开前还按原模式发停止，否则停止会被离线拦下或被沙盒吞掉
        if mode in {"sandbox", "offline"}:
            if self.mqtt.client:
                self._connect()
            self.zlm_timer.stop()
        self.mode = mode
        self.ssh_row.setVisible(mode == "direct")
        sandbox = self.mode == "sandbox"
        self.scenario_row.setVisible(sandbox)
        self.send_btn.setEnabled(self.mode != "offline")
        self.send_btn.setText("模拟发送" if sandbox else "发送")
        if not self.mqtt.client:
            self._set_link({"sandbox": "沙盒模式，不联网", "offline": "离线分析，不发送"}.get(self.mode, "未连接"), GRAY)
        self.drafts.clear()
        self._template()
        if self.mode == "offline":
            self._go(self.LOGS)
            self.notify("离线分析只看本地历史：在这里导入日志文件")

    def _quick_pick(self):
        self._go(self.CONTROL)
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
        ordered = sorted(self.catalog, key=lambda item: MODULES.index(item["module"]) if item["module"] in MODULES else len(MODULES))
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

    def _select_command(self, current, previous=None):
        if current and current.data(Qt.UserRole):
            if self.selected:
                self.drafts[self.selected["method"]] = (self.topic.text(), self.editor.toPlainText())
            self.selected = current.data(Qt.UserRole)
            self._template(self.drafts.pop(self.selected["method"], None))

    def _open_command(self, method):
        self.command_search.clear()
        for row in range(self.commands.count()):
            item = self.commands.item(row).data(Qt.UserRole)
            if item and item["method"] == method:
                self.commands.setCurrentRow(row)
                self._go(self.CONTROL)
                return
        self.notify(method + " 是上报类条目，不在控制列表")

    def _topic_for(self, item):
        """直连时按 GW 对云端模板的路由选模块 Topic，例如喊话器目录归 MEDIA，GW 实际转给 audio"""
        # pcd_switch 这类本地指令云端没有，哪种模式都发本地 Topic
        if item.get("local_only"):
            return f"local/robotdog/{item['module']}/command"
        if self.mode in {"sandbox", "gw", "offline"}:
            return f"tta/robotdog/{self.sn.text().strip()}/services"
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
        try:
            payload = json.loads(self.editor.toPlainText())
        except ValueError:
            self.form_note.setText("JSON 暂时无效，表单暂停同步；照原文仍可发送")
            self.form_area.hide()
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            self.form_note.setText("没有 data 对象，直接编辑下方 JSON")
            self.form_area.hide()
            return
        self.form_note.setText("改这里会同步到 JSON；列表和对象请在 JSON 里改" if data else "data 为空")
        self.form_area.setVisible(bool(data))
        for index, (key, value) in enumerate(data.items()):
            row, column = divmod(index, 2)
            self.form_grid.addWidget(label(key, "caption"), row, column * 2)
            if isinstance(value, bool):
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
        """只改 data 里这一个字段，其他字段（包括模板里没有的）原样保留"""
        try:
            payload = json.loads(self.editor.toPlainText())
        except ValueError:
            return
        payload["data"][key] = value
        self.form_syncing = True
        cursor = self.editor.textCursor().position()
        self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
        cursor_obj = self.editor.textCursor()
        cursor_obj.setPosition(min(cursor, len(self.editor.toPlainText())))
        self.editor.setTextCursor(cursor_obj)
        self.form_syncing = False

    def _new_tid(self):
        """只换 tid、ts（有 bid 也换），参数和其他字段保持；用于同样参数再发一次"""
        try:
            payload = json.loads(self.editor.toPlainText())
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            self.notify("不是 JSON 对象，没法换 tid，原文保持不变")
            return
        refresh_ids(payload)
        self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

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
        self.contract.setToolTip(self.selected["label"].split(" · ")[0] + "　·　" + self.selected["source"])
        if draft:
            self.editor.setPlainText(draft[1])
            self.topic.setText(draft[0])
        else:
            payload = make_command(self.selected)
            if self.mode == "direct":
                payload = local_payload(payload)
            self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            self._update_topic()
        self._rebuild_form()

    # ---- 连接与发送 ----

    def _frp_toggled(self, on):
        # 经远程时 Broker 就是狗自己，地址由整机 SN 定，不用填
        self.host.setEnabled(not on)
        self.port.setEnabled(not on)

    def _connect(self):
        if self.ssh_connecting:
            self.notify("正在登录设备，稍等")
            return
        if self.mqtt.client:
            # 断开前先发停止，断开后就发不出了
            self.remote.halt()
            self.mqtt.close()
            self._close_ssh()
            self._set_link("已断开", GRAY)
            return
        if self.mode in {"sandbox", "offline"}:
            self.notify("先在上面选择“云端 → 真实 GW”或“直连模块”")
            return
        # 记住这次的连接（含密码，本机自用），下次启动自动填上
        settings().setValue("connection", json.dumps({
            "mode": self.mode, "host": self.host.text().strip(), "port": self.port.value(), "sn": self.sn.text().strip(),
            "user": self.user.text(), "password": self.password.text(), "tls": self.tls.isChecked(),
            "frp": self.frp.isChecked()}))
        if self.mode == "direct" and self.frp.isChecked():
            self._connect_frp()
            return
        self.mqtt.connect(self.host.text().strip(), self.port.value(), self.sn.text().strip(),
                          self.user.text(), self.password.text(), self.tls.isChecked())
        self.scene.reset_telemetry()
        self.scene.clear_cloud()
        self._set_link("连接中…", ORANGE)

    def _open_ssh(self, via_frp):
        """在后台线程里调用：按当前填写登录设备的 SSH；远程走 FRP，局域网连 Broker 地址的 22 端口"""
        sn = self.sn.text().strip()
        host = self.host.text().strip()
        keys = json.loads(settings().value("ssh_host_keys", "{}"))
        name = sn if via_frp else host
        return Remote(sn, host, self.ssh_user.text().strip(), self.ssh_password.text(), keys.get(name, ""), via_frp), name

    def _connect_frp(self):
        """经 FRP 登录狗、把 1883 和 80 转到本机，再按直连的方式连 Broker；登录在后台，结果进 inbox"""
        self.ssh_connecting = True
        self._set_link(f"正在经 FRP 登录 {self.sn.text().strip()}…", ORANGE)

        def work():
            try:
                remote, name = self._open_ssh(True)
                host, port = broker_target(remote)
                self.inbox.put(("ssh_ready", remote, (name, remote.forward(port, host), remote.forward(80), f"{host}:{port}")))
            except Exception as error:
                # 代理、网络、认证出错的类型各不相同，原样告诉用户
                self.inbox.put(("ssh_error", f"远程登录失败：{error}"))

        threading.Thread(target=work, daemon=True).start()

    def _ssh_ready(self, remote, name, mqtt_port, zlm_port, broker):
        self.ssh_connecting = False
        keys = json.loads(settings().value("ssh_host_keys", "{}"))
        keys[name] = remote.host_key
        settings().setValue("ssh_host_keys", json.dumps(keys))
        self.ssh, self.ssh_zlm = remote, zlm_port
        # 设备身份仍是整机 SN；127.0.0.1 只是隧道的本机入口
        self.mqtt.connect("127.0.0.1", mqtt_port, self.sn.text().strip(), self.user.text(), self.password.text(), False)
        self.scene.reset_telemetry()
        self._set_link(f"已登录设备，正在连 Broker {broker}…", ORANGE)

    def _close_ssh(self):
        self._stop_follow()
        if self.ssh:
            self.ssh.close()
        self.ssh, self.ssh_zlm, self.ssh_rtsp = None, 0, {}

    def _send(self):
        """发送编辑框内容；非对象或非法 JSON 照原文发出，沙盒里只记录不模拟"""
        text, topic = self.editor.toPlainText(), self.topic.text().strip()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        record = payload if isinstance(payload, dict) else {}
        sent = self._emit(payload, text, topic, self.selected["module"])
        self._flash_send("✓ 已发出" if sent else "✗ 没发出")
        if not sent:
            return
        self.watch, self.watch_size = sent, 0
        self.result_title.setText(f"本次结果 · {self.selected['title']}")
        self._remember(self.selected)
        self._refresh()
        if not record:
            self.notify("不是 JSON 对象，无法按对象模拟，已记录原文" if self.mode == "sandbox" else "已按原文发送")
        else:
            text = "沙盒已模拟完整链路" if self.mode == "sandbox" else "已发送，等待 ACK"
            self.notify(text + ("；" + self.demo_note if self.demo_note else ""))

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
        QTimer.singleShot(1200, lambda: self.send_btn.setText("模拟发送" if self.mode == "sandbox" else "发送"))

    def _emit(self, payload, text, topic, module):
        """按当前模式发出并入库，返回代表本次发送的事件；发布失败提示原因并返回 None"""
        record = payload if isinstance(payload, dict) else {}
        if self.mode == "offline":
            self.notify("离线分析模式不发送")
            return None
        if self.mode == "sandbox":
            sn = self.sn.text().strip()
            if record:
                events = simulate(payload, sn, self.session, SCENARIOS[self.scenario.index()][1])
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
                         infer_module(topic, wire) if isinstance(wire, dict) else module, qos=0, raw=text)
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
        sent = self._emit(payload, json.dumps(payload, ensure_ascii=False), self._topic_for(item), item["module"])
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
            chip.setToolTip(f"{self.quick.method} · {status}" + (f"\n{detail}" if detail else "") + "\n点一下到消息页看证据链")
            marker.setStyleSheet(f"background:{color};border-radius:5px")
            caption.setText(text)

    def _open_watch(self):
        if not self.watch:
            self.notify("还没有发送过指令")
            return
        self.filter_text.setText(self.watch.tid)
        self._go(self.MESSAGES)

    def _demo(self):
        payload = make_command(find("take_photo"))
        for event in simulate(payload, "TEST_DOG_001", "demo-" + self.session, "failure"):
            self.store.add(event)
        self.store.flush()
        self._go(self.MESSAGES)
        self._refresh()
        if self.views.currentIndex() == 0:
            self.request_table.selectRow(0)
            self._inspect_request(self.request_model.index(0, 0))
        else:
            self.table.selectRow(0)
            self._inspect(self.model.index(0, 0))

    # ---- 刷新 ----

    def _show_store_size(self):
        size = self.store.size()
        self.store_size.setText(f"本地 {self.store.count()} 条" + (f" · {size / 1e6:.0f} MB" if size else ""))

    def _clear_store(self):
        """清空库是唯一不可恢复的操作，保留一次确认"""
        if self.import_thread and self.import_thread.is_alive():
            self.notify("导入进行中，先停止再清空")
            return
        answer = QMessageBox.question(self, "清空本地数据", f"删除本地保存的 {self.store.count()} 条消息？\n"
                                      "导入的原始日志文件不受影响；需要留存请先导出诊断包")
        if answer != QMessageBox.Yes:
            return
        self.store.clear()
        self.current_event, self.current_request, self.scope, self.watch, self.quick = None, None, None, None, None
        self._first_page()
        self._show_store_size()
        self._log_batches()
        self.notify("本地数据已清空")

    def _import_progress(self, done, total, count, started):
        rate = done / max(time.monotonic() - started, 0.001)
        left = (total - done) / rate if rate else 0
        self.import_bar.setValue(int(done * 1000 / total) if total else 0)
        self.import_state.setText(f"{done / 1e6:.0f} / {total / 1e6:.0f} MB　{count} 条　{rate / 1e6:.1f} MB/s　约剩 {left:.0f} 秒")

    def _refresh(self):
        self.refresh_due, self.refreshed = False, time.monotonic()
        if self.stack.currentIndex() != self.MESSAGES:
            # 消息页不可见时不重建它的表格（切过去时 _page 会刷一次），只更新控制结果和状态页
            self._refresh_tail()
            return
        module_index = self.filter_module.index()
        module = "" if module_index == 0 else "unknown" if module_index > len(MODULES) else MODULES[module_index - 1]
        kind = "" if self.filter_kind.index() == 0 else ["", "指令", "ACK", "状态", "事件", "日志"][self.filter_kind.index()]
        method = "" if self.filter_method.currentIndex() == 0 else self.filter_method.currentText()
        before, text = (self.pages[-1] if self.pages else None), self.filter_text.text()
        wanted = ((module, method, kind, text, before, self._requests_visible())
                  if not self.scope and text and self.store.on_disk else None)
        # 条件变了（含清空搜索、进入请求作用域、换页、换视图），在途的旧搜索作废，迟到的结果不再落表
        if self.search and self.search["params"] != wanted:
            self._cancel_search()
        if self.scope:
            # 请求作用域下仍可叠加其他筛选，只是不再跨设备/会话
            needle = self.filter_text.text().lower()
            rows = [(0, e) for e in reversed(self.store.groups([self.scope]).get(self.scope, []))
                    if (not module or e.module == module) and (not method or e.method == method)
                    and (not kind or e.kind == kind)
                    and (not needle or needle in json.dumps(e.clean(), ensure_ascii=False).lower())]
            self._show_rows(rows, None)
        else:
            if wanted:
                # 文本搜索在大库上要全表扫描，放到后台只读连接里做，结果回来再更新表格
                self._search_async(wanted)
            else:
                rows = self.store.query(module, method, kind, text, PAGE_SIZE, before)
                # 请求列表只在有新的指令/ACK 等或筛选变了时重算；实时状态再多也不重算旧请求
                drawn = (module, method, kind, text, self.failures_only.isChecked(), self.store.request_version)
                request_rows = None
                if self._requests_visible() and drawn != self.requests_drawn:
                    self.requests_drawn = drawn
                    request_rows = self.store.query(module, method, kind or REQUEST_KINDS, text, 3000)
                self._show_rows(rows, request_rows)
        if self.views.currentIndex() == 2 and self.stack.currentIndex() == self.MESSAGES:
            self._refresh_chains()
        self._refresh_tail()

    def _requests_visible(self):
        return self.views.currentIndex() == 0 and self.stack.currentIndex() == self.MESSAGES

    def _show_rows(self, rows, request_rows):
        """把查询结果放进逐条表和请求表；request_rows 为 None 时请求表不动"""
        # 翻页按未过滤的整页判断还有没有更早的
        self._update_pager(rows)
        if self.failures_only.isChecked():
            rows = [(i, e) for i, e in rows if diagnose(e).status in FAILED | {"契约待核"}]
        keep = self.current_event.event_id if self.current_event else None
        if request_rows is not None:
            self._refresh_requests(request_rows)
        self.model.replace(rows)
        # 重建模型会清掉选中行，按事件 ID 恢复，避免实时消息刷新时选择跳走
        for row, (_, e, _) in enumerate(self.model.rows):
            if e.event_id == keep:
                self.table.selectRow(row)
                break

    def _search_async(self, params):
        if self.search and self.search["params"] == params:
            # 同样的条件已在查，不因实时刷新反复重来
            return
        self._cancel_search()
        reader = self.store.reader()
        token = object()
        self.search = {"params": params, "reader": reader, "token": token}
        self.search_hint.show()
        module, method, kind, text, before, want_requests = params

        def work():
            try:
                rows = reader.query(module, method, kind, text, PAGE_SIZE, before)
                request_rows = reader.query(module, method, kind or REQUEST_KINDS, text, 3000) if want_requests else None
                self.inbox.put(("search", token, rows, request_rows))
            except sqlite3.OperationalError:
                # 被新的搜索打断
                pass
            finally:
                reader.db.close()

        threading.Thread(target=work, daemon=True).start()

    def _cancel_search(self):
        if not self.search:
            return
        try:
            self.search["reader"].db.interrupt()
        except sqlite3.ProgrammingError:
            # 上一次已经查完并关闭了连接
            pass
        self.search = None
        self.search_hint.hide()

    def _search_done(self, token, rows, request_rows):
        if not self.search or self.search["token"] is not token:
            return
        self.search = None
        self.search_hint.hide()
        self._show_rows(rows, request_rows)

    def _refresh_chains(self):
        """检测链路视图：取最近的检测相关消息按关联键并链，失败的排前"""
        chains = detection_chains([e for _, e in reversed(self.store.query_methods(DETECTION_METHODS, 5000))])
        keep = self.current_chain
        self.chain_model.replace(chains[:500])
        for row, chain in enumerate(self.chain_model.rows):
            if chain["events"][0].event_id == keep:
                self.chain_table.selectRow(row)
                break

    def _copy_selection(self):
        """Ctrl+C：逐条视图复制所选消息的完整 JSON；请求和检测链路复制结论与 tid，便于贴到工单"""
        view = self.views.currentIndex()
        if view == 1:
            rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
            text = "\n".join(json.dumps(self.model.rows[r][1].clean(), ensure_ascii=False) for r in rows)
        elif view == 0:
            rows = sorted({i.row() for i in self.request_table.selectionModel().selectedRows()})
            text = "\n".join(f"{r.events[0].sn}\t{r.method}\t{r.status}\t{r.summary}\ttid={r.events[0].tid}"
                             for r in (self.request_model.rows[i] for i in rows))
        else:
            rows = sorted({i.row() for i in self.chain_table.selectionModel().selectedRows()})
            text = "\n".join(f"{c['file']}\t{c['verdict']}" for c in (self.chain_model.rows[i] for i in rows))
        if text:
            QApplication.clipboard().setText(text)
            self.notify(f"已复制 {len(rows)} 行")

    def _inspect_chain(self, index):
        if not index.isValid():
            return
        chain = self.chain_model.rows[index.row()]
        self.current_chain = chain["events"][0].event_id
        stage = next((s for s in DETECTION_STAGES if chain["stages"][s][0] == "失败"), None)
        focus = chain["stages"][stage][2][-1] if stage else chain["events"][-1]
        color = RED if chain["failed"] else GREEN if chain["verdict"] == "全链路已观测" else ORANGE
        detail = "　".join(f"{s} {chain['stages'][s][0]}" for s in DETECTION_STAGES)
        self.details.show_trace(focus, chain["events"], headline=(chain["verdict"], color, detail,
                                "“未观测”只表示没看到对应消息，可能是没订阅到或日志里没有，不等于没发生"))
        self.note_btn.setEnabled(False)

    def _demo_detection(self):
        session = "demo-detect-" + self.session
        for stop, fail in (("云端事件", ""), ("FSVR 登记", ""), ("上传终态", "上传终态")):
            for event in simulate_detection("TEST_DOG_001", session, stop, fail):
                self.store.add(event)
        self.store.flush()
        self._refresh()

    def _refresh_tail(self):
        if self.watch:
            trace = self.store.trace(self.watch)
            if len(trace) != self.watch_size:
                self.watch_size = len(trace)
                # 新消息到了还停在用户点开的那条上
                chosen = self.result.selected
                self.result.show_trace(next((e for e in trace if chosen and e.event_id == chosen.event_id), self.watch), trace)
        if self.quick:
            self._quick_status()
        if self.stack.currentIndex() == self.STATUS:
            # 没有新状态、设备没换就不重读状态；详情里的指令与回复由它自己按模块消息数判断
            if (self.store.status_version, self._status_sn()) != self.status_drawn:
                self._refresh_status()
            elif self.detail_module:
                self._render_detail()

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
        self.status_drawn = (self.store.status_version, self._status_sn())
        before, self.status_seen = self.status_seen, {}
        latest = {}
        for _, e in self.store.query(kind="状态", limit=3000, sn=self._status_sn(), live=True):
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

    def _update_pager(self, rows):
        self.pager.setVisible(self.views.currentIndex() == 1 and not self.scope)
        self.page_label.setText(f"第 {len(self.pages) + 1} 页 · 本页 {len(rows)} 条" +
                                ("　（最新一页随新消息刷新）" if not self.pages else ""))
        self.newer_btn.setEnabled(bool(self.pages))
        self.older_btn.setEnabled(len(rows) >= PAGE_SIZE)
        self.page_rows = rows

    def _older_page(self):
        self.pages.append(min(i for i, _ in self.page_rows))
        self._refresh()

    def _newer_page(self):
        self.pages.pop()
        self._refresh()

    def _first_page(self):
        self.pages = []
        self._refresh()

    def _refresh_requests(self, rows):
        found = requests(self.store, rows)
        if self.failures_only.isChecked():
            found = [r for r in found if r.status in FAILED | {"契约待核", "无 ACK"}]
        keep = self.current_request
        self.request_model.noted = set(self.store.notes())
        self.request_model.replace(found[:500])
        for row, r in enumerate(self.request_model.rows):
            if r.key == keep:
                self.request_table.selectRow(row)
                break

    def _view_changed(self, index):
        fade_to(self.views, index)
        self.scope_chip.setVisible(bool(self.scope) and index == 1)
        self.chain_demo.setVisible(index == 2)
        self._refresh()

    def _inspect_request(self, index):
        if not index.isValid():
            return
        self._trace_request(self.request_model.rows[index.row()])

    def _trace_request(self, r):
        self.current_request = r.key
        # 结论卡优先落在失败那条，否则落在最后一条回复
        focus = next((e for e in r.events if diagnose(e).status in FAILED), r.events[-1])
        self.details.show_trace(focus, r.events, self.store.note(r.key))
        self.note_btn.setEnabled(True)

    def _details_switched(self, key):
        """证据链换到别的来源那组时，备注写给那一组"""
        self.current_request = key
        self.note_btn.setEnabled(True)

    def _edit_note(self):
        """给当前请求写问题备注：预期、实际、备注、是否已复现；绑定完整 (设备, 会话, tid)"""
        key = self.current_request
        if not key:
            return
        note = self.store.note(key) or {}
        dialog = QDialog(self)
        dialog.setWindowTitle("问题备注")
        dialog.setMinimumWidth(480)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.addWidget(label("问题备注", "heading"))
        layout.addWidget(label(f"{key[0]} · tid {key[2]}", "caption"))
        fields = {}
        for caption, name in (("预期", "expected"), ("实际", "actual"), ("备注", "remark")):
            layout.addWidget(label(caption, "groupHeader"))
            edit = QPlainTextEdit(note.get(name, ""))
            edit.setObjectName("code")
            edit.setFixedHeight(64 if name != "remark" else 96)
            layout.addWidget(edit)
            fields[name] = edit
        reproduced = Switch("已复现")
        reproduced.setChecked(bool(note.get("reproduced")))
        layout.addWidget(reproduced)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", dialog.reject))
        actions.addWidget(button("保存", dialog.accept, "primary"))
        layout.addLayout(actions)
        self.note_dialog = dialog
        if dialog.exec() != QDialog.Accepted:
            return
        self.store.set_note(key, {**{k: w.toPlainText().strip() for k, w in fields.items()},
                                  "reproduced": reproduced.isChecked()})
        self._refresh()
        row = next((i for i, r in enumerate(self.request_model.rows) if r.key == key), None)
        if row is not None and not self.details.origin:
            self._inspect_request(self.request_model.index(row, 0))
        else:
            self.details.refresh()
        self.notify("备注已保存，会随诊断包导出")

    def _open_request(self, index):
        """双击请求切到逐条视图，只看这次请求（同设备、同会话、同 tid）的原始消息"""
        self.set_scope(self.request_model.rows[index.row()].key)
        self.view_mode.set_index(1)

    def _show_request(self, r):
        """从别处跳到逐条视图，只看这次请求的消息并显示证据链；按 tid 搜索会混进别的设备或会话里同 tid 的请求"""
        self.filter_text.clear()
        self._go(self.MESSAGES)
        self.view_mode.set_index(1)
        self.set_scope(r.key)
        self._trace_request(r)

    def set_scope(self, key):
        """限定逐条视图只显示一个请求；None 回到全部消息"""
        self.scope = key
        if key:
            sn, session, tid = key
            self.scope_chip.setText(f"仅此请求 · {sn} · {tid[:8]}…  ✕")
        self.scope_chip.setVisible(bool(key))
        self._refresh()

    def _inspect(self, index):
        if not index.isValid():
            return
        self.current_event = self.model.rows[index.row()][1]
        self.details.show_trace(self.current_event, self.store.trace(self.current_event))

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

    # ---- 日志导入 ----

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.start_import(paths)

    def _import(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "导入日志", "", "日志 (*.log *.txt *.jsonl);;所有文件 (*)")
        if paths:
            self.start_import(paths)

    def start_import(self, paths):
        if self.import_thread and self.import_thread.is_alive():
            self.notify("上一批还在导入，可先停止")
            return
        self.import_cancel.clear()
        session, sn = "import-" + str(uuid.uuid4()), self.sn.text()

        total = sum(Path(path).stat().st_size for path in paths)

        def offer(item):
            # 队列满时等主线程消费，取消后立即放弃，不会卡住线程
            while not self.import_cancel.is_set():
                try:
                    self.inbox.put(item, timeout=0.2)
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
                    for event in import_log(path, sn, session, self.import_cancel.is_set, stats):
                        batch.append(Store.row(event))
                        if len(batch) >= IMPORT_BATCH:
                            count += len(batch)
                            if not offer(("rows", batch, (done + stats["bytes"], total, count, started))):
                                break
                            batch = []
                    if batch and not self.import_cancel.is_set():
                        count += len(batch)
                        offer(("rows", batch, (done + stats["bytes"], total, count, started)))
                    done += stats["bytes"]
                    if self.import_cancel.is_set():
                        break
                    bad = (f"；{stats['undecoded']} 行有无法按 {stats['encoding']} 解码的字节（行 "
                           f"{', '.join(map(str, stats['undecoded_lines']))}{' 等' if stats['undecoded'] > 10 else ''}），"
                           "已用替换字符保留，原文位置不变" if stats["undecoded"] else "")
                    self.inbox.put(("import", f"{Path(path).name}：编码 {stats['encoding']}，sha256 "
                                              f"{stats['sha256'][:16]}…{bad}"))
                cost = time.monotonic() - started
                verdict = "已停止，已导入的保留" if self.import_cancel.is_set() else "导入完成"
                self.inbox.put(("import", f"{verdict}：{count} 条，{done / 1e6:.1f} MB，用时 {cost:.1f} 秒"))
            except Exception as error:
                self.inbox.put(("import", "导入失败：" + str(error)))
            self.inbox.put(("import_done", None))

        self.import_thread = threading.Thread(target=work, daemon=True)
        self.import_thread.start()
        self.import_info.appendPlainText(time.strftime("%H:%M:%S  开始导入\n") + "\n".join(paths))
        self.import_bar.setValue(0)
        self.import_row.show()
        self.import_state.setText(f"准备读取 {total / 1e6:.1f} MB")
        self._go(self.LOGS)

    def _drain(self):
        now = time.monotonic()
        if now - self.rate_at >= 1:
            self.rate_label.setText(f"{self.rate_count / (now - self.rate_at):.0f} 条/秒" if self.mqtt.connected else "")
            self.rate_count, self.rate_at = 0, now
        # OSD 停发后没有新消息也没有动画，这里顺带把“实时”标记按时效退掉
        if self.scene.live != self.shown_live:
            self._device_changed()
        if self.stack.currentIndex() == self.STATUS and time.monotonic() - self.status_aged >= 1:
            self._status_ages()
        if self.scene.cloud and now - self.cloud_aged >= 1:
            self.cloud_aged = now
            self._cloud_status()
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
                self._import_progress(*extra[0])
                changed = True
            elif kind == "search":
                self._search_done(value, *extra)
            elif kind == "model":
                self._import_done(value, extra[0])
            elif kind == "model_error":
                self._import_failed(value, extra[0])
            elif kind == "preview":
                if self.preview and self.preview["token"] is value and self.export_dialog:
                    self._fill_preview(self.export_dialog, extra[0])
            elif kind == "replay_sessions":
                self._replay_menu(value)
            elif kind == "replay_frames":
                self._replay_start(value, *extra)
            elif kind == "zlm":
                self._zlm_done(value)
            elif kind == "media_cfg":
                self._media_cfg_done(value, *extra)
            elif kind == "notice":
                self.notify(value)
            elif kind == "export_done":
                self.export_running = False
                self.notify(value)
            elif kind == "import_done":
                self.import_row.hide()
                self.import_state.setText("")
                self._show_store_size()
                # 后台用另一个只读连接查批次，先把这一批最后几行提交，否则可能查不到
                self.store.flush()
                self._log_batches(newest=True)
            elif kind == "log_batches":
                self._log_batches_done(value, *extra)
            elif kind == "log_rows":
                self._log_rows_done(value, *extra)
            elif kind == "import":
                self.import_info.appendPlainText(time.strftime("%H:%M:%S  ") + value)
                self.notify(value)
            elif kind == "connection":
                if self.mqtt.connected:
                    self._connection_hint()
                    self._zlm_follow()
                else:
                    self._set_link(str(value), RED)
            elif kind == "disconnect":
                self.remote.halt()
                self.mqtt.close()
                self._close_ssh()
                self._set_link(str(value), RED)
            elif kind == "ssh_ready":
                self._ssh_ready(value, *extra[0])
            elif kind == "fetch":
                done, total, name = value
                self.import_bar.setValue(int(done * 1000 / total))
                self.import_state.setText(f"从设备取 {name}　{done / 1e6:.1f} / {total / 1e6:.1f} MB")
            elif kind == "fetch_done":
                self._fetch_done(value, *extra)
            elif kind == "log_line":
                self._live_line(*value)
                changed = True
            elif kind == "follow_error":
                self._stop_follow()
                self.notify(value)
            elif kind == "ssh_error":
                self.ssh_connecting = False
                self._set_link(value, RED)
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

    # ---- 导出与用例 ----

    def _fill_preview(self, dialog, info):
        dialog.loading.hide()
        devices = "、".join(info["devices"][:6]) + ("…" if len(info["devices"]) > 6 else "") or "无"
        sources = "、".join(f"{k or '未知'} {v}" for k, v in info["sessions"].items()) or "无"
        for caption, text in (("消息", f"{info['events']} 条，全部写入"), ("请求", f"{info['requests']} 个"),
                              ("设备", devices), ("会话来源", sources),
                              ("已脱敏", f"{info['redacted']} 条消息含 [REDACTED]，入库时已处理，包里不会有原值"),
                              ("问题备注", f"{info['notes']} 条"),
                              ("不包含", "导入的原始日志文件、MQTT 密码")):
            row = QHBoxLayout()
            name = label(caption, "caption")
            name.setFixedWidth(64)
            row.addWidget(name)
            row.addWidget(label(text, wrap=True), 1)
            dialog.body.addLayout(row)
        if info["redacted_topics"]:
            dialog.body.addWidget(label("脱敏样例 Topic：" + "，".join(t or "无 Topic" for t in info["redacted_topics"]), "caption", True))
        dialog.confirm.setEnabled(True)
        dialog.info = info

    def _export(self):
        """先给出导出概览（内容全部来自已脱敏的库），确认后再选保存位置

        库文件上的概览统计要扫全表，在后台只读连接里做，对话框先显示“正在统计…”；取消即中断统计
        """
        dialog = QDialog(self)
        self.export_dialog = dialog
        dialog.setWindowTitle("导出诊断包")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(8)
        layout.addWidget(label("导出诊断包", "heading"))
        dialog.body = QVBoxLayout()
        dialog.body.setSpacing(8)
        dialog.loading = label("正在统计本地数据…", "caption")
        dialog.body.addWidget(dialog.loading)
        layout.addLayout(dialog.body)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", dialog.reject))
        dialog.confirm = button("选择位置并导出", dialog.accept, "primary")
        dialog.confirm.setEnabled(False)
        actions.addWidget(dialog.confirm)
        layout.addSpacing(6)
        layout.addLayout(actions)
        if self.store.on_disk:
            self.store.flush()
            reader, token = self.store.reader(), object()
            self.preview = {"reader": reader, "token": token}

            def work():
                try:
                    self.inbox.put(("preview", token, export_preview(reader)))
                except sqlite3.OperationalError:
                    # 用户取消，统计被中断
                    pass
                finally:
                    reader.db.close()

            threading.Thread(target=work, daemon=True).start()
        else:
            self._fill_preview(dialog, export_preview(self.store))
        accepted = dialog.exec() == QDialog.Accepted
        if self.preview:
            try:
                self.preview["reader"].db.interrupt()
            except sqlite3.ProgrammingError:
                pass
        self.preview, self.export_dialog = None, None
        if not accepted:
            return
        name = time.strftime("dog-debug-%Y%m%d-%H%M%S.zip")
        path, _ = QFileDialog.getSaveFileName(self, "导出诊断包", name, "诊断包 (*.zip)")
        if not path:
            return
        if not self.store.on_disk:
            result = self.export_bundle(path)
            self.notify(f"诊断包已导出：{result['events']} 条消息，{result['listed']}/{result['requests']} 个请求")
            return
        # 大库导出要几分钟，后台只读连接里写，界面照常可用
        self.store.flush()
        environment, focus = self._environment(), self.current_request
        self.export_running = True
        self.notify("正在后台导出诊断包…")

        def work():
            reader = self.store.reader()
            try:
                result = diagnostic_zip(reader, path, environment, focus)
                self.inbox.put(("export_done", f"诊断包已导出：{result['events']} 条消息，{result['listed']}/{result['requests']} 个请求"))
            except Exception as error:
                self.inbox.put(("export_done", "导出失败：" + str(error)))
            finally:
                reader.db.close()

        threading.Thread(target=work, daemon=True).start()

    def export_bundle(self, path):
        return diagnostic_zip(self.store, path, self._environment(), self.current_request)

    def _environment(self):
        return {"version": __version__, "mode": self.mode, "platform": sys.platform,
                "broker": f"{self.mqtt.host}:{self.port.value()}" if self.mqtt.connected else "",
                "device_sn": self.mqtt.sn if self.mqtt.connected else self.sn.text().strip(),
                "model": self.scene.variant, "osd": self.scene.osd}

    def _save_case(self):
        import yaml
        try:
            payload = json.loads(self.editor.toPlainText())
        except ValueError as error:
            self.notify("JSON 格式错误：" + str(error))
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存用例", str(payload.get("method", "case")) + ".yaml", "YAML (*.yaml)")
        if not path:
            return
        scenario = SCENARIOS[self.scenario.index()][1]
        preview = simulate(payload, "TEST", "preview", scenario)
        expected = [diagnose(e).status for e in preview if e.kind == "ACK"]
        Path(path).write_text(yaml.safe_dump({"name": payload.get("method"), "payload": payload, "scenario": scenario,
                                              "expect": {"ack_status": expected[-1] if expected else None}},
                                             allow_unicode=True), encoding="utf-8")
        self.notify("用例已保存")

    def _suite_html(self, rows, title=""):
        body = f"<p style='margin:0 0 14px 0;font-weight:700'>{html.escape(title)}</p>" if title else ""
        for passed, name, detail in rows:
            # passed 为 None 表示用例没写断言，只执行不判定
            color, verdict = (GRAY, "仅执行") if passed is None else (GREEN, "PASS") if passed else (RED, "FAIL")
            body += (f"<p style='margin:0 0 12px 0'><span style='color:{color};font-weight:700'>"
                     f"{verdict}</span>　<b>{html.escape(name)}</b><br>"
                     f"<span style='color:{GRAY}'>{html.escape(detail)}</span></p>")
        self.suite_info.setHtml(body)

    def _run_suite(self):
        cases = [("GW 配置查询", "oss_config_get", "success"), ("VFC 导航受理", "start_nav", "success"),
                 ("PTZ 复位", "gimbal_reset", "success"), ("Media 拍照失败", "take_photo", "failure"),
                 ("Vision AI", "start_ai", "success"), ("FSVR 上传终态", "sync_media_file", "success"),
                 ("直播活跃语义", "camera_livestream", "success"), ("嵌套失败", "camera_livestream", "nested_failure"),
                 ("缺 ACK 保持未知", "take_photo", "no_ack")]
        rows = []
        for name, method, scenario in cases:
            events = simulate(make_command(find(method)), "TEST_DOG_001", "suite-" + str(uuid.uuid4()), scenario)
            statuses = [diagnose(e).status for e in events]
            passed = ("失败" in statuses if scenario in {"failure", "nested_failure"} else
                      not any(e.kind == "ACK" for e in events) if scenario == "no_ack" else
                      "失败" not in statuses)
            rows.append((passed, name, " → ".join(dict.fromkeys(statuses))))
            for e in events:
                self.store.add(e)
        self.store.flush()
        self._suite_html(rows)
        self._refresh()

    def _load_case(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "读取沙盒用例（可多选，按文件名顺序执行）", "", "YAML (*.yaml *.yml)")
        if paths:
            self.run_cases(sorted(paths))

    def run_cases(self, paths):
        """按顺序在沙盒执行用例，每条记 PASS/FAIL/仅执行和证据；某条读不出来只记失败，不影响后面的"""
        import yaml
        rows = []
        for path in paths:
            name = Path(path).stem
            # 用例文件由用户编写，读取、准备、模拟任一步出错都只记这一条失败，接着跑后面的
            try:
                # utf-8-sig 兼容记事本等工具保存的带 BOM 的 UTF-8
                case = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
                payload, name = case["payload"], str(case.get("name") or name)
                payload["tid"] = str(uuid.uuid4())
                events = simulate(payload, "TEST_DOG_001", "case-" + str(uuid.uuid4()), case.get("scenario", "success"))
                expect = case.get("expect") or {}
            except Exception as error:
                rows.append((False, name, f"用例无法执行：{type(error).__name__} {error}"))
                continue
            for e in events:
                self.store.add(e)
            actual = [diagnose(e).status for e in events if e.kind == "ACK"]
            got = actual[-1] if actual else None
            passed = got == expect["ack_status"] if "ack_status" in expect else None
            evidence = trace_report(events).split("\n\n")[0]
            if passed is False:
                evidence = f"期望 {expect['ack_status']}，实际 {got}（tid {payload['tid'][:8]}…）；" + evidence
            rows.append((passed, name, evidence))
        self.store.flush()
        counts = {"PASS": sum(r[0] is True for r in rows), "FAIL": sum(r[0] is False for r in rows),
                  "仅执行": sum(r[0] is None for r in rows)}
        self._suite_html(rows, f"{len(rows)} 条用例：" + "，".join(f"{k} {v}" for k, v in counts.items() if v))
        self._refresh()

    def changeEvent(self, event):
        # 切到别的窗口时方向键的松开事件收不到，先停掉连续运动
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
            self.remote.halt()
        super().changeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.stack.currentIndex() in self.OBSERVED:
            self._ensure_viewer()

    def closeEvent(self, event):
        # 连接和数据库关闭前收尾遥控，停止指令才能发出并入库；在途的模型导入一并终止（STEP 子进程不留后台）
        self.remote.halt()
        self.cancel_import()
        self.import_cancel.set()
        self.mqtt.close()
        self._close_ssh()
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
    window._go(window.REMOTE)
    deadline = time.monotonic() + 30
    # 窗口刚显示时布局还在调整，此时截图里 3D 区是空白的；视图尺寸和上一次检查相同才算稳定
    last_size = [None]

    def finish():
        (folder / "self_check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        window.close()
        QApplication.instance().exit(1 if failures else 0)

    def capture():
        viewer = window.viewer
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
            fail(f"3D 渲染：30 秒内没有出现有效画面（{window.stage_hint.text().replace(chr(10), ' ') if not viewer else '视图对象已创建但画面空白'}），"
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


# 浅色样式里的颜色 → 深色外观（iOS 深色系统色）；样式表只维护浅色一份，深色按表替换
DARK_COLORS = {
    "#1C1C1E": "#F2F2F7", "#8E8E93": "#98989F", "#F2F2F7": "#2C2C2E", "#E5E5EA": "#38383A", "#D1D1D6": "#48484A",
    "#E9E9EE": "#3A3A3C", "#E5F1FF": "#0A2A4D", "#FFE9E8": "#3A1D1B", "#F7F7FA": "#232325", "#E3E3E8": "#3A3A3C",
    "#FFF8E6": "#3A3020", "#FFF0EF": "#3A1F1D", "#FFE2A8": "#5A4A20", "#FFD0CC": "#5A2A27", "#FAFAFC": "#202022",
    "#F5F5F8": "#2C2C2E", "#F2F7FF": "#1E2A3A", "#F0F0F5": "#2A2A2C", "#F0F0F3": "#2A2A2C", "#EEF5FF": "#1A2A40",
    "#EBEBF0": "#161618", "#EAF3FF": "#1A2A40", "#E1E1E6": "#3A3A3C", "#E0E0E6": "#3A3A3C", "#E0E0E5": "#38383A",
    "#DCDCE1": "#2C2C2E", "#D7D7DE": "#3A3A3C", "#D6E8FF": "#12345A", "#CFE2FF": "#12345A", "#B0B0B5": "#6C6C70",
    "#9CC8FF": "#2A5A8F", "#2C2C2E": "#3A3A3C", "#007AFF": "#0A84FF", "#FF3B30": "#FF453A",
    "#F4F6FA": "#000000", "#EFF2F7": "#161618", "#DEE5EF": "#2C2C2E", "#DFE6F0": "#38383A", "#FFFFFF": "#1C1C1E",
    "#FCFDFF": "#1A1A1C", "#F7F9FC": "#232325", "#D6DFEB": "#3A3A3C", "#F2F6FD": "#2C2C2E", "#B5C8E6": "#48484A",
    "#E4ECF7": "#3A3A3C", "#A7BEDF": "#545458", "#33496A": "#E5E5EA", "#FBFCFE": "#232325", "#DCE4EF": "#38383A",
    "#548BEA": "#0A84FF", "#578EEF": "#0A84FF", "#FFF3F1": "#3A1D1B", "#F0D6D1": "#5A2A27",
}


def dark_style(style):
    """由浅色样式表生成深色版：白底卡片换成深灰，其余颜色按 DARK_COLORS 一次替换；白字（蓝底上的）保持白色"""
    def swap(match):
        token = match[0]
        if token.startswith("background"):
            return "background:#1C1C1E"
        return DARK_COLORS.get(token.upper(), token)

    style = re.sub(r"background:white|#[0-9A-Fa-f]{6}\b", swap, style)
    # 分段控件的选中块要比底色亮
    return style.replace("QFrame#segThumb {background:#1C1C1E", "QFrame#segThumb {background:#636366")


def dark_palette():
    palette = QPalette()
    for role, color in ((QPalette.Window, "#000000"), (QPalette.WindowText, "#F2F2F7"), (QPalette.Base, "#1C1C1E"),
                        (QPalette.AlternateBase, "#202022"), (QPalette.Text, "#F2F2F7"), (QPalette.Button, "#3A3A3C"),
                        (QPalette.ButtonText, "#0A84FF"), (QPalette.Highlight, "#0A84FF"),
                        (QPalette.HighlightedText, "#FFFFFF"), (QPalette.PlaceholderText, "#6C6C70"),
                        (QPalette.ToolTipBase, "#3A3A3C"), (QPalette.ToolTipText, "#FFFFFF")):
        palette.setColor(role, QColor(color))
    return palette


# 本机记住的设置（外观、上次的连接、ZLM 地址和 secret）的存储格式；测试改成 IniFormat 并指到临时目录，不碰本机注册表
SETTINGS_FORMAT = QSettings.NativeFormat


def settings():
    """本机设置；必须用显式指定格式的构造，QSettings(组织, 程序) 会无视 setDefaultFormat 直接写注册表"""
    return QSettings(SETTINGS_FORMAT, QSettings.UserScope, "TTA", "DogDebug")


def setup_fonts(app):
    """全局字体：按系统常用中文界面字体依次取，Windows 上是微软雅黑"""
    font = QFont()
    font.setFamilies(["SF Pro Text", "PingFang SC", "Microsoft YaHei UI", "Segoe UI", "Noto Sans CJK SC"])
    font.setPointSizeF(10)
    app.setFont(font)


def apply_theme(app, dark):
    app.setPalette(dark_palette() if dark else light_palette())
    app.setStyleSheet(dark_style(STYLE) if dark else STYLE)


def light_palette():
    """固定浅色调色板，Windows 深色系统下文字也保持可读"""
    palette = QPalette()
    for role, color in ((QPalette.Window, "#F2F2F7"), (QPalette.WindowText, "#1C1C1E"), (QPalette.Base, "#FFFFFF"),
                        (QPalette.AlternateBase, "#FAFAFC"), (QPalette.Text, "#1C1C1E"), (QPalette.Button, "#E9E9EE"),
                        (QPalette.ButtonText, "#007AFF"), (QPalette.Highlight, "#007AFF"),
                        (QPalette.HighlightedText, "#FFFFFF"), (QPalette.PlaceholderText, "#A1A1A6"),
                        (QPalette.ToolTipBase, "#2C2C2E"), (QPalette.ToolTipText, "#FFFFFF")):
        palette.setColor(role, QColor(color))
    return palette


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
    window.restore_connection()
    window.show()
    if args.demo:
        QTimer.singleShot(50, window._demo)
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
