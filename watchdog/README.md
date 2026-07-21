# watchdog

봇 프로세스 감시·자동 재기동. 현재 server3의 `sub_binance_watchdog.sh`가 **repo 미커밋 로컬 파일**로 존재 — 이곳으로 이관 예정.

## 설계 (현행 server3, 이관 시 유지할 원칙)

- **내부 fd-200 락**: 외부 `flock LOCK cmd` 금지(봇이 락 FD 상속 → watchdog 영구 무력화). 스크립트 내부 `exec 200>/tmp/sub_binance_wd.lock; flock -n 200`.
- **봇 spawn 시 락 FD 미상속**: `setsid ... 200>&-`.
- **PID 지정 kill만** (`pkill -f` 금지 — 명령 문자열에 `run_bot_server.py`가 있으면 self-kill).
- 재기동 성공/실패를 텔레그램으로 통지.

이관 후: `watchdog/sub_binance_watchdog.sh` + cron은 `cron/server3.crontab`에 선언.
