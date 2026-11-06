#!/usr/bin/env bash
# 打 Linux（Ubuntu 22.04 x86_64）单目录运行包，和 build_windows.bat 同一套 PyInstaller 参数：build_linux.sh [输出目录]
# 默认输出到本目录的 dist/KunKunDebug。DOG_PYTHON 指定装好依赖的解释器（默认 python3）
# 程序不用 matplotlib、pkg_resources 等；构建机系统 Python 里装的旧版本会被 PyInstaller 的钩子带进来，和 numpy 2 冲突，显式排除
# Qt 6.5 起 xcb 平台插件要 libxcb-cursor.so.0，很多桌面没装；找得到就一起打进包里（系统库或 DOG_EXTRA_LIBDIR 里的）
set -euo pipefail
cd -- "$(dirname -- "$0")"
PY="${DOG_PYTHON:-python3}"
OUT="${1:-$PWD/dist}"
WORK=/tmp/dog_debug_build/work
# 版本号只在 dog_debug/__init__.py 里改；Linux 没有版本资源，只用来提示
VERSION="$("$PY" tools/make_version_file.py "$PWD/build/version_info.txt")"
echo "版本 $VERSION"
ocp=1
"$PY" -c "import OCP" 2>/dev/null || ocp=0

extra=()
cursor=""
for dir in "${DOG_EXTRA_LIBDIR:-}" /usr/lib/x86_64-linux-gnu /lib/x86_64-linux-gnu; do
    if [ -n "$dir" ] && [ -e "$dir/libxcb-cursor.so.0" ]; then
        cursor="$dir/libxcb-cursor.so.0"
        break
    fi
done
if [ -n "$cursor" ]; then
    extra+=(--add-binary "$(readlink -f "$cursor"):.")
    echo "打包 $cursor"
else
    echo "没找到 libxcb-cursor.so.0，运行的机器上需要 sudo apt install libxcb-cursor0"
fi

"$PY" -m PyInstaller --noconfirm --clean --windowed --name KunKunDebug \
    --distpath "$OUT" --workpath "$WORK" --specpath "$PWD/build" \
    --add-data "$PWD/dog_debug/data:dog_debug/data" \
    --collect-submodules pyqtgraph --collect-data pyqtgraph \
    --hidden-import OpenGL.platform.glx --hidden-import OpenGL.platform.egl --hidden-import OpenGL.arrays.numpymodule \
    --hidden-import OCP \
    --exclude-module matplotlib --exclude-module tkinter --exclude-module scipy --exclude-module IPython --exclude-module pkg_resources \
    "${extra[@]}" \
    "$PWD/main.py"
# PyInstaller 按真实文件名放库，Qt 找的是 .so.0
if [ -n "$cursor" ] && [ ! -e "$OUT/KunKunDebug/_internal/libxcb-cursor.so.0" ]; then
    cp "$(readlink -f "$cursor")" "$OUT/KunKunDebug/_internal/libxcb-cursor.so.0"
fi
"$PY" -c "import glob,shutil,sys; [shutil.copy(f, sys.argv[1]) for f in glob.glob('docs/*.md') if 'STATUS' not in f]" "$OUT/KunKunDebug"
echo "完成：$OUT/KunKunDebug/KunKunDebug（版本 $VERSION）"
if [ "$ocp" = 0 ]; then
    echo "注意：这个 Python 没装 cadquery-ocp，打出的包不能导入 STEP/STP"
fi
