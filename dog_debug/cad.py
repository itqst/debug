"""STEP/STP 读取与网格化：在独立进程里用 OpenCASCADE（cadquery-ocp）做，可随时终止，不阻塞界面

这里不导入 Qt；子进程只依赖 numpy 和 OCP。结果是带部件名、颜色和装配位置的三角网格，长度统一换算为米
"""

import multiprocessing
import queue
import re
import time
from pathlib import Path

import numpy as np

# 随包附带的 STEP 装配样件（OCP 生成，毫米制两部件），自检和测试用；生成脚本见 tests/data/gen_sample_step.py
SAMPLE = Path(__file__).parent / "data" / "samples" / "sample_assembly.step"
# STEP 头里的长度单位写法 → 显示名
UNIT_PREFIX = {"MILLI": "mm", "CENTI": "cm", "DECI": "dm", "": "m", "KILO": "km", "MICRO": "µm"}
# OpenCASCADE 内部长度单位（xstep.cascade.unit）到米的换算
CASCADE_UNITS = {"MM": 0.001, "CM": 0.01, "M": 1.0, "KM": 1000.0, "INCH": 0.0254, "IN": 0.0254, "FT": 0.3048, "UM": 1e-6}


def file_unit(path):
    """从 STEP 文本里找长度单位，找不到返回空串；只读文件开头的定义段"""
    with open(path, "rb") as source:
        text = source.read(4 * 1024 * 1024).decode("latin-1", "replace").upper()
    match = re.search(r"SI_UNIT\(\s*(?:\.(\w+)\.|\$)\s*,\s*\.METRE\.\s*\)", text)
    if match:
        prefix = match.group(1) or ""
        return UNIT_PREFIX.get(prefix, prefix.lower())
    match = re.search(r"CONVERSION_BASED_UNIT\(\s*'([^']+)'", text)
    return match.group(1).lower() if match else ""


def _label_name(label):
    from OCP.TDataStd import TDataStd_Name
    attribute = TDataStd_Name()
    if not label.FindAttribute(TDataStd_Name.GetID_s(), attribute):
        return ""
    name = attribute.Get().ToExtString()
    # 有些导出工具把 UTF-8 字节直接写进 STEP 字符串（标准应为 \X2\ 转义），读回来成了 Latin-1 乱码，这里还原
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _label_color(color_tool, label, shape):
    from OCP.Quantity import Quantity_Color
    from OCP.XCAFDoc import XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_ColorTool
    color = Quantity_Color()
    for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
        # 按标签取色在这版 OCP 里是静态方法，按形体取色是实例方法
        if XCAFDoc_ColorTool.GetColor_s(label, kind, color) or color_tool.GetColor(shape, kind, color):
            return (color.Red(), color.Green(), color.Blue(), 1.0)
    return None


def _triangles(shape, deflection):
    """把一个形体网格化成 (n, 3, 3) float32；面朝向反转时交换顶点顺序，法线朝外"""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    BRepMesh_IncrementalMesh(shape, deflection, False, 0.5, True)
    chunks = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        location = TopLoc_Location()
        mesh = BRep_Tool.Triangulation_s(face, location)
        if mesh is not None:
            trsf = location.Transformation()
            nodes = np.array([(lambda p: (p.X(), p.Y(), p.Z()))(mesh.Node(i)) for i in range(1, mesh.NbNodes() + 1)], dtype=np.float64)
            if not location.IsIdentity():
                m = np.array([[trsf.Value(r, c) for c in range(1, 5)] for r in range(1, 4)])
                nodes = nodes @ m[:, :3].T + m[:, 3]
            index = np.array([mesh.Triangle(i).Get() for i in range(1, mesh.NbTriangles() + 1)], dtype=np.int64) - 1
            if face.Orientation() == TopAbs_REVERSED:
                index = index[:, [0, 2, 1]]
            chunks.append(nodes[index].astype(np.float32))
        explorer.Next()
    return np.concatenate(chunks) if chunks else np.zeros((0, 3, 3), np.float32)


def _worker(path, output, events):
    """子进程入口：读 STEP、按装配展开部件、网格化，结果写到 output（npz），进度和错误经 events 回报"""
    try:
        events.put(("stage", "正在读取 STEP 文件…"))
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.Interface import Interface_Static
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TCollection import TCollection_AsciiString
        from OCP.TDF import TDF_Label, TDF_LabelSequence, TDF_Tool
        from OCP.TDocStd import TDocStd_Document
        from OCP.TopLoc import TopLoc_Location
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        document = TDocStd_Document(TCollection_ExtendedString("step"))
        reader = STEPCAFControl_Reader()
        # 建 reader 时会重置单位参数，之后再设；最后按实际生效的单位统一换算到米
        Interface_Static.SetCVal_s("xstep.cascade.unit", "M")
        reader.SetNameMode(True)
        reader.SetColorMode(True)
        if reader.ReadFile(str(path)) != IFSelect_RetDone:
            events.put(("error", "OpenCASCADE 无法读取该文件（不是有效的 STEP，或文件损坏）"))
            return
        reader.Transfer(document)
        shapes = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
        colors = XCAFDoc_DocumentTool.ColorTool_s(document.Main())
        roots = TDF_LabelSequence()
        shapes.GetFreeShapes(roots)
        leaves = []

        def walk(label, location, prefix):
            name = _label_name(label) or prefix
            if shapes.IsAssembly_s(label):
                components = TDF_LabelSequence()
                shapes.GetComponents_s(label, components)
                for i in range(1, components.Length() + 1):
                    component = components.Value(i)
                    referred = TDF_Label()
                    shapes.GetReferredShape_s(component, referred)
                    walk(referred, location.Multiplied(shapes.GetLocation_s(component)), _label_name(component) or name)
            else:
                shape = shapes.GetShape_s(label)
                # 同一零件在装配里常被引用多次：按标签只网格化一次，各实例再用装配位置变换
                entry = TCollection_AsciiString()
                TDF_Tool.Entry_s(label, entry)
                leaves.append((name or f"部件 {len(leaves) + 1}", shape, location, _label_color(colors, label, shape),
                               entry.ToCString()))

        for i in range(1, roots.Length() + 1):
            walk(roots.Value(i), TopLoc_Location(), "")
        if not leaves:
            events.put(("error", "文件里没有可显示的形体"))
            return
        box = Bnd_Box()
        for _, shape, location, _, _ in leaves:
            BRepBndLib.Add_s(shape.Moved(location), box)
        x0, y0, z0, x1, y1, z1 = box.Get()
        # 弦高误差取整体尺寸的千分之一：外形看起来平滑，三角形数也可控
        deflection = max(((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5 / 1000, 1e-6)
        scale = CASCADE_UNITS.get(Interface_Static.CVal_s("xstep.cascade.unit").upper(), 1.0)
        arrays, names, tints, meshed = {}, [], [], {}
        for index, (name, shape, location, color, entry) in enumerate(leaves):
            if entry not in meshed:
                events.put(("stage", f"正在网格化 {len(meshed) + 1} 种零件（共 {len(leaves)} 个实例）：{name}"))
                meshed[entry] = _triangles(shape, deflection)
            trsf = location.Transformation()
            matrix = np.array([[trsf.Value(r, c) for c in range(1, 5)] for r in range(1, 4)])
            local = meshed[entry].reshape(-1, 3).astype(np.float64)
            arrays[f"part{index}"] = ((local @ matrix[:, :3].T + matrix[:, 3]) * scale).astype(np.float32).reshape(-1, 3, 3)
            names.append(name)
            tints.append(color or (-1, -1, -1, -1))
        np.savez(output, names=np.array(names), colors=np.array(tints, dtype=np.float32), **arrays)
        events.put(("done", len(leaves)))
    except Exception as error:
        events.put(("error", f"{type(error).__name__}: {error}"))


class StepJob:
    """在子进程里读一个 STEP 文件；poll() 返回 (类型, 内容)，cancel() 立即终止子进程

    子进程用 spawn 启动，主程序入口必须有 if __name__ == "__main__" 保护（main.py 已有）
    """

    def __init__(self, path, workdir):
        self.path, self.started = Path(path), time.monotonic()
        self.output = Path(workdir) / f"step-{int(time.time() * 1000)}.npz"
        context = multiprocessing.get_context("spawn")
        self.events = context.Queue()
        self.process = context.Process(target=_worker, args=(str(self.path), str(self.output), self.events), daemon=True)
        self.process.start()

    def poll(self):
        """非阻塞取一条进度；子进程意外退出时返回 error"""
        try:
            return self.events.get_nowait()
        except queue.Empty:
            if not self.process.is_alive() and self.process.exitcode not in (0, None):
                return "error", f"读取进程异常退出（代码 {self.process.exitcode}）"
            return None

    def cancel(self):
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(2)
        self.output.unlink(missing_ok=True)

    def result(self, robot_module):
        """读回网格并包成 Robot；读完删除临时文件"""
        with np.load(self.output) as data:
            names, colors = list(data["names"]), data["colors"]
            parts = [(str(names[i]), data[f"part{i}"], tuple(colors[i]) if colors[i][0] >= 0 else None)
                     for i in range(len(names)) if len(data[f"part{i}"])]
        self.output.unlink(missing_ok=True)
        unit = file_unit(self.path)
        robot = robot_module.mesh_model(self.path.stem, parts, "STEP", self.path, unit or "未声明")
        return robot


def available():
    """CAD 后端是否可用；不可用时返回原因"""
    try:
        import OCP  # noqa: F401
        return ""
    except ImportError as error:
        return f"未安装 STEP 后端 cadquery-ocp（{error}）"
