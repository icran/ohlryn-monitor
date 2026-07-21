# watchdog — 봇 프로세스 감시·자동 재기동

3계층 모니터링의 **계층 1**: 봇 프로세스가 죽으면 cron이 자동 재기동하고 텔레그램으로 알립니다. 쉘 스크립트 + cron만 사용합니다 (파이썬 불필요).

## 설계 원칙 (실운영에서 검증된 함정 회피 포함)

- **내부 fd-200 락**: 스크립트 안에서 `exec 200>LOCK; flock -n 200`. 외부 `flock LOCK cmd` 방식은 금지 — 봇이 락 FD를 상속해 watchdog이 영구 무력화됩니다.
- **봇 spawn 시 락 FD 미상속**: `setsid ... 200>&-` — 재기동된 봇이 watchdog의 세션·락을 물려받지 않게 분리.
- **포트 기반 매칭 + self-kill 주의**: 봇 식별은 `run_bot_server.py.*<PORT>` 패턴. 수동으로 죽일 때 `pkill -f`는 자기 명령 문자열까지 매칭될 수 있으니 **PID 지정 kill**을 권장.
- **결과 통보**: 재기동 성공/실패를 텔레그램으로 — 실패 시에만 사람이 개입.
- **봇마다 스크립트 1개 + cron 1줄**: 봇별 config·env·로그가 달라 단순 복제가 가장 명확합니다.

## 사용

```bash
cp watchdog/watchdog.example.sh watchdog/mybot_watchdog.sh
# → 상단 변수(NAME/REPO/PORT/ENV_FILE/CONFIGS/LOG) 수정
chmod +x watchdog/mybot_watchdog.sh
bash watchdog/mybot_watchdog.sh     # 수동 검증: 봇이 떠 있으면 no-op(exit 0)이어야 정상
```

cron 등록 — 봇이 여럿이면 **분 오프셋을 서로 다르게**(`*/5`, `1-59/5`, `2-59/5` ...) 해서 동시 콜드스타트를 피합니다:

```cron
*/5 * * * *    /path/to/watchdog/bot1_watchdog.sh >> ~/watchdog_bot1.log 2>&1
1-59/5 * * * * /path/to/watchdog/bot2_watchdog.sh >> ~/watchdog_bot2.log 2>&1
```

## 주의

- watchdog은 **프로세스 존재만** 봅니다. 살아있지만 멈춘 봇(좀비)은 계층 2(health_check)가 잡습니다.
- 봇을 의도적으로 내릴 때는 **cron부터 주석 처리**하세요 — 안 그러면 5분 내 되살아납니다.
- 재기동 시 봇은 디스크의 최신 config를 읽습니다 — config를 미리 바꿔뒀다면 재기동이 곧 반영 시점입니다.
