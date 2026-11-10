# SIoT状态查询修复与完整部署

2026-10-28：用户授权全部更新后测试。此前设备保留32项工具版本，未安装前一轮源码扩展。

实际登录响应中的token含一个普通空格，旧校验拒绝所有低于ASCII33的字符，导致有效认证被拒绝。修复为允许普通空格、保留服务端令牌原样，同时拒绝控制字符、非ASCII、空白令牌和超长令牌；新增带认证前缀令牌回归。

已备份并同步6个服务文件（主服务及5个SIoT模块）、监控main.py与mcp_control.py；文件哈希一致。按显示先于音频重启，小智恢复idle，SIoT服务PID保持不变。备份在设备 `/root/xiaozhi_backups/siot-mcp-20261028-113929`。

已安装版本的实际MCP分发验证：共46项工具；get_status返回connected=true、server_catalog_available=true、server_topics_total=10、local_subscription_count=0；list_topics返回10项。0表示这次独立查询客户端尚未监听主题，不是服务器没有主题。

固定测试音频经运行中的小智云端成功选择get_status，日志确认isError=False。这不替代真人麦克风验证，也不代表其余主题写操作、媒体控制或ESP32物理执行已验收。没有删除业务主题或改动设备凭据。
