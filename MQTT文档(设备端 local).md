# MQTT文档(设备端 local)

> 2026-10-19：补齐 Media/Vision 拆分后的本地命令、ACK、状态、事件及航点归属，修正 FSVR 已被后续决定替代的上传说明。本文的 Media/Vision/FSVR 专节覆盖旧通用模板中的 heartbeat、ACK 和状态样例；VFC/PTZ 等既有协议保持原样。云端路由见 [MQTT通信协议-智身机器狗](MQTT通信协议-智身机器狗.md)，拆分原则见 [主方案 5.10](L2视觉与媒体模块拆分方案.md)。文档约定不代表 GW/云端代码已完成适配，也不将未覆盖功能标为已验证。

## Topic基本格式

**local/{****type****}/{subsystem}/{****fuction****}**

| 参数名 | 说明 |
| --- | --- |
| type | 设备类型    无人机：uav；机器狗：robotdog |
| subsystem | 子系统名称（gw、ptz、vfc、media、vision、fsvr 等）；`media` 与 `vision` 使用各自的普通 command/ack/state |
| fuction | 心跳：heartbeat<br>实时数据上报：data<br>事件上报：events<br>状态上报：state<br>指令下发：command<br>指令下发回复：ack |

## Topic （必选字段）

### 心跳状态：旧通用约定为1Hz心跳主题

本段保留其他模块的旧约定。拆分后的 Media、Vision、FSVR 不发独立 heartbeat，而是约每 2 秒发布 `type=subsystem_state`；GW 自身云端心跳不受影响，不能因为没有这些子模块的 heartbeat 就判离线。

*   **local/{type}/{subsystem}/heartbeat**
    

```json
{
  "sn":"1232312312312",
  "ts":1760093093502，
  "seq":1,
  "data":{}
}
```

### 实时上报数据：固定频率发布的数据信息，例如：姿态数据、位置数据等

*   **local/{type}/{subsystem}/data**
    

```json
{
  "sn":"1232312312312",
  "ts":1760093093502,
  "data":{}
}
```

### 事件上报：根据子系统的具体功能，定义可能发出的事件

*   **local/{type}/{subsystem}/events**
    
    ```json
    {
      "sn":"1232312312312",
      "ts":1760093093502,
      "data":{}
    }
    ```
    
    ### 状态上报：过程类的进度等具有状态变换性质的信息
    
    *   **local/{type}/{subsystem}/state**
        

```json
{
  "sn":"1232312312312",
  "ts":1760093093502,
  "type":"",
  "data":{}
}

// type: 根据状态,定义不同的data内容；
```

### 指令：一般用于传递控制指令信息，具体根据子系统功能定义，处理成功与否都需要给出ack状态

*   **指令：local/{type}/{subsystem}/command**
    

```json
{
  "method":"jump",
  "ts":1760093093502,
  "tid":"adfsdfdsfdsf",
  "target":"vfc",
  "data":{}
}

// tid：全局唯一，便于追踪指令传输路径，如果指令源是服务器，则traceId就是服务器发布主题中的tid；
```

*   **应答：local/{type}/{subsystem}/ack**
    

```json
{
  "method":"jump",
  "ts":1760093093502,
  "tid":"adfsdfdsfdsf",
  "result": 1,
  "msg":"ok",
  "data":{}
}

// tid：全局唯一，便于追踪指令传输路径，将指令发起者的traceId返回；
```

以上顶层 result/msg 是旧通用模板。Media、Vision 和 FSVR 的普通 ACK 均按各自专节使用 `data.result/msg`；GW 不得只读顶层 result 后认定结果缺失。航点专用回复和设备内部 detect/ack 有各自格式，不能套用普通 ACK。

## gw 主题

注意：此内容为 "data**"** 字段内

### 心跳

```json
{
  "version":"3.2.1", 
}
```

## vfc 主题

注意：此内容为基本格式中的data字段内容

### 心跳

```json
{
   "version":"3.2.1" 
}
```

### 数据

```json
{
  "sn":"1209039909594066",
  "lon":116.0,
  "lat":40.0,
  "x":1.1,
  "y":-1.2,
  "z":0.0,
  "speed":3.5,
  "heading":40,
  "batteryRemain":50,
  "functionMode":3,
  "controller":1,
  "temperature":45,
  "flyingTime":40,
  "ts":1760093093502
}

//functionMode - 功能模式 1：SDK巡航模式，2：手柄遥控模式，3：普通SDK控制模式
//controller - 控制权归属 1：SDK, 2：手柄遥控器 3:建图模式
```

### 指令

```json
{
   // 透传服务器下发的信息
}
```

### 状态

*   子系统状态：type=subsystem\_status
    

```json
{
  "name":"vfc",
  "version":"3.2.1",
  "type":"subsystem_status",
  "state":1,
  "info":[
    {
      "index":1,
      "sn":"123456787",
      "type":"robotdog"
    },
  ]
}
```

*   导航状态：type=nav\_status
    

```json
{
  "name":"vfc",
  "version":"3.2.1",
  "type":"nav_status",
  "state":1,
  "info":{
    "result": 0,
    "msg":"导航激活运行中",
    "data": "Active"
  }
}
```

*   定位状态： type=loc\_status
    

```json
{
  "name":"vfc",
  "version":"3.2.1",
  "type":"loc_status",
  "state":1,
  "info":{
    "result": 0,
    "msg": "定位地图加载中",
    "data":"MapLoading"
  }
}
```

*   路径导航状态上报 type： nav\_status\_progress
    

```json
{
  "name":"vfc",
  "version":"3.2.1",
  "type":"nav_status_progress",
  "state":1,
  "info":{
    "result": 0,
    "msg":"",
    "flight_id":""
    "data": {
      "count":4,
      "current_index":4,
    }
  }
}
```

## ptz 主题

注意：此内容为基本格式中的data字段内容

### 心跳

```json
{
   "version":"3.2.1" 
}
```

### 数据

```json
{
   "version":"3.2.1" 
}
```

### 指令

```json
{
   {
  "method":"",
  "ts":1760093093502,
  "tid":"adfsdfdsfdsf",
  "target":"",
  "data":{}
}
```

### 状态

*   子系统状态： type=subsystem\_status
    
    ```json
    {
      "name":"ptz",
      "version":"3.2.1",
      "type":"loc_status",
      "state":1,
      "info":[
        {
          "index":1,
          "sn":"123456787",
          "type":"camera/ptz"
        },
      ]
    }
    ```
    
    云台状态
    
     type = gimbal\_status
    
    ```json
    {
        "name": "ptz",
        "version": "3.2.1",
        "type": "gimbal_status",
        "info": [
            {
                "sn": "123456787",
                "type": "ptz",
                "zoomFactor": 2.3
            }
        ]
    }
    ```
    
## media 主题

本节为完整本地报文。Media 执行拍照、录像、直播、供帧、画框和指定帧 JPEG，不执行 AI 决策或普通文件列表/上传。

| Topic | 方向 | 内容 |
| --- | --- | --- |
| local/robotdog/media/command | GW → Media | 普通媒体控制 |
| local/robotdog/media/ack | Media → GW | 普通控制 ACK |
| local/robotdog/media/state | Media → GW | 子系统及视频流状态 |

### 普通命令与 ACK

| method | data | 说明 |
| --- | --- | --- |
| camera_livestream | index、state=1/2，可选 sn | 1 开流，2 关流 |
| take_photo | index，可选 sn | 普通拍照 |
| camera_recording | index、mode=1/0，可选 sn | 1 开始录像，0 停止录像 |

index 是非负整数，实际可用流以配置为准，sn 如提供则为字符串；其余既有校验保持不变。新调用保留 target=media；当前实现兼容省略 target，但显式 target 必须为 media。旧 target=vision 的媒体命令由 GW 兼容路由并转换目标，不让 Media 放宽错误目标校验。

```json
{
  "method": "camera_livestream",
  "tid": "media-live-001",
  "bid": "batch-001",
  "ts": 1760093093502,
  "target": "media",
  "data": {
    "index": 1,
    "sn": "002",
    "state": 1
  }
}
```

```json
{
  "method": "camera_livestream",
  "tid": "media-live-001",
  "bid": "batch-001",
  "ts": 1760093093600,
  "target": "media",
  "data": {
    "result": 0,
    "msg": "",
    "index": 1,
    "sn": "002",
    "type": "camera",
    "stream_id": "video1",
    "livestream_enabled": true,
    "mode": "processed"
  }
}
```

普通 ACK 的结果是 data.result/msg：0 成功，1 失败。target 在请求提供时原样回带；bid 非空时回带，data.task_name 非空时提升到 ACK 顶层 task_name。GW 应读取这一层结果，再按云端协议透传或转换一次，不额外套一层固定成功。普通 ACK 不使用顶层 result/msg，也不增加内部 phase。

### 状态（约 2 秒）

子系统状态是带 sn/ts/data 外壳的完整消息：

```json
{
  "sn": "10300262872499",
  "ts": 1760093093502,
  "type": "subsystem_state",
  "data": {
    "name": "media",
    "version": "1.0.0",
    "state": 1,
    "info": [
      {
        "index": 1,
        "sn": "002",
        "type": "robotdog"
      }
    ]
  }
}
```

视频流状态是另一个独立的扁平报文，当前实现没有上述 data 外壳：

```json
{
  "name": "media",
  "version": "1.0.0",
  "type": "stream_status",
  "state": 1,
  "info": {
    "type": "video",
    "index": "1",
    "sn": "002",
    "result": 1,
    "msg": "OK"
  }
}
```

这里 info.result=1 表示直播外推活跃，0 表示不活跃，不是普通 ACK 的成功/失败码。该报文也不能替代相机输入、本地 RTSP 可播放和云端 RTMP 外推这几种不同状态的核对。GW 转为云端 method=stream_status，不把本地 type 当普通命令名。

Media 不再发布独立 heartbeat。AI 状态和检测事件不在本节；音频、红外、WebRTC 目标能力尚未接入，不能仅改 target 就宣称支持。

## vision 主题

Vision 保留人脸推理、AI 开关、事件开关及检测事件；普通拍照/录像/直播归 Media，文件列表/同步归 FSVR。

| Topic | 方向 | 内容 |
| --- | --- | --- |
| local/robotdog/vision/command | GW → Vision | AI 和事件控制 |
| local/robotdog/vision/ack | Vision → GW | 普通控制 ACK |
| local/robotdog/vision/state | Vision → GW | 子系统及 AI 状态 |
| local/robotdog/vision/events | Vision → GW | 检测事件，过渡期由 GW 上传检测图 |

### 普通命令与 ACK

| method | data | 说明 |
| --- | --- | --- |
| start_ai | filters=[1] 或 [] | 完整目标集合；当前只支持人脸 1，[] 不启用目标 |
| stop_ai | {}，兼容旧空字符串/filters | 全局停止 AI，沿用既有兼容行为 |
| event_switch | mode=0/1；开启时 upload_path 必填 | 控制事件抓拍/上报，upload_path 为 HTTP(S) 目标前缀 |

当前 start_ai 严格拒绝含 2、3 或重复元素的 filters，不是忽略不支持的目标继续成功。开启检测前须已开流；Vision 不替 Media 开流。重复设置同值 event_switch 不应重置 5 秒抓拍节流窗口。

```json
{
  "method": "start_ai",
  "tid": "vision-ai-001",
  "bid": "batch-001",
  "ts": 1760093093502,
  "target": "vision",
  "data": {
    "filters": [
      1
    ]
  }
}
```

```json
{
  "method": "start_ai",
  "tid": "vision-ai-001",
  "bid": "batch-001",
  "ts": 1760093093600,
  "target": "vision",
  "data": {
    "result": 0,
    "msg": ""
  }
}
```

普通 target 存在时必须是 vision，省略时保留兼容；ACK 的 target/bid/task_name 回带规则与 Media 相同，业务结果仍为 data.result/msg。GW 遇到 result=missing，应先对照原始 ACK 核对取值层级，不能仅因此反复重发或由子模块伪造顶层成功。

### 状态（约 2 秒）

```json
{
  "sn": "10300262872499",
  "ts": 1760093093502,
  "type": "subsystem_state",
  "data": {
    "name": "vision",
    "version": "1.0.0",
    "state": 1,
    "info": [
      {
        "index": 1,
        "sn": "002",
        "type": "robotdog"
      }
    ]
  }
}
```

AI 开关状态是独立扁平报文：

```json
{
  "name": "vision",
  "version": "1.0.0",
  "type": "ai_switch_status",
  "state": 1,
  "info": {
    "state": 1,
    "filters": [
      1
    ]
  }
}
```

info.state 表示 AI 开关，filters 为 [1] 或 []；它不等同于视频流已开，也不能代替模型加载/输入/实际检测阶段的诊断。Vision 不再发布视频 stream_status 或独立 heartbeat。

### 检测事件

以下是当前本地事件完整消息，不在外面再套 sn/type/data：

```json
{
  "ts": 1760093093502,
  "method": "detect_result_event",
  "data": {
    "stream_id": "video1",
    "type": 1,
    "msg": "",
    "bbox": [],
    "file_path": "events/2026-10-19/TTA_20261019105447631.jpg"
  }
}
```

当前人脸事件 type=1，msg 为空、bbox 保留旧空数组，框已画在 JPEG 内；图片路径非空时带 file_path。它是相对媒体根目录的路径，GW 根据已确认目录访问，不是云端 URL。当前事件没有必填的顶层 event_id/tid/bid；不能把新内部抓拍的字段强加给普通旧事件。

检测图由 Media 使用本次保留检测帧生成，当前 RK 成品为 AI 输入尺寸（现网 640×360），不是从直播用 FFmpeg 另截一帧。GW 保持唯一上传者；文件上传接管和 FSVR 登记没有因本节更新就自动完成。GW 上云继续适配原 detect_result_event/file_name 等云端字段。

## 航点动作兼容（Media / Vision）

给 VFC 同事的完整请求/回复、任务顺序、超时/取消及 FSVR 边界见 [VFC 航点动作与模块通信对接约定](VFC航点动作与模块通信对接约定.md)。

普通 command/ack 拆开不代表航点 Topic 随之改名：

| Topic | 说明 |
| --- | --- |
| local/robotdog/vision/WaypointAction/command | 保留旧请求 Topic，Media 和 Vision 各处理自己拥有的动作 |
| local/robotdog/WaypointAction/reply | 保留旧专用回复 Topic |

请求和回复的 type=vision、param 和既有 task_name 校验保持原约定。Media 只处理 take_photo/start_record/stop_record，Vision 只处理 start_ai/stop_ai；同一条动作只由归属方回复，不能两边同时回 unsupported。航点专用回复使用顶层 result=0/-1、msg、data，不改成普通 ACK 的嵌套格式。云端导航命令的 target 仍为 vfc。

## 检测抓拍内部接口（Vision → Media）

本节只描述设备内部联调边界，不是云端 services 的新控制指令，也不是 Media/Vision 共用普通 command。

| Topic | 方向 | 内容 |
| --- | --- | --- |
| local/robotdog/media/detect/command | Vision → Media | method=capture_detection |
| local/robotdog/detect/ack | Media → Vision | 指定帧 JPEG 的最终回复 |

请求显式带 requester=vision 和 tid，保留 bid、ts、event_id；data 带 stream_id、frame_ref、ai_op_gen、detect_wall_ms、boxes。Vision 先在帧套接字上 Hold，成功才发 MQTT 请求。回复使用该内部契约的顶层 result/msg 和 data.phase=completed/rejected/failed；completed 带 delivery_id、root_id=media、file_path、file_size、width、height、encode_ms，失败带 reason。该格式不能套用普通 ACK，更不能作为普通云端回复直接上报。

详细字段、去重、保留/编码超时和 stop_ai 边界以 [Media 接口契约第 6、14 节](../tta_media/docs/MODULE_INTERFACES.md) 为准。JPEG 完成不等于已上传；GW 不执行此内部命令，也不将 detect/ack 冒充云端上传完成。

普通文件方法最终路由 FSVR。当前工作区仍有待负责人清理核验的 Vision 旧文件处理分支，不能以残留分支为由继续双路执行或把文件 target 写回 vision。
    
## fsvr 主题

本节按用户要求沿用云端文件业务格式，覆盖之前复制 Vision 的示例。所有 JSON 均为完整消息，不是再嵌套一层的 data 内容。职责依据见 [FSVR MQTT 协议对齐方案](FSVR_MQTT协议对齐方案.md) 和 [主拆分方案 5.10](L2视觉与媒体模块拆分方案.md)；其中 2026-10-16 的“默认向配置服务分片、upload_path 仅附带”假设已被后续整文件 PUT 决定替代，当前传输约定以本节及 [FSVR MQTT 协议](../tta_fsvr/docs/mqtt_protocol.md) 为准。

### 主题

| Topic | 方向 | 内容 |
| --- | --- | --- |
| local/robotdog/fsvr/command | GW → FSVR | 云端原文件命令按 target 路由 |
| local/robotdog/fsvr/ack | FSVR → GW | 按旧云端结构回复 data.result/msg |
| local/robotdog/fsvr/state | FSVR → GW | 文件业务 method 状态；子系统 type 状态 |

不使用独立 heartbeat、版本 data 或视频 stream_status。FSVR 版本放子系统状态；GW 自身云端心跳不受影响。通用 ACK 的顶层 result/msg 不覆盖本节 FSVR 的云端嵌套格式，GW 为 FSVR 验证后透传，不重复套壳

### 指令

```json
{
  "method": "sync_media_file",
  "tid": "media-sync-001",
  "bid": "batch-001",
  "target": "fsvr",
  "data": {
    "file_path": ["123/345.png", "456/678.mp4"],
    "upload_path": "http://a.com/xxx"
  }
}
```

普通命令无 requester 时内部按 gw 处理；ts 可省略以接受旧同步指令。GW 应保留正确的 target=fsvr；当前 FSVR 也兼容省略 target，但显式 target 存在时必须为 fsvr。旧 target=gw/vision 的文件方法由 GW 改路由并转换目标，不让 FSVR 自行执行其他 target。

| method | data | 回复 |
| --- | --- | --- |
| get_media_filelist | {} | data.result/msg/data，data 为旧树形节点数组 |
| get_map_list | {} | data.result/msg/data，data 为 map_id/size 数组，size 沿用旧 MiB 整数 |
| sync_media_file | file_path 数组、upload_path | 下方媒体 ACK，结果来自持久受理 |
| sync_map_file | map_id 数组、upload_path | data.result/msg，进度另发 sync_map_file_status |

upload_path 是本次命令指定的 HTTP(S) 目标前缀，随请求和传输保存，并纳入请求冲突判定。默认 upload.mode=put，整文件直接 PUT 到该目标下的最终资源：媒体保留相对路径，地图使用 map_id.zip；具体编码和请求体以 FSVR 文档为准。保留 HTTPS 和授权参数，不默默改发配置地址或 Mock。

可选 upload.mode=chunked 仅在服务端支持分片接口时使用，服务入口随传输绑定；普通 MQTT 不因此增加必填分片字段。整文件 PUT 没有分片断点续传，失败后整文件重传；不能把 chunked 模式的恢复能力当成默认 PUT 已支持。

### 媒体 ACK

```json
{
  "tid": "media-sync-001",
  "bid": "batch-001",
  "ts": 1792108800200,
  "method": "sync_media_file",
  "data": {
    "result": 0,
    "msg": "",
    "data": [],
    "total": 2,
    "state": "1"
  }
}
```

ACK 表示受理，data.data 是已经最终成功的路径，刚受理通常为空；不能提前把待上传项报成功。total 为真实请求数量，旧文档样例的 total=8 不是固定值。拒绝时媒体 result=1、state="0"；地图拒绝沿用 result=-1

地图 ACK 保留原结构：

```json
{
  "tid": "map-sync-001",
  "bid": "batch-map-001",
  "ts": 1792108800200,
  "method": "sync_map_file",
  "data": {"result": 0, "msg": ""}
}
```

### 媒体上传状态（约 3 秒，结束即时）

```json
{
  "tid": "media-sync-001",
  "bid": "batch-001",
  "ts": 1792108803000,
  "method": "media_file_upload_status",
  "data": {
    "result": 0,
    "msg": "",
    "data": [],
    "total": 2,
    "percent": 37.5,
    "state": "1"
  }
}
```

成功结束时 state="0"、result=0、percent=100、data 列出实际成功路径；失败也 state="0"，但 result=1、msg 给出原因，data 仅列成功项。不能仅靠 state=0 或 percent=100 判断成功

### 地图上传状态（约 3 秒，结束即时）

```json
{
  "tid": "map-sync-001",
  "bid": "batch-map-001",
  "ts": 1792108803000,
  "method": "sync_map_file_status",
  "data": {
    "result": 0,
    "msg": "",
    "data": ["123"],
    "total": 2,
    "percent": 37.5,
    "state": "1"
  }
}
```

data 是最终成功的 map_id，total 是请求地图数，percent 是当前 ZIP 进度；全部成功 state="0"、result=0，结束但失败 result=-1。原地图保留，不能把 ZIP 回收描述成删除地图

两类上传状态都补顶层 tid/bid 关联；普通使用不要求 phase、requester、command_id、tasks。详细诊断和终态确认标识可放可选 data.ext，内部持久化/去重/补发仍保留

### 存储状态（3 秒）

```json
{
  "ts": 1792108803000,
  "method": "storage_status",
  "data": {
    "upload_remain": 8,
    "total": 128849018880,
    "used": 42949672960
  }
}
```

容量单位为媒体目录所在文件系统的字节；upload_remain 为最近扫描仍存在的本地媒体文件数量（含受过渡保护的 events），不是任务数或 READY 数。当前报文不要求 available。GW 应停止旧的同名扫描/上报，具体切换仍需 GW 负责人实施。

### 子系统在线（2 秒）

```json
{
  "sn": "10300262026905",
  "ts": 1792108800000,
  "type": "subsystem_state",
  "data": {
    "name": "fsvr",
    "version": "1.0.0",
    "state": 1,
    "info": []
  }
}
```

本地 type 使用现有 GW 识别的 subsystem_state，GW 聚合后上云 method=subsystem_status。info 为空，不复制相机 index/sn/type。state=1 表示运行在线，不证明磁盘、数据库和 HTTP 服务全部正常

### 断点与新增业务

仅在 chunked 模式，断点恢复由 FSVR 直接向该传输绑定的服务 HTTP GET /api/v1/uploads/{upload_id}，按 next_part/next_offset 继续传，不需要云端经 GW 下发偏移。普通 MQTT 不必增加分片字段；默认 put 模式不使用这套接口。

删除、query_request、confirm_result 和 Media/Vision 的新内部登记/事件接口属于扩展，按完整方案和各自契约推进。检测图仍由 GW 唯一上传，直到正式切换；音频、预览下载和原地图删除不在此处宣称已实现
