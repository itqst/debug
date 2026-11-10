"""3D 场景状态：模型、关节角、机身位置和遥测，不含 OpenGL，界面和测试共用

数据来源分两类，界面上分开标注：
- 位置/航向/电量来自设备 OSD（`tta/robotdog/{sn}/osd` 或 `local/robotdog/vfc/data`），收到后 3 s 内视为实时
- 关节角设备不上报，始终是演示：按已发出的动作指令播放预设姿态，或用户手动拖动
"""

import math
import time

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from . import pointcloud
from . import robot as model

# 超过这个时间没有新 OSD，位置改回演示
LIVE_SECONDS = 3.0
FRAME_MS = 33
# 动作指令对应的演示姿态，未列出的指令不改姿态
COMMAND_POSES = {"stand_up": "站立", "lie_down": "半蹲", "passive": "趴下"}
OSD_FIELDS = ("x", "y", "z", "heading", "speed", "batteryRemain", "functionMode", "controller", "temperature", "flyingTime")


def osd_fields(event):
    """从 OSD 类消息取出遥测字段；不是 OSD 返回空字典。无人机的 OSD（local/uav/vfc/osd）在无人机页看，不驱动狗的 3D"""
    if event.topic.startswith("local/uav/"):
        return {}
    if event.topic.endswith("/osd"):
        body = event.payload
        # VFC 实际发的是根上 sn/ts、遥测放在 data 里（local/robotdog/vfc/osd）；文档样例是扁平的，两种都认
        if isinstance(body.get("data"), dict):
            body = body["data"]
    elif event.topic == "local/robotdog/vfc/data":
        body = event.payload.get("data", {})
    else:
        return {}
    if not isinstance(body, dict):
        return {}
    return {k: body[k] for k in OSD_FIELDS if isinstance(body.get(k), (int, float)) and not isinstance(body.get(k), bool)}


# 点云超过这个时间没刷新就在界面上标“已停”，最后一帧仍然显示
CLOUD_LIVE_SECONDS = 3.0


class Scene(QObject):
    """changed 在每次需要重绘时发出；posed 在一次姿态动画结束或换模型后发出；clouded 在点云换帧或清空时发出"""

    changed = Signal()
    posed = Signal()
    clouded = Signal()

    def __init__(self):
        super().__init__()
        self.variant, self.robot = None, None
        self.angles, self.height, self.lift = {}, 0.0, 0.0
        self.x = self.y = self.heading = 0.0
        self.velocity = (0.0, 0.0, 0.0)
        self.phase = 0.0
        self.osd, self.osd_time = {}, 0.0
        self.trail = []
        # 最近一帧雷达点云（pointcloud.Cloud）和最近几帧的接收时刻，算频率用
        self.cloud, self.cloud_times = None, []
        # 新格式的点云在原点坐标系里，逐帧累加成一张地图，看建图效果
        self.cloud_map = pointcloud.MapAccumulator()
        self.pose_name = "站立"
        self.animation = None
        self.heights = {}
        self.last = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    # ---- 模型与姿态 ----

    def set_variant(self, variant):
        self.set_robot(model.load(variant))

    def set_robot(self, robot):
        """切换到任意已加载的模型；只有内置 L2 套用演示姿态，其他模型从零位开始"""
        self.variant, self.robot = robot.name, robot
        pose = self.pose_name if self.is_l2 else "零位"
        self.angles = model.pose_angles(robot, pose)
        self.height = self._height(pose)
        self.animation, self.phase, self.lift = None, 0.0, 0.0
        self.posed.emit()
        self.changed.emit()

    @property
    def is_l2(self):
        return bool(self.robot) and self.robot.kind == "L2"

    def _height(self, pose):
        key = (id(self.robot), pose)
        if key not in self.heights:
            self.heights[key] = model.ground_height(self.robot, model.pose_angles(self.robot, pose))
        return self.heights[key]

    def pose(self, name, seconds=0.6):
        """平滑过渡到预设姿态；模型未加载时只记下，加载后直接用；非 L2 模型只有“零位”"""
        if self.robot and not self.is_l2:
            name = "零位"
        self.pose_name = name
        if not self.robot:
            return
        target = model.pose_angles(self.robot, name)
        self.animation = {"start": dict(self.angles), "target": target, "h0": self.height,
                          "h1": self._height(name), "t": 0.0, "seconds": seconds, "keys": None}
        self._run()

    def gesture(self, keys):
        """播放一段关键帧动作 [(秒, 关节角增量, 机身抬高 m)]，结束后回到当前姿态"""
        if not self.robot:
            return
        self.animation = {"base": dict(self.angles), "keys": keys, "t": 0.0, "seconds": sum(k[0] for k in keys)}
        self._run()

    def set_joint(self, name, angle):
        """手动调关节，打断正在播放的动画"""
        self.animation = None
        joint = next(j for j in self.robot.movable if j.name == name)
        self.angles[name] = self.robot.clamp(joint, angle)
        self.changed.emit()

    # ---- 指令与遥测 ----

    def command(self, method, data):
        """按已发出的指令播放演示；move 只在没有实时 OSD 时驱动演示位置

        报文是用户自由编辑的，字段不能用于演示时忽略并返回一句说明，否则返回空串；
        姿态和动作演示只对内置 L2 模型有意义，导入的其他模型不套用
        """
        if not self.is_l2 and method != "move":
            return ""
        if method in COMMAND_POSES:
            self.pose(COMMAND_POSES[method])
        elif method in ("jump", "front_jump"):
            self.gesture([(0.18, {}, 0.0), (0.22, {}, 0.12), (0.25, {}, 0.0)])
        elif method == "shake_hand":
            wave = {"FBL_HIP_JOINT": -0.9, "FBL_KNEE_JOINT": 0.9}
            self.gesture([(0.3, wave, 0.0), (0.25, {**wave, "FBL_ABAD_JOINT": 0.2}, 0.0),
                          (0.25, {**wave, "FBL_ABAD_JOINT": -0.2}, 0.0), (0.3, {}, 0.0)])
        elif method == "move":
            values = [data.get(k, 0) if isinstance(data, dict) else None for k in ("vx", "vy", "yaw_rate")]
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
                self.velocity = (0.0, 0.0, 0.0)
                return "move 速度字段不是数字，3D 演示停在原地"
            self.velocity = tuple(float(v) for v in values)
            self._run()
        return ""

    def telemetry(self, event):
        fields = osd_fields(event)
        if not fields:
            return
        self.osd.update(fields)
        self.osd_time = time.monotonic()
        self.x, self.y = float(self.osd.get("x", self.x)), float(self.osd.get("y", self.y))
        if "heading" in fields:
            # 协议只写“航向”整数，按角度处理，0° 对齐场景 x 轴
            self.heading = math.radians(fields["heading"])
        self._append_trail()
        self._run()
        self.changed.emit()

    @property
    def live(self):
        return bool(self.osd) and time.monotonic() - self.osd_time < LIVE_SECONDS

    def replay(self, fields, trail):
        """显示一帧历史 OSD：位置、航向和遥测按这一帧，轨迹换成 trail；不算实时，也不触发步态动画"""
        self.osd, self.osd_time = dict(fields), 0.0
        self.x, self.y = float(fields.get("x", self.x)), float(fields.get("y", self.y))
        if "heading" in fields:
            self.heading = math.radians(fields["heading"])
        self.velocity = (0.0, 0.0, 0.0)
        self.trail = list(trail[-2000:]) or [(self.x, self.y)]
        self.changed.emit()

    def reset_telemetry(self):
        """换连接后旧设备的 OSD 不再算实时"""
        self.osd, self.osd_time = {}, 0.0
        self.changed.emit()

    def set_cloud(self, cloud):
        """换上新一帧点云；本地 data 和 GW 转发的云端 pcd 是同一帧，seq 和 stamp 都相同时不重复算"""
        key = (cloud.meta.get("seq"), cloud.meta.get("stamp"))
        if self.cloud and key == (self.cloud.meta.get("seq"), self.cloud.meta.get("stamp")):
            return False
        self.cloud = cloud
        self.cloud_times = [t for t in self.cloud_times if cloud.received - t < 5.0] + [cloud.received]
        self.cloud_map.add(cloud)
        self.clouded.emit()
        return True

    def clear_cloud(self):
        """清掉最近一帧和累加的地图"""
        self.cloud, self.cloud_times = None, []
        self.cloud_map.clear()
        self.clouded.emit()

    @property
    def cloud_live(self):
        return bool(self.cloud) and time.monotonic() - self.cloud.received < CLOUD_LIVE_SECONDS

    @property
    def cloud_rate(self):
        """最近 5 s 的帧率；跨度不到 1 s（刚开始收、沙盒一次给几帧）时算不准，为 0"""
        times = self.cloud_times
        span = times[-1] - times[0] if times else 0.0
        return (len(times) - 1) / span if span >= 1.0 else 0.0

    def clear_trail(self):
        self.trail = [(self.x, self.y)]
        self.changed.emit()

    def _append_trail(self):
        if not self.trail or math.dist(self.trail[-1], (self.x, self.y)) > 0.02:
            self.trail.append((self.x, self.y))
            del self.trail[:-2000]

    # ---- 动画 ----

    def _run(self):
        if not self.timer.isActive():
            self.last = time.monotonic()
            self.timer.start(FRAME_MS)

    def tick(self):
        now = time.monotonic()
        dt, self.last = min(now - self.last, 0.1), now
        busy = self._animate(dt)
        walking = any(abs(v) > 1e-6 for v in self.velocity) and not self.live
        if walking:
            vx, vy, yaw = self.velocity
            c, s = math.cos(self.heading), math.sin(self.heading)
            self.x += (vx * c - vy * s) * dt
            self.y += (vx * s + vy * c) * dt
            self.heading += yaw * dt
            # 步频随速度变化，静止转向也给一点步态
            self.phase += dt * 2 * math.pi * (1.6 + 1.5 * min(math.hypot(vx, vy), 1.0))
            self._append_trail()
        elif self.phase:
            self.phase = 0.0
        if not busy and not walking:
            self.timer.stop()
        self.changed.emit()

    def _animate(self, dt):
        a = self.animation
        if not a:
            return False
        a["t"] += dt
        done = a["t"] >= a["seconds"]
        if a["keys"] is None:
            k = 1.0 if done else a["t"] / a["seconds"]
            k = k * k * (3 - 2 * k)
            self.angles = {n: a["start"].get(n, 0.0) + (v - a["start"].get(n, 0.0)) * k for n, v in a["target"].items()}
            self.height = a["h0"] + (a["h1"] - a["h0"]) * k
        else:
            self.angles, self.lift = self._keyframe(a)
        if done:
            self.animation = None
            self.lift = 0.0
            self.posed.emit()
        return not done

    def _keyframe(self, a):
        """关键帧之间做平滑插值，返回 (关节角, 机身抬高)"""
        t, previous, lift0 = a["t"], {}, 0.0
        for seconds, offsets, lift in a["keys"]:
            if t <= seconds:
                k = t / seconds
                k = k * k * (3 - 2 * k)
                names = set(previous) | set(offsets)
                mix = {n: previous.get(n, 0.0) + (offsets.get(n, 0.0) - previous.get(n, 0.0)) * k for n in names}
                angles = dict(a["base"])
                for joint in self.robot.movable:
                    if joint.name in mix:
                        angles[joint.name] = self.robot.clamp(joint, a["base"].get(joint.name, 0.0) + mix[joint.name])
                return angles, lift0 + (lift - lift0) * k
            t -= seconds
            previous, lift0 = offsets, lift
        return dict(a["base"]), 0.0

    # ---- 渲染输入 ----

    def display_angles(self):
        """叠加演示步态后的关节角：对角腿成对摆动、抬腿；只对 L2 腿部命名生效"""
        if not self.phase or not self.is_l2:
            return self.angles
        angles = dict(self.angles)
        for joint in self.robot.movable:
            leg, part = joint.name[:3], joint.name.split("_")[-2]
            wave = math.sin(self.phase + (0 if leg in ("FBL", "RAR") else math.pi))
            if part == "HIP":
                angles[joint.name] = self.robot.clamp(joint, angles[joint.name] + 0.22 * wave)
            elif part == "KNEE":
                angles[joint.name] = self.robot.clamp(joint, angles[joint.name] - 0.35 * max(wave, 0.0))
        return angles

    def base(self):
        m = model.rotation((0, 0, 1), self.heading)
        m[:3, 3] = (self.x, self.y, self.height + self.lift)
        return m

    def link_poses(self):
        return self.robot.poses(self.display_angles(), self.base())

    def link_matrix(self, poses, name):
        return np.asarray(poses[name] @ self.robot.links[name].visual, dtype=np.float32)
