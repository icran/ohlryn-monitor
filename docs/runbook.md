# 운영 런북

설정을 마친 뒤( [세팅 매뉴얼](../config/README.md) ) 일상 운영에서 필요한 것: **알림이 왔을 때 무엇을 하면 되는가**.

## 원칙

- **침묵 = 정상.** 정기 리포트는 없습니다. 알림이 없으면 볼 것도 없습니다.
- 같은 문제는 쿨다운(기본 1시간) 내 재알림되지 않고, 해소되면 `✅ 해소` 가 옵니다.

## 알림 해석표

### health_check (`[my-server]` 프리픽스)

| 알림 | 의미 | 대응 |
|------|------|------|
| `🚨 CRITICAL <봇>: N분+ 데이터 정체(좀비 의심)` | 프로세스는 살았는데 캔들 처리가 멈춤 — **watchdog이 못 잡는 유형** | 봇 재시작. 로그에서 마지막 활동 확인 (`tail -50 <봇로그>`) |
| `🚨 CRITICAL <봇>: API 응답 없음` | 봇 프로세스/웹서버 다운 | watchdog이 곧 재기동함 — 몇 분 내 `해소` 알림이 따라오는지 확인. 안 오면 수동 재시작 |
| `🚨 CRITICAL <봇>: 엔진 중지됨` | 봇은 떠 있는데 트레이딩 루프 미가동 | 봇 재시작 |
| `🚨 CRITICAL 메모리 잔여 N MB` | OOM 임박 | 큰 프로세스 확인(`ps aux --sort=-%mem \| head`), 필요시 봇 순차 재시작 |
| `🚨 WARNING 디스크/스왑/load` | 리소스 압박 진행 | 급하지 않음 — 여유 있을 때 로그/데이터 정리 |
| `🚨 WARNING <봇>: 신규 에러 로그 N건` | 5분 내 에러 급증 (rate-limit, API 오류 등) | `grep -iE "error" <봇로그> \| tail` 로 원인 확인 |
| `✅ 해소: <키> (지속 N분)` | 위 문제가 사라짐 | 조치 불필요 |

### healthchecks.io 발 알림

| 알림 | 의미 | 대응 |
|------|------|------|
| "check is DOWN" | **서버 통째 사망 / 네트워크 단절 / cron 정지** — health_check ping이 Grace 시간 이상 끊김 | 클라우드 콘솔에서 인스턴스 상태 확인 → 재부팅. SSH 되면 `crontab -l` 로 cron 살아있는지 확인 |
| "check is UP" | 복구됨 | 봇들이 전부 정상 재기동됐는지 health_check 다음 알림 여부로 확인 |

### pnl_watch (`[my-pnl]` 프리픽스)

| 알림 | 의미 |
|------|------|
| `🚀 ...` + `🚀 최고` 행 | 해당 계좌가 역대 최고 수익률 갱신 |
| `🙏 ...` + `🙏 최저` 행 | 역대 최저 갱신 — 손실 확대 중이면 전략/포지션 점검 계기 |
| `✨ 최초` | 기록 추적 시작 (설정 직후 1회) |

## 자주 하는 작업

**봇 하나 수동 재시작** (watchdog을 쓰는 경우):
```bash
kill <봇PID>          # watchdog cron이 다음 주기(≤5분)에 자동 재기동 + 알림
# 즉시 올리려면: bash watchdog/<봇>_watchdog.sh
```

**모니터링 자체 점검**:
```bash
python3 -m ohlryn_monitor.alerters.health_check --config <config> --dry-run   # 현재 판정 미리보기
tail -5 ~/health_check.log                                                     # cron 실행 이력
```

**임계값 조정** — config JSON 수정만으로 즉시 반영 (cron이 매번 새로 읽음, 재시작 불필요).

**계좌 추가/초기금 변경** (pnl_watch) — `accounts[]` 수정. 기록을 리셋하려면 상태 파일에서 해당 계좌 키 삭제:
```bash
python3 -c "import json;p='<state_file>';d=json.load(open(p));d['records'].pop('<계좌명>',None);json.dump(d,open(p,'w'))"
```

## 트러블슈팅

| 증상 | 원인 후보 | 확인 |
|------|----------|------|
| 텔레그램이 안 옴 | 토큰/chat_id 오타, 봇과 채팅 Start 안 함 | `--dry-run` 출력은 정상인지 → 정상이면 env 문제 |
| 좀비 오탐 (봇 정상인데 stale 알림) | `stale_minutes`가 봇 타임프레임 대비 짧음 | 캔들 라벨이 오픈시각이면 임계 ≥ 타임프레임×2 + 버퍼 |
| healthchecks DOWN인데 서버 정상 | cron 정지, ping_url 오타, outbound 차단 | `crontab -l`, `tail ~/health_check.log` |
| equity가 예상과 다름 (binance) | 스테이블 교차 자산 | 자산별 합산이 기본 — 그래도 다르면 선물지갑 외 자산(현물 등)은 집계 대상 아님 |
| 첫 실행에서 과거 로그 에러 폭탄 | — | 그런 일 없음: 로그 스캔은 최초 실행 시 과거를 소급하지 않음 |

## 확장

- **서버 추가**: config 파일 하나 + cron 등록이면 끝 (코드 공유)
- **새 알리미 작성**: `alerters/`의 기존 파일이 패턴 — 순수 로직은 별도 모듈로 분리해 테스트, I/O는 `notify`/`state` 재사용, `--dry-run` 필수 제공
