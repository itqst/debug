"""ZLM 拉流播放窗口：PyAV 在后台线程拉流解码，界面只画最新一帧；只拉流，不往设备推任何东西"""

import re
import threading
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QSizePolicy, QVBoxLayout

# 调试看画面要低延迟：不攒缓冲、探测只读一小段；RTSP 走 TCP，经 SSH 隧道时只能是 TCP
OPTIONS = {"rtsp_transport": "tcp", "fflags": "nobuffer", "flags": "low_delay", "probesize": "500000", "analyzeduration": "1000000"}
# 打开和读包的超时（秒）：经 FRP 隧道时每条新连接要 1~3 s，实测吞吐只有约 800 kbps（流本身 1.5~2 Mbps），
# 从点开到第一帧要 15~20 s，打开给足 40 s；流断了或地址不通时线程最多这么久退出
TIMEOUT = (40, 20)


def hide_password(url):
    """地址里带账号密码（rtsp://user:pass@host）时把密码换成 ***，用于显示"""
    return re.sub(r"(//[^/:@]*:)[^/]*@", r"\1***@", url)


class StreamWindow(QDialog):
    """播放一路流；resolve() 在后台线程里调用，返回要拉的地址，出错时抛异常，信息原样显示在窗口里

    关窗口后后台线程在下一帧或最多 TIMEOUT 里的秒数后退出
    """

    frame = Signal()
    status = Signal(str)

    def __init__(self, title, resolve, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(800, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.view = QLabel("正在连接…")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumSize(320, 180)
        self.view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.view.setStyleSheet("background:black;color:#8E8E93;border-radius:8px")
        self.info = QLabel("")
        self.info.setObjectName("caption")
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.info)
        self.image, self.pixmap, self.stopped = None, None, threading.Event()
        # 界面还没画完上一帧时后台只换最新帧、不再发信号，信号不会越积越多
        self.pending = False
        self.frame.connect(self._show_frame)
        self.status.connect(self.info.setText)
        threading.Thread(target=self._run, args=(resolve,), daemon=True).start()

    def _run(self, resolve):
        import av
        try:
            url = resolve()
            shown = hide_password(url)
            self.status.emit(f"{shown}　正在连接…")
            with av.open(url, options=OPTIONS, timeout=TIMEOUT) as container:
                stream = container.streams.video[0]
                stream.thread_type = "AUTO"
                codec = stream.codec_context.name
                count, received, start, stats = 0, 0, time.monotonic(), shown
                # 按包解，顺便统计实际收到的码率：远程画面卡时一眼看出是不是线路带宽不够
                for packet in container.demux(stream):
                    received += packet.size
                    for picture in packet.decode():
                        if self.stopped.is_set():
                            return
                        rgb = picture.to_ndarray(format="rgb24")
                        height, width = rgb.shape[:2]
                        # QImage 不持有 numpy 内存，copy 一份再交给界面线程
                        self.image = QImage(rgb.data, width, height, 3 * width, QImage.Format_RGB888).copy()
                        count += 1
                        elapsed = time.monotonic() - start
                        if count % 25 == 1:
                            stats = f"{shown}　{codec} {width}×{height}" + (
                                f"　{count / elapsed:.1f} fps　收到 {received * 8 / 1000 / elapsed:.0f} kbps" if elapsed > 1 else "")
                            self.status.emit(stats)
                        if not self.pending:
                            self.pending = True
                            self.frame.emit()
            self.status.emit(f"{stats}　流已结束")
        except Exception as error:
            # 地址不通、超时、ZLM 上没这路流、解码失败的异常类型各不相同，原样显示
            if not self.stopped.is_set():
                self.status.emit(f"拉流失败：{hide_password(str(error))}")

    def _show_frame(self):
        self.pending = False
        if self.image is not None:
            self.pixmap = QPixmap.fromImage(self.image)
            self._fit()

    def _fit(self):
        if self.pixmap:
            self.view.setPixmap(self.pixmap.scaled(self.view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def closeEvent(self, event):
        self.stopped.set()
        super().closeEvent(event)
