"""IDE、脚本和打包后的 exe 共用的启动入口"""

import multiprocessing

from dog_debug.app import main

if __name__ == "__main__":
    # 打包后 STEP 读取子进程用 spawn 启动，必须先处理冻结环境下的子进程入口
    multiprocessing.freeze_support()
    raise SystemExit(main())
