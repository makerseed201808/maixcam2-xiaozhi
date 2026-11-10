# SIoT 设备命令与执行反馈协议

本协议为小智与 ESP32 等设备提供命令关联，不代表已完成物理设备联调。本轮只做源码与主机模拟测试；真实 GPIO、执行器、电气连接及 ESP32 固件由后续真机验收确认。

## 小智接口

`self.siot.device.list_devices` 无参数，列出本地已登记的设备、允许动作和命令/反馈主题，不扫描网络，不宣称设备在线。

`self.siot.device.command` 参数：

| 参数 | 类型/默认 | 含义 |
|---|---|---|
| `device` | 必填字符串 | 本地设备别名 |
| `action` | 必填字符串 | 此设备允许的动作 |
| `value_json` | JSON对象字符串，默认`{}` | 动作参数，最多2048 UTF-8字节，不执行代码 |
| `timeout_ms` | 整数，默认3000，100–10000 | 消息提交后等待匹配执行反馈的时间；连接/订阅/发送另有各自有界等待 |

例如：“查询已登记的 SIoT 设备”“让测试指示灯执行 set_indicator，enabled 为 true，并确认反馈”。实际使用应配置用户熟悉的别名和明确动作，不让小智猜测主题或设备。

## 本地登记

在设备私有 `/etc/siot/monitor.json` 中添加 `devices`（保留现有服务器连接配置，不提交真实凭据）：

```json
{
  "devices": {
    "测试指示灯": {
      "device_id": "esp32-demo-1",
      "command_topic": "siot/demo-device/command",
      "reply_topic": "siot/demo-device/reply",
      "allowed_actions": ["read_state", "set_indicator"]
    }
  }
}
```

默认没有设备；此时列表为空，发送明确返回未登记。最多32个设备，每台最多32个允许动作。命令和反馈必须是不同的完整主题，不能含 MQTT 通配符；传输客户端同时最多监听16个主题，因此接近上限时需要显式取消不再使用的订阅。

## 消息契约

小智先订阅反馈主题，取得当前接收序号，再发送一次非 retained、QoS0命令：

```json
{
  "type": "command",
  "command_id": "1234567890abcdef1234567890abcdef",
  "device_id": "esp32-demo-1",
  "action": "set_indicator",
  "value": {"enabled": true}
}
```

接收设备验证目标 ID 和动作白名单后执行，再向反馈主题发布：

```json
{
  "type": "command_result",
  "command_id": "1234567890abcdef1234567890abcdef",
  "device_id": "esp32-demo-1",
  "success": true,
  "state": {"indicator": true}
}
```

`success` 必须是 JSON 布尔值，`state` 必须是对象，整条反馈最多4096字节。失败也返回同一 ID、`success:false` 和当前状态。反馈必须非 retained；设备状态消息可以另设主题，不拿旧 retained 状态确认新命令。

小智只接受命令发送前订阅基线之后收到的、设备ID与命令ID同时匹配的有效反馈。错设备、错ID、retained、旧序号及无效JSON都不会确认成功。一笔命令最多处理64条无关反馈，超过限制按无法确认处理。

| 返回状态 | 含义 |
|---|---|
| `execution_confirmed` | 收到匹配的 `success:true`，代表设备自身报告成功；不等于外部传感器已验证物理动作 |
| `execution_failed` | 收到匹配的 `success:false` |
| `execution_unknown` | 超时、断线、发送不确定或发出后会话取消；设备可能已经执行，不自动重试 |

同一时刻只执行一笔设备命令事务，重复调用会生成新的命令ID，不做自动重发。取消仅能阻止尚未提交的命令，已经发出的字节不能撤回。

## 接收端实现要求

- 只处理自己的精确命令主题、设备ID和允许动作；不要将字符串作为程序、命令行或表达式执行。
- 在实际操作成功后再反馈，不在刚收到消息时提前回复成功。
- 根据 `command_id` 去重，同一ID与相同内容重复到达时返回原结果，不重复动作；相同ID不同内容拒绝。
- 控制应优先使用绝对状态（如`enabled:true`），避免“翻转一次”或“再转90度”这类重试后难以判断的相对动作。
- 重启、网络恢复和去重记录过期如何处理，必须在真实接收固件中明确；不承诺跨设备重启的恰好一次执行。

## 主机模拟示例

仓库提供 `examples/siot_device_responder.py`。显式运行后才连接MQTT，仅模拟内存中的指示灯，没有任何GPIO/物理硬件调用：

```sh
python examples/siot_device_responder.py --config /path/to/private-monitor.json --device 测试指示灯
```

示例支持 `read_state` 与 `set_indicator`，并与配置允许动作取交集。去重缓存最多128条、保留5分钟，只覆盖该进程运行期间；ESP32移植时需根据动作风险设计自己的缓存与掉电策略。凭据文件保存在本地，不放入仓库。

后续真机验收：设备自身动作正确 → 独立状态回报 → 小智命令/反馈匹配 → 错ID与超时不误报 → 重复包不重复动作 → 断线/重启恢复。
