"""回归验证结果语义、请求隔离、脱敏及日志导入"""

import json

import pytest

from dog_debug.core import Event, Store, diagnose, redact, requests, trace_report
from dog_debug.logs import import_log
from dog_debug.registry import entries, find, make_command
from dog_debug.transport import simulate


def test_catalog_covers_fsvr_method_table():
    """FSVR 现行协议文档“方法与 ACK”表里的 method 都要在目录里（文档不在本机时跳过）"""
    import re
    from pathlib import Path
    doc = Path(__file__).parents[2] / "dog_L2/tta_fsvr/docs/mqtt_protocol.md"
    if not doc.exists():
        pytest.skip("本机没有 FSVR 协议文档")
    text = doc.read_text(encoding="utf-8")
    table = text[text.index("## 方法与 ACK"):].split("\n## ", 1)[0]
    methods = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M))
    assert {"delete_file", "delete_map_archive", "query_request", "confirm_result"} <= methods
    # live_lens_change 文档里还在，用户 2026-11-02 确认切镜头只用 camera_switch，目录里去掉了
    assert methods - {"live_lens_change"} <= {e["method"] for e in entries()}


def test_protocol_coverage_matches_every_document_method():
    import re
    from pathlib import Path
    doc = Path(__file__).parents[2] / "dog_L2/doc/MQTT通信协议-智身机器狗.md"
    if not doc.exists():
        pytest.skip("独立分发包不包含原始协议")
    methods = set(re.findall(r'"method"\s*:\s*"([\w]+)"', doc.read_text(encoding="utf-8")))
    # live_lens_change 文档里还在，用户 2026-11-02 确认切镜头只用 camera_switch，目录里去掉了
    assert methods - {"live_lens_change"} <= {e["method"] for e in entries()}
    assert {e["module"] for e in entries()} == {"gw", "vfc", "ptz", "media", "vision", "fsvr", "ros2bridge"}


@pytest.mark.parametrize("payload,expected", [
    ({"data": {"result": 0}}, "操作成功"),
    ({"data": {"result": 1, "msg": "failed"}}, "失败"),
    ({"data": {"result": 0, "data": {"result": 1, "msg": "disabled"}}}, "失败"),
    ({"result": 1, "msg": "ok"}, "失败"),
    # 实机 VFC 的本地 ACK：result/msg 在根上，data 为 null
    ({"method": "get_all_paths", "result": 1, "msg": "not connected", "data": None}, "失败"),
    ({"method": "get_all_paths", "result": 0, "msg": "", "data": None}, "操作成功"),
    ({"data": {"result": True}}, "契约待核"),
    ({"data": {"result": "0"}}, "契约待核"),
])
def test_ack_layers(payload, expected):
    assert diagnose(Event("local/robotdog/media/ack", payload)).status == expected


@pytest.mark.parametrize("method,status", [("speaker_play_start", "已开始"),
    ("camera_livestream", "请求已处理"), ("start_nav", "导航已受理")])
def test_ack_does_not_claim_business_finished(method, status):
    assert diagnose(Event("local/robotdog/media/ack", {"method": method, "data": {"result": 0}})).status == status


@pytest.mark.parametrize("value,status", [(1, "活跃"), (0, "未活跃")])
def test_stream_one_means_active(value, status):
    assert diagnose(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"result": value}})).status == status


def test_retained_never_proves_fresh_success():
    assert diagnose(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"result": 1}}, retained=True)).status == "历史快照"


def test_room_is_address_only():
    assert diagnose(Event("local/robotdog/media/state", {"type": "room_state"})).status == "寻址信息"


def test_jpeg_completed_not_upload():
    e = Event("local/robotdog/detect/ack", {"method": "capture_detection", "result": 0, "data": {"phase": "completed"}})
    assert diagnose(e).status == "JPEG 已生成"
    assert "上传" in diagnose(e).suggestion


def test_upload_ack_and_terminal():
    ack = Event("local/robotdog/fsvr/ack", {"method": "sync_media_file", "data": {"result": 0, "state": "1"}})
    final = Event("local/robotdog/fsvr/state", {"method": "media_file_upload_status", "data": {"result": 0, "state": "0"}})
    assert diagnose(ack).status == "已受理/上传中"
    assert diagnose(final).status == "完成"


def test_cross_device_and_session_tid_never_merge():
    store = Store()
    for sn, session in [("a", "s"), ("b", "s"), ("a", "other")]:
        store.add(Event("local/robotdog/media/ack", {"tid": "same", "data": {"result": 0}}, sn=sn, session=session))
    assert len(store.trace(Event("", {"tid": "same"}, sn="a", session="s"))) == 1
    store.close()


def test_missing_ack_stays_unobserved():
    report = trace_report(simulate(make_command(find("take_photo")), "a", "s", "no_ack"))
    assert "ACK 未观测" in report
    assert "不确定是丢包" in report


def test_duplicate_observation_not_duplicate_execution():
    report = trace_report(simulate(make_command(find("take_photo")), "a", "s", "duplicate"))
    assert "不能据此断言重复执行" in report


def test_chinese_preserved_and_diagnosis_does_not_ban_chinese():
    raw = {"method": "take_photo", "task_name": "西4 楼宇(1)", "data": {"result": 1, "msg": "task_name invalid"}}
    assert redact(raw)["task_name"] == "西4 楼宇(1)"
    assert "中文本身合法" in diagnose(Event("local/robotdog/media/ack", raw)).suggestion


def test_secrets_never_persist_or_export(tmp_path):
    path = tmp_path / "session.sqlite3"
    store = Store(path)
    secrets = {"secret": "SAMPLE_PRIVATE", "credentials": {"accessKeySecret": "OSS_PRIVATE"},
               "url": "https://example.invalid/file?signature=URL_PRIVATE", "tid": "keep-me"}
    store.add(Event("x", secrets, raw='secret=RAW_PRIVATE password="PASS_PRIVATE"'))
    store.flush()
    export = tmp_path / "session.jsonl"
    store.export(export)
    store.close()
    content = path.read_bytes() + export.read_bytes()
    for value in (b"SAMPLE_PRIVATE", b"OSS_PRIVATE", b"URL_PRIVATE", b"RAW_PRIVATE", b"PASS_PRIVATE"):
        assert value not in content
    assert b"keep-me" in content


def test_bearer_and_mqtt_url_credentials_redacted():
    raw = "Authorization: Bearer PRIVATE_BEARER\nmqtt://name:PRIVATE_PASS@localhost:1883"
    cleaned = redact(raw)
    assert "PRIVATE_BEARER" not in cleaned
    assert "PRIVATE_PASS" not in cleaned


def test_multiline_log_keeps_outer_message(tmp_path):
    path = tmp_path / "tta.log"
    text = '12:03:04.120 local/robotdog/media/ack {\n"method":"take_photo",\n"tid":"t",\n"data":{"result":1,"msg":"camera timeout"}\n}\n'
    path.write_text(text, encoding="utf-8")
    events = list(import_log(path, "a", "s"))
    assert len(events) == 1
    assert events[0].tid == "t"
    assert events[0].payload["data"]["result"] == 1
    assert events[0].line == 1 and events[0].offset == 0
    assert events[0].source_time == "12:03:04.120"
    assert path.read_text(encoding="utf-8") == text


def test_truncated_log_remains_searchable(tmp_path):
    path = tmp_path / "tta.log"
    path.write_text('error local/robotdog/ptz/ack {"method":', encoding="utf-8")
    event = list(import_log(path, "a", "s"))[0]
    assert event.payload == {} and "error" in event.raw


def test_export_import_roundtrip(tmp_path):
    store = Store()
    store.add(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "roundtrip", "data": {"result": 0}}, sn="dog", session="old"))
    path = tmp_path / "export.jsonl"
    store.export(path)
    result = list(import_log(path, "x", "new"))[0]
    assert result.session == "new" and result.sn == "dog" and result.tid == "roundtrip"
    store.close()


def test_cancel_during_hash(tmp_path):
    path = tmp_path / "big.log"
    path.write_bytes(b"x" * 10)
    assert list(import_log(path, "a", "s", lambda: True)) == []


def test_stream_locator_survives_redaction():
    url = "srt://172.17.0.1:10080?streamid=#!::h=live/dog_001,m=publish&auth_key=PRIVATE_KEY"
    cleaned = redact(url)
    assert "streamid=#!::h=live/dog_001" in cleaned
    assert "PRIVATE_KEY" not in cleaned


def test_network_queue_overflow_is_counted():
    import queue
    from dog_debug.transport import MqttTransport
    transport = MqttTransport(queue.Queue(maxsize=1))
    transport._offer(("x", 1))
    transport._offer(("x", 2))
    assert transport.dropped == 1


def _request(scenario, method="take_photo", session="s"):
    from dog_debug.core import summarize
    return summarize(simulate(make_command(find(method)), "a", session, scenario))


def test_request_failure_names_module():
    r = _request("failure")
    assert r.status == "失败" and r.summary.startswith("MEDIA") and r.module == "media"
    assert r.chain.startswith("GW 指令 → MEDIA 指令 → MEDIA ACK")


def test_request_success_uses_module_ack_and_latency():
    r = _request("success")
    assert r.status == "操作成功" and r.latency_ms is not None and not r.note


def test_gw_forwarded_reply_is_not_duplicate_but_redelivery_noted():
    assert not _request("success").note
    r = _request("duplicate")
    assert r.status == "操作成功" and "重投" in r.note


def test_request_without_ack():
    assert _request("no_ack").status == "无 ACK"


def test_requests_sorted_failures_first_and_isolated():
    from dog_debug.core import requests
    store = Store()
    for scenario, session in (("success", "s1"), ("failure", "s2"), ("no_ack", "s3")):
        for e in simulate(make_command(find("take_photo")), "a", session, scenario):
            store.add(e)
    found = requests(store, store.query())
    assert [r.status for r in found] == ["失败", "无 ACK", "操作成功"]
    assert len({r.key for r in found}) == 3
    store.close()


def test_request_cache_recomputes_only_when_its_tid_gets_new_messages():
    """请求结论缓存：同一请求后来收到 ACK 要重算，别的请求不动；清库后缓存一起清"""
    store = Store()
    events = simulate(make_command(find("take_photo")), "a", "s1", "success")
    commands = [e for e in events if e.kind == "指令"]
    for e in commands:
        store.add(e)
    for e in simulate(make_command(find("take_photo")), "a", "s2", "failure"):
        store.add(e)
    first = {r.key: r for r in requests(store, store.query())}
    assert first[("a", "s1", commands[0].tid)].status == "无 ACK"
    for e in events:
        if e.kind != "指令":
            store.add(e)
    again = {r.key: r for r in requests(store, store.query())}
    assert again[("a", "s1", commands[0].tid)].status == "操作成功"
    other = next(k for k in first if k[1] == "s2")
    assert again[other] is first[other]
    store.clear()
    assert not store.request_cache and requests(store, store.query()) == []
    store.close()


def test_imported_request_has_no_cross_host_latency():
    from dog_debug.core import summarize
    events = simulate(make_command(find("take_photo")), "a", "s", "success")
    for e in events:
        e.source = "log:hash"
    assert summarize(events).latency_ms is None


def test_diagnostic_zip_contents(tmp_path):
    import zipfile
    from dog_debug.core import diagnostic_zip
    store = Store()
    for scenario, session in (("success", "s1"), ("failure", "s2")):
        for e in simulate(make_command(find("take_photo")), "dog", session, scenario):
            store.add(e)
    store.add(Event("tta/robotdog/dog/request_reply", {"method": "oss_config_get", "tid": "k",
                                                      "data": {"credentials": {"accessKeySecret": "ZIP_PRIVATE"}}}, sn="dog", session="s3"))
    failed = next(r for r in __import__("dog_debug.core", fromlist=["requests"]).requests(store, store.query()) if r.status == "失败")
    path = tmp_path / "bundle.zip"
    diagnostic_zip(store, path, {"version": "x"}, failed.key)
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert names == {"README.txt", "summary.md", "session.jsonl", "environment.json", "focus.json"}
        content = b"".join(archive.read(n) for n in names)
        summary = archive.read("summary.md").decode("utf-8")
    assert b"ZIP_PRIVATE" not in content
    table = [line for line in summary.splitlines() if line.startswith("| ") and "---" not in line][1:]
    assert "| 失败 |" in table[0]
    store.close()


def test_upload_terminal_state_finishes_request():
    r = _request("success", "sync_media_file")
    assert r.status == "完成"


def test_upload_failure_state_overrides_ack():
    from dog_debug.core import summarize
    events = simulate(make_command(find("sync_media_file")), "a", "s", "success")
    events[-1].payload["data"]["result"] = 1
    events[-1].payload["data"]["msg"] = "oss 403"
    assert summarize(events).status == "失败"


def test_periodic_states_do_not_finish_ordinary_requests():
    from dog_debug.core import summarize
    events = simulate(make_command(find("camera_livestream")), "a", "s", "success")
    assert summarize(events).status == "请求已处理"


def test_log_rows_store_payload_once_and_export_roundtrip(tmp_path):
    path = tmp_path / "tta.log"
    path.write_text('12:00:00 local/robotdog/media/ack {"method":"take_photo","tid":"t1","data":{"result":1,"msg":"camera timeout"}}\n'
                    '12:00:01 local/robotdog/fsvr/ack {"method":"x","tid":"t2","data":{"password":"P_SECRET"}}\n', encoding="utf-8")
    store = Store()
    for e in import_log(path, "a", "s"):
        store.add(e)
    bodies = [b for (b,) in store.db.execute("SELECT body FROM events ORDER BY id")]
    assert '"payload"' not in bodies[0] and "payload_in_raw" in bodies[0]
    assert '"payload"' in bodies[1] and "P_SECRET" not in bodies[1]
    first, second = (e for _, e in reversed(store.query()))
    assert first.payload["data"]["msg"] == "camera timeout" and diagnose(first).status == "失败"
    assert store.query(text="camera timeout")
    export = tmp_path / "out.jsonl"
    store.export(export)
    again = list(import_log(export, "x", "new"))
    assert again[0].payload == first.payload and again[0].tid == "t1"
    store.close()


def test_reply_method_mismatch_is_noted():
    from dog_debug.core import summarize
    events = simulate(make_command(find("ir_metering_area_set")), "a", "s", "success")
    for e in events:
        if e.kind == "ACK":
            e.payload["method"] = "ir_metering_point_set"
    r = summarize(events)
    assert "不一致" in r.note and "ir_metering_point_set" in r.note
    assert r.status == "契约待核" and "原结果：操作成功" in r.summary


def test_method_mismatch_keeps_explicit_failure():
    from dog_debug.core import summarize
    events = simulate(make_command(find("ir_metering_area_set")), "a", "s", "failure")
    for e in events:
        if e.kind == "ACK":
            e.payload["method"] = "ir_metering_point_set"
    assert summarize(events).status == "失败"


def test_detection_chain_full_and_gaps():
    from dog_debug.core import detection_chains
    from dog_debug.transport import simulate_detection
    full = detection_chains(simulate_detection("a", "s"))
    assert len(full) == 1 and full[0]["verdict"] == "全链路已观测"
    assert all(v[0] == "通过" for v in full[0]["stages"].values())
    stopped = detection_chains(simulate_detection("a", "s", "FSVR 登记"))[0]
    assert stopped["verdict"] == "停在 FSVR 登记，后续未观测" and stopped["stages"]["上传终态"][0] == "未观测"
    failed = detection_chains(simulate_detection("a", "s", "云端事件", "上传终态"))[0]
    assert failed["failed"] and failed["verdict"].startswith("失败在 上传终态")
    jpeg = detection_chains(simulate_detection("a", "s", "云端事件", "JPEG 生成"))[0]
    assert jpeg["verdict"] == "失败在 JPEG 生成：hold expired"


def test_detection_chain_links_only_by_real_keys():
    from dog_debug.core import detection_chains
    from dog_debug.transport import simulate_detection
    events = simulate_detection("a", "s")
    # 去掉中间的登记和上传：剩下的抓拍与事件只靠 file_path/file_name 相连，仍是一条链，中间阶段为未观测
    kept = [e for e in events if e.method not in ("register_file", "upload_detection_image", "detection_upload_status", "confirm_result")]
    chain = detection_chains(kept)
    assert len(chain) == 1 and "FSVR 登记、上传受理、上传终态、终态确认 未观测" in chain[0]["verdict"]
    two = detection_chains(simulate_detection("a", "s") + simulate_detection("a", "s"))
    assert len(two) == 2
    other_session = detection_chains(simulate_detection("a", "s1")[:2] + [e for e in simulate_detection("a", "s2")])
    assert len(other_session) == 2


def test_detect_ack_without_method_belongs_to_capture():
    assert Event("local/robotdog/detect/ack", {"tid": "x", "result": 0, "data": {"phase": "completed"}}).method == "capture_detection"


@pytest.mark.parametrize("encoding,label", [("utf-8", "utf-8"), ("utf-8-sig", "utf-8-sig"), ("gb18030", "gb18030"), ("gbk", "gb18030")])
def test_log_encodings_keep_chinese(tmp_path, encoding, label):
    from dog_debug.logs import import_log
    text = ('12:00:00 [任务] 西4楼宇巡检 local/robotdog/media/ack {"method":"take_photo","tid":"t1","data":{"result":1,"msg":"相机超时"}}\n'
            "12:00:01 普通中文日志行\n")
    path = tmp_path / f"中文目录_{encoding}.log"
    path.write_bytes(text.encode(encoding))
    stats = {}
    events = list(import_log(path, "狗", "会话", stats=stats))
    assert stats["encoding"] == label and stats["undecoded"] == 0
    assert events[0].payload["data"]["msg"] == "相机超时" and "西4楼宇巡检" in events[0].raw
    assert events[1].raw.startswith("12:00:01 普通中文日志行") and events[1].line == 2


def test_mixed_encoding_lines_are_marked_not_silently_dropped(tmp_path):
    from dog_debug.logs import import_log
    path = tmp_path / "mixed.log"
    path.write_bytes("第一行 UTF-8\n".encode("utf-8") + "第二行 GBK\n".encode("gbk") + b"third\n")
    stats = {}
    events = list(import_log(path, "a", "s", stats=stats))
    assert stats["encoding"] == "utf-8" and stats["undecoded"] == 1 and stats["undecoded_lines"] == [2]
    assert len(events) == 3 and "�" in events[1].raw and events[1].offset == len("第一行 UTF-8\n".encode("utf-8"))


def test_utf16_log_reports_unsupported(tmp_path):
    from dog_debug.logs import import_log
    path = tmp_path / "u16.log"
    path.write_bytes("日志".encode("utf-16"))
    with pytest.raises(ValueError, match="UTF-16"):
        list(import_log(path, "a", "s"))


def test_jsonl_roundtrip_keeps_chinese_device_and_session(tmp_path):
    store = Store()
    store.add(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "中文tid", "task_name": "西4 楼宇(1)",
                                                "data": {"result": 1, "msg": "相机连接超时"}}, sn="狗一号", session="旧会话"))
    path = tmp_path / "导出 会话.jsonl"
    store.export(path)
    assert path.read_bytes().decode("utf-8")
    again = list(import_log(path, "x", "新会话"))[0]
    assert (again.sn, again.session, again.tid) == ("狗一号", "新会话", "中文tid")
    assert again.payload["task_name"] == "西4 楼宇(1)" and diagnose(again).summary == "相机连接超时"
    store.close()


def test_local_event_without_uploaded_flag_is_flagged():
    from dog_debug.core import detection_chains
    from dog_debug.transport import simulate_detection
    events = simulate_detection("a", "s")
    for e in events:
        if e.method == "detect_result_event" and e.topic.startswith("local/"):
            e.payload["data"].pop("uploaded")
    chain = detection_chains(events)[0]
    assert chain["stages"]["本地事件"][0] == "待核" and chain["verdict"].startswith("本地事件 待核")


@pytest.mark.parametrize("item", [e for e in entries() if e["category"] == "command"], ids=lambda e: e["method"])
def test_every_command_sandbox_roundtrip(item):
    """遍历全部控制指令：沙盒成功链路不判失败、失败链路判失败，请求只按自己的 tid 聚合"""
    from dog_debug.core import summarize
    ok = summarize(simulate(make_command(item), "dog", "s-ok", "success"))
    bad = summarize(simulate(make_command(item), "dog", "s-bad", "failure"))
    assert ok.status != "失败" and ok.method == item["method"]
    assert bad.status == "失败" and len({e.tid for e in bad.events}) == 1


def test_not_found_retry_then_accepted_recovers():
    import copy
    from dog_debug.core import detection_chains
    from dog_debug.transport import simulate_detection
    events = simulate_detection("a", "s")
    ack = next(e for e in events if e.method == "upload_detection_image" and e.kind == "ACK")
    assert detection_chains(events)[0]["stages"]["上传受理"][0] == "通过"
    retry = copy.deepcopy(ack)
    retry.timestamp -= 0.001
    retry.payload["data"] = {"result": 1, "phase": "rejected", "reason": "not_found"}
    chain = detection_chains([retry] + events)[0]
    assert chain["verdict"] == "全链路已观测" and not chain["failed"]
    assert chain["stages"]["上传受理"][0] == "通过" and "not_found" in chain["stages"]["上传受理"][1]
    # 只有拒绝、没有后续受理：仍是失败
    alone = [e for e in events if not (e.method == "upload_detection_image" and e.kind == "ACK")] + [retry]
    assert detection_chains(alone)[0]["stages"]["上传受理"][0] == "失败"


def test_running_stage_is_not_green():
    from dog_debug.core import detection_chains
    from dog_debug.transport import simulate_detection
    events = simulate_detection("a", "s")
    for e in events:
        if e.method == "detection_upload_status":
            e.payload["data"]["state"] = "1"
    chain = detection_chains(events)[0]
    assert chain["verdict"].startswith("上传终态 进行中")


def _reg(n, directory, kind="detection"):
    return Event("local/robotdog/fsvr/command", {"method": "register_file", "tid": f"r{n}", "data": {
        "kind": kind, "event_id": f"e{n}", "delivery_id": f"d{n}", "file_path": f"events/{directory}/same.jpg"}}, sn="a", session="s")


def test_cloud_event_joins_detection_by_file_name():
    from dog_debug.core import detection_chains
    cloud = Event("tta/robotdog/a/events", {"method": "detect_result_event", "data": {"file_name": "x.jpg"}}, sn="a", session="s")
    reg = _reg(1, "a")
    reg.payload["data"]["file_path"] = "events/a/x.jpg"
    chains = detection_chains([reg, cloud])
    assert len(chains) == 1 and chains[0]["stages"]["云端事件"][2] == [cloud]


def test_ordinary_photo_register_not_in_detection_chain():
    from dog_debug.core import detection_chains
    assert detection_chains([_reg(3, "photo", "photo")]) == []


def test_duplicate_cloud_observations_attach_to_single_detection():
    import copy
    from dog_debug.core import detection_chains
    cloud = Event("tta/robotdog/a/events", {"method": "detect_result_event", "data": {"file_name": "same.jpg"}}, sn="a", session="s")
    chains = detection_chains([_reg(1, "a"), cloud, copy.deepcopy(cloud)])
    assert len(chains) == 1 and len(chains[0]["stages"]["云端事件"][2]) == 2


def test_photo_register_reply_and_lone_confirm_are_not_detections():
    from dog_debug.core import detection_chains
    photo = Event("local/robotdog/fsvr/command", {"method": "register_file", "tid": "p1", "requester": "media",
                                                   "data": {"kind": "photo", "file_path": "photos/one.jpg"}}, sn="a", session="s")
    ack = Event("local/robotdog/fsvr/ack", {"method": "register_file", "tid": "p1", "requester": "media",
                                             "data": {"result": 0, "phase": "completed"}}, sn="a", session="s")
    confirm = Event("local/robotdog/fsvr/command", {"method": "confirm_result", "tid": "c1", "data": {"request_tid": "x", "result_seq": 1}},
                    sn="a", session="s")
    assert detection_chains([photo, ack, confirm]) == []


def test_payload_diff_and_previous_same_topic():
    from dog_debug.core import payload_diff
    old = {"tid": "a", "data": {"speed": 0.3, "mode": 1, "gone": 1}}
    new = {"tid": "b", "data": {"speed": 0.5, "mode": 1, "added": [1]}}
    assert payload_diff(old, new) == [("data.added", None, [1]), ("data.gone", 1, None), ("data.speed", 0.3, 0.5)]
    store = Store(":memory:")
    first = Event("local/robotdog/media/state", {"type": "stream_status", "info": {"result": 0}}, sn="d", timestamp=1.0)
    other = Event("local/robotdog/ptz/state", {"type": "stream_status", "info": {"result": 5}}, sn="d", timestamp=1.5)
    second = Event("local/robotdog/media/state", {"type": "stream_status", "info": {"result": 1}}, sn="d", timestamp=2.0)
    for e in (first, other, second):
        store.add(e)
    store.flush()
    found = store.previous(second)
    assert found.topic == first.topic and found.payload["info"]["result"] == 0
    assert store.previous(first) is None


def test_heartbeat_tid_is_not_a_request():
    from dog_debug.core import requests
    store = Store(":memory:")
    store.add(Event("local/robotdog/vfc/heartbeat", {"sn": "D", "tid": "D-hb-347", "seq": 347, "data": {"version": "3.0.1"}}, sn="D"))
    store.add(Event("local/robotdog/media/command", {"method": "take_photo", "tid": "t1", "data": {}}, "发送", sn="D"))
    store.flush()
    found = requests(store, store.query(limit=10))
    assert [r.events[0].tid for r in found] == ["t1"]
    assert store.recent_requests(10) == ([("D", "", "t1")], 1)
    assert store.query(limit=10)[-1][1].method == "heartbeat"


def test_retained_command_not_reported_as_no_ack():
    from dog_debug.core import summarize
    e = Event("local/robotdog/media/command", {"method": "camera_livestream", "tid": "1", "data": {"state": 1}}, retained=True, sn="D")
    r = summarize([e])
    assert r.status == "保留消息" and "Retain" not in r.summary and "保留" in r.summary


@pytest.mark.parametrize("replier, topic", [("gw", "tta/robotdog/A/services_reply"), ("fsvr", "local/robotdog/fsvr/ack")])
def test_request_module_follows_actual_replier(replier, topic):
    command = Event("tta/robotdog/A/services", {"method": "get_media_filelist", "target": "gw", "tid": "t", "data": {}}, "发送",
                    sn="A", session="s")
    reply = Event(topic, {"method": "get_media_filelist", "target": replier, "tid": "t", "data": {"result": 0}}, sn="A", session="s")
    store = Store(":memory:")
    for event in (command, reply):
        store.add(event)
    assert requests(store, store.query())[0].module == replier


def test_sandbox_replies_follow_module_and_gw_shapes():
    """沙盒按各模块源码造本地 ACK，按 GW 两种转发方式造云端回复"""
    from dog_debug.core import summarize
    by_topic = lambda events: {e.topic.split("/")[-1] if e.topic.startswith("tta") else e.topic: e.payload for e in events}
    photo = make_command(find("take_photo"))
    events = simulate(photo, "DOG", "s", "failure")
    wire = by_topic(events)
    assert "target" not in wire["local/robotdog/media/command"]
    assert wire["local/robotdog/media/ack"]["data"]["result"] == 1
    # GW 通用转发读本地根上的 result，Media 放在 data 里，所以外层是 0、失败在 data.data
    reply = wire["services_reply"]
    assert reply["target"] == "vision" and reply["bid"] == photo.get("bid", "") and "ts" in reply
    assert reply["data"]["result"] == 0 and reply["data"]["data"]["result"] == 1
    assert summarize(events).status == "失败"

    paths = by_topic(simulate(make_command(find("get_all_paths")), "DOG", "s"))
    assert paths["local/robotdog/vfc/ack"]["result"] == 0
    assert paths["services_reply"]["data"]["result"] == 0

    files = by_topic(simulate(make_command(find("get_media_filelist")), "DOG", "s"))
    assert files["local/robotdog/fsvr/ack"]["data"]["data"][0]["file_name"] == "2026-10-28"
    # FSVR 回执 GW 专门透传，不再包一层
    assert files["services_reply"]["target"] == "gw" and files["services_reply"]["data"]["data"][0]["id"] == "d1"
