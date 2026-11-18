"""3D 观察区：模型加载和导入、关节、点云、OSD 日志回放、设备卡"""

import math
import threading
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from .mission import spin
from .. import cad, robot as model_module
from ..core import diagnose
from ..pointcloud import CloudError, decode as decode_cloud, summary as cloud_summary
from ..robot import VARIANTS
from ..scene import LIVE_SECONDS, osd_fields
from ..ui import (
    button, card, Compass, GRAY, GREEN, guard_wheel, label, method_label, ORANGE, pill, PURPLE, Segmented,
    SPARK_COLORS, Sparkline, Switch, tint,
)


class ScenePanel:
    """遥控和控制调试页右侧的 3D 观察区：模型、点云、日志回放和设备卡"""

    def __init__(self, app):
        self.app = app
        self.joint_sliders = {}
        self.importing = None
        self.step_timer = QTimer(app)
        self.step_timer.timeout.connect(self._poll_step)
        self.shown_live = False
        # None 未加载，False 加载中，否则为 RobotView
        self.viewer = None
        self.spark_at, self.compass = 0.0, None
        self.replay = None
        # 最近一次点云解析错误（同样的错误不重复提示）和点云标签上次按时效刷新的时刻
        self.cloud_error, self.cloud_aged = "", 0.0

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
        self.app.scene.changed.connect(self._device_changed)
        self.app.scene.posed.connect(self._sync_joints)
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
        row.addWidget(button("清除", self.app.scene.clear_cloud, "link"))
        box.addLayout(row)
        self.cloud_info = label("ros2bridge 采集时（GW 建图中，或导航与文件页手动打开）收到的点云挂在机身位置上显示", "caption", True)
        box.addWidget(self.cloud_info)
        self.app.scene.clouded.connect(self._cloud_status)
        return box

    def _cloud_options(self, *_):
        if self.viewer:
            self.viewer.set_cloud_options(self.cloud_show.isChecked(), ["高度", "强度"][self.cloud_mode.index()],
                                          self.lidar_height.value(), self.cloud_map_show.isChecked())

    def _cloud_status(self):
        """点云标签：实时（3 s 内有新帧）/ 已停 / 未收到；每秒和每次换帧时更新"""
        scene, cloud = self.app.scene, self.app.scene.cloud
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
        self.replay_timer = QTimer(self.app)
        self.replay_timer.setInterval(100)
        self.replay_timer.timeout.connect(self._replay_tick)
        self.replay_bar.hide()
        return self.replay_bar

    def _replay_scan(self):
        self.app.store.flush()
        self.app.notify("正在查找带 OSD 的会话…")
        self.app._background(lambda reader: reader.osd_sessions(), "replay_sessions")

    def _replay_menu(self, sessions):
        if not sessions:
            self.app.notify("库里还没有 OSD 消息：先连上设备，或在日志排障页导入带 OSD 的日志")
            return
        menu = QMenu(self.app)
        for sn, session, count in sessions:
            menu.addAction(f"{sn or '未知设备'} · 会话 {session[:8]} · {count} 条 OSD",
                           lambda sn=sn, session=session: self.app._background(lambda r: r.osd_frames(sn, session),
                                                                           "replay_frames", sn, session))
        menu.popup(self.replay_btn.mapToGlobal(self.replay_btn.rect().bottomLeft()))

    def _replay_start(self, rows, sn, session):
        frames = [(row_id, e, fields) for row_id, e in rows if (fields := osd_fields(e))]
        if not frames:
            self.app.notify("这个会话的 OSD 里没有位置或遥测字段")
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
        self.app.scene.replay(fields, self.replay["trail"][:index + 1])
        stamp = lambda m: m.source_time or time.strftime("%H:%M:%S", time.localtime(m.timestamp))
        self.replay_label.setText(f"第 {index + 1} / {len(frames)} 帧　{stamp(e)}")
        after = frames[index - 1][0] if index else 0
        self.replay_list.clear()
        for found_id, m in self.app.store.recent_before(self.replay["sn"], self.replay["session"], row_id):
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
        if event:
            self.app.open_request(event)

    def _replay_exit(self):
        self.replay_timer.stop()
        self.replay_play.setText("▶")
        self.replay = None
        self.replay_bar.hide()
        self.app.scene.reset_telemetry()
        self.app.scene.clear_trail()

    def _ensure_viewer(self):
        """观察区首次显示才加载模型和 OpenGL，先显示加载中，避免拖慢启动"""
        if self.viewer is not None:
            return
        self.viewer = False
        self.stage_hint.setText("正在加载模型…")
        QTimer.singleShot(40, self._load_viewer)

    def _load_viewer(self):
        try:
            from ..viewer import RobotView
        except ImportError as error:
            self.stage_hint.setText("3D 需要安装 numpy、PyOpenGL、pyqtgraph（见 requirements.txt）\n" + str(error))
            self.viewer = None
            return
        if not self.app.scene.robot:
            self.app.scene.set_variant(self.model_box.currentData())
        self.viewer = RobotView(self.app.scene)
        self.viewer.follow = self.app.teleop.follow.isChecked()
        self.compass = Compass(self.viewer)
        self.viewer.set_dark(self.app.dark)
        self._cloud_options()
        self.stage_hint.hide()
        self.stage.insertWidget(self.stage.indexOf(self.stage_hint), self.viewer, 1)
        self._model_loaded()

    def _model_chosen(self, index):
        """下拉框里选内置模型或本次导入过的模型；内置模型按名字现读，导入的直接复用"""
        data = self.model_box.itemData(index)
        robot = model_module.load(data) if isinstance(data, str) else data
        self.app.scene.set_robot(robot)
        self._model_loaded()

    def _model_loaded(self):
        """模型切换后刷新侧栏：L2 显示演示姿态和按腿分组的关节；其他 URDF 按自身关节；STEP/STL 列部件"""
        robot = self.app.scene.robot
        self.app.pose_card.setVisible(robot.kind == "L2")
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
        while self.app.teleop.joint_box.count():
            item = self.app.teleop.joint_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.joint_sliders = {}
        robot = self.app.scene.robot
        if robot.kind in ("STEP", "STL"):
            self.app.teleop.joint_title.setText("部件")
            note = "STEP 是结构外形，文件里没有关节定义，这里只做结构查看" if robot.kind == "STEP" else "STL 只有网格，没有部件和关节信息"
            self.app.teleop.joint_box.addWidget(label(note, "caption", True))
            parts = QListWidget()
            parts.setObjectName("commands")
            for name, link in robot.links.items():
                if link.mesh is not None:
                    item = QListWidgetItem(f"{name.split(' ', 1)[-1]}　　{len(link.mesh)} 面")
                    item.setData(Qt.UserRole, name)
                    parts.addItem(item)
            # 点部件在 3D 里高亮
            parts.currentItemChanged.connect(lambda item, _: self.viewer and self.viewer.highlight(item.data(Qt.UserRole) if item else None))
            self.app.teleop.joint_box.addWidget(parts, 1)
            self.part_list = parts
            return
        self.app.teleop.joint_title.setText("关节")
        if not robot.movable:
            self.app.teleop.joint_box.addWidget(label("这个模型没有可动关节", "caption"))
        l2 = robot.kind == "L2"
        legs = {"FBL": "左前", "FAR": "右前", "RBL": "左后", "RAR": "右后"}
        parts = {"ABAD": "侧摆", "HIP": "髋", "KNEE": "膝", "FOOT": "轮"}
        current = None
        for joint in robot.movable:
            if l2:
                leg, part = joint.name[:3], joint.name.split("_")[-2]
                if leg != current:
                    current = leg
                    self.app.teleop.joint_box.addWidget(label(legs.get(leg, leg) + "腿", "groupHeader"))
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
                shown.setText(self._joint_text(j, v / s)), self.app.scene.set_joint(name, v / s)))
            # 拖动时把对应连杆染成强调色，松手恢复
            slider.sliderPressed.connect(lambda child=joint.child: self.viewer and self.viewer.highlight(child))
            slider.sliderReleased.connect(lambda: self.viewer and self.viewer.highlight(None))
            rl.addWidget(slider, 1)
            rl.addWidget(value)
            self.app.teleop.joint_box.addWidget(row)
            self.joint_sliders[joint.name] = (slider, value, scale, joint)
        self.app.teleop.joint_box.addStretch()
        self._sync_joints()

    @staticmethod
    def _joint_text(joint, value):
        return f"{value * 1000:.0f} mm" if joint.kind == "prismatic" else f"{math.degrees(value):.0f}°"

    def _sync_joints(self):
        for name, (slider, value, scale, joint) in self.joint_sliders.items():
            angle = self.app.scene.angles.get(name, 0.0)
            slider.blockSignals(True)
            slider.setValue(round(angle * scale))
            slider.blockSignals(False)
            value.setText(self._joint_text(joint, angle))

    def import_model_dialog(self, kind):
        filters = {"urdf": ("选择 URDF 文件", "URDF (*.urdf *.xml)"), "urdf_zip": ("选择 URDF 压缩包", "压缩包 (*.zip)"),
                   "step": ("选择 STEP 文件", "STEP (*.step *.stp *.STEP *.STP)"), "stl": ("选择 STL 文件", "STL (*.stl *.STL)")}
        if kind == "urdf_dir":
            path = QFileDialog.getExistingDirectory(self.app, "选择包含 URDF 的模型目录")
        else:
            path, _ = QFileDialog.getOpenFileName(self.app, *filters[kind])
        if not path:
            return
        unit = "m"
        if kind == "stl":
            answer = QMessageBox.question(self.app, "STL 单位", "STL 文件本身不带单位。按毫米导入？\n选“否”按米导入",
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
                self.app.inbox.put(("model", token, robot))
            except model_module.ModelError as error:
                self.app.inbox.put(("model_error", token, error))
            except Exception as error:
                self.app.inbox.put(("model_error", token, model_module.ModelError(f"{type(error).__name__}: {error}")))

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
        self.app.scene.set_robot(robot)
        if self.observer.isVisible():
            self._ensure_viewer()
        self._model_loaded()
        self.app.notify(f"已导入 {robot.name}" + (f"，{len(robot.missing)} 个网格缺失未显示" if robot.missing else ""))

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
        directory = QFileDialog.getExistingDirectory(self.app, "选择网格所在的资源目录（例如 meshes 的上级或 ROS 包目录）")
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
        scene, osd = self.app.scene, self.app.scene.osd
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
        """侧栏底部的当前设备档案：狗写 SN、型号、电量和 OSD，无人机写 Broker 地址；圆点颜色只在变了时改，这里调用很频繁"""
        uav = self.app.device_box.currentText() == "无人机"
        self.app.device_badge.setText("UAV 无人机" if uav else "DOG 机器狗")
        if uav:
            live = self.app.mqtt.connected and self.app.mqtt.device == "uav"
            self.app.device_name.setText(self.app.mqtt.host if live else self.app.host.text().strip() or "未连接")
            self.app.device_meta.setText("VFC / Bridge　·　" + ("已连接" if live else "未连接"))
        else:
            osd, live = self.app.scene.osd, self.app.scene.live
            sn = self.app.mqtt.sn if self.app.mqtt.connected else self.app.sn.text().strip()
            self.app.device_name.setText(sn or "未设置 SN")
            robot = self.app.scene.robot
            parts = [(self.app.scene.variant or list(VARIANTS)[0]).split(" ")[0] if not robot or robot.kind == "L2" else robot.name]
            if "batteryRemain" in osd:
                parts.append(f"电量 {osd['batteryRemain']:.0f}%")
            parts.append("OSD 实时" if live else "未收到 OSD" if not osd else "OSD 已停")
            self.app.device_meta.setText("　·　".join(parts))
        color = GREEN if live else "#C7C7CC"
        if getattr(self, "device_color", None) != color:
            self.device_color = color
            self.app.device_dot.setStyleSheet(f"background:{color};border-radius:4px")

    def set_dark(self, dark):
        if self.viewer:
            self.viewer.set_dark(dark)

    def take_cloud(self, data):
        """当前设备的一帧点云交给 3D；同一个解析错误连着来只提示一次，中间解出过好帧再出错会再提示"""
        try:
            self.app.scene.set_cloud(decode_cloud(data))
            self.cloud_error = ""
        except CloudError as error:
            if str(error) != self.cloud_error:
                self.cloud_error = str(error)
                self.app.notify("点云解析失败：" + self.cloud_error)
                self.cloud_info.setText("最近一帧没解出来：" + self.cloud_error)

    def tick(self, now):
        """事件队列每轮调用：OSD 停发后没有新消息也没有动画，这里按时效退掉“实时”标记，点云标签每秒刷一次"""
        if self.app.scene.live != self.shown_live:
            self._device_changed()
        if self.app.scene.cloud and now - self.cloud_aged >= 1:
            self.cloud_aged = now
            self._cloud_status()
