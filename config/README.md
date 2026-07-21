# 세팅 매뉴얼

처음 설정하는 순서 그대로입니다. 이 폴더의 예시 파일을 복사해 자기 값으로 채우면 됩니다.

```
config/
  env.example                  # 시크릿 (텔레그램·거래소 키) 템플릿
  health_check.example.json    # 서버/봇 헬스체크 설정 템플릿
  pnl_watch.example.json       # 계좌 수익률 기록 알림 설정 템플릿
  crontab.example              # cron 등록 예시
```

---

## 0. 사전 준비 (한 번만)

1. **텔레그램 봇** — [@BotFather](https://t.me/BotFather)에서 `/newbot` → 토큰 받기. 만든 봇과 채팅을 **Start** 해두고, `https://api.telegram.org/bot<토큰>/getUpdates`에서 `chat.id` 확인.
2. **healthchecks.io** (선택, 서버 사망 감지) — 무료 가입 → Check 생성 → ping URL 복사. **Period=5분, Grace=10분** 설정. Integrations 메뉴에서 Telegram 연결.
3. **거래소 API 키** (pnl_watch용) — **읽기 전용** 권한이면 충분. 서버 IP를 화이트리스트에 등록.

## 1. 시크릿 env 만들기

```bash
cp config/env.example config/.env_monitor      # (.env* 는 gitignore 됨)
```

`config/.env_monitor`를 열어 값 입력. 알림 전용이면 텔레그램 두 줄만 있으면 됩니다. 이미 봇을 운영 중이라 봇별 env 파일(`.env_mybot` 등)에 같은 키들이 있다면 그것을 그대로 참조해도 됩니다 — config의 `env`/`alert_env` 필드는 파일 경로일 뿐입니다.

## 2. health_check 설정

```bash
cp config/health_check.example.json config/health_check.myserver.json
```

핵심 필드:
- `repo`: 봇이 실행되는 디렉토리 (env·로그 파일 상대경로의 기준)
- `bots[]`: 감시할 봇마다 `{name, port, env, log}` — `env`에는 봇 API 인증(`WEB_USERNAME/PASSWORD`)이 든 파일
- `ping_url`: 0번에서 받은 healthchecks URL (없으면 줄 삭제 — heartbeat 생략)
- `stale_minutes`: 15분봉 봇이면 40 권장 (캔들 라벨 지연 감안)

**검증:**
```bash
python3 -m ohlryn_monitor.alerters.health_check --config config/health_check.myserver.json --dry-run
# → "[HH:MMZ] issues=0 (all OK)" 가 나오면 성공
```

## 3. pnl_watch 설정

```bash
cp config/pnl_watch.example.json config/pnl_watch.myserver.json
```

- `accounts[]`: 계좌마다 `{name, exchange, env, initial}` — `initial`은 수익률의 기준이 되는 초기 투자금
- `start_date`: 오늘 날짜로 (알림에 "N일째"로 표기됨)
- `alert_env`: 알림 보낼 텔레그램 토큰이 든 env (1번에서 만든 `.env_monitor` 경로)

**검증:**
```bash
python3 -m ohlryn_monitor.alerters.pnl_watch --config config/pnl_watch.myserver.json --dry-run
# → 계좌별 equity가 조회되고 미리보기 메시지가 출력되면 성공
```

## 4. cron 등록

```bash
crontab -e     # config/crontab.example 내용을 자기 경로로 수정해 추가
```

등록 후 첫 실행(최대 5분 내)에서:
- pnl_watch가 "(✨ 최초)" 요약을 1회 발송 — 이후엔 기록 갱신 때만
- healthchecks.io 대시보드의 체크가 초록(up)으로 바뀜

## 5. 이후 운영

정상일 땐 아무 알림도 오지 않습니다. 알림이 왔을 때의 해석과 대응은 [운영 런북](../docs/runbook.md)을 보세요.

**서버가 여러 대라면** — 서버마다 이 절차를 반복하되 config 파일명만 다르게 (`health_check.server2.json` 등). 코드는 동일합니다.
