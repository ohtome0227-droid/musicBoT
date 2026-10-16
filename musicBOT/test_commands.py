import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from music_bot_2_GPT6 import MusicBot, Track, FairQueue


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MusicBot(1, "mpv", "deno")
        self.bot.say = AsyncMock()
        self.bot.update_dashboard = AsyncMock()

    async def asyncTearDown(self):
        await self.bot.close()

    def message(self, text, channel=1, bot=False):
        return SimpleNamespace(content=text, author=SimpleNamespace(bot=bot),
                               guild=SimpleNamespace(id=10),
                               channel=SimpleNamespace(id=channel))

    async def test_ping_prefixes(self):
        for text in ("!ping", "/ping", "  !PING  "):
            with self.subTest(text=text):
                self.bot.say.reset_mock()
                await self.bot.on_message(self.message(text))
                self.bot.say.assert_awaited_once_with("接続OK！")

    async def test_faces(self):
        for command, expected in (("./nyaruratohotepu", "(」・ω・)」うー!(/・ω・)/にゃー!"),
                                  ("./umauma", "ｳｯｰｳｯｰｳﾏｳﾏ(ﾟ∀ﾟ)")):
            self.bot.say.reset_mock()
            await self.bot.on_message(self.message(command))
            self.bot.say.assert_awaited_once_with(expected)

    async def test_slash_faces_are_public(self):
        for name in ("nyaruratohotepu", "umauma", "ping"):
            interaction = SimpleNamespace(
                guild=SimpleNamespace(id=10), channel_id=1, user=SimpleNamespace(id=1),
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()))
            await self.bot.slash_command(interaction, name)
            ephemeral = name == "ping"
            interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=ephemeral)
            self.assertEqual(interaction.followup.send.call_args.kwargs["ephemeral"], ephemeral)

    async def test_personal_order(self):
        self.bot.refresh_prefetch = lambda: None
        tracks = [Track(1, "user", str(i), "url") for i in range(3)]
        other = Track(2, "other", "other", "url")
        self.bot.queue.add(1, tracks)
        self.bot.queue.add(2, [other])
        author = SimpleNamespace(id=1)
        await self.bot.handle_command(author, "/first 3", self.bot.say)
        self.assertEqual(list(self.bot.queue.queues[1]), [tracks[2], tracks[0], tracks[1]])
        with patch("music_bot_2_GPT6.random.shuffle", side_effect=lambda items: items.reverse()):
            await self.bot.handle_command(author, "/shuffle", self.bot.say)
        self.assertEqual(list(self.bot.queue.queues[1]), [tracks[1], tracks[0], tracks[2]])
        self.assertEqual(list(self.bot.queue.queues[2]), [other])
        self.assertEqual(list(self.bot.queue.turns), [1, 2])

    def test_failed_user_gets_next_turn_and_preview_agrees(self):
        queue = FairQueue()
        first, second = [Track(1, "user", str(i), "url") for i in range(2)]
        other = Track(2, "other", "other", "url")
        queue.add(1, [first, second])
        queue.add(2, [other])
        self.assertIs(queue.next(), first)
        queue.retry_user = 1
        self.assertEqual(queue.preview(), [second, other])
        self.assertIs(queue.next(), second)
        self.assertIs(queue.next(), other)

    async def test_ignores_other_channels_and_bots(self):
        await self.bot.on_message(self.message("!ping", channel=2))
        await self.bot.on_message(self.message("!ping", bot=True))
        self.bot.say.assert_not_awaited()

    async def test_play_preserves_url(self):
        self.bot.add_url = AsyncMock()
        message = self.message("!play https://youtu.be/abcdefghijk")
        await self.bot.on_message(message)
        self.bot.add_url.assert_awaited_once_with(
            message.author, "https://youtu.be/abcdefghijk", False, self.bot.say)
