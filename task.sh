#!/usr/bin/env bash
# 个人任务管理工具启停脚本:start | stop | status | export
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/app.py"
PID_FILE="$DIR/server.pid"
PORT_FILE="$DIR/.port"
PORT=8765

# 读取记录的端口(不探测),用于 status/stop 展示
read_port() {
  local base=8765
  [ -f "$PORT_FILE" ] && base="$(cat "$PORT_FILE" 2>/dev/null)"
  [ -n "$base" ] || base=8765
  PORT="${TODO_PORT:-$base}"
}

# 显式 TODO_PORT 直接使用(被占则启动失败);否则从上次端口(.port,默认 8765)
# 起顺延找第一个空闲端口,解决多个 workspace 的 todo 并存冲突,选定后回写 .port
resolve_port() {
  local base out
  if [ -n "${TODO_PORT:-}" ]; then
    PORT="$TODO_PORT"
    return 0
  fi
  base=8765
  [ -f "$PORT_FILE" ] && base="$(cat "$PORT_FILE" 2>/dev/null)"
  [ -n "$base" ] || base=8765
  out="$(python3 - "$base" <<'PYPORT'
import socket, sys
for p in range(int(sys.argv[1]), int(sys.argv[1]) + 50):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", p))
    except OSError:
        s.close()
        continue
    s.close()
    print(p)
    break
PYPORT
)"
  if [ -z "$out" ]; then
    echo "✗ 自端口 $base 起连续 50 个端口均被占用,无法自动选口" >&2
    exit 1
  fi
  PORT="$out"
}

# 输出仍在运行的 pid;清理失效 pid 文件后返回 1
running_pid() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "$pid"
    return 0
  fi
  rm -f "$PID_FILE"
  return 1
}

wait_http() {
  local pid="$1"
  local i
  for i in $(seq 1 50); do
    if curl -s -o /dev/null "http://127.0.0.1:$PORT/api/tasks" &&
       kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

cmd_start() {
  local pid
  if pid="$(running_pid)"; then
    read_port
    echo "已在运行 (pid=$pid, 端口=$PORT)"
    exit 0
  fi
  resolve_port
  TODO_PORT="$PORT" nohup python3 "$APP" >/dev/null 2>&1 &
  echo $! > "$PID_FILE"
  if wait_http "$!"; then
    echo "$PORT" > "$PORT_FILE"
    python3 "$APP" --export   # 立即生成初始快照,保证 file:// 只读兜底可用
    echo "已启动 (pid=$(cat "$PID_FILE"), 端口=$PORT)"
  else
    echo "启动失败:端口 $PORT 无法访问,请手动运行 python3 app.py 查看原因" >&2
    kill "$(cat "$PID_FILE")" 2>/dev/null
    rm -f "$PID_FILE"
    exit 1
  fi
}

cmd_stop() {
  local pid
  if pid="$(running_pid)"; then
    kill "$pid"
    local i
    for i in $(seq 1 50); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    echo "已停止 (pid=$pid)"
  else
    echo "未在运行"
  fi
  python3 "$APP" --export
  rm -f "$PID_FILE"
}

cmd_status() {
  local pid
  read_port
  if pid="$(running_pid)"; then
    echo "运行中 (pid=$pid, 端口=$PORT)"
  else
    echo "未运行"
  fi
}

cmd_export() {
  python3 "$APP" --export
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  export) cmd_export ;;
  *) echo "用法:$0 {start|stop|status|export}" >&2; exit 1 ;;
esac
