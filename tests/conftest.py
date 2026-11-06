"""测试公共设置：程序的本机设置改写到临时目录的 ini，测试不读也不改本机记住的外观、连接和 ZLM 设置"""
import tempfile

from PySide6.QtCore import QSettings

import dog_debug.ui as ui_module

ui_module.SETTINGS_FORMAT = QSettings.IniFormat
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tempfile.mkdtemp(prefix="kunkun-test-settings-"))
