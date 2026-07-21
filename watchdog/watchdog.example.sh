#!/usr/bin/env bash
# ohlryn-monitor watchdog 예시 — 봇마다 복사해 상단 변수만 수정.
# 봇이 살아있으면 no-op(exit 0), 죽어있으면 재기동 + 텔레그램 통보.
set -uo pipefail

NAME=mybot                                   # 알림에 표시될 이름
REPO=/path/to/your-bot-repo                  # 봇 실행 디렉토리
PORT=8010                                    # 봇 API 포트 (프로세스 식별용)
ENV_FILE=.env_mybot                          # TELEGRAM_TOKEN/CHAT_ID 포함 env (REPO 기준)
CONFIGS="configs/my_strategy.json"           # 봇 실행 인자
LOG=my_bot.log                               # 봇 stdout 로그 (REPO 기준)
START_CMD=".venv/bin/python scripts/run_bot_server.py $CONFIGS --port $PORT --env-file $ENV_FILE --auto-start"

MATCH="run_bot_server.py.*${PORT}"
LOCK=/tmp/${NAME}_wd.lock

cd "$REPO" || exit 1

# 내부 fd-200 락 — cron 주기 겹침 방지 (외부 flock 래핑 금지: 봇이 락 FD 상속함)
exec 200>"$LOCK"
flock -n 200 || exit 0

if pgrep -f "$MATCH" >/dev/null; then exit 0; fi

echo "[$(date -u "+%F %T")Z] bot(${PORT} ${NAME}) down -> restart"
# setsid + 200>&- : 재기동된 봇이 watchdog 세션·락 FD를 상속하지 않게 분리
setsid $START_CMD >> "$LOG" 2>&1 < /dev/null 200>&- &

sleep 10
if pgrep -f "$MATCH" >/dev/null; then STATUS="restart OK"; else STATUS="restart FAILED - manual check"; fi

TOKEN=$(grep -E '^TELEGRAM_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"\r')
CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"\r')
TS=$(date "+%F %H:%M %Z")
if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
  curl -s --max-time 10 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT}" \
    --data-urlencode "text=watchdog(${PORT} ${NAME}) ${STATUS} ${TS}" >/dev/null || true
fi
