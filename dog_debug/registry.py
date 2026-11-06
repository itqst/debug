"""加载全协议目录并生成有来源的命令模板"""

import copy
import json
import time
import uuid
from pathlib import Path

DATA = Path(__file__).parent / "data" / "protocol.json"
# zlm 不收指令，列在这里是为了状态总览和消息筛选里有它；ros2bridge 是雷达点云（本地 data/heartbeat，只收 pcd_switch）
MODULES = ("gw", "vfc", "ptz", "media", "vision", "fsvr", "ros2bridge", "zlm")
UPLOAD = {"sync_media_file", "sync_map_file", "upload_detection_image"}
NAV = {"start_nav", "start_multi_nav", "start_multi_nav_by_points", "start_nav_return_home"}


# 目录只在首次使用时读一次；日志导入时每条消息都要按 method 查，不能每次读盘
_CATALOG = []
_BY_METHOD = {}


def _load():
    if not _CATALOG:
        _CATALOG.extend(json.loads(DATA.read_text(encoding="utf-8"))["entries"])
        for item in _CATALOG:
            _BY_METHOD.setdefault(item["method"], item)
    return _CATALOG


def entries():
    """返回独立模板副本，调用方的编辑不会修改目录"""
    return copy.deepcopy(_load())


def find(method):
    """按 method 查目录条目；返回共享对象，调用方只读，要改模板先用 make_command 复制"""
    _load()
    return _BY_METHOD.get(method)


def make_command(item):
    return refresh_ids(copy.deepcopy(item["template"]))


def refresh_ids(payload):
    """原地换新 tid、ts，带 bid 的也换；返回 payload"""
    payload["tid"] = str(uuid.uuid4())
    payload["ts"] = int(time.time() * 1000)
    if "bid" in payload:
        payload["bid"] = str(uuid.uuid4())
    return payload


# 当前 GW 把云端 vision 下的这三条改投 MEDIA，回复时 target 再映射回 vision
VISION_TO_MEDIA = {"take_photo", "camera_recording", "camera_livestream"}


def route(payload):
    """按当前 GW 的路由规则算本地目的模块：看云端 target，不看目录里的业务归类

    target=gw 的文件业务按 file_service=fsvr 分支转给 FSVR；legacy 分支由 GW 自己处理，直连时没有对应的模块
    """
    target = payload.get("target")
    if not target:
        item = find(payload.get("method"))
        return item["module"] if item else "unknown"
    if target == "vision" and payload.get("method") in VISION_TO_MEDIA:
        return "media"
    if target == "gw":
        return "fsvr"
    return target


def local_payload(payload):
    """GW 转给模块的报文：只删掉顶层 target，其余字段原样保留"""
    result = copy.deepcopy(payload)
    result.pop("target", None)
    return result
