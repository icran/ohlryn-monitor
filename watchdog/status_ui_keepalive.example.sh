#!/usr/bin/env bash
# status UI keepalive — 반드시 스크립트 파일로 두고 cron이 이 파일을 호출한다.
# (cron 인라인에 pgrep+기동명령을 같이 쓰면 cron sh 자신의 cmdline에 모듈명이
#  들어가 pgrep이 영구 자기매칭 → keepalive가 아무것도 안 하는 함정)
pgrep -f "ohlryn_monitor[.]status_ui" >/dev/null && exit 0
cd /home/ubuntu/ohlryn-monitor || exit 1
setsid nohup /usr/bin/python3 -m ohlryn_monitor.status_ui --config config/status_ui.myserver.json >> /home/ubuntu/status_ui.log 2>&1 &
