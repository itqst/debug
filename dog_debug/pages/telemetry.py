"""状态总览顶上的关键遥测卡：狗和无人机共用，按设备实际上报的 OSD 显示数字，没报的显示“—”"""

import math
import time

from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout

from ..ui import card, GRAY, GREEN, label, ORANGE, pill, Sparkline, tint

# 狗的 OSD 来自 scene（tta/robotdog/<sn>/osd、local/robotdog/vfc/data），无人机的来自 VFC OSD（local/uav/vfc/osd）
# 每格：(键, 标题, 格式)；位置是 x、y 两个键合成一格
DOG_TILES = (("batteryRemain", "电量"), ("speed", "速度"), ("heading", "航向 Yaw"), ("position", "位置 x, y"),
             ("pitch", "Pitch"), ("roll", "Roll"), ("functionMode", "模式"), ("controller", "控制权"),
             ("temperature", "温度"), ("flyingTime", "运行"))
UAV_TILES = (("batteryRemain", "电量"), ("speed", "速度"), ("heading", "航向 Yaw"), ("z", "高度 z"),
             ("position", "位置 x, y"), ("pitch", "Pitch"), ("roll", "Roll"))
# 协议里目前狗和无人机都没报姿态角，有了才出格子，不占位
OPTIONAL = ("pitch", "roll")
SPARKS = {"batteryRemain": "#34C759", "speed": "#007AFF", "heading": "#AF52DE", "z": "#FF9500"}
# 和状态卡同一个时效：超过这么久没新 OSD 就标“已停”
LIVE_SECONDS = 3.0
MODES = {1: "SDK 巡航", 2: "手柄遥控", 3: "普通 SDK"}
OWNERS = {1: "SDK", 2: "手柄", 3: "建图"}


def number(data, key):
    """有限的数才算测量值；字符串、null、bool 都当没报"""
    value = data.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def text_of(data, key):
    """一格显示的文字；没报的是“—”，不当成 0"""
    if key == "position":
        x, y = number(data, "x"), number(data, "y")
        return f"{x:.2f}, {y:.2f}" if x is not None and y is not None else "—"
    value = number(data, key)
    if value is None:
        return "—"
    if key == "batteryRemain":
        return f"{value:.0f}%"
    if key == "speed":
        return f"{value:.2f} m/s"
    if key in ("heading", "pitch", "roll"):
        return f"{value:.0f}°"
    if key == "z":
        return f"{value:.2f} m"
    if key == "temperature":
        return f"{value:.0f} ℃"
    if key == "flyingTime":
        return f"{int(value) // 60} 分 {int(value) % 60} 秒"
    if key == "functionMode":
        return MODES.get(value, str(value))
    if key == "controller":
        return OWNERS.get(value, str(value))
    return str(value)


class TelemetryCard:
    """一张卡：标题、实时/已停标签、数字格；换设备时换一套格子，平时只改文字不重建控件"""

    def __init__(self, app):
        self.app = app
        self.frame, layout = card(8, 14)
        head = QHBoxLayout()
        head.addWidget(label("关键遥测", "sectionTitle"))
        self.state = pill("未收到 OSD", GRAY)
        head.addWidget(self.state)
        head.addStretch()
        self.source = label("", "caption")
        head.addWidget(self.source)
        layout.addLayout(head)
        self.grid = QGridLayout()
        self.grid.setSpacing(8)
        layout.addLayout(self.grid)
        self.tiles, self.sparks, self.device, self.drawn, self.spark_at = {}, {}, None, None, 0.0

    def _build(self, device):
        """按设备建格子；只在换设备时调用"""
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        self.tiles, self.sparks, self.drawn = {}, {}, None
        for key, caption in UAV_TILES if device == "uav" else DOG_TILES:
            tile = QFrame()
            tile.setObjectName("stat")
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(10, 5, 8, 5)
            tl.setSpacing(0)
            value = label("—", "statValue")
            tl.addWidget(value)
            tl.addWidget(label(caption, "caption"))
            # 数值类带最近 60 s 走势线，其他格子留同样高度对齐
            spark = Sparkline(SPARKS[key]) if key in SPARKS else label("")
            spark.setFixedHeight(14)
            if key in SPARKS:
                self.sparks[key] = spark
            tl.addWidget(spark)
            self.tiles[key] = (tile, value)
        self.device = device
        self._place()

    def _place(self):
        """可选格（姿态角）没报时不占位；一行排开，不挤占下面模块卡片的高度"""
        shown = [key for key in self.tiles if key not in OPTIONAL or self.tiles[key][1].text() != "—"]
        for key, (tile, _) in self.tiles.items():
            tile.setVisible(key in shown)
        for index, key in enumerate(shown):
            self.grid.addWidget(self.tiles[key][0], 0, index)
        self.placed = tuple(shown)

    def _reading(self, device):
        """(数据, 是否实时, 来源说明)"""
        mqtt = self.app.mqtt
        if device == "uav":
            # 只看本次连接的；连了新的还没收到第一条时不显示上一次连接的数
            event = self.app.uav.latest.get("vfc_osd") if self.app.uav.session == mqtt.session else None
            if not event:
                return {}, False, "local/uav/vfc/osd"
            data = event.payload.get("data") if isinstance(event.payload.get("data"), dict) else {}
            # 断开后立刻标“已停”，不等时效过期
            live = mqtt.connected and time.time() - event.timestamp < LIVE_SECONDS
            return data, live, f"VFC OSD · {time.strftime('%H:%M:%S', time.localtime(event.timestamp))} 收到"
        scene = self.app.scene
        if scene.osd and not scene.osd_time:
            return scene.osd, False, "回放中的历史 OSD"
        return scene.osd, scene.live and mqtt.connected, "设备 OSD"

    def update(self, device):
        """每轮事件队列调用；内容没变就不碰控件"""
        if device != self.device:
            self._build(device)
        # 换了连接，走势线从头画，不接着上次连接的点；断开本身不清，保留最后的样子
        session = self.app.mqtt.session
        if session != getattr(self, "session", None):
            self.session = session
            for spark in self.sparks.values():
                spark.points = []
                spark.update()
        data, live, source = self._reading(device)
        texts = tuple(text_of(data, key) for key in self.tiles)
        state = ("OSD 实时", GREEN) if live else ("OSD 已停", ORANGE) if data else ("未收到 OSD", GRAY)
        if (texts, state, source) != self.drawn:
            self.drawn = (texts, state, source)
            for (key, (_, value)), text in zip(self.tiles.items(), texts):
                value.setText(text)
            self.state.setText(state[0])
            self.state.setStyleSheet(f"background:{tint(state[1], 34)};color:{state[1]};border-radius:9px;"
                                     "padding:2px 9px;font-size:11px;font-weight:600")
            self.source.setText(source)
            shown = tuple(key for key in self.tiles if key not in OPTIONAL or self.tiles[key][1].text() != "—")
            if shown != self.placed:
                self._place()
        # 走势线每 0.5 s 采一次，只采实时数据
        now = time.monotonic()
        if live and now - self.spark_at >= 0.5:
            self.spark_at = now
            for key, spark in self.sparks.items():
                value = number(data, key)
                if value is not None:
                    spark.add(now, float(value))
