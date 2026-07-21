# bot-ops

라이브 트레이딩 봇의 **운영 보조 도구** — 모니터링, 알림(텔레그램), watchdog. 백테스트/실행 엔진([`vector-backtester`](../vector-backtester))과 분리된 관측(observability) 레이어.

## 왜 별도 레포인가

| | vector-backtester (엔진) | bot-ops (관측/알림) |
|---|---|---|
| 변경 빈도 | 느림 (안정성 우선) | 잦음 (전략마다 새 알림) |
| 변경 리스크 | 높음 (Golden Snapshot·Rust parity) | 낮음 (알림 오류는 거래 영향 X) |
| 런타임 의존성 | 무거움 (pandas·Rust·polars) | **stdlib만** |
| 결합 | — | 봇 `/api/v1` · 거래소 public REST만 소비 |

**경계 규칙**: `vector_backtester`를 import하면 → 엔진 레포. 봇 HTTP(`/api/v1`)나 거래소 API로만 대화하면 → 여기(bot-ops).

## 설계 원칙 — 순수 로직 + I/O 분리 (ports & adapters)

- **순수 로직** (`bot_ops/signals.py` 등): 네트워크·파일 없이 입력→출력. 단위 테스트 대상.
- **I/O 어댑터** (`bot_ops/prices.py`, `notify.py`, `state.py`): 거래소 price fetch, 텔레그램 전송, JSON state.
- **오케스트레이션** (`bot_ops/alerters/*`): 순수 로직 + 어댑터를 조립한 `main()`.

## 구조

```
bot_ops/
  signals.py                # [순수] sma, sma_signal, sma_cross_signal, detect_change
  prices.py                 # [I/O] fetch_closes (binance public klines), to_binance_symbol
  notify.py                 # [I/O] parse_env, telegram_send
  state.py                  # [I/O] load_state, save_state (JSON)
  alerters/
    portfolio_signal_alert.py   # 포트폴리오 SMA 시그널 변경 알리미
watchdog/                   # 봇 프로세스 watchdog (마이그레이션 예정)
cron/server3.crontab        # 서버별 cron 선언 (버전관리)
tests/                      # 순수 로직 단위 테스트 (stdlib unittest)
```

## portfolio_signal_alert

여러 티커의 **SMA 레짐(가격 vs SMA)**을 주기적으로 계산해, 시그널이 **뒤집힐 때만** 텔레그램 알림. 상태는 JSON에 영속(첫 실행은 무음 초기화 — ATH 알림과 동일 패턴).

```bash
python3 -m bot_ops.alerters.portfolio_signal_alert \
  --env-file /home/ubuntu/vector-backtester/.env_binance_sub \
  --symbols BTC,ETH,SOL --interval 1d --sma 50 \
  --state-file /home/ubuntu/portfolio_signal_state.json --label Portfolio
# 미리보기(전송 안 함):
python3 -m bot_ops.alerters.portfolio_signal_alert --env-file ... --symbols BTC,ETH --dry-run
```

- 텔레그램 자격증명은 `--env-file`의 `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` (vector-backtester `.env_binance_sub` 재사용).
- 가격은 Binance USDⓈ-M **public** klines (인증·봇 불필요).

## 테스트

```bash
python3 -m unittest discover -s tests -v      # 의존성 0 (stdlib)
```

## 관련

- 운영 런북: `$OBSIDIAN/02_PARA/02_Areas/트레이딩 시스템/ctrend 라이브 봇 운영 런북 - server3.md`
- 엔진/전략: [`vector-backtester`](../vector-backtester)
