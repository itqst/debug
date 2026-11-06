"""通用模型导入：URDF（目录/文件/zip、package://、中文路径、基本几何、缺资源）、STL、STEP 子进程网格化"""

import math
import os
import time
import zipfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from dog_debug import cad, robot

DATA = os.path.join(os.path.dirname(__file__), "data")
STEP = str(cad.SAMPLE)


def write_stl(path, triangles):
    record = np.zeros(len(triangles), dtype=robot.STL_RECORD)
    record["v"] = triangles
    with open(path, "wb") as out:
        out.write(b"\0" * 80 + np.uint32(len(triangles)).tobytes() + record.tobytes())


def cube(size=0.1):
    box = robot.primitive(__import__("xml.etree.ElementTree", fromlist=["fromstring"]).fromstring(
        f"<geometry><box size='{size} {size} {size}'/></geometry>"))
    return box


URDF = """<?xml version="1.0"?>
<robot name="测试臂">
  <material name="blue"><color rgba="0 0 1 1"/></material>
  <link name="base"><visual><geometry><box size="0.4 0.2 0.1"/></geometry><material name="blue"/></visual></link>
  <link name="upper"><visual><origin xyz="0 0 0.15"/><geometry><mesh filename="package://my_pkg/meshes/上臂.STL" scale="2 2 2"/></geometry></visual></link>
  <link name="slider"><visual><geometry><mesh filename="../meshes/滑块.stl"/></geometry></visual></link>
  <link name="tool"><visual><geometry><cylinder radius="0.02" length="0.1"/></geometry></visual>
                    <visual><origin xyz="0 0 0.08"/><geometry><sphere radius="0.03"/></geometry></visual></link>
  <joint name="shoulder" type="revolute"><parent link="base"/><child link="upper"/><origin xyz="0 0 0.05"/>
    <axis xyz="0 1 0"/><limit lower="-1.57" upper="1.57" effort="1" velocity="1"/></joint>
  <joint name="rail" type="prismatic"><parent link="upper"/><child link="slider"/><origin xyz="0 0 0.3"/>
    <axis xyz="1 0 0"/><limit lower="0" upper="0.2" effort="1" velocity="1"/></joint>
  <joint name="wrist" type="continuous"><parent link="slider"/><child link="tool"/><axis xyz="0 0 1"/></joint>
</robot>
"""


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "工作区" / "my_pkg"
    (root / "urdf").mkdir(parents=True)
    (root / "meshes").mkdir()
    (root / "package.xml").write_text("<package><name>my_pkg</name></package>", encoding="utf-8")
    (root / "urdf" / "测试臂.urdf").write_text(URDF, encoding="utf-8")
    write_stl(root / "meshes" / "上臂.STL", cube(0.05))
    # 文件名大小写和 URDF 里写的不一样，Windows 导出常见
    write_stl(root / "meshes" / "滑块.STL", cube(0.03))
    return root


def test_urdf_directory_with_package_paths_and_primitives(package):
    r = robot.load_urdf(package)
    assert r.name == "测试臂" and r.kind == "URDF" and not r.missing
    assert set(r.links) == {"base", "upper", "slider", "tool"} and len(r.movable) == 3
    assert r.links["base"].color == (0, 0, 1, 1)
    upper = r.links["upper"].mesh
    assert np.isclose(upper[:, :, 2].max(), 0.15 + 0.05) and np.isclose(upper[:, :, 0].max(), 0.05)
    assert len(r.links["tool"].mesh) == 32 * 4 + 12 * 24 * 2
    rail = next(j for j in r.movable if j.name == "rail")
    assert (rail.kind, rail.lower, rail.upper) == ("prismatic", 0.0, 0.2)
    # 肩转 90°（夹到限位 1.57）后滑轨的 x 轴朝下：滑块在 (0.3, 0, 0.05 - 0.1)
    moved = r.poses({"rail": 0.1, "shoulder": math.pi / 2})
    assert np.allclose(moved["slider"][:3, 3], [0.3, 0, -0.05], atol=1e-3)
    assert np.allclose(r.poses({"rail": 5})["slider"][:3, 3], [0.2, 0, 0.35])


def test_urdf_file_and_zip_entry_points(package, tmp_path):
    assert len(robot.load_urdf(package / "urdf" / "测试臂.urdf").links) == 4
    archive = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as out:
        for path in package.rglob("*"):
            if path.is_file():
                out.write(path, "my_pkg/" + path.relative_to(package).as_posix())
    assert len(robot.load_urdf(archive).links) == 4


def test_missing_mesh_located_then_resource_dir_resolves(package, tmp_path):
    moved = tmp_path / "别处的网格"
    moved.mkdir()
    (package / "meshes" / "上臂.STL").rename(moved / "上臂.STL")
    with pytest.raises(robot.ModelError) as error:
        robot.load_urdf(package)
    link, filename, tried = error.value.missing[0]
    assert link == "upper" and filename == "package://my_pkg/meshes/上臂.STL" and len(tried) > 2
    partial = robot.load_urdf(package, allow_missing=True)
    assert partial.links["upper"].mesh is None and len(partial.missing) == 1
    assert robot.load_urdf(package, [str(moved)]).links["upper"].mesh is not None


def test_bad_inputs_have_clear_errors(tmp_path):
    (tmp_path / "x.urdf").write_text("<robot><link", encoding="utf-8")
    with pytest.raises(robot.ModelError, match="解析失败"):
        robot.load_urdf(tmp_path / "x.urdf")
    (tmp_path / "空目录").mkdir()
    with pytest.raises(robot.ModelError, match="没有找到"):
        robot.load_urdf(tmp_path / "空目录")


def test_real_l2_urdf_imported_from_extracted_folder(tmp_path):
    with zipfile.ZipFile(robot.MODELS / robot.VARIANTS["L2 点足运动版"]) as archive:
        archive.extractall(tmp_path)
    r = robot.load_urdf(tmp_path)
    assert r.kind == "URDF" and r.name == "ZSL2" and len(r.links) == 17 and len(r.movable) == 12
    assert sum(len(l.mesh) for l in r.links.values()) == 511672




def test_stl_direct_open(tmp_path):
    path = tmp_path / "零件.stl"
    write_stl(path, cube(100))
    r = robot.load_stl(path, "mm")
    low, high = robot.bounds(r)
    assert r.kind == "STL" and np.allclose(high - low, 0.1)


def wait(job, seconds=60):
    deadline = time.monotonic() + seconds
    events = []
    while time.monotonic() < deadline:
        event = job.poll()
        if event:
            events.append(event)
            if event[0] in ("done", "error"):
                return events
        time.sleep(0.05)
    raise AssertionError("STEP 读取超时")


needs_ocp = pytest.mark.skipif(bool(cad.available()), reason="未安装 cadquery-ocp")


@needs_ocp
def test_step_assembly_units_placement_names(tmp_path):
    job = cad.StepJob(STEP, tmp_path)
    events = wait(job)
    assert events[-1] == ("done", 2) and any("网格化" in text for kind, text in events if kind == "stage")
    r = job.result(robot)
    assert r.kind == "STEP" and r.unit == "mm" and not job.output.exists()
    names = {name.split(" ", 1)[1]: link for name, link in r.links.items() if link.mesh is not None}
    assert set(names) == {"底板", "立柱"}
    low, high = robot.bounds(r)
    assert np.allclose(low, [0, 0, 0], atol=1e-6) and np.allclose(high, [0.2, 0.1, 0.08], atol=1e-6)
    post = names["立柱"].mesh.reshape(-1, 3)
    assert np.allclose(post.min(axis=0)[:2] + post.max(axis=0)[:2], [0.3, 0.1], atol=1e-3)
    assert np.isclose(post[:, 2].min(), 0.02) and np.allclose(names["底板"].color[:3], (0.2, 0.4, 0.9), atol=1e-3)
    assert not r.movable


@needs_ocp
def test_step_cancel_and_bad_file(tmp_path):
    job = cad.StepJob(STEP, tmp_path)
    job.cancel()
    assert not job.process.is_alive() and not job.output.exists()
    bad = tmp_path / "坏文件.step"
    bad.write_text("not a step file", encoding="utf-8")
    events = wait(cad.StepJob(bad, tmp_path))
    assert events[-1][0] == "error"


def test_file_unit_detection(tmp_path):
    assert cad.file_unit(STEP) == "mm"
    meters = tmp_path / "m.step"
    meters.write_text("#1=( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT($,.METRE.) );", encoding="utf-8")
    assert cad.file_unit(meters) == "m"


@needs_ocp
def test_step_from_chinese_path(tmp_path):
    import shutil
    folder = tmp_path / "模型 资料" / "装配体"
    folder.mkdir(parents=True)
    target = folder / "测试 装配.STEP"
    shutil.copy(STEP, target)
    job = cad.StepJob(target, tmp_path)
    assert wait(job)[-1] == ("done", 2)
    assert job.result(robot).name == "测试 装配"


@pytest.mark.parametrize("encoding,declaration", [("utf-8-sig", "utf-8"), ("gbk", "GBK")])
def test_urdf_bom_and_declared_encoding(package, encoding, declaration):
    text = URDF.replace('<?xml version="1.0"?>', f'<?xml version="1.0" encoding="{declaration}"?>')
    (package / "urdf" / "测试臂.urdf").write_bytes(text.encode(encoding))
    r = robot.load_urdf(package)
    assert r.name == "测试臂" and r.links["upper"].mesh is not None


def test_package_uri_resolves_sibling_package(tmp_path):
    workspace = tmp_path / "工作区"
    for name, size in (("robot_description", 0.5), ("mesh_library", 0.1)):
        (workspace / name / "meshes").mkdir(parents=True)
        (workspace / name / "package.xml").write_text(f"<package><name>{name}</name></package>", encoding="utf-8")
        write_stl(workspace / name / "meshes" / "body.stl", cube(size))
    (workspace / "robot_description" / "urdf").mkdir()
    urdf = workspace / "robot_description" / "urdf" / "test.urdf"
    urdf.write_text("<robot name='t'><link name='a'><visual><geometry>"
                    "<mesh filename='package://mesh_library/meshes/body.stl'/></geometry></visual></link></robot>", encoding="utf-8")
    r = robot.load_urdf(urdf)
    assert np.isclose(r.links["a"].mesh[:, :, 0].max(), 0.05)
