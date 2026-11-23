"""生成 PyInstaller 的 Windows 版本资源文件，版本号只取 dog_debug.__version__：make_version_file.py 输出路径"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_version():
    # 不 import dog_debug：打包用的解释器不一定能从这里找到包，正则读就够了
    text = (ROOT / "dog_debug" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', text, re.M)
    if not match:
        raise SystemExit("dog_debug/__init__.py 里没有 x.y.z 格式的 __version__")
    return ".".join(match.groups()), tuple(int(part) for part in match.groups())


def main():
    version, (major, minor, patch) = read_version()
    numbers = (major, minor, patch, 0)
    # 080404b0：简体中文 + Unicode，和下面 Translation 的 2052、1200 对应
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
      StringStruct('FileDescription', '坤坤调试台'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'KunKunDebug'),
      StringStruct('OriginalFilename', 'KunKunDebug.exe'),
      StringStruct('ProductName', '坤坤调试台'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    # PyInstaller 6 按 UTF-8 读版本文件
    out.write_text(content, encoding="utf-8")
    print(version)


if __name__ == "__main__":
    main()
