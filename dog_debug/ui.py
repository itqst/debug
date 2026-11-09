"""界面公用的常量、小控件、表格模型、提示条、配色和本机设置"""

import json
import re
import time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QEasingCurve, QEvent, QModelIndex, QObject, QPointF, QPropertyAnimation, QRectF, QSettings,
    QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGraphicsOpacityEffect, QGridLayout,
    QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QSizePolicy, QSlider, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from .core import DETECTION_STAGES, diagnose
from .registry import find


DATA = Path(__file__).parent / "data"
APP_NAME = "坤坤调试台"
STYLE = (DATA / "style.qss").read_text(encoding="utf-8").replace("@DATA", DATA.as_posix())
DEVICES = {"机器狗": "dog", "无人机": "uav"}
MODES = {"沙盒 · 全链路模拟": "sandbox", "云端 → 真实 GW": "gw",
         "直连模块": "direct", "离线日志分析": "offline"}
SCENARIOS = [("正常", "success"), ("执行失败", "failure"), ("无 ACK", "no_ack"),
             ("嵌套失败", "nested_failure"), ("重复回复", "duplicate")]

BLUE, GREEN, ORANGE, RED, GRAY = "#007AFF", "#34C759", "#FF9500", "#FF3B30", "#8E8E93"
PURPLE = "#AF52DE"
MODULE_COLORS = {"gw": "#5856D6", "vfc": BLUE, "ptz": ORANGE, "media": "#FF2D55", "vision": "#AF52DE", "fsvr": GREEN,
                 "ros2bridge": "#A2845E", "zlm": "#30B0C7"}
FAILED = {"失败", "错误线索", "重复请求未执行"}
WARNING = {"契约待核", "未活跃", "待核", "无 ACK", "保留消息", "结果不确定"}
PROGRESS = {"已受理/上传中", "阶段待判", "受理中"}
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
# 日志排障页按原文时间筛的时间段：名称 → 往前多少秒，`None` 为不按时间筛，`"today"` 为今天 0 点起，`"custom"` 为自己选起止
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
    elif name == "无人机":
        for cx, cy in ((5, 5), (19, 5), (5, 19), (19, 19)):
            p.drawEllipse(QPointF(cx, cy), 3.2, 3.2)
        line((7.5, 7.5), (16.5, 16.5))
        line((16.5, 7.5), (7.5, 16.5))
        p.drawRoundedRect(QRectF(9.5, 9.5, 5, 5), 1.5, 1.5)
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
