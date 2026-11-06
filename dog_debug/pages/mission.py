"""导航与文件页：建图定位、航点导航、文件同步、红外测温和喊话的表单"""

import html
import json
import math
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QComboBox, QDoubleSpinBox, QFileDialog, QGraphicsItem, QGraphicsScene,
    QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout, QHeaderView, QLineEdit, QListWidget, QListWidgetItem,
    QTableWidget, QVBoxLayout, QWidget,
)

from ..core import diagnose
from ..ui import (
    BLUE, button, card, FAILED, FlowGrid, GRAY, GREEN, guard_wheel, label, MODULE_COLORS, ORANGE, pill, Switch,
)


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
