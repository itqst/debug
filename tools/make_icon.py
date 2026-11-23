"""由选定的标志原图生成程序用的图标：dog_debug/data/logo.png（512 px，圆角外透明）和 icon.ico（多尺寸，给打包的 exe）

原图 logo_source.png 是不带透明通道的正方形，圆角方块外面是米白底并有一圈浅灰描边；这里向内裁掉描边，
再按 iOS 图标的圆角把四角做成透明。换标志时替换原图后重跑：python tools/make_icon.py
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter, QPainterPath  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

DATA = ROOT / "dog_debug" / "data"
# 原图 1254 px 里描边在距边约 20 px 处，向内多裁一点，只留描边以内的部分
INSET = 0.028
CORNER = 0.225

app = QApplication([])
source = QImage(str(DATA / "logo_source.png"))
inset = round(source.width() * INSET)
square = source.copy(inset, inset, source.width() - 2 * inset, source.height() - 2 * inset)
logo = QImage(512, 512, QImage.Format_ARGB32_Premultiplied)
logo.fill(Qt.transparent)
painter = QPainter(logo)
painter.setRenderHint(QPainter.Antialiasing)
painter.setRenderHint(QPainter.SmoothPixmapTransform)
clip = QPainterPath()
clip.addRoundedRect(QRectF(0, 0, 512, 512), 512 * CORNER, 512 * CORNER)
painter.setClipPath(clip)
painter.drawImage(QRectF(0, 0, 512, 512), square)
painter.end()
logo.save(str(DATA / "logo.png"))

import dog_debug.app as A  # noqa: E402  logo.png 生成后再导入，取它的分级缩小

sizes = (16, 24, 32, 48, 64, 128, 256)
images = []
for size in sizes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    A.logo_image(size).save(buffer, "PNG")
    images.append(bytes(data))
# ICO：6 字节文件头 + 每个尺寸 16 字节目录项，图像数据直接用 PNG（Vista 起支持）；宽高 256 记为 0
header = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
entries = b""
for size, png in zip(sizes, images):
    entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
    offset += len(png)
(DATA / "icon.ico").write_bytes(header + entries + b"".join(images))
print("logo.png", (DATA / "logo.png").stat().st_size, "icon.ico", (DATA / "icon.ico").stat().st_size)
