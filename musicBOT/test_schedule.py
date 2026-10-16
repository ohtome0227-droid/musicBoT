import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path

from music_bot_2_GPT6 import JST, MusicBot, Track


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MusicBot(1, "mpv", "deno")
        self.bot.say = AsyncMock()
        self.bot.update_dashboard = AsyncMock()

    async def asyncTearDown(self):
        await self.bot.close()

    def at(self, hour, minute, day=16):
        return datetime(2026, 10, day, hour, minute, tzinfo=JST)

    async def test_prepare_interrupt_once_and_repeat_next_day(self):
        async def pending(track):
            await asyncio.Event().wait()
        self.bot.download_track = AsyncMock(side_effect=pending)
        self.bot.skip_current = AsyncMock()
        await self.bot.schedule_tick(self.at(23, 50))
        await asyncio.sleep(0)
        track = self.bot.schedule_track
        download = self.bot.downloads[id(track)]
        self.bot.refresh_prefetch()
        self.assertIs(self.bot.downloads[id(track)], download)
        await self.bot.schedule_tick(self.at(23, 55))
        await self.bot.schedule_tick(self.at(23, 55))
        self.bot.skip_current.assert_awaited_once()
        self.assertTrue(self.bot.schedule_pending)
        self.assertEqual(self.bot.volume, 40)
        self.bot.schedule_pending = False
        self.bot.finish_scheduled_playback()
        await self.bot.schedule_tick(self.at(8, 0, 17))
        self.assertFalse(self.bot.stopped)
        await self.bot.schedule_tick(self.at(23, 55, 17))
        self.assertTrue(self.bot.schedule_pending)

    async def test_finish_clears_queue_and_night_resume_is_denied(self):
        self.bot.queue.add(1, [Track(1, "user", "song", "url")])
        self.bot.schedule_active = True
        self.bot.finish_scheduled_playback()
        self.assertFalse(self.bot.queue.preview())
        self.assertTrue(self.bot.stopped)
        author = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(manage_guild=True))
        with patch("music_bot_2_GPT6.japan_now", return_value=self.at(2, 0)):
            await self.bot.handle_command(author, "/resume", self.bot.say)
        self.assertTrue(self.bot.stopped)
        self.assertEqual(self.bot.queue_generation, 1)

    async def test_scheduled_volume_can_be_changed_by_listener(self):
        self.bot.schedule_active = True
        self.bot.current = Track(0, "schedule", "song", "url")
        self.bot.volume = 40
        self.bot.send_ipc = AsyncMock()
        self.bot.refresh_prefetch = lambda: None
        author = SimpleNamespace(id=123, guild_permissions=SimpleNamespace(manage_guild=False))
        await self.bot.handle_command(author, "/volumeup", self.bot.say)
        self.assertEqual(self.bot.volume, 42)
        self.bot.send_ipc.assert_awaited_once_with(["set_property", "volume", 42])

    async def test_quiet_hours_and_late_start(self):
        self.assertTrue(self.bot.night_blocked(self.at(7, 59)))
        self.assertFalse(self.bot.night_blocked(self.at(8, 0)))
        self.assertFalse(self.bot.night_blocked(self.at(23, 54)))
        self.assertTrue(self.bot.night_blocked(self.at(23, 55)))
        await self.bot.schedule_tick(self.at(23, 56))
        self.assertFalse(self.bot.schedule_pending)
        self.assertTrue(self.bot.stopped)

    async def test_scheduled_playback_reuses_download_and_finishes_stopped(self):
        directory = Mock()
        directory.name = str(Path.cwd())
        audio = Path(directory.name) / "scheduled.webm"
        self.bot.download_track = AsyncMock(return_value=(directory, audio))
        self.bot.wait_until_ready = AsyncMock()
        self.bot.run_process = AsyncMock()
        await self.bot.schedule_tick(self.at(23, 50))
        await asyncio.sleep(0)
        await self.bot.schedule_tick(self.at(23, 55))
        with patch("music_bot_2_GPT6.japan_now", return_value=self.at(23, 55)):
            self.bot.worker = asyncio.create_task(self.bot.playback_loop())
            for _ in range(100):
                if self.bot.night_finished:
                    break
                await asyncio.sleep(0)
            self.assertTrue(self.bot.night_finished)
            self.assertTrue(self.bot.stopped)
            self.assertFalse(self.bot.schedule_active)
            self.bot.download_track.assert_awaited_once()
            self.bot.run_process.assert_awaited_once()
            args = self.bot.run_process.call_args.args[0]
            self.assertIn("--volume=40", args)
            self.assertEqual(args[-1], str(audio))
            await self.bot.close()
            await asyncio.sleep(0)
            directory.cleanup.assert_called_once()
