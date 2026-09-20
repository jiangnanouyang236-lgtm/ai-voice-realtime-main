import asyncio
import unittest

from gateway.workflow_playback_barrier import (
    PLAYBACK_CANCELLED,
    PLAYBACK_COMPLETE,
    PLAYBACK_INTERRUPTED,
    PLAYBACK_TIMEOUT,
    PlaybackBarrierRegistry,
)


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class PlaybackBarrierRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_matching_complete_report_releases_waiter(self):
        registry = PlaybackBarrierRegistry()
        waiter = registry.prepare("s1", "r1", "p1")

        matched = registry.record_report(
            session_id="s1",
            round_id="r1",
            playback_id="p1",
            report_type=PLAYBACK_COMPLETE,
        )
        result = await waiter.wait(0.1)

        self.assertTrue(matched)
        self.assertTrue(result.completed)
        self.assertEqual({"active_waiters": 0, "recent_reports": 1}, registry.snapshot())

    async def test_early_report_is_consumed_by_later_prepare(self):
        registry = PlaybackBarrierRegistry()

        matched = registry.record_report(
            session_id="s1",
            round_id="r1",
            playback_id="p1",
            report_type=PLAYBACK_COMPLETE,
        )
        waiter = registry.prepare("s1", "r1", "p1")
        result = await waiter.wait(0.1)

        self.assertFalse(matched)
        self.assertEqual(PLAYBACK_COMPLETE, result.status)
        self.assertEqual(1, registry.snapshot()["recent_reports"])

    async def test_mismatched_old_report_does_not_release_waiter(self):
        registry = PlaybackBarrierRegistry()
        waiter = registry.prepare("s1", "r2", "p2")

        registry.record_report(
            session_id="s1",
            round_id="r1",
            playback_id="p1",
            report_type=PLAYBACK_COMPLETE,
        )
        result = await waiter.wait(0.01)

        self.assertEqual(PLAYBACK_TIMEOUT, result.status)
        self.assertEqual(0, registry.snapshot()["active_waiters"])

    async def test_interrupted_report_cancels_barrier(self):
        registry = PlaybackBarrierRegistry()
        waiter = registry.prepare("s1", "r1", "p1")

        registry.record_report(
            session_id="s1",
            round_id="r1",
            playback_id="p1",
            report_type=PLAYBACK_INTERRUPTED,
            reason="wake_interrupt",
        )
        result = await waiter.wait(0.1)

        self.assertEqual(PLAYBACK_INTERRUPTED, result.status)
        self.assertEqual("wake_interrupt", result.reason)
        self.assertFalse(result.completed)

    async def test_session_cancel_releases_all_matching_waiters(self):
        registry = PlaybackBarrierRegistry()
        first = registry.prepare("s1", "r1", "p1")
        second = registry.prepare("s1", "r2", "p2")
        other = registry.prepare("s2", "r1", "p1")

        cancelled = registry.cancel_session("s1", reason="client_interrupt")
        first_result, second_result = await asyncio.gather(first.wait(0.1), second.wait(0.1))

        self.assertEqual(2, cancelled)
        self.assertEqual(PLAYBACK_CANCELLED, first_result.status)
        self.assertEqual(PLAYBACK_CANCELLED, second_result.status)
        self.assertEqual(PLAYBACK_TIMEOUT, (await other.wait(0.01)).status)

    async def test_duplicate_prepare_reuses_same_waiter(self):
        registry = PlaybackBarrierRegistry()
        first = registry.prepare("s1", "r1", "p1")
        second = registry.prepare("s1", "r1", "p1")

        registry.record_report(
            session_id="s1",
            round_id="r1",
            playback_id="p1",
            report_type=PLAYBACK_COMPLETE,
        )
        results = await asyncio.gather(first.wait(0.1), second.wait(0.1))

        self.assertTrue(all(result.completed for result in results))

        late_waiter = registry.prepare("s1", "r1", "p1")
        self.assertTrue((await late_waiter.wait(0.1)).completed)

    async def test_first_terminal_report_wins(self):
        registry = PlaybackBarrierRegistry()
        waiter = registry.prepare("s1", "r1", "p1")
        self.assertTrue(
            registry.record_report(
                session_id="s1",
                round_id="r1",
                playback_id="p1",
                report_type=PLAYBACK_INTERRUPTED,
                reason="wake_interrupt",
            )
        )
        self.assertFalse(
            registry.record_report(
                session_id="s1",
                round_id="r1",
                playback_id="p1",
                report_type=PLAYBACK_COMPLETE,
            )
        )

        result = await waiter.wait(0.1)
        self.assertEqual(PLAYBACK_INTERRUPTED, result.status)

    async def test_invalid_report_type_is_ignored(self):
        registry = PlaybackBarrierRegistry()
        waiter = registry.prepare("s1", "r1", "p1")

        matched = registry.record_report(
            session_id="s1",
            round_id="r1",
            playback_id="p1",
            report_type="playback_started",
        )

        self.assertFalse(matched)
        self.assertEqual(PLAYBACK_TIMEOUT, (await waiter.wait(0.01)).status)

    async def test_recent_report_cache_expires_and_is_bounded(self):
        clock = FakeClock()
        registry = PlaybackBarrierRegistry(cache_ttl_sec=5.0, cache_max_entries=2, clock=clock)
        for index in range(3):
            registry.record_report(
                session_id="s1",
                round_id=f"r{index}",
                playback_id=f"p{index}",
                report_type=PLAYBACK_COMPLETE,
            )
        self.assertEqual(2, registry.snapshot()["recent_reports"])

        clock.value += 6.0
        self.assertEqual(0, registry.snapshot()["recent_reports"])

    async def test_empty_barrier_ids_are_rejected(self):
        registry = PlaybackBarrierRegistry()
        with self.assertRaises(ValueError):
            registry.prepare("s1", "", "p1")


if __name__ == "__main__":
    unittest.main()
