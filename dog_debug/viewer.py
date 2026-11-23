"""OpenGL 3D 视图：渲染 Scene 中的机器狗、地面网格、轨迹、投影和雷达点云；依赖 pyqtgraph + PyOpenGL"""

import math

import numpy as np
import pyqtgraph.opengl as gl
from pyqtgraph.opengl import shaders
from PySide6.QtGui import QMatrix4x4, QSurfaceFormat, QVector3D

from . import robot as robot_module

BACKGROUND = (244, 245, 248)
# 深色外观的视图背景和网格
DARK_BACKGROUND, DARK_GRID = (28, 28, 30), (235, 235, 245, 45)
# URDF 只给了两档灰度：浅色件做白色外壳，深色件做石墨色
SHELL, GRAPHITE, HIGHLIGHT = (0.93, 0.94, 0.96, 1.0), (0.23, 0.24, 0.27, 1.0), (0.0, 0.48, 1.0, 1.0)

# 视图空间两盏方向光 + 半球环境光 + 高光 + 冷色轮廓光，接近产品渲染的质感
STUDIO = shaders.ShaderProgram("dog_studio", [
    shaders.VertexShader("""
        varying vec3 normal;
        varying vec3 position;
        void main() {
            normal = normalize(gl_NormalMatrix * gl_Normal);
            position = vec3(gl_ModelViewMatrix * gl_Vertex);
            gl_FrontColor = gl_Color;
            gl_BackColor = gl_Color;
            gl_Position = ftransform();
        }
    """),
    shaders.FragmentShader("""
        varying vec3 normal;
        varying vec3 position;
        void main() {
            vec3 n = normalize(normal);
            if (!gl_FrontFacing) n = -n;
            vec3 v = normalize(-position);
            vec3 key = normalize(vec3(0.45, 0.65, 0.9));
            vec3 fill = normalize(vec3(-0.7, 0.1, 0.5));
            float diffuse = max(dot(n, key), 0.0) * 0.68 + max(dot(n, fill), 0.0) * 0.22;
            float ambient = 0.24 + 0.10 * (0.5 + 0.5 * n.y);
            float spec = pow(max(dot(n, normalize(key + v)), 0.0), 40.0) * 0.28;
            float rim = pow(1.0 - max(dot(n, v), 0.0), 3.0) * 0.16;
            vec3 base = gl_Color.rgb;
            gl_FragColor = vec4(base * (ambient + diffuse) + vec3(spec) + rim * vec3(0.55, 0.75, 1.0), gl_Color.a);
        }
    """),
])


# 点云着色：低 → 高依次为蓝、青、绿、黄、红
RAMP = np.array([(0.18, 0.33, 0.95), (0.10, 0.75, 0.90), (0.25, 0.80, 0.35), (0.98, 0.80, 0.15), (0.92, 0.25, 0.20)],
                dtype=np.float32)


def cloud_colors(cloud, mode, alpha=1.0):
    """按高度（z，取 2%~98% 分位，避开个别离群点）或强度（0~255）给每个点上色，返回 (N,4) float32；
    cloud 可以是 pointcloud.Cloud，也可以是累加的地图（有 xyz、intensity 即可）"""
    if mode == "强度":
        value = cloud.intensity.astype(np.float32) / 255.0
    else:
        z = cloud.xyz[:, 2]
        low, high = np.percentile(z, (2, 98)) if len(z) else (0.0, 1.0)
        value = (z - low) / max(float(high - low), 1e-3)
    value = np.clip(value, 0.0, 1.0) * (len(RAMP) - 1)
    index = np.minimum(value.astype(np.int32), len(RAMP) - 2)
    k = (value - index)[:, None]
    rgb = RAMP[index] * (1 - k) + RAMP[index + 1] * k
    return np.hstack([rgb, np.full((len(rgb), 1), alpha, dtype=np.float32)]).astype(np.float32)


def ellipse(rx, ry, segments=48):
    """地面投影用的椭圆扇形网格"""
    angles = np.linspace(0, 2 * math.pi, segments, endpoint=False)
    rim = np.stack([rx * np.cos(angles), ry * np.sin(angles), np.zeros(segments)], axis=1)
    vertices = np.vstack([[0, 0, 0], rim]).astype(np.float32)
    faces = np.array([[0, i + 1, (i + 1) % segments + 1] for i in range(segments)], dtype=np.int32)
    return vertices, faces


class RobotView(gl.GLViewWidget):
    """跟随 Scene.changed 刷新；follow 为 True 时镜头中心跟着机身走"""

    def __init__(self, scene):
        super().__init__()
        surface = QSurfaceFormat()
        surface.setSamples(4)
        surface.setDepthBufferSize(24)
        self.setFormat(surface)
        self.scene = scene
        self.follow = True
        self.meshes, self.highlighted, self.model = {}, None, None
        self.setBackgroundColor(BACKGROUND)
        self.grid = gl.GLGridItem(size=QVector3D(12, 12, 1))
        self.grid.setSpacing(0.25, 0.25, 1)
        self.grid.setColor((60, 60, 67, 70))
        self.addItem(self.grid)
        self.background = BACKGROUND
        vertices, faces = ellipse(0.42, 0.24)
        self.shadow = gl.GLMeshItem(vertexes=vertices, faces=faces, color=(0, 0, 0, 0.09), smooth=False,
                                    glOptions="translucent", drawEdges=False)
        self.addItem(self.shadow)
        self.trail = gl.GLLinePlotItem(pos=np.zeros((1, 3)), color=(0.0, 0.48, 1.0, 0.85), width=3, antialias=True)
        self.addItem(self.trail)
        # 雷达点云挂在机身位置上（点在雷达坐标系里）；默认的 additive 混合在浅色背景上看不见，改半透明
        self.cloud = gl.GLScatterPlotItem(pos=np.zeros((1, 3), dtype=np.float32), size=4.0, pxMode=True)
        self.cloud.setGLOptions("translucent")
        # 最后画：半透明的点本来就该在实体之后画；另外 4× 多重采样下（Linux Mesa 实测）点先于网格画会丢掉每个点的颜色
        self.cloud.setDepthValue(10)
        self.cloud.setVisible(False)
        self.addItem(self.cloud)
        # 累加的地图（新格式才有）：点小一些、画在实时帧下面；雷达走过的轨迹和当前位置
        self.cloud_map = gl.GLScatterPlotItem(pos=np.zeros((1, 3), dtype=np.float32), size=2.0, pxMode=True)
        self.cloud_map.setGLOptions("translucent")
        self.cloud_map.setDepthValue(9)
        self.cloud_map.setVisible(False)
        self.addItem(self.cloud_map)
        self.lidar_trail = gl.GLLinePlotItem(pos=np.zeros((1, 3)), color=(0.95, 0.25, 0.2, 0.9), width=3, antialias=True)
        self.lidar_trail.setDepthValue(11)
        self.lidar_trail.setVisible(False)
        self.addItem(self.lidar_trail)
        self.lidar_mark = gl.GLScatterPlotItem(pos=np.zeros((1, 3), dtype=np.float32), size=14.0, pxMode=True,
                                               color=(0.95, 0.25, 0.2, 1.0))
        self.lidar_mark.setDepthValue(12)
        self.lidar_mark.setVisible(False)
        self.addItem(self.lidar_mark)
        self.cloud_on, self.cloud_mode, self.lidar_height, self.cloud_stale = True, "高度", 0.45, False
        self.map_on = True
        self.reset_camera()
        scene.changed.connect(self.sync)
        scene.clouded.connect(self.sync_cloud)
        if scene.robot:
            self.rebuild()
            self.fit()

    def reset_camera(self):
        self.view("等轴")

    def _extent(self):
        """当前模型在世界中的包围盒中心和对角线长度"""
        low, high = robot_module.bounds(self.scene.robot, self.scene.display_angles()) if self.scene.robot else (np.zeros(3), np.ones(3))
        base = self.scene.base()
        low, high = low + base[:3, 3], high + base[:3, 3]
        return (low + high) / 2, max(float(np.linalg.norm(high - low)), 0.01)

    def set_dark(self, dark):
        """切换深浅外观：只换背景和网格，模型配色不变"""
        self.background = DARK_BACKGROUND if dark else BACKGROUND
        self.setBackgroundColor(self.background)
        self.grid.setColor(DARK_GRID if dark else (60, 60, 67, 70))

    def fit(self):
        """镜头距离和地面网格按模型尺寸自适应：小零件和整机都能一眼看全"""
        center, size = self._extent()
        step = 10 ** math.floor(math.log10(size / 2))
        self.grid.setSpacing(step, step, 1)
        self.grid.setSize(step * 48, step * 48, 1)
        self.opts["center"] = QVector3D(*center)
        self.opts["distance"] = size * 1.6
        self.update()

    VIEWS = {"前": (0, 5), "侧": (90, 5), "顶": (-90, 89), "等轴": (-55, 22)}

    def view(self, name):
        """标准视角：前（从 +x 看）、侧（从 +y 看）、顶、等轴；同时适配视口"""
        azimuth, elevation = self.VIEWS[name]
        self.opts["azimuth"], self.opts["elevation"] = azimuth, elevation
        self.fit()

    def rebuild(self):
        """换模型时先移除旧网格再重建，重复导入不会累积；L2 按 URDF 深浅分成外壳和石墨两色，其他模型用自带颜色"""
        for item in self.meshes.values():
            self.removeItem(item)
        robot = self.scene.robot
        self.meshes, self.highlighted, self.model = {}, None, robot
        # 地面投影按 L2 机身比例画，其他模型不显示
        self.shadow.setVisible(robot.kind == "L2")
        for name, link in robot.links.items():
            if link.mesh is None or not len(link.mesh):
                continue
            if robot.kind == "L2":
                color = GRAPHITE if link.color[0] < 0.6 else SHELL
            else:
                color = tuple(float(c) for c in link.color[:3]) + (1.0,)
            item = gl.GLMeshItem(vertexes=link.mesh, smooth=False, color=color, shader=STUDIO, glOptions="opaque")
            item.base_color = color
            self.addItem(item)
            self.meshes[name] = item
        self.sync()

    def highlight(self, link):
        """把指定连杆染成强调色，None 取消"""
        if self.highlighted in self.meshes:
            self.meshes[self.highlighted].setColor(self.meshes[self.highlighted].base_color)
        self.highlighted = link
        if link in self.meshes:
            self.meshes[link].setColor(HIGHLIGHT)

    def set_cloud_options(self, visible=None, mode=None, lidar_height=None, map_visible=None):
        """显示开关、着色方式（高度/强度）、雷达离地高度（米）、累加地图开关，改完立即重画"""
        if map_visible is not None:
            self.map_on = map_visible
        if visible is not None:
            self.cloud_on = visible
        if mode is not None:
            self.cloud_mode = mode
        if lidar_height is not None:
            self.lidar_height = lidar_height
        self.sync_cloud()

    def sync_cloud(self):
        """点云换帧、清空或改选项时重算点和颜色；位置跟随在 sync 里只改变换矩阵"""
        cloud = self.scene.cloud
        shown = bool(self.cloud_on and cloud is not None and len(cloud.xyz))
        self.cloud.setVisible(shown)
        if shown:
            self.cloud_stale = not self.scene.cloud_live
            self.cloud.setData(pos=cloud.xyz, color=cloud_colors(cloud, self.cloud_mode, 0.35 if self.cloud_stale else 0.95))
            self._place_cloud()
        self._sync_map()
        self.update()

    def _sync_map(self):
        """累加地图和雷达轨迹：都在原点坐标系里，只按离地高度抬起，不跟着狗走"""
        acc = self.scene.cloud_map
        shown = bool(self.map_on and len(acc.xyz))
        self.cloud_map.setVisible(shown)
        lift = QMatrix4x4()
        lift.translate(0, 0, self.lidar_height)
        if shown:
            self.cloud_map.setData(pos=acc.xyz, color=cloud_colors(acc, self.cloud_mode, 0.55))
            self.cloud_map.setTransform(lift)
        has_trail = bool(self.map_on and acc.trail)
        self.lidar_trail.setVisible(has_trail and len(acc.trail) > 1)
        self.lidar_mark.setVisible(has_trail)
        if has_trail:
            trail = np.array(acc.trail, dtype=np.float32)
            if len(trail) > 1:
                self.lidar_trail.setData(pos=trail)
                self.lidar_trail.setTransform(lift)
            self.lidar_mark.setData(pos=trail[-1:])
            self.lidar_mark.setTransform(lift)

    def _place_cloud(self):
        scene = self.scene
        matrix = QMatrix4x4()
        if scene.cloud is not None and scene.cloud.in_map:
            # 新格式已经是原点坐标系（原点 = 建图开始时雷达的位置），不跟着狗走，只抬到雷达离地的高度
            matrix.translate(0, 0, self.lidar_height)
        else:
            matrix.translate(scene.x, scene.y, self.lidar_height)
            matrix.rotate(math.degrees(scene.heading), 0, 0, 1)
        self.cloud.setTransform(matrix)

    def sync(self):
        scene = self.scene
        if self.cloud.visible():
            self._place_cloud()
            # 点云停发后变淡，最后一帧仍然留着
            if self.cloud_stale == scene.cloud_live:
                self.sync_cloud()
        if not scene.robot:
            return
        if scene.robot is not self.model:
            self.rebuild()
            self.fit()
            return
        poses = scene.link_poses()
        for name, item in self.meshes.items():
            item.setTransform(QMatrix4x4(*scene.link_matrix(poses, name).flatten().tolist()))
        shadow = QMatrix4x4()
        shadow.translate(scene.x, scene.y, 0.002)
        shadow.rotate(math.degrees(scene.heading), 0, 0, 1)
        self.shadow.setTransform(shadow)
        if len(scene.trail) > 1:
            self.trail.setData(pos=np.array([(x, y, 0.004) for x, y in scene.trail], dtype=np.float32))
        else:
            self.trail.setData(pos=np.zeros((1, 3), dtype=np.float32))
        if self.follow:
            self.opts["center"] = QVector3D(scene.x, scene.y, 0.25)
        self.update()
