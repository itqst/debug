"""雷达点云（pcd）：解码、入库前瘦身、沙盒演示帧；只依赖 numpy，界面和测试共用

两处 Topic 的 data 完全一样（MQTT通信协议-点云数据pcd.md）：
- 云端 `tta/robotdog/{sn}/state`，`method=pcd`，GW 只在建图中转发
- 本地 `local/robotdog/ros2bridge/data`，`data.type=pointcloud`，ros2bridge 采集时约 2 Hz

每点 7 字节：小端 int16 x/y/z + uint8 强度，偏移 = 整数 × scale
- 设备端点云程序 3.1.1 起：frame_id=map，带 o_x/o_y/o_z/o_yaw，点在原点坐标系（建图原点）里的坐标 = 偏移 + o
- 更早的旧格式：没有 o_x，点在雷达坐标系里（随狗移动）
"""

import base64
import math
import time
from dataclasses import dataclass

import numpy as np

ENCODING = "xyz_i16_i8"
POINT = np.dtype([("x", "<i2"), ("y", "<i2"), ("z", "<i2"), ("i", "u1")])
SCALES = (0.001, 0.002, 0.005, 0.01)


def cloud_data(event):
    """是点云消息就返回它的 data 对象，否则 None"""
    data = event.payload.get("data")
    if not isinstance(data, dict):
        return None
    if event.topic.endswith("/state") and event.payload.get("method") == "pcd":
        return data
    if event.topic.endswith("/ros2bridge/data") and data.get("type") == "pointcloud":
        return data
    return None


@dataclass
class Cloud:
    xyz: np.ndarray          # (N, 3) float32，米；in_map 时是原点坐标系，否则是雷达坐标系
    intensity: np.ndarray    # (N,) uint8
    meta: dict               # data 里除 points 以外的字段
    received: float          # 本机收到的时刻 time.monotonic()
    origin: tuple = None     # (o_x, o_y, o_z, o_yaw)：本帧雷达在原点坐标系里的位置和狗的朝向；旧格式为 None

    @property
    def in_map(self):
        return self.origin is not None


class CloudError(ValueError):
    pass


def decode(data, received=None):
    """按协议解析 data；encoding 不认识、长度和 count 对不上都报错，不硬解"""
    if data.get("encoding") != ENCODING:
        raise CloudError(f"不认识的 encoding：{data.get('encoding')}")
    try:
        raw = base64.b64decode(data.get("points", ""), validate=True)
    except (ValueError, TypeError) as error:
        raise CloudError(f"points 不是合法 base64：{error}") from None
    count, scale = data.get("count"), data.get("scale")
    if not isinstance(count, int) or len(raw) != count * POINT.itemsize:
        raise CloudError(f"points 解出 {len(raw)} 字节，count={count} 应为 {count * 7 if isinstance(count, int) else '?'} 字节")
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        raise CloudError(f"scale 不对：{scale}")
    origin = _origin(data)
    points = np.frombuffer(raw, dtype=POINT)
    xyz = np.stack([points["x"], points["y"], points["z"]], axis=1).astype(np.float32) * np.float32(scale)
    if origin is not None:
        xyz += np.array(origin[:3], dtype=np.float32)
    meta = {k: v for k, v in data.items() if k != "points"}
    return Cloud(xyz, points["i"].copy(), meta, time.monotonic() if received is None else received, origin)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _origin(data):
    """o_x/o_y/o_z 三个都在才是新格式；只有一部分、或不是数字都报错。o_yaw 缺了按 0"""
    present = [k for k in ("o_x", "o_y", "o_z") if k in data]
    if not present:
        return None
    if len(present) != 3 or not all(_number(data[k]) for k in ("o_x", "o_y", "o_z")):
        raise CloudError(f"o_x/o_y/o_z 不全或不是数字：{ {k: data.get(k) for k in ('o_x', 'o_y', 'o_z')} }")
    yaw = data.get("o_yaw", 0.0)
    if not _number(yaw):
        raise CloudError(f"o_yaw 不是数字：{yaw}")
    return (float(data["o_x"]), float(data["o_y"]), float(data["o_z"]), float(yaw))


def slim(payload):
    """入库用：points 换成一句说明，其余字段原样。一条约 50 KB，2 Hz 一小时就是几百 MB，原始点只留在内存里显示"""
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("points"), str):
        return payload
    return {**payload, "data": {**data, "points": f"<base64 {len(data['points'])} 字符，未入库>"}}


def encode(xyz, intensity=None, scale=None):
    """把 (N,3) 米坐标编成 data 里的 encoding/scale/count/points；scale 按最远坐标选，和设备端规则一致"""
    xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
    if scale is None:
        far = float(np.abs(xyz).max()) if len(xyz) else 0.0
        scale = next((s for s in SCALES if far / s <= 32767), SCALES[-1])
    points = np.zeros(len(xyz), dtype=POINT)
    # 和设备端 TTA_RB_Encoder 一样：按最远坐标选 scale，量化后截在 ±32767
    quantized = np.clip(np.round(xyz / scale), -32767, 32767).astype("<i2")
    points["x"], points["y"], points["z"] = quantized[:, 0], quantized[:, 1], quantized[:, 2]
    points["i"] = 0 if intensity is None else np.clip(np.asarray(intensity), 0, 255).astype("u1")
    return {"encoding": ENCODING, "scale": scale, "count": len(points),
            "points": base64.b64encode(points.tobytes()).decode("ascii")}


def demo_frame(seq, sweep=0.0, in_map=True):
    """沙盒演示用的一帧：6×4 m 房间的墙和地面、一张桌子；不是设备数据
    in_map=True（新格式）：房间固定在原点坐标系里，雷达沿半径 1 m 的圆走，点是相对雷达的偏移并带 o_*；
    in_map=False（旧格式）：房间绕雷达慢慢转，看得出是在刷新
    """
    rng = np.random.default_rng(seq)
    walls = []
    for x0, y0, x1, y1 in ((-3, -2, 3, -2), (3, -2, 3, 2), (3, 2, -3, 2), (-3, 2, -3, -2)):
        t = rng.random(850)
        walls.append(np.stack([x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, rng.random(850) * 2.4 - 0.4], axis=1))
    floor = np.stack([rng.random(1600) * 6 - 3, rng.random(1600) * 4 - 2, np.full(1600, -0.4)], axis=1)
    desk = np.stack([rng.random(400) * 1.2 + 0.8, rng.random(400) * 0.6 - 1.2, np.full(400, 0.35)], axis=1)
    xyz = np.vstack(walls + [floor, desk])
    xyz += rng.normal(0, 0.01, xyz.shape)
    intensity = np.clip(40 + (xyz[:, 2] + 0.4) * 50 + rng.normal(0, 10, len(xyz)), 0, 255)
    angle = sweep + seq * 0.02
    head = {"type": "pointcloud", "source": "/front_lidar", "stamp": int(time.time() * 1000), "seq": seq}
    tail = {"src_count": len(xyz) * 4, "leaf": 0.08}
    if not in_map:
        c, s = math.cos(angle), math.sin(angle)
        xyz[:, :2] = xyz[:, :2] @ np.array([[c, s], [-s, c]])
        return {**head, "frame_id": "livox_frame", **encode(xyz, intensity), **tail}
    origin = (math.cos(angle), math.sin(angle), 0.0)
    yaw = math.atan2(math.sin(angle + math.pi / 2), math.cos(angle + math.pi / 2))
    origin_fields = {"o_x": round(origin[0], 3), "o_y": round(origin[1], 3), "o_z": origin[2], "o_yaw": round(yaw, 4)}
    offsets = xyz - np.array([origin_fields["o_x"], origin_fields["o_y"], origin_fields["o_z"]])
    return {**head, "frame_id": "map", **origin_fields, **encode(offsets, intensity), **tail}


class MapAccumulator:
    """把新格式（原点坐标系）的每一帧累加成一张地图：按体素去重，先到的点留下；同时记雷达走过的轨迹

    只用于建图时看效果：点是稀疏后的，也没有回环优化，和 SLAM 保存的 map.pcd 会有偏差
    """

    def __init__(self, voxel=0.1, max_points=600_000):
        self.voxel, self.max_points = voxel, max_points
        self.clear()

    def clear(self):
        self.keys = np.zeros(0, dtype=np.int64)
        self.xyz = np.zeros((0, 3), dtype=np.float32)
        self.intensity = np.zeros(0, dtype=np.uint8)
        self.trail = []          # [(o_x, o_y, o_z)]，按帧
        self.frames = 0
        self.full = False

    def add(self, cloud):
        """累加一帧；旧格式（雷达坐标系）不累加，返回 False"""
        if not cloud.in_map:
            return False
        self.frames += 1
        o = cloud.origin[:3]
        if not self.trail or math.dist(self.trail[-1], o) > 0.05:
            self.trail.append(o)
        if self.full or not len(cloud.xyz):
            return True
        cell = np.floor(cloud.xyz / self.voxel).astype(np.int64)
        # 每轴 21 位、偏移 2^20：±10 万个体素（10 cm 时 ±100 km），够用
        keys = ((cell[:, 0] + (1 << 20)) << 42) | ((cell[:, 1] + (1 << 20)) << 21) | (cell[:, 2] + (1 << 20))
        all_keys = np.concatenate([self.keys, keys])
        _, first = np.unique(all_keys, return_index=True)
        first.sort()
        self.keys = all_keys[first]
        self.xyz = np.concatenate([self.xyz, cloud.xyz])[first]
        self.intensity = np.concatenate([self.intensity, cloud.intensity])[first]
        if len(self.keys) >= self.max_points:
            self.full = True
        return True

    @property
    def distance(self):
        """雷达走过的路程，米"""
        return float(sum(math.dist(a, b) for a, b in zip(self.trail, self.trail[1:])))


def summary(cloud, rate=None):
    """界面上一行说明：点数、稀疏程度、频率、多久前"""
    meta = cloud.meta
    parts = [f"{len(cloud.xyz)} 点"]
    if isinstance(meta.get("src_count"), int):
        parts[0] += f" / 原始 {meta['src_count']}"
    if isinstance(meta.get("leaf"), (int, float)):
        parts.append(f"体素 {meta['leaf'] * 100:.1f} cm")
    if isinstance(meta.get("scale"), (int, float)):
        parts.append(f"精度 {meta['scale'] * 1000:g} mm")
    if cloud.in_map:
        o_x, o_y, _, yaw = cloud.origin
        parts.append(f"地图坐标，雷达在 ({o_x:.2f}, {o_y:.2f}) 朝向 {math.degrees(yaw):.0f}°")
    else:
        parts.append("雷达坐标（旧格式）")
    if rate:
        parts.append(f"{rate:.1f} Hz")
    parts.append(f"{time.monotonic() - cloud.received:.1f} s 前")
    return " · ".join(parts)
