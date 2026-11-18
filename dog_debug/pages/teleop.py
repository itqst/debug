"""遥控页的常用指令面板：运动、姿态、云台镜头、音视频"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox, QGridLayout, QHBoxLayout, QLineEdit, QPushButton, QSlider, QSpinBox, QStackedWidget, QVBoxLayout,
    QWidget,
)

from ..robot import POSES
from ..ui import button, card, fade_to, FlowGrid, label, MODULE_COLORS, pill, scroller, Segmented, Switch


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
        # 云台固定在主码流可见光、副码流红外，不再切镜头；红外不能变焦，变焦只作用在可见光
        layout.addWidget(label("方向键按住转动，松手回中；方向键盘 ↑↓←→ 同样可用。可见光和红外两路一直同时在出，变焦只作用在可见光", "caption", True))
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


class TeleopPage:
    """遥控页：常用指令面板和 3D 观察区"""

    def __init__(self, app):
        self.app = app

    def _remote_page(self):
        """遥控和模型设置两栏切换；切到“模型”时遥控面板隐藏，按住的方向会按松手处理"""
        page = QWidget()
        sv = QVBoxLayout(page)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(12)
        self.app._page_header(sv, "机器狗 · 遥控与载荷", "按住方向键或 WASD / QE 移动，松手、切走页面或窗口、断线都会补发零速；空格急停")
        self.side_tabs = Segmented(["遥控", "模型"])
        # “模型”一栏是 3D 模型的姿态和关节设置，3D 不显示时先藏起来
        self.side_tabs.setVisible(bool(self.app.OBSERVED))
        sv.addWidget(self.side_tabs)
        self.side_stack = QStackedWidget()
        self.side_tabs.changed.connect(lambda i: fade_to(self.side_stack, i))
        self.remote = RemotePanel(self.app._quick_send)
        self.side_stack.addWidget(scroller(self.remote))
        model_side = QWidget()
        ms = QVBoxLayout(model_side)
        ms.setContentsMargins(0, 0, 0, 0)
        ms.setSpacing(14)
        self.side_stack.addWidget(model_side)
        sv.addWidget(self.side_stack, 1)

        self.app.pose_card, pv = card(10, 16)
        pv.addWidget(label("演示姿态", "sectionTitle"))
        row = QGridLayout()
        row.setSpacing(8)
        for index, name in enumerate(POSES):
            row.addWidget(button(name, lambda checked=False, name=name: self.app.scene.pose(name), "tile"), 0, index)
        pv.addLayout(row)
        pv.addWidget(label("只改 3D 演示，不发指令。遥控发站立、趴下、跳跃、打招呼、move 时模型同步演示", "caption", True))
        ms.addWidget(self.app.pose_card)

        joints, jv = card(8, 16)
        head = QHBoxLayout()
        self.joint_title = label("关节", "sectionTitle")
        head.addWidget(self.joint_title)
        head.addStretch()
        self.follow = Switch("镜头跟随")
        self.follow.setChecked(True)
        self.follow.toggled.connect(lambda on: setattr(self.app.scene3d.viewer, "follow", on) if self.app.scene3d.viewer else None)
        head.addWidget(self.follow)
        jv.addLayout(head)
        holder = QWidget()
        self.joint_box = QVBoxLayout(holder)
        self.joint_box.setContentsMargins(0, 0, 6, 0)
        self.joint_box.setSpacing(4)
        jv.addWidget(scroller(holder), 1)
        actions = QHBoxLayout()
        actions.addWidget(button("关节归零", lambda: self.app.scene.pose("零位")))
        actions.addWidget(button("清除轨迹", self.app.scene.clear_trail))
        jv.addLayout(actions)
        ms.addWidget(joints, 1)
        return page

    def shown(self):
        self.remote.setFocus()
