#!/usr/bin/env bash
# 通过 ssh 在远程主机本机回环地址上验证 S05 服务（无需开放公网端口）。
# 用法: bash deploy/smoke.sh [host] [port]
set -euo pipefail

HOST="${1:-oracle-1}"
PORT="${2:-8080}"
BASE="http://127.0.0.1:${PORT}"

remote() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$1"
}

echo "== GET ${BASE}/health =="
health=""
for _ in $(seq 1 30); do
  if health=$(remote "curl -fsS -m 3 ${BASE}/health" 2>/dev/null); then
    break
  fi
  sleep 1
done
if [ -z "${health}" ]; then
  echo "FAIL: ${BASE}/health unreachable on ${HOST}" >&2
  exit 1
fi
echo "${health}"
if ! grep -q '"status": *"ok"' <<<"${health}"; then
  echo "FAIL: health payload does not contain status=ok" >&2
  exit 1
fi

echo "== POST ${BASE}/chat =="
chat=$(remote "curl -fsS -m 30 -X POST -H 'Content-Type: application/json' -d '{\"message\":\"计算 365 * 24\"}' ${BASE}/chat")
echo "${chat}"
if ! grep -q '8760' <<<"${chat}"; then
  echo "FAIL: /chat did not return the calc result 8760" >&2
  exit 1
fi
if ! grep -q '"stopped_reason": *"completed"' <<<"${chat}"; then
  echo "FAIL: /chat did not complete normally" >&2
  exit 1
fi

echo "SMOKE OK (${HOST} ${BASE})"