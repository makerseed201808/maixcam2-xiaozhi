#!/bin/sh
# MaixCAM2 上启停显微服务。不要用 kill 直接结束：相机打开时被强杀可能导致相机“vi init failed”，需重启才能恢复。
#   /root/microscope/run_scope.sh start | stop | status | log
cd "$(dirname "$0")"
LOG=/tmp/scope_service.log
pid() { pgrep -f "^/usr/bin/python3 -u scope_service.py" | head -1; }
case "$1" in
  start)
    [ -n "$(pid)" ] && { echo "已在运行 pid $(pid)"; exit 0; }
    rm -f /tmp/scope_service.stop
    nohup /usr/bin/python3 -u scope_service.py > $LOG 2>&1 &
    sleep 2; echo "已启动 pid $(pid)" ;;
  stop)
    P=$(pid); [ -z "$P" ] && { echo "未在运行"; exit 0; }
    touch /tmp/scope_service.stop
    for i in $(seq 1 30); do kill -0 $P 2>/dev/null || { echo "已停止"; exit 0; }; sleep 0.5; done
    echo "15 秒内未退出，pid $P 仍在运行（未强杀，请检查日志）"; exit 1 ;;
  status) P=$(pid); [ -n "$P" ] && echo "运行中 pid $P" || echo "未在运行" ;;
  log) grep -vE "COMM_|AX_|i2c_init|^-- \[I\]" $LOG | tail -n "${2:-30}" ;;
esac
