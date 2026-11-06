"""模型导入截图：内置 L2、缺资源、导入 URDF、STEP 样件；用法 python import_shots.py 源码目录 输出目录 宽 高 前缀"""
import os, sys, time, shutil, tempfile
os.environ["QT_QPA_PLATFORM"] = "windows"
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, os.path.join(sys.argv[1], "tests"))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import dog_debug.app as A
from test_models import URDF, cube, write_stl, STEP
out = sys.argv[2]
def main():
    app = QApplication([])
    app.setStyle("Fusion"); app.setPalette(A.light_palette())
    A.setup_fonts(app)
    app.setStyleSheet(A.STYLE)
    work = tempfile.mkdtemp()
    root = os.path.join(work, "工作区", "my_pkg"); os.makedirs(os.path.join(root, "urdf")); os.makedirs(os.path.join(root, "meshes"))
    open(os.path.join(root, "urdf", "测试臂.urdf"), "w", encoding="utf-8").write(URDF)
    write_stl(os.path.join(root, "meshes", "上臂.STL"), cube(0.05)); write_stl(os.path.join(root, "meshes", "滑块.STL"), cube(0.03))
    broken = os.path.join(work, "broken"); shutil.copytree(root, broken); os.remove(os.path.join(broken, "meshes", "上臂.STL"))
    w = A.Studio(":memory:"); w.resize(int(sys.argv[3]), int(sys.argv[4])); w.show()
    steps = []
    def wait_import(then):
        def check():
            if w.importing:
                QTimer.singleShot(100, check)
            else:
                QTimer.singleShot(400, then)
        QTimer.singleShot(100, check)
    def s1():
        w.grab().save(f"{out}/imp_{sys.argv[5]}_l2.png")
        w.import_model("urdf", broken); wait_import(s2)
    def s2():
        w.grab().save(f"{out}/imp_{sys.argv[5]}_missing.png")
        w.import_model("urdf", root); wait_import(s3)
    def s3():
        w.scene.set_joint("shoulder", 0.6); w.scene.set_joint("rail", 0.12); w._sync_joints()
        QTimer.singleShot(300, lambda: (w.grab().save(f"{out}/imp_{sys.argv[5]}_urdf.png"), w.import_model("step", STEP), wait_import(s4)))
    def s4():
        w.grab().save(f"{out}/imp_{sys.argv[5]}_step.png"); w.close(); app.quit()
    QTimer.singleShot(200, lambda: w._go(w.REMOTE))
    QTimer.singleShot(2500, s1)
    app.exec()
if __name__ == "__main__":
    main()
