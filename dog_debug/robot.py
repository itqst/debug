"""从 URDF 压缩包加载机器狗模型，计算各连杆位姿；只用 numpy，不依赖 Qt"""

import math
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import unquote, urlparse
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import numpy as np

MODELS = Path(__file__).parent / "data" / "models"
# 显示名 → 压缩包；内容是 L2 对外 URDF 原包，未改动
VARIANTS = {"L2 点足运动版": "L2_point_foot_V4.zip", "L2W 轮足运动版": "L2W_wheel_V4.zip"}
STL_RECORD = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def transform(xyz=(0, 0, 0), rpy=(0, 0, 0)):
    """URDF origin 转 4x4 齐次矩阵，旋转顺序与 URDF 一致：R = Rz(yaw)·Ry(pitch)·Rx(roll)"""
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    m = np.eye(4)
    m[:3, :3] = [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                 [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                 [-sp, cp * sr, cp * cr]]
    m[:3, 3] = xyz
    return m


def rotation(axis, angle):
    """绕单位轴旋转 angle 弧度的 4x4 矩阵"""
    x, y, z = axis
    c, s, t = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    m = np.eye(4)
    m[:3, :3] = [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
                 [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
                 [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]
    return m


def read_stl(data):
    """解析二进制 STL，返回 (三角形数, 3, 3) float32 顶点；包内网格均为二进制格式"""
    count = int(np.frombuffer(data, "<u4", 1, 80)[0])
    if 84 + count * STL_RECORD.itemsize != len(data):
        raise ValueError("不是二进制 STL")
    return np.frombuffer(data, STL_RECORD, count, 84)["v"].copy()


@dataclass
class Joint:
    name: str
    kind: str
    parent: str
    child: str
    origin: np.ndarray
    axis: tuple
    lower: float = -math.pi
    upper: float = math.pi

    @property
    def movable(self):
        return self.kind in ("revolute", "continuous", "prismatic")


@dataclass
class Link:
    name: str
    visual: np.ndarray
    color: tuple
    mesh: np.ndarray = field(repr=False, default=None)


@dataclass
class Robot:
    name: str
    links: dict
    joints: list
    # L2（内置）/ URDF / STEP / STL；只有 L2 才套用演示姿态和步态
    kind: str = "URDF"
    source: str = ""
    missing: list = field(default_factory=list)
    unit: str = "m"

    @property
    def movable(self):
        return [j for j in self.joints if j.movable]

    def clamp(self, joint, angle):
        return min(max(angle, joint.lower), joint.upper)

    def poses(self, angles=None, base=None):
        """各连杆世界位姿 {连杆名: 4x4}；angles 缺省的关节取 0 并夹到限位内"""
        angles = angles or {}
        world = {self.root: np.eye(4) if base is None else base}
        pending = list(self.joints)
        # URDF 关节顺序不保证父在前，按已知父连杆逐轮展开
        while pending:
            rest = []
            for j in pending:
                if j.parent not in world:
                    rest.append(j)
                    continue
                m = world[j.parent] @ j.origin
                if j.kind == "prismatic":
                    shift = np.eye(4)
                    shift[:3, 3] = np.array(j.axis) * self.clamp(j, angles.get(j.name, 0.0))
                    m = m @ shift
                elif j.movable:
                    m = m @ rotation(j.axis, self.clamp(j, angles.get(j.name, 0.0)))
                world[j.child] = m
            if len(rest) == len(pending):
                break
            pending = rest
        return world

    @property
    def root(self):
        children = {j.child for j in self.joints}
        return next(name for name in self.links if name not in children)


def _floats(text, default):
    return tuple(float(v) for v in text.split()) if text else default


def read_ascii_stl(data):
    """解析 ASCII STL，返回 (三角形数, 3, 3) float32"""
    values = [float(v) for line in data.decode("utf-8", "replace").splitlines()
              if line.strip().startswith("vertex") for v in line.split()[1:4]]
    return np.array(values, dtype=np.float32).reshape(-1, 3, 3)


def read_mesh_bytes(name, data):
    """按扩展名读网格：STL（二进制/ASCII）和 OBJ；其他格式抛 ValueError 说明原因"""
    suffix = PurePosixPath(name.lower()).suffix
    if suffix == ".stl":
        if data[:5].lower() == b"solid" and b"facet" in data[:400]:
            try:
                return read_stl(data)
            except ValueError:
                return read_ascii_stl(data)
        return read_stl(data)
    if suffix == ".obj":
        vertices, faces = [], []
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "v":
                vertices.append([float(v) for v in parts[1:4]])
            elif parts[0] == "f":
                index = [int(p.split("/")[0]) for p in parts[1:]]
                index = [i - 1 if i > 0 else len(vertices) + i for i in index]
                faces += [(index[0], index[k], index[k + 1]) for k in range(1, len(index) - 1)]
        return np.array(vertices, dtype=np.float32)[np.array(faces, dtype=np.int64)].reshape(-1, 3, 3)
    raise ValueError(f"不支持的网格格式 {suffix}（目前支持 STL、OBJ；DAE 等请先转成 STL）")


def primitive(geometry):
    """URDF 基本几何（box / cylinder / sphere）转三角网格，坐标在几何自身中心"""
    box, cylinder, sphere = geometry.find("box"), geometry.find("cylinder"), geometry.find("sphere")
    if box is not None:
        x, y, z = (v / 2 for v in _floats(box.get("size"), (0.1, 0.1, 0.1)))
        corners = np.array([[sx * x, sy * y, sz * z] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)], dtype=np.float32)
        quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
        return corners[np.array([t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))])]
    if cylinder is not None:
        r, h, n = float(cylinder.get("radius", 0.05)), float(cylinder.get("length", 0.1)), 32
        angles = np.linspace(0, 2 * math.pi, n, endpoint=False)
        ring = np.stack([r * np.cos(angles), r * np.sin(angles)], axis=1)
        tris = []
        for i in range(n):
            (x0, y0), (x1, y1) = ring[i], ring[(i + 1) % n]
            tris += [[(x0, y0, -h / 2), (x1, y1, -h / 2), (x1, y1, h / 2)], [(x0, y0, -h / 2), (x1, y1, h / 2), (x0, y0, h / 2)],
                     [(0, 0, h / 2), (x0, y0, h / 2), (x1, y1, h / 2)], [(0, 0, -h / 2), (x1, y1, -h / 2), (x0, y0, -h / 2)]]
        return np.array(tris, dtype=np.float32)
    if sphere is not None:
        r, rows, cols = float(sphere.get("radius", 0.05)), 12, 24
        def at(i, j):
            t, f = math.pi * i / rows, 2 * math.pi * j / cols
            return (r * math.sin(t) * math.cos(f), r * math.sin(t) * math.sin(f), r * math.cos(t))
        tris = []
        for i in range(rows):
            for j in range(cols):
                a, b, c, d = at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1)
                tris += [[a, b, c], [a, c, d]]
        return np.array(tris, dtype=np.float32)
    return None


def parse_xml(data):
    """解析 XML；声明为 GBK 等多字节编码时 expat 不支持，按声明用 Python 编解码后再解析"""
    try:
        return ET.fromstring(data)
    except ValueError as error:
        match = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([\w.-]+)[\"']", data)
        if not match or "multi-byte" not in str(error):
            raise
        text = data.decode(match.group(1).decode("ascii"))
        return ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1))


class ModelError(Exception):
    """模型打不开；missing 为缺失的网格 [(连杆, URDF 中写的路径, 尝试过的位置)]"""

    def __init__(self, message, missing=()):
        super().__init__(message)
        self.missing = list(missing)


class _Source:
    """URDF 所在的文件系统目录或 zip，统一按 POSIX 风格相对路径读文件"""

    def __init__(self, path):
        self.path = Path(path)
        self.archive = zipfile.ZipFile(self.path) if self.path.suffix.lower() == ".zip" else None
        if self.archive:
            # 大小写不敏感的索引：Windows 上导出的 URDF 常把 .STL 写成 .stl
            self.names = {n.lower(): n for n in self.archive.namelist()}

    def find_urdf(self):
        if self.archive:
            found = sorted((n for n in self.archive.namelist() if n.lower().endswith(".urdf")), key=lambda n: ("/urdf/" not in n, len(n)))
            return found[0] if found else None
        if self.path.is_file():
            return str(self.path)
        found = sorted(self.path.rglob("*.urdf"), key=lambda p: ("urdf" != p.parent.name, len(p.parts)))
        return str(found[0]) if found else None

    def read(self, name):
        if self.archive:
            return self.archive.read(name)
        return Path(name).read_bytes()

    def exists(self, name):
        """存在则返回实际名字（处理大小写差异），否则 None"""
        if self.archive:
            return self.names.get(posixpath.normpath(name).lower())
        candidate = Path(name)
        if candidate.is_file():
            return str(candidate)
        parent = candidate.parent
        if parent.is_dir():
            lower = candidate.name.lower()
            match = next((p for p in parent.iterdir() if p.name.lower() == lower), None)
            if match:
                return str(match)
        return None

    def join(self, base, relative):
        if self.archive:
            return posixpath.normpath(posixpath.join(base, relative))
        return str((Path(base) / relative).resolve())

    def parent(self, name):
        return posixpath.dirname(name) if self.archive else str(Path(name).parent)

    def close(self):
        if self.archive:
            self.archive.close()


def _candidates(source, urdf, filename, resource_dirs):
    """网格路径的候选位置：相对 URDF、package:// 按包名向上找、file://、绝对路径，以及用户指定的资源目录"""
    urdf_dir = source.parent(urdf)
    if filename.startswith("file://"):
        local = unquote(urlparse(filename).path)
        # file:///C:/x 解析出 /C:/x，去掉开头的斜杠
        return [local[1:] if len(local) > 2 and local[2] == ":" else local]
    if filename.startswith("package://"):
        package, _, rest = filename[10:].partition("/")
        out, cursor = [], urdf_dir
        # 向上逐级找目录名等于包名的包根，也看每一级下名为包名的子目录（ROS 工作区里兄弟包）
        while True:
            if _basename(source, cursor) == package:
                out.append(source.join(cursor, rest))
            out.append(source.join(source.join(cursor, package), rest))
            parent = source.parent(cursor)
            if parent == cursor or not parent:
                break
            cursor = parent
        # 兼容 SolidWorks 导出等包名和目录名对不上的模型：URDF 上一级当包根
        out.append(source.join(source.parent(urdf_dir) or urdf_dir, rest))
        # 用户选的资源目录可能是包根、包的上级，或直接放网格的目录
        for directory in resource_dirs:
            out += [str(Path(directory) / rest), str(Path(directory) / package / rest),
                    str(Path(directory) / PurePosixPath(rest).name)]
        return out
    if not source.archive and Path(filename).is_absolute():
        return [filename] + [str(Path(d) / Path(filename).name) for d in resource_dirs]
    out = [source.join(urdf_dir, filename)]
    for directory in resource_dirs:
        out += [str(Path(directory) / filename), str(Path(directory) / PurePosixPath(filename).name)]
    return out


def _basename(source, path):
    return posixpath.basename(path) if source.archive else Path(path).name


def _first_existing(source, tried):
    """按顺序找第一个存在的候选，返回 (字节, 实际路径)；zip 包之外的资源目录走本地文件"""
    for candidate in tried:
        real = source.exists(candidate)
        if real:
            return source.read(real), real
        if source.archive and Path(candidate).is_file():
            return Path(candidate).read_bytes(), candidate
    return None, None


def load_urdf(path, resource_dirs=(), allow_missing=False, cancelled=lambda: False, kind="URDF", name=None):
    """读取 URDF 文件、含 URDF 的目录或 zip 包，返回 Robot

    多个 visual 合并到同一连杆；网格支持 STL/OBJ 和 box/cylinder/sphere。找不到的网格汇总后抛 ModelError，
    allow_missing 为 True 时跳过它们照常返回（robot.missing 记录跳过的项）。cancelled() 为真时抛 ModelError("已取消")
    """
    source = _Source(path)
    try:
        urdf = source.find_urdf()
        if not urdf:
            raise ModelError(f"{path} 里没有找到 .urdf 文件")
        try:
            root = parse_xml(source.read(urdf))
        except (ET.ParseError, LookupError, UnicodeDecodeError) as error:
            raise ModelError(f"URDF 解析失败：{urdf}：{error}")
        if root.tag != "robot":
            raise ModelError(f"{urdf} 不是 URDF（根节点是 <{root.tag}>）")
        materials = {m.get("name"): _floats(m.find("color").get("rgba"), None)
                     for m in root.findall("material") if m.find("color") is not None}
        links, missing = {}, []
        for node in root.findall("link"):
            if cancelled():
                raise ModelError("已取消")
            parts, color = [], None
            for visual in node.findall("visual"):
                origin = visual.find("origin")
                pose = transform(_floats(origin.get("xyz") if origin is not None else "", (0, 0, 0)),
                                 _floats(origin.get("rpy") if origin is not None else "", (0, 0, 0)))
                material = visual.find("material")
                if material is not None and color is None:
                    rgba = material.find("color")
                    color = _floats(rgba.get("rgba"), None) if rgba is not None else materials.get(material.get("name"))
                geometry = visual.find("geometry")
                if geometry is None:
                    continue
                mesh = geometry.find("mesh")
                if mesh is None:
                    triangles = primitive(geometry)
                else:
                    filename = mesh.get("filename", "")
                    tried = _candidates(source, urdf, filename, resource_dirs)
                    data, found = _first_existing(source, tried)
                    if data is None:
                        missing.append((node.get("name"), filename, tried))
                        continue
                    try:
                        triangles = read_mesh_bytes(found, data)
                    except ValueError as error:
                        missing.append((node.get("name"), filename, [str(error)]))
                        continue
                    scale = _floats(mesh.get("scale"), None)
                    if scale:
                        triangles = triangles * np.array(scale, dtype=np.float32)
                if triangles is not None and len(triangles):
                    # visual 的 origin 直接烘进顶点，连杆只保留一份网格
                    flat = triangles.reshape(-1, 3) @ pose[:3, :3].T.astype(np.float32) + pose[:3, 3].astype(np.float32)
                    parts.append(flat.reshape(-1, 3, 3))
            link = Link(node.get("name"), np.eye(4), color or (0.75, 0.75, 0.75, 1))
            link.mesh = np.concatenate(parts) if parts else None
            links[link.name] = link
        if missing and not allow_missing:
            raise ModelError(f"有 {len(missing)} 个网格找不到或读不了", missing)
        joints = []
        for node in root.findall("joint"):
            origin, axis, limit = node.find("origin"), node.find("axis"), node.find("limit")
            joint = Joint(node.get("name"), node.get("type"), node.find("parent").get("link"), node.find("child").get("link"),
                          transform(_floats(origin.get("xyz") if origin is not None else "", (0, 0, 0)),
                                    _floats(origin.get("rpy") if origin is not None else "", (0, 0, 0))),
                          _floats(axis.get("xyz") if axis is not None else "", (1, 0, 0)))
            norm = math.sqrt(sum(v * v for v in joint.axis)) or 1
            joint.axis = tuple(v / norm for v in joint.axis)
            if limit is not None and joint.kind in ("revolute", "prismatic"):
                joint.lower, joint.upper = float(limit.get("lower", -math.pi)), float(limit.get("upper", math.pi))
            elif joint.kind == "prismatic":
                joint.lower, joint.upper = -1.0, 1.0
            joints.append(joint)
        if not links:
            raise ModelError(f"{urdf} 里没有 <link>")
        robot = Robot(name or root.get("name") or Path(path).stem, links, joints)
        robot.kind, robot.source, robot.missing, robot.unit = kind, str(path), missing, "m"
        return robot
    finally:
        source.close()


def load(variant):
    """读取内置模型；网格从压缩包直接读取，不解压到磁盘"""
    return load_urdf(MODELS / VARIANTS[variant], kind="L2", name=variant)


def mesh_model(name, parts, kind, source, unit):
    """把若干网格部件（STEP/STL）包成没有可动关节的 Robot；parts 为 [(名字, 三角形, 颜色)]"""
    links = {"__root__": Link("__root__", np.eye(4), (0.75, 0.75, 0.75, 1))}
    joints = []
    for index, (part, triangles, color) in enumerate(parts):
        key = f"{index:04d} {part}"
        link = Link(key, np.eye(4), color or (0.80, 0.81, 0.84, 1))
        link.mesh = triangles
        links[key] = link
        joints.append(Joint(key + " fixed", "fixed", "__root__", key, np.eye(4), (1, 0, 0)))
    robot = Robot(name, links, joints)
    robot.kind, robot.source, robot.missing, robot.unit = kind, str(source), [], unit
    return robot


def load_stl(path, unit="m"):
    """直接打开 STL；STL 没有单位，unit 为 mm 时缩成米"""
    triangles = read_mesh_bytes(str(path), Path(path).read_bytes())
    if unit == "mm":
        triangles = triangles / 1000
    return mesh_model(Path(path).stem, [(Path(path).stem, triangles, None)], "STL", path, unit)


def bounds(robot, angles=None):
    """所有网格在当前姿态下的包围盒 (min xyz, max xyz)；没有网格时返回原点"""
    world = robot.poses(angles)
    lows, highs = [], []
    for name, link in robot.links.items():
        if link.mesh is None or not len(link.mesh) or name not in world:
            continue
        m = world[name] @ link.visual
        points = link.mesh.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
        lows.append(points.min(axis=0))
        highs.append(points.max(axis=0))
    if not lows:
        return np.zeros(3), np.zeros(3)
    return np.min(lows, axis=0), np.max(highs, axis=0)


# 演示姿态：按腿部关节名后缀给角度（弧度），四条腿相同；数值取自限位范围内，仅用于展示
POSES = {
    "站立": {"ABAD": 0.0, "HIP": 0.75, "KNEE": -1.5},
    "半蹲": {"ABAD": 0.0, "HIP": 1.05, "KNEE": -2.1},
    "趴下": {"ABAD": 0.0, "HIP": 1.25, "KNEE": -2.70},
    "零位": {},
}


def pose_angles(robot, pose):
    """把演示姿态展开成每个可动关节的角度，轮子等不在表内的关节保持 0"""
    table = POSES[pose]
    angles = {}
    for j in robot.movable:
        part = j.name.split("_")[-2] if j.name.count("_") >= 2 else ""
        angles[j.name] = robot.clamp(j, table.get(part, 0.0))
    return angles


def ground_height(robot, angles):
    """各连杆网格的最低点，用来把模型抬到刚好落在地面上"""
    return -float(bounds(robot, angles)[0][2])
