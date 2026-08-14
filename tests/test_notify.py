"""ohlryn_monitor.notify 견고화(재시도·백오프·IPv4 우선) 테스트 — 네트워크 없이 검증."""

import socket
import unittest
from urllib.error import URLError

from ohlryn_monitor import notify
from ohlryn_monitor.notify import prefer_ipv4, telegram_send


class _Flaky:
    """호출 N회까지 실패 후 성공하는 가짜 sender.

    연결 단계 오류는 urllib이 URLError로 래핑한다 — 재시도 대상의 실제 타입.
    """

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise URLError(TimeoutError("handshake timed out"))


class TestTelegramSendRetry(unittest.TestCase):
    def test_첫_시도_성공이면_재시도_안_함(self):
        sender = _Flaky(fail_times=0)
        sleeps = []
        telegram_send("t", "c", "m", _sender=sender, _sleep=sleeps.append)
        self.assertEqual(sender.calls, 1)
        self.assertEqual(sleeps, [])  # 성공 시 대기 없음

    def test_두_번_실패_후_성공(self):
        # blip 2회 → 3번째 성공. sender 3회 호출, sleep 2회(1s,2s)
        sender = _Flaky(fail_times=2)
        sleeps = []
        telegram_send("t", "c", "m", retries=3, backoff=1.0, _sender=sender, _sleep=sleeps.append)
        self.assertEqual(sender.calls, 3)
        self.assertEqual(sleeps, [1.0, 2.0])  # backoff * 2**attempt

    def test_모두_실패하면_마지막_예외_raise(self):
        sender = _Flaky(fail_times=99)
        sleeps = []
        with self.assertRaises(URLError):
            telegram_send("t", "c", "m", retries=3, _sender=sender, _sleep=sleeps.append)
        self.assertEqual(sender.calls, 3)  # retries만큼 시도
        self.assertEqual(len(sleeps), 2)  # 마지막 시도 뒤엔 대기 없음

    def test_응답_읽기_타임아웃은_재시도_안_함(self):
        # bare TimeoutError = 요청이 이미 텔레그램에 도달한 뒤 응답만 못 읽은 경우.
        # 재전송하면 같은 메시지가 중복 발송된다(2026-08-14 3연속 발송 사고) —
        # 전송된 것으로 간주하고 즉시 종료(재시도 0회, 예외 없음).
        calls = []

        def read_timeout():
            calls.append(1)
            raise TimeoutError("The read operation timed out")

        sleeps = []
        telegram_send("t", "c", "m", retries=3, _sender=read_timeout, _sleep=sleeps.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_비네트워크_예외는_재시도_안_함(self):
        # OSError가 아닌 예외(예: ValueError)는 즉시 전파
        def bad():
            raise ValueError("programming error")

        with self.assertRaises(ValueError):
            telegram_send("t", "c", "m", _sender=bad, _sleep=lambda _: None)


class TestPreferIpv4(unittest.TestCase):
    def test_블록_안에서는_AF_INET만_반환(self):
        with prefer_ipv4():
            infos = socket.getaddrinfo("localhost", 80)
        # localhost가 IPv6(::1)도 갖더라도 블록 안에선 IPv4만
        self.assertTrue(infos)
        self.assertTrue(all(fam == socket.AF_INET for fam, *_ in infos))

    def test_블록_종료_후_원복(self):
        before = socket.getaddrinfo
        with prefer_ipv4():
            self.assertIsNot(socket.getaddrinfo, before)  # 패치됨
        self.assertIs(socket.getaddrinfo, notify._orig_getaddrinfo)  # 복원됨

    def test_예외가_나도_원복(self):
        with self.assertRaises(RuntimeError), prefer_ipv4():
            raise RuntimeError("boom")
        self.assertIs(socket.getaddrinfo, notify._orig_getaddrinfo)


if __name__ == "__main__":
    unittest.main()
