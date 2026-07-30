#!/usr/bin/env bash
# ohlryn-monitor watchdog 예시 — 봇마다 복사해 상단 변수만 수정.
# 봇이 살아있으면 no-op(exit 0), 죽어있으면 crash-loop 판정 후 재기동/차단.
#
# crash-loop guard (watchdog_guard.py):
#   부팅(120s) 내 사망 연속 2회 또는 30분 내 3회 사망 → 소생 중단 + CRITICAL 텔레그램
#   복구: 원인 수정 후  rm ~/wd_state/<NAME>.crashloop
set -uo pipefail

NAME=mybot                                   # 알림에 표시될 이름
REPO=/path/to/your-bot-repo                  # 봇 실행 디렉토리
MONITOR_DIR=$HOME/ohlryn-monitor             # ohlryn-monitor 위치 (guard 모듈)
PORT=8010                                    # 봇 API 포트 (프로세스 식별용)
ENV_FILE=.env_mybot                          # TELEGRAM_TOKEN/CHAT_ID 포함 env (REPO 기준)
CONFIGS="configs/my_strategy.json"           # 봇 실행 인자
LOG=my_bot.log                               # 봇 stdout 로그 (REPO 기준)
START_CMD=".venv/bin/python scripts/run_bot_server.py $CONFIGS --port $PORT --env-file $ENV_FILE --auto-start"

MATCH="run_bot_server.py.*${PORT}"
LOCK=/tmp/${NAME}_wd.lock
STATE_DIR=$HOME/wd_state
GUARD_STATE=$STATE_DIR/${NAME}.json
FLAG=$STATE_DIR/${NAME}.crashloop

cd "$REPO" || exit 1
mkdir -p "$STATE_DIR"

# 내부 fd-200 락 — cron 주기 겹침 방지 (외부 flock 래핑 금지: 봇이 락 FD 상속함)
exec 200>"$LOCK"
flock -n 200 || exit 0

# crash-loop 차단 중이면 침묵 (복구 = 플래그 삭제)
[ -f "$FLAG" ] && exit 0

if pgrep -f "$MATCH" >/dev/null; then exit 0; fi

# ── 사망 확인됨 → guard 판정 (사망 기록 + RESTART/BREAK 결정) ──
send_telegram() {  # $1 = 메시지
  local TOKEN CHAT
  TOKEN=$(grep -E '^TELEGRAM_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"\r')
  CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"\r')
  [ -n "$TOKEN" ] && [ -n "$CHAT" ] && curl -s --max-time 10 \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT}" \
    --data-urlencode "text=$1" >/dev/null || true
}

DECISION=$(PYTHONPATH="$MONITOR_DIR" python3 -m ohlryn_monitor.watchdog_guard check \
  --name "$NAME" --state "$GUARD_STATE" --flag "$FLAG" --log "$LOG" 2>>"$STATE_DIR/${NAME}.guard.err" || echo RESTART)
# guard 자체가 실패하면(파이썬 부재 등) 안전측 = 기존 동작(RESTART)으로 폴백

if [ "$DECISION" = "BREAK" ]; then
  echo "[$(date -u "+%F %T")Z] bot(${PORT} ${NAME}) CRASH LOOP -> break (no restart)"
  send_telegram "$(cat "$FLAG.alert" 2>/dev/null || echo "🚨 crash loop 차단: ${NAME} — rm ${FLAG} 후 재개")"
  exit 0
fi

echo "[$(date -u "+%F %T")Z] bot(${PORT} ${NAME}) down -> restart"
# setsid + 200>&- : 재기동된 봇이 watchdog 세션·락 FD를 상속하지 않게 분리
setsid $START_CMD >> "$LOG" 2>&1 < /dev/null 200>&- &

sleep 10
if pgrep -f "$MATCH" >/dev/null; then
  STATUS="restart OK"
  PYTHONPATH="$MONITOR_DIR" python3 -m ohlryn_monitor.watchdog_guard on-start \
    --state "$GUARD_STATE" >/dev/null 2>&1 || true   # 기동 시각 기록 (부팅실패 판정용)
else
  STATUS="restart FAILED - manual check"
fi

TS=$(date "+%F %H:%M %Z")
send_telegram "watchdog(${PORT} ${NAME}) ${STATUS} ${TS}"
