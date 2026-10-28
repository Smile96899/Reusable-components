#!/usr/bin/env bash
# deploykit-monitor: 通用健康检查 + 告警（Telegram / Webhook）。
# 由 systemd timer 每分钟运行一次；只在状态变化时发消息（故障一次、恢复一次）。
#
# 配置: /etc/deploykit/monitor.conf   检查项: /etc/deploykit/checks.list
# 参数: --test  立即执行所有检查并打印结果，同时发一条测试消息
set -uo pipefail

CONF=${DEPLOYKIT_MONITOR_CONF:-/etc/deploykit/monitor.conf}
[ -f "$CONF" ] && . "$CONF"
CHECKS=${CHECKS_FILE:-/etc/deploykit/checks.list}
STATE=${STATE_DIR:-/var/lib/deploykit-monitor}
NODE=${NODE_NAME:-$(hostname)}
THRESHOLD=${FAIL_THRESHOLD:-2}
TEST=0; [ "${1:-}" = "--test" ] && TEST=1
mkdir -p "$STATE"

notify() {
  local text="[$NODE] $1"
  echo "$text"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    curl -sS -m 10 -o /dev/null "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=${text}" || true
  fi
  if [ -n "${WEBHOOK_URL:-}" ]; then
    local body
    body=$(printf '%s' "$text" | sed 's/\\/\\\\/g; s/"/\\"/g')
    curl -sS -m 10 -o /dev/null -H 'Content-Type: application/json' \
      -d "{\"text\":\"$body\",\"msgtype\":\"text\",\"content\":\"$body\"}" "$WEBHOOK_URL" || true
  fi
}

# 每个检查输出一行说明；返回 0 表示正常
check_http() {   # http <名称> <URL> [期望状态码] [包含的文字]
  local url=$1 expect=${2:-200} contains=${3:-} out code
  out=$(mktemp); code=$(curl -sS -m "${HTTP_TIMEOUT:-10}" -o "$out" -w '%{http_code}' "$url" 2>/dev/null) || true
  if [ "$code" != "$expect" ]; then echo "HTTP $code (期望 $expect)"; rm -f "$out"; return 1; fi
  if [ -n "$contains" ] && ! grep -qF -- "$contains" "$out"; then echo "响应里没有 '$contains'"; rm -f "$out"; return 1; fi
  rm -f "$out"; echo "HTTP $code"
}
check_systemd() { # systemd <名称> <单元名>
  local s; s=$(systemctl is-active "$1" 2>&1); echo "$s"; [ "$s" = active ]
}
check_disk() {   # disk <名称> <挂载点> [最大使用率%]
  local pct; pct=$(df -P "$1" | awk 'NR==2 {gsub("%","",$5); print $5}')
  echo "使用 ${pct}%"; [ "${pct:-100}" -lt "${2:-90}" ]
}
check_tcp() {    # tcp <名称> <主机> <端口>
  if timeout 5 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; then echo "端口可连"; else echo "连不上 $1:$2"; return 1; fi
}
check_cmd() {    # cmd <名称> <命令...>
  local out; out=$(timeout "${CMD_TIMEOUT:-30}" bash -c "$*" 2>&1); local rc=$?
  echo "${out:0:200}"; return $rc
}

[ -f "$CHECKS" ] || { echo "缺少检查项文件 $CHECKS" >&2; exit 1; }
[ "$TEST" = 1 ] && notify "deploykit-monitor 测试消息"

while read -r type name rest; do
  case "$type" in ''|\#*) continue ;; esac
  # shellcheck disable=SC2086
  case "$type" in
    http)    detail=$(check_http $rest) ;;
    systemd) detail=$(check_systemd $rest) ;;
    disk)    detail=$(check_disk $rest) ;;
    tcp)     detail=$(check_tcp $rest) ;;
    cmd)     detail=$(check_cmd "$rest") ;;
    *)       echo "未知检查类型: $type" >&2; continue ;;
  esac
  rc=$?
  key=$(printf '%s' "$type-$name" | tr -c 'A-Za-z0-9_.-' '_')
  fails=$(cat "$STATE/$key.fails" 2>/dev/null || echo 0)
  alerted=$(cat "$STATE/$key.alerted" 2>/dev/null || echo 0)
  [ "$TEST" = 1 ] && echo "$([ $rc = 0 ] && echo ✓ || echo ✗) $type $name: $detail"
  if [ $rc = 0 ]; then
    echo 0 > "$STATE/$key.fails"
    if [ "$alerted" = 1 ]; then notify "✅ 恢复: $name ($detail)"; echo 0 > "$STATE/$key.alerted"; fi
  else
    fails=$((fails + 1)); echo "$fails" > "$STATE/$key.fails"
    if [ "$fails" -ge "$THRESHOLD" ] && [ "$alerted" != 1 ]; then
      notify "🔴 故障: $name — $detail（连续 $fails 次）"; echo 1 > "$STATE/$key.alerted"
    fi
  fi
done < "$CHECKS"
exit 0
