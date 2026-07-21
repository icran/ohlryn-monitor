# ohlryn-monitor

라이브 트레이딩 봇을 위한 **운영 모니터링·알림 도구 모음**. 트레이딩 엔진과 완전히 분리된 사이드카(관측 레이어)로 동작하며, **런타임 의존성이 표준 라이브러리뿐**이라 서버에 복사 + cron 등록만으로 붙습니다.

> 철학: **침묵 = 정상.** 문제가 생기거나 기록이 갱신될 때만 텔레그램이 웁니다. 운영자가 대시보드를 들여다보지 않아도 되는 상태가 목표입니다.

## 제공 알리미

| 알리미 | 주기(권장) | 하는 일 |
|--------|-----------|---------|
| **health_check** | */5분 | 봇 좀비 감지(프로세스는 살았지만 데이터 처리 정체) · 시스템 리소스(디스크/메모리/스왑/load) · 로그 에러 급증 → 텔레그램. 정상이면 [healthchecks.io](https://healthchecks.io) heartbeat ping (침묵 = 서버 사망 알림) |
| **pnl_watch** | 매시 | 계좌별 초기 투자금 대비 equity 수익률 추적. **역대 최저(🙏)/최고(🚀) 기록 갱신 때만** 전 계좌 요약 발송 |
| **portfolio_signal_alert** | 일 1회 등 | 티커별 가격 vs SMA 레짐이 **뒤집힐 때만** 알림 |

지원 거래소(equity 조회): Binance USDⓈ-M, Bybit UNIFIED. 봇 상태 조회는 봇의 HTTP API(`/api/v1`)를 사용합니다.

## 3계층 모니터링 (권장 구성)

```
계층 1  watchdog (cron)        → 봇 프로세스 죽음 감지 + 자동 재기동     watchdog/ 참조
계층 2  health_check (cron)    → 좀비·리소스·로그 → 직접 텔레그램
계층 3  healthchecks.io (외부) → heartbeat 침묵 = 서버 통째 사망 → 텔레그램
```

서버 한 대로 운영해도 "서버 자체가 죽는" 맹점까지 커버됩니다 — 계층 3은 무료 외부 서비스의 dead-man's switch를 이용하며, 서버는 outbound ping만 보내므로 포트 개방이 필요 없습니다.

## Quickstart

**모든 설정은 [`config/`](config/) 폴더에서 시작합니다** — 예시 파일 복사 → 값 입력 → dry-run → cron. 단계별 안내는 **[config/README.md (세팅 매뉴얼)](config/README.md)**.

```bash
git clone <this-repo> && cd ohlryn-monitor

cp config/env.example              .env_monitor              # 시크릿 (텔레그램/거래소 키)
cp config/health_check.example.json health_check.myserver.json
cp config/pnl_watch.example.json    pnl_watch.myserver.json
# → 두 json과 .env_monitor를 자기 값으로 수정 (config/README.md 참조)

# 미리보기 (전송·저장 없음)
python3 -m ohlryn_monitor.alerters.health_check --config health_check.myserver.json --dry-run
python3 -m ohlryn_monitor.alerters.pnl_watch    --config pnl_watch.myserver.json    --dry-run

# cron 등록 (config/crontab.example 참조)
```

시크릿은 config JSON에 직접 넣지 않고 **env 파일 경로만** 참조합니다. `.env*`·`*.myserver.json`·`*.server*.json`·상태파일은 `.gitignore`에 있어 커밋되지 않습니다. 서버가 여러 대면 서버마다 config 하나씩 만들면 됩니다 (`--config`로 주입되므로 파일명은 자유).

## 설정 레퍼런스

**health_check** — [`config/health_check.example.json`](config/health_check.example.json)

| 키 | 설명 |
|----|------|
| `repo` | 봇 실행 디렉토리 (env·로그 파일의 기준 경로) |
| `bots[]` | `{name, port, env, log}` — 봇별 API 포트·인증 env·로그 파일 |
| `stale_minutes` | 좀비 판정 임계. **주의**: 봇의 `last_updated`가 캔들 *오픈시각* 라벨이면 타임프레임×2의 자연 지연이 있음 (15m봉 → 40 권장) |
| `ping_url` | healthchecks.io ping URL (생략 시 heartbeat 없음). CRITICAL 시 `/fail`로 이중 발화 |
| `cooldown_sec` | 같은 문제 재알림 억제 (기본 3600초). 해소 시 회복 알림 1회 |
| `system_limits` | 디스크%·메모리MB·스왑MB·load 임계 |

**pnl_watch** — [`config/pnl_watch.example.json`](config/pnl_watch.example.json)

| 키 | 설명 |
|----|------|
| `accounts[]` | `{name, exchange(binance\|bybit), env, initial}` — initial = 기준 투자금 |
| `start_date` | 기록 시작일 — 알림 헤더에 "N일째" 표기 |
| `alert_env` | 텔레그램 토큰 출처 env (절대경로 가능 — 알림용 봇을 분리할 때) |

equity는 Binance의 경우 **자산별(USDT/USDC 등) marginBalance 합산**입니다 — 톱레벨 합계는 USDT만 집계해 스테이블 교차 거래분이 누락되기 때문입니다.

## 설계 원칙

- **경계**: 트레이딩 엔진을 import하지 않습니다. 봇 HTTP API와 거래소 REST만 소비 → 이 도구가 죽어도 거래는 무사하고, 어떤 봇 프레임워크와도 사이드카로 결합할 수 있습니다.
- **순수 로직 / I/O 분리**: 판정·기록 로직(`health.py`·`pnl.py`·`signals.py`)은 네트워크 없이 단위 테스트되고, I/O 어댑터(`notify`·`state`·`exchanges`·`prices`)와 알리미(`alerters/*`)가 이를 조립합니다.
- **stdlib only**: 런타임 의존성 0 — 파이썬 3.10+만 있으면 됩니다.
- 텔레그램 전송은 재시도 3회 + 지수 백오프 + IPv4 우선(일부 환경의 IPv6 경로 불안정 회피).

## 구조

```
config/         # ★ 설정 시작점 — 세팅 매뉴얼 + 모든 예시 파일 (env/json/crontab)
ohlryn_monitor/
  health.py     # [순수] 좀비/리소스/로그 판정, 쿨다운·회복 알림 계획
  pnl.py        # [순수] 수익률 기록(worst/best) 추적, 메시지 조립
  signals.py    # [순수] SMA 레짐 시그널
  notify.py     # [I/O] env 파싱, 텔레그램 전송(재시도)
  state.py      # [I/O] JSON 상태 원자적 영속
  exchanges.py  # [I/O] Binance/Bybit equity 조회
  prices.py     # [I/O] Binance public klines
  alerters/     # 조립된 main() — health_check, pnl_watch, portfolio_signal_alert
watchdog/       # 봇 프로세스 watchdog 패턴 + 예시 스크립트
docs/runbook.md # 운영 런북 (배포·알림 해석·트러블슈팅)
tests/          # 순수 로직 단위 테스트
```

## 테스트

```bash
pip install pytest   # dev 전용 (런타임은 stdlib only)
python3 -m pytest tests/ -q
```

## 문서

- **[세팅 매뉴얼](config/README.md)** — 처음 설정하는 순서 그대로
- **[운영 런북](docs/runbook.md)** — 배포 절차, 알림 해석표, 트러블슈팅
- [watchdog 패턴](watchdog/README.md) — 프로세스 감시·자동 재기동
