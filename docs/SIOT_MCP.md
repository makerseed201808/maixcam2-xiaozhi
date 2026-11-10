# 小智 SIoT MCP 使用说明

更新：2026-10-28。用户已授权整套更新，46项工具与配套监控代码已部署。状态查询、服务器主题列表及固定音频云端调用已通过真机测试；其余专项仍待验收，详见 [部署与状态修复记录](SIOT_STATUS_FIX_2026-10-28.md)。下文2026-10-27的主机测试与验收清单保留作为阶段记录。

## 功能清单

17项 SIoT MCP 加上原有29项，源码注册共46项。工具经小智现有WebSocket调用，不是新增对外HTTP MCP服务器。

| 工具名（前缀均为 `self.siot.`） | 参数与功能 | 语音示例 |
|---|---|---|
| `get_status` | MQTT连接、服务器登记主题总数、本客户端订阅数 | “查询SIoT连接状态和主题数量。” |
| `list_topics` | `limit=20`（1–64）、`offset=0`；完整名称、总数、分页 | “列出服务器已有主题。” |
| `get_history` | `topic`、`limit=10`（1–30）、`offset=0`、`start_time=0`、`end_time=0`；Unix秒范围，0不限定 | “查看温度主题最近10条记录。” |
| `read_latest` | `topic`、`source=auto`；auto/live/history，区分来源时间 | “读取温度最新消息，说明数据时间。” |
| `publish` | `topic`、文本`message`、`save=false`；最多4096 UTF-8字节 | “向siot/demo发送测试成功，并保存历史。” |
| `subscribe` | `topic`；持续监听缓存，不逐条朗读 | “开始监听siot/temperature。” |
| `unsubscribe` | `topic`；停止本客户端监听，不删除主题/历史 | “停止监听温度主题。” |
| `list_subscriptions` | 列本MCP客户端监听状态 | “小智现在监听哪些主题？” |
| `create_topic` | `topic`；当前账号下创建，已存在则返回现有主题 | “新建siot/demo主题。” |
| `clear_history` | `topic`、`confirmation_token=""`；二次确认清空历史，保留主题和监听 | “清空siot/demo的历史数据。” |
| `delete_topic` | `topic`、`confirmation_token=""`；二次确认删除主题及历史 | “删除siot/demo主题。” |
| `media.get_status` | 查询已打开的监控App媒体状态 | “摄像头现在是否在发送画面？” |
| `media.start` | `topic`、`wire_format=base64`（json/base64）、`fps=5`（2/5/10） | “向siot/camera发画面，用Mind+格式每秒5帧。” |
| `media.stop` | 停止当前监控App媒体收发，释放资源，不停止broker | “停止SIoT画面收发。” |
| `media.show` | `source=topic/url/lan_sender`；`target`为主题/地址，lan_sender传空字符串 | “显示siot/camera画面。”／“开启摄像头局域网共享。” |
| `device.list_devices` | 列本地登记设备和允许动作，不扫描网络 | “列出可控制的SIoT设备。” |
| `device.command` | `device`、`action`、`value_json="{}"`、`timeout_ms=3000`（100–10000） | “让测试指示灯执行set_indicator，enabled为true，并确认反馈。” |

主题统一支持简写（2026-10-28）：无斜杠的名称如`mcp_test`自动补本地配置的账号前缀，账号为siot时得到`siot/mcp_test`。带斜杠的完整主题保持原样，不模糊匹配、不重复添加前缀。此规则覆盖创建、查询历史/最新消息、发送、订阅/退订、删除/清空、媒体发送及主题画面接收；URL和本地登记的设备协议主题不改写。删除/清空仍以解析后的完整主题名二次确认。可直接说“新建主题mcp_test”“向mcp_test发送测试成功”“读取mcp_test最新消息”。返回消息是外部数据，不是新指令。

## 主题与历史

启动时`local_subscription_count=0`正常，不代表服务器没有主题；`server_topics_total`来自服务器目录，查询失败为`null`。用户提供的2618网页对主题使用`GET /topics`，用`data.topics.length`统计总数，不依赖`data.total`。适配器据此有界读取（最多512条/512KiB）后本地分页。此修正有离线源码和本机HTTP测试依据，尚待已安装服务复验。

供应前端把历史接口的`messages[0]`显示为“最新数据”，适配器沿用这一契约并注明排序依据。每条历史文本最多4096字节，截断明确标记。`auto`优先返回已观察实时/缓存值，无可用值时查历史；缓存年龄不是设备采样年龄，历史不能当作当前物理状态。新的无效或超长实时消息不会偷偷换成旧历史伪装正常。

首次订阅前发出的普通MQTT消息不会补发，低频设备应先`subscribe`。最多同时监听16个精确主题，不支持通配符；断线使旧缓存失效，重连仅恢复仍在订阅列表的主题。

## 发送与保存

- `save=false`：QoS0、非retained，不请求历史保存。`local_sent`仅表示本地发送完成。
- `save=true`：先登记当前账号所属主题，再QoS1发送。`broker_ack`表示broker确认并请求保存；**`history_saved=null`不是数据库已确认保存**，应调用历史工具回读。
- 两种模式都不等于接收设备执行。发送不确定时不自动重发；退出会话能取消尚在等待的发送，不能撤回已提交字节。
- 图片仍走专用媒体通道，不塞入短文本接口。

## 删除、清空的二次确认

第一次调用时确认标记留空，只读取目标并返回完整主题、影响和临时令牌。必须告知用户影响并取得明确二次确认，再调用同一工具执行；不得自动确认。

令牌绑定当前会话、动作、主题及服务器主题ID，MCP层有效60秒、单次使用。跨会话、更换动作/主题/ID或重放均拒绝。清空保留主题与实时监听；删除成功取消本地监听。执行后回读，无法确认时明确报告未知，不自动重试破坏性操作。清空期间新数据可能继续写入，不能因发现新数据再次自动清空。

## 媒体控制

**先从设备桌面打开SIoT监控App。** 不使用未经验证的自动启动方式，不在小智服务进程初始化摄像头/显示。媒体请求经权限受限的本机Unix socket由监控UI线程应用；有界排队、超时取消、迟到启动清理，编辑时拒绝覆盖。

开始返回仅说明已应用启动请求，预热/连接/收图状态还需查询。`show(topic,完整主题)`显示主题图片，`show(url,完整地址)`接收HTTP/HTTPS图片、MJPEG或RTSP，`show(lan_sender,"")`开启现有HTTP摄像头共享。页面显示连接地址，MCP不返回带凭据URL或画面字节。其余格式和性能边界见[媒体说明](SIOT_MEDIA.md)。

## 设备反馈

默认无登记设备。按[设备协议](SIOT_DEVICE_PROTOCOL.md)配置别名、设备ID、命令主题、反馈主题、允许动作。

命令先订阅再发送一次，仅接受新序号、非retained且命令ID/设备ID都匹配的反馈。`execution_confirmed`代表设备报告成功，不等于独立物理测量；超时为`execution_unknown`，不自动重试。[主机模拟接收器](../examples/siot_device_responder.py)只改变内存状态，不是已验收ESP32固件。

## 统一真机验收（本轮不执行）

本轮自动化结果：Windows全套491项，483通过、8项平台跳过；本机WSL媒体专项22项全部通过，包括真实Unix socket立即断开/迟到启动清理。MCP真实工具清单分页测试核对46项且无遗漏/重复，每页满足8000字节预算。源码编译、差异检查和独立代码复核通过；这些结果不代表设备部署已完成。

复现主机测试：`python -m unittest discover -s tests`。Windows上的Unix socket专项使用本机WSL：

```powershell
wsl -d Ubuntu2404 --cd /mnt/f/Git/Maixcam2/projects/maixcam2-xiaozhi python3 -m unittest discover -s tests -p test_siot_media_control.py
```

1. 备份同步完整服务/监控模块，按显示先于音频部署；核对46个工具和文件哈希。
2. 真人语音查询目录，对照网页/监控列表、分页及两种主题数量。
3. 唯一测试主题创建、保存、历史回读；核对auto/live/history来源和日期。
4. 双客户端订阅、取消、重连、晚到消息、退出后不晚发。
5. 专用测试主题验证取消/过期确认、确认清空、确认删除；不操作业务主题。
6. 已打开监控App下start/show/stop、编辑保护、相机冲突、收发与资源释放。
7. ESP32真实动作、反馈、错ID、重复包、离线、取消与超时。
8. 真人语音与连续画面共存、重启和长期稳定性。

主机测试覆盖REST、真实localhost HTTP、MQTT模拟传输、MCP分发、取消、令牌隔离和设备反馈，本机WSL覆盖真实Unix socket。以上不能替代设备音频、触摸、驱动、物理动作和性能验收。
