"""DiscordからYouTube・ニコニコ動画を予約し、PCのスピーカーで再生するBot。
必要: discord.py, yt-dlp[default], mpv, Deno, FFmpeg
実行: py -3.13 music_bot.py
予約は起動中のみ保持。1投稿1URL、リスト先頭50件、1人の待ち曲100件。
音声を一時取得して再生し、終了・スキップ時に削除します。
"""
import asyncio
from collections import deque
from dataclasses import dataclass
import getpass
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit
import uuid

import discord
from discord import app_commands


QUEUE_CHANNEL_ID = 1559531557348450395
QUEUE_STATE = Path(__file__).with_name("queue_message.json")
HISTORY_STATE = Path(__file__).with_name("play_history.sqlite3")
LOUDNESS_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=50:dual_mono=true"

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
NICO_ID = re.compile(r"^(?:(?:sm|nm|so)\d+|\d+)$")


def normalize_url(url, force_playlist=False):
    """許可したホストから動画/リストIDのみ取り出しURLを再構築。"""
    p = urlsplit(url.strip().strip("<>"))
    if p.scheme not in ("http", "https") or p.hostname not in {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
        "nicovideo.jp", "www.nicovideo.jp", "sp.nicovideo.jp", "nico.ms"
    } or p.username or p.password or p.port not in (None, 80, 443):
        raise ValueError("YouTube・ニコニコ動画の動画URLか再生リストURLを送ってください。")
    if p.hostname in {"nicovideo.jp", "www.nicovideo.jp", "sp.nicovideo.jp", "nico.ms"}:
        path = p.path.strip("/")
        video = path if p.hostname == "nico.ms" else path.removeprefix("watch/") if path.startswith("watch/") else ""
        if NICO_ID.fullmatch(video):
            if force_playlist:
                raise ValueError("ニコニコ動画のマイリストまたはシリーズURLを送ってください。")
            return "https://www.nicovideo.jp/watch/" + video, False
        listing = re.fullmatch(r"(?:user/\d+/)?mylist/(\d+)|series/(\d+)", path)
        if listing and p.hostname != "nico.ms":
            kind = "mylist" if listing[1] else "series"
            return f"https://www.nicovideo.jp/{kind}/{listing[1] or listing[2]}", True
        raise ValueError("ニコニコ動画の動画・マイリスト・シリーズURLを送ってください。")
    query = parse_qs(p.query)
    video = query.get("v", [""])[0]
    parts = p.path.strip("/").split("/")
    if p.hostname == "youtu.be":
        video = parts[0]
    elif parts[0] in ("shorts", "live", "embed") and len(parts) > 1:
        video = parts[1]
    playlist = query.get("list", [""])[0]
    if force_playlist or not video:
        if playlist and re.fullmatch(r"[A-Za-z0-9_-]+", playlist):
            return "https://www.youtube.com/playlist?list=" + playlist, True
        raise ValueError("再生リストのlist=が含まれるURLを送ってください。")
    if VIDEO_ID.fullmatch(video):
        return "https://www.youtube.com/watch?v=" + video, False
    raise ValueError("動画IDを読み取れません。動画の共有URLを送ってください。")


@dataclass
class Track:
    user_id: int
    user_name: str
    title: str
    url: str


@dataclass
class Preparation:
    track: Track
    directory: tempfile.TemporaryDirectory
    skip: asyncio.Event
    task: asyncio.Task


def search_key(text):
    return unicodedata.normalize("NFKC", text).casefold()


class PlayHistory:
    """受付チャンネルごとに動画を重複なく保存する。音声は保存しない。"""
    def __init__(self, path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS tracks (
                channel_id INTEGER NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                last_played INTEGER NOT NULL, PRIMARY KEY (channel_id, url))""")
        except sqlite3.Error:
            db.close()
            raise
        return db

    def remember(self, channel_id, track):
        db = self.connect()
        try:
            with db:
                db.execute("""INSERT INTO tracks VALUES (?, ?, ?, ?)
                    ON CONFLICT(channel_id, url) DO UPDATE SET
                    title=excluded.title, last_played=excluded.last_played""",
                    (channel_id, track.url, track.title, time.time_ns()))
        finally:
            db.close()

    def search(self, channel_id, query):
        key = search_key(query.strip())
        if not key:
            return []
        db = self.connect()
        try:
            rows = db.execute("SELECT title, url FROM tracks WHERE channel_id=? ORDER BY last_played DESC, url",
                              (channel_id,)).fetchall()
            return [(title, url) for title, url in rows if key in search_key(title)]
        finally:
            db.close()


class HistoryView(discord.ui.View):
    def __init__(self, bot, author_id, results):
        super().__init__(timeout=180)
        self.bot, self.author_id, self.results = bot, author_id, results
        self.page = 0
        self.used = False
        self.refresh()

    def refresh(self):
        start = self.page * 25
        self.choose.options = [discord.SelectOption(label=title[:100], value=str(i),
            description=url[:100]) for i, (title, url) in enumerate(self.results[start:start + 25], start)]
        self.choose.placeholder = f"再生する曲を選択（{self.page + 1}/{(len(self.results) + 24) // 25}ページ）"
        self.previous.disabled = self.page == 0
        self.next_page.disabled = start + 25 >= len(self.results)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("検索した本人が選択してください。", ephemeral=True)
            return False
        if interaction.guild is None or interaction.channel_id != self.bot.channel_id:
            await interaction.response.send_message("音楽受付チャンネルで選択してください。", ephemeral=True)
            return False
        if self.used or self.is_finished():
            await interaction.response.send_message("この候補は選択済み、または期限切れです。もう一度検索してください。", ephemeral=True)
            return False
        return True

    async def consume(self, interaction):
        # 最初のawaitより前に使用済みにし、連打による重複予約を防ぐ。
        if self.used:
            await interaction.response.send_message("選択済みです。もう一度検索してください。", ephemeral=True)
            return False
        self.used = True
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)
        return True

    @discord.ui.select()
    async def choose(self, interaction: discord.Interaction, select: discord.ui.Select):
        url = self.results[int(select.values[0])][1]
        if not await self.consume(interaction):
            return
        async def reply(text):
            await interaction.followup.send(embed=self.bot.embed(text), ephemeral=True)
        await self.bot.handle_command(interaction.user, "!play " + url, reply)

    @discord.ui.button(label="検索結果をすべて追加", style=discord.ButtonStyle.primary)
    async def add_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.consume(interaction):
            return
        async def reply(text):
            await interaction.followup.send(embed=self.bot.embed(text), ephemeral=True)
        await self.bot.add_history_results(interaction.user, self.results, reply)

    @discord.ui.button(label="前のページ", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self.refresh()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="次のページ", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min((len(self.results) - 1) // 25, self.page + 1)
        self.refresh()
        await interaction.response.edit_message(view=self)

    async def on_error(self, interaction, error, item):
        await self.bot.on_command_error(interaction, error)


class FairQueue:
    def __init__(self):
        self.queues = {}
        self.turns = deque()
        self.last_user = None

    def add(self, user_id, tracks):
        if not tracks:
            return
        if user_id not in self.queues:
            self.queues[user_id] = deque()
            self.turns.append(user_id)
        self.queues[user_id].extend(tracks)

    def next(self):
        if not self.turns:
            return None
        if len(self.turns) > 1 and self.turns[0] == self.last_user:
            self.turns.rotate(-1)
        user = self.turns.popleft()
        track = self.queues[user].popleft()
        if self.queues[user]:
            self.turns.append(user)
        else:
            del self.queues[user]
        self.last_user = user
        return track

    def clear_user(self, user_id):
        count = len(self.queues.pop(user_id, []))
        self.turns = deque(u for u in self.turns if u != user_id)
        return count

    def preview(self, count=10):
        other = FairQueue()
        other.queues = {u: deque(q) for u, q in self.queues.items()}
        other.turns = deque(self.turns)
        other.last_user = self.last_user
        result = []
        for _ in range(count):
            t = other.next()
            if t is None:
                break
            result.append(t)
        return result


class Skipped(Exception):
    pass


class ProcessError(RuntimeError):
    """Discordにも通知する外部プログラムの実行エラー。"""


def track_url(entry, source_url):
    """抽出データを信頼せず、元サービスの動画IDからURLを構築する。"""
    video_id = str(entry.get("id") or "")
    if urlsplit(source_url).hostname == "www.nicovideo.jp":
        if NICO_ID.fullmatch(video_id):
            return "https://www.nicovideo.jp/watch/" + video_id
    elif VIDEO_ID.fullmatch(video_id):
        return "https://www.youtube.com/watch?v=" + video_id
    return None


def executable(name):
    root = Path(__file__).resolve().parent
    filename = name + (".exe" if os.name == "nt" else "")
    for path in (root / filename, root / ".tools" / name / "bin" / filename,
                 root / ".tools" / name / filename):
        if path.is_file():
            return str(path)
    return shutil.which(name)


def measured_loudness_filter(output):
    """FFmpegの解析ログから、2回目の処理用の測定値を取り出す。"""
    try:
        blocks = re.findall(r"\{[^{}]*\}", output.decode("utf-8", errors="replace"))
        stats = json.loads(blocks[-1])
        loudness, peak = float(stats["input_i"]), float(stats["input_tp"])
        if loudness == -math.inf and peak == -math.inf:
            return None  # 無音は増幅しない。
        fields = {"measured_I": "input_i", "measured_TP": "input_tp",
                  "measured_LRA": "input_lra", "measured_thresh": "input_thresh",
                  "offset": "target_offset"}
        values = {option: float(stats[key]) for option, key in fields.items()}
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("non-finite loudness")
        return LOUDNESS_FILTER + ":linear=true:" + ":".join(
            f"{option}={value}" for option, value in values.items())
    except (IndexError, KeyError, TypeError, ValueError) as e:
        raise RuntimeError("音量解析の結果を読み取れませんでした。") from e


class MusicBot(discord.Client):
    def __init__(self, channel_id, mpv, deno, ffmpeg=None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.channel_id, self.mpv, self.deno = channel_id, mpv, deno
        self.ffmpeg = ffmpeg or executable("ffmpeg")
        self.queue = FairQueue()
        self.history = PlayHistory(HISTORY_STATE)
        self.wake = asyncio.Event()
        self.add_lock = asyncio.Lock()
        self.current = None
        self.skip_event = None
        self.phase = "待機中"
        self.stopped = False
        self.worker = None
        self.processes = set()
        self.ipc_path = None
        self.prefetch = None
        self.prefetch_lock = asyncio.Lock()
        self.tree = app_commands.CommandTree(self)
        self.dashboard_lock = asyncio.Lock()
        self.dashboard_message = None
        self.dashboard_payload = None
        self.dashboard_worker = None
        self.last_errors = {}
        self.register_commands()
        self.tree.on_error = self.on_command_error

    async def setup_hook(self):
        channel = await self.fetch_channel(self.channel_id)
        queue_channel = await self.fetch_channel(QUEUE_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel) or not isinstance(queue_channel, discord.TextChannel):
            raise ValueError("受付・キュー表示先にはテキストチャンネルを指定してください。")
        if channel.guild.id != queue_channel.guild.id:
            raise ValueError("受付・キュー表示先は同じサーバーにしてください。")
        guild = discord.Object(id=channel.guild.id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.worker = asyncio.create_task(self.playback_loop())
        self.dashboard_worker = asyncio.create_task(self.dashboard_loop())

    async def close(self):
        if self.dashboard_worker:
            self.dashboard_worker.cancel()
            await asyncio.gather(self.dashboard_worker, return_exceptions=True)
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        async with self.prefetch_lock:
            preparation, self.prefetch = self.prefetch, None
            await self.dispose_preparation(preparation)
        for process in list(self.processes):
            await self.kill(process)
        await super().close()

    async def on_ready(self):
        print(f"接続成功: {self.user}\n指定チャンネルにYouTube・ニコニコ動画のURLを送ってください。終了はCtrl+C。")

    async def say(self, text, error=False):
        try:
            channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
            if channel:
                embed = self.embed(text)
                if error:
                    embed.title = "Music Bot エラー"
                    embed.colour = discord.Colour.red()
                await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"Discordへ返信できません: {type(e).__name__}")

    async def report_error(self, context, error, repeat=False):
        detail = f"{type(error).__name__}: {error}"
        token = getattr(self.http, "token", None)
        if token:
            detail = detail.replace(token, "[トークン非表示]")
        # 外部ツールが出力する署名付きURL・認証情報を共有しない。
        detail = re.sub(r"https?://[^\s<>]+", lambda match: match[0].split("?")[0].split("#")[0], detail)
        detail = re.sub(r"(?i)(authorization|cookie|token|password)(\s*[:=]\s*)[^\s]+",
                        r"\1\2[非表示]", detail)
        detail = re.sub(r"\x1b\[[0-9;]*m", "", detail)
        key = (context, detail)
        now = asyncio.get_running_loop().time()
        if repeat and now - self.last_errors.get(key, -300) < 300:
            return
        self.last_errors = {k: t for k, t in self.last_errors.items() if now - t < 300}
        self.last_errors[key] = now
        print(f"{context}: {detail}")
        await self.say(f"{context[:300]}\n{discord.utils.escape_markdown(detail[-3000:])}", error=True)

    async def on_error(self, event_method, *args, **kwargs):
        await self.report_error(f"イベント処理失敗: {event_method}", sys.exception())

    async def on_command_error(self, interaction, error):
        await self.report_error("スラッシュコマンド処理失敗", getattr(error, "original", error))
        try:
            embed = self.embed("処理に失敗しました。詳細を音楽受付チャンネルに送信しました。")
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass

    @staticmethod
    def embed(text):
        return discord.Embed(title="Music Bot", description=text[:4000], colour=discord.Colour.blurple())

    def queue_embed(self):
        count = sum(len(q) for q in self.queue.queues.values())
        state = "停止中" if self.stopped else self.phase
        embed = discord.Embed(title="ミュージックキュー", colour=discord.Colour.blurple())
        embed.add_field(name="状態", value=state, inline=False)
        current = self.current
        embed.add_field(name="現在の曲", value=(
            f"[{discord.utils.escape_markdown(current.title[:180])}]({current.url})\n"
            f"投稿者: {discord.utils.escape_markdown(current.user_name[:80])}"
        ) if current else "再生していません。", inline=False)
        # Embedの上限を超える場合は残り件数を表示する。
        lines, shown = [], 0
        for i, track in enumerate(self.queue.preview(count), 1):
            line = (f"{i}. [{discord.utils.escape_markdown(track.title[:60])}]({track.url})"
                    f" — {discord.utils.escape_markdown(track.user_name[:24])}")
            if sum(len(x) + 1 for x in lines) + len(line) > 3900:
                break
            lines.append(line)
            shown += 1
        embed.description = "\n".join(lines) or "予約はありません。"
        embed.set_footer(text=f"待ち曲: {count}曲 | 表示: {shown}曲 | ユーザー交互の予測順" +
                              (f" | ほか{count - shown}曲" if count > shown else ""))
        return embed

    async def update_dashboard(self):
        async with self.dashboard_lock:
            try:
                channel = self.get_channel(QUEUE_CHANNEL_ID) or await self.fetch_channel(QUEUE_CHANNEL_ID)
                if self.dashboard_message is None:
                    try:
                        saved = json.loads(QUEUE_STATE.read_text(encoding="utf-8"))
                        if saved.get("channel_id") == QUEUE_CHANNEL_ID:
                            message = await channel.fetch_message(int(saved["message_id"]))
                            if message.author.id == self.user.id:
                                self.dashboard_message = message
                    except (OSError, ValueError, KeyError, discord.NotFound):
                        pass
                embed = self.queue_embed()
                payload = embed.to_dict()
                if self.dashboard_message is not None and payload == self.dashboard_payload:
                    # 定期的に存在を確認し、手動削除からも復旧する。
                    await channel.fetch_message(self.dashboard_message.id)
                    return
                if self.dashboard_message is not None:
                    try:
                        await self.dashboard_message.edit(content=None, embed=embed)
                    except discord.NotFound:
                        self.dashboard_message = None
                if self.dashboard_message is None:
                    self.dashboard_message = await channel.send(embed=embed)
                    try:
                        QUEUE_STATE.write_text(json.dumps({"channel_id": QUEUE_CHANNEL_ID,
                            "message_id": self.dashboard_message.id}), encoding="utf-8")
                    except OSError as e:
                        await self.report_error("キューメッセージIDの保存失敗", e, repeat=True)
                self.dashboard_payload = payload
            except discord.NotFound:
                self.dashboard_message = None
                self.dashboard_payload = None
            except discord.HTTPException as e:
                await self.report_error("キュー表示更新失敗", e, repeat=True)

    async def dashboard_loop(self):
        await self.wait_until_ready()
        while True:
            await self.update_dashboard()
            await asyncio.sleep(15)

    def register_commands(self):
        descriptions = {"ping": "接続を確認", "help": "使い方を表示", "queue": "キューを表示",
            "now": "現在の曲を表示", "skip": "現在の曲をスキップ", "pause": "一時停止",
            "resume": "再生を再開", "clear": "自分の待ち曲を削除", "stop": "予約を残して停止",
            "play": "動画URLで追加、またはタイトルの一部で履歴を検索", "playlist": "再生リスト・マイリスト・シリーズを追加"}
        descriptions["search"] = "再生履歴をタイトルの一部で検索して選択"
        def callback_for(name):
            if name == "search":
                async def with_title(interaction: discord.Interaction, title: str):
                    await self.slash_command(interaction, name, title)
                return with_title
            if name in ("play", "playlist"):
                async def with_url(interaction: discord.Interaction, url: str):
                    await self.slash_command(interaction, name, url)
                return with_url
            async def without_url(interaction: discord.Interaction):
                await self.slash_command(interaction, name)
            return without_url
        for name, description in descriptions.items():
            self.tree.add_command(app_commands.Command(name=name, description=description,
                                                       callback=callback_for(name)))

    async def slash_command(self, interaction, name, url=None):
        if interaction.guild is None or interaction.channel_id != self.channel_id:
            await interaction.response.send_message(embed=self.embed(
                f"音楽受付チャンネル <#{self.channel_id}> で使ってください。"), ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        async def reply(text, **kwargs):
            await interaction.followup.send(embed=self.embed(text), ephemeral=True, **kwargs)
        await self.handle_command(interaction.user, f"!{name}" + (f" {url}" if url else ""), reply)

    async def search_history(self, author, query, reply):
        if not query.strip():
            await reply("/search の title に動画タイトルの一部を入力してください。例: !search 曲名")
            return
        results = self.history.search(self.channel_id, query)
        if not results:
            await reply("一致する再生履歴がありません。初めての曲はURLで追加してください。")
            return
        await reply(f"再生履歴に{len(results)}件見つかりました。下の候補から曲を選ぶか、すべて追加できます。\n"
                    "すべて追加は全ページが対象です（自分の待ち曲100曲まで）。\n"
                    "新しく再生した順です。選択期限は3分です。",
                    view=HistoryView(self, author.id, results))

    async def add_history_results(self, author, results, reply):
        # 保存済みの動画情報を使い、一括追加のための外部取得は行わない。
        # 公開状態や音声の取得可否は、通常と同じ再生処理で確認する。
        async with self.add_lock:
            room = max(0, 100 - len(self.queue.queues.get(author.id, [])))
            tracks = []
            for title, raw_url in results[:room]:
                url, is_playlist = normalize_url(raw_url)
                if is_playlist:
                    raise ValueError("再生履歴に動画以外のURLが含まれています。")
                tracks.append(Track(author.id, author.display_name, title, url))
            self.queue.add(author.id, tracks)
            if tracks:
                self.wake.set()
        await self.refresh_prefetch()
        await self.update_dashboard()
        text = f"検索結果{len(results)}曲のうち{len(tracks)}曲を、あなたのキューに追加しました。"
        if len(tracks) < len(results):
            text += f"\n待ち曲は1人100曲までのため、残り{len(results) - len(tracks)}曲は追加していません。"
        if self.stopped:
            text += "\n停止中です。/resumeで開始できます。"
        await reply(text)

    async def kill(self, process):
        if process.returncode is None:
            if os.name == "nt":
                # ランチャー経由で起動した再生ソフトも、子プロセスごと停止する。
                taskkill = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe")
                try:
                    killer = await asyncio.create_subprocess_exec(
                        taskkill, "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(killer.wait(), 3)
                    except asyncio.TimeoutError:
                        killer.kill()
                        await killer.wait()
                except OSError:
                    pass  # taskkillが使えない場合は直接終了を試みる。
            if process.returncode is not None:
                return
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def run_process(self, args, timeout=None, skip=None, capture_stderr=False):
        """シェルを使わず起動し、スキップ/終了時には子プロセスも止める。"""
        if skip and skip.is_set():
            raise Skipped()
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        self.processes.add(proc)
        output = asyncio.create_task(proc.communicate())
        skip_task = asyncio.create_task(skip.wait()) if skip else None
        try:
            tasks = {output} | ({skip_task} if skip_task else set())
            done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if skip and skip.is_set():
                raise Skipped()
            if output not in done:
                await self.kill(proc)
                out, err = await output
                detail = (err or out).decode("utf-8", errors="replace")[-2400:]
                raise TimeoutError(f"外部プログラムが{timeout}秒以内に完了しませんでした。\n{detail}")
            out, err = output.result()
            if proc.returncode:
                detail = (err or out).decode("utf-8", errors="replace")[-2400:]
                raise ProcessError(f"取得・再生に失敗しました（終了コード {proc.returncode}）。\n{detail or '詳細出力なし'}")
            if b"ERROR:" in err:
                await self.report_error("一部の動画を取得できませんでした",
                                        ProcessError(err.decode("utf-8", errors="replace")[-2400:]))
            return err if capture_stderr else out
        finally:
            if skip_task:
                skip_task.cancel()
                await asyncio.gather(skip_task, return_exceptions=True)
            await self.kill(proc)
            await asyncio.gather(output, return_exceptions=True)
            self.processes.discard(proc)

    def ytdlp(self):
        return [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings",
                "--socket-timeout", "15", "--retries", "2", "--fragment-retries", "2",
                "--js-runtimes", "deno:" + self.deno]

    async def normalize_audio(self, source, skip):
        """全曲解析→調整済みFLACの作成。失敗時に元音量で再生しない。"""
        if not self.ffmpeg:
            raise RuntimeError("自動音量調整にはFFmpegが必要です。.tools/ffmpeg/bin に配置してください。")
        args = [self.ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-y", "-i", str(source),
                "-map", "0:a:0", "-vn", "-sn", "-dn"]
        analysis = await self.run_process(args + ["-af", LOUDNESS_FILTER + ":print_format=json",
            "-f", "null", "-"], timeout=600, skip=skip, capture_stderr=True)
        audio_filter = measured_loudness_filter(analysis)
        if audio_filter is None:
            return source
        output = source.with_name("normalized.flac")
        await self.run_process(args + ["-af", audio_filter, "-ar", "48000", "-c:a", "flac",
            str(output)], timeout=600, skip=skip)
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("音量調整済みの音声を作成できませんでした。")
        return output

    async def prepare_audio(self, track, root, skip):
        await self.run_process(self.ytdlp() + [
            "--no-playlist", "--no-progress", "--max-filesize", "100M",
            "--match-filters", "!is_live & duration <= 1800",
            "-f", "bestaudio/best", "-o", str(root / "audio.%(ext)s"), "--", track.url
        ], timeout=300, skip=skip)
        files = [p for p in root.glob("audio.*") if p.suffix in {
            ".m4a", ".webm", ".mp4", ".mp3", ".opus", ".ogg", ".aac", ".flac", ".wav"
        }]
        if len(files) != 1:
            raise RuntimeError("音声を取得できませんでした。ライブ配信・30分超・サイズ超過の曲は対象外です。")
        if self.current is track:
            self.phase = "音量調整中"
            await self.update_dashboard()
            await self.say(f"音量調整中: {track.title[:150]}")
        return await self.normalize_audio(files[0], skip)

    def start_preparation(self, track):
        directory = tempfile.TemporaryDirectory(prefix="discord-music-")
        skip = asyncio.Event()
        task = asyncio.create_task(self.prepare_audio(track, Path(directory.name), skip))
        return Preparation(track, directory, skip, task)

    async def dispose_preparation(self, preparation):
        if preparation is None:
            return
        preparation.task.cancel()
        try:
            await asyncio.gather(preparation.task, return_exceptions=True)
        finally:
            preparation.directory.cleanup()

    async def refresh_prefetch(self):
        # キューから取り出さずに準備することで、交互再生や待ち曲上限を維持する。
        async with self.prefetch_lock:
            upcoming = self.queue.preview(1)
            target = upcoming[0] if upcoming and not self.stopped and (
                self.current is None or self.phase in ("再生中", "一時停止中")) else None
            if self.prefetch is not None and self.prefetch.track is target:
                return
            old, self.prefetch = self.prefetch, None
            await self.dispose_preparation(old)
            # キャンセル待ちの間に予約や再生状態が変わる場合がある。
            upcoming = self.queue.preview(1)
            if upcoming and not self.stopped and (
                    self.current is None or self.phase in ("再生中", "一時停止中")):
                self.prefetch = self.start_preparation(upcoming[0])

    async def add_url(self, author, raw_url, force_playlist, reply=None):
        reply = reply or self.say
        url, is_playlist = normalize_url(raw_url, force_playlist)
        async with self.add_lock:
            await reply("再生リストを確認しています（先頭50件まで）…" if is_playlist else "動画を確認しています…")
            args = self.ytdlp() + ["--skip-download", "--dump-single-json"]
            args += ["--flat-playlist", "--playlist-end", "50", "--ignore-errors"] if is_playlist else ["--no-playlist"]
            data = json.loads((await self.run_process(args + ["--", url], timeout=120)).decode("utf-8"))
            entries = data.get("entries", []) if is_playlist else [data]
            room = 100 - len(self.queue.queues.get(author.id, []))
            tracks = []
            for entry in entries:
                if not entry or entry.get("is_live") or entry.get("duration", 0) and entry["duration"] > 1800:
                    continue
                video_url = track_url(entry, url)
                if not video_url:
                    continue
                tracks.append(Track(author.id, author.display_name, entry.get("title") or str(entry["id"]),
                                    video_url))
            accepted = tracks[:max(0, room)]
            if not accepted:
                await reply("追加できる曲がありません。公開状態・30分以内か・予約上限を確認してください。")
                return
            self.queue.add(author.id, accepted)
            self.wake.set()
            await self.refresh_prefetch()
            await self.update_dashboard()
            await reply(f"{author.display_name}さんの曲を{len(accepted)}曲追加しました。" +
                           ("\n停止中です。/resumeで開始できます。" if self.stopped else ""))

    def controller(self, author):
        return author.guild_permissions.manage_guild or bool(self.current and self.current.user_id == author.id)

    async def skip_current(self):
        # await中に次の曲へ進んでも、その曲のイベントを操作しない。
        skip = self.skip_event
        if skip is None:
            return
        try:
            if self.ipc_path and self.phase in ("再生中", "一時停止中"):
                await self.send_ipc(["quit"])
        except (ValueError, OSError):
            pass  # IPCが未準備でも、イベント経由でプロセスを停止する。
        finally:
            skip.set()

    async def send_ipc(self, command):
        path = self.ipc_path
        if not path or self.phase not in ("再生中", "一時停止中"):
            raise ValueError("音声の準備中、または再生していません。")
        payload = (json.dumps({"command": command}) + "\n").encode()

        def send():
            if os.name == "nt":
                with open(path, "r+b", buffering=0) as pipe:
                    pipe.write(payload)
            else:
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.settimeout(2)
                    sock.connect(path)
                    sock.sendall(payload)
        for attempt in range(10):
            try:
                await asyncio.to_thread(send)
                return
            except OSError:
                if attempt == 9:
                    raise ValueError("再生操作に失敗しました。少し待ってもう一度送ってください。")
                await asyncio.sleep(0.1)

    async def on_message(self, message):
        if message.author.bot or not message.guild or message.channel.id != self.channel_id:
            return
        async def reply(text, **kwargs):
            await message.channel.send(embed=self.embed(text), **kwargs)
        await self.handle_command(message.author, message.content.strip(), reply)

    async def handle_command(self, author, text, reply):
        parts = text.split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        argument = parts[1] if len(parts) > 1 else ""
        try:
            if command == "!ping":
                await reply("接続OK！")
            elif command == "!help":
                await reply("/play URL: 1曲追加\n/search タイトルの一部: 履歴から選んで追加\n"
                               "!play タイトルの一部、またはタイトルだけの投稿でも履歴を検索\n"
                               "/playlist URL: リスト追加\n/queue: 順番\n/now: 現在の曲\n"
                               "/skip: 次へ\n/pause: 一時停止\n/resume: 再開\n/clear: 自分の待ち曲を削除\n"
                               "/stop: 停止（管理者のみ・予約は残す）\nスキップ/一時停止/再開: 曲の本人か管理者。")
            elif command == "!search":
                await self.search_history(author, argument, reply)
            elif command == "!queue":
                upcoming = self.queue.preview()
                count = sum(len(q) for q in self.queue.queues.values())
                listing = "\n".join(f"{i}. {t.title[:80]} — {t.user_name[:40]}" for i, t in enumerate(upcoming, 1))
                await reply(f"待ち曲: {count}曲（先頭10曲・現在の予測順）\n" + (listing or "予約はありません。"))
            elif command == "!now":
                await reply(f"{self.phase}: {self.current.title[:150]}\n投稿者: {self.current.user_name}"
                               if self.current else ("停止中です。" if self.stopped else "待機中です。"))
            elif command == "!clear":
                await reply(f"あなたの待ち曲を{self.queue.clear_user(author.id)}曲削除しました。")
            elif command == "!stop":
                if not author.guild_permissions.manage_guild:
                    await reply("/stopはサーバー管理権限のある人が使えます。")
                    return
                self.stopped = True
                await self.skip_current()
                await reply("停止しました。待ち曲は残ります。/resumeで次の曲から再開します。")
            elif command in ("!skip", "!pause", "!resume"):
                if command == "!resume" and self.stopped:
                    if not author.guild_permissions.manage_guild:
                        await reply("停止からの再開はサーバー管理権限のある人が操作してください。")
                        return
                    self.stopped = False
                    self.wake.set()
                    await reply("再生を再開します。")
                    return
                if not self.current:
                    await reply("現在再生していません。")
                    return
                if not self.controller(author):
                    await reply("この操作は、現在の曲を追加した本人かサーバー管理者が使えます。")
                    return
                if command == "!skip":
                    await self.skip_current()
                    await reply("この曲をスキップします。")
                else:
                    await self.send_ipc(["set_property", "pause", command == "!pause"])
                    self.phase = "一時停止中" if command == "!pause" else "再生中"
                    await reply(self.phase + "です。")
            else:
                match = re.search(r"https?://[^\s<>]+", text)
                if match:
                    await self.add_url(author, match.group().rstrip("。"), command == "!playlist", reply)
                elif command == "!playlist":
                    await reply("!playlist の後に再生リストURLを付けてください。")
                elif command == "!play":
                    await self.search_history(author, argument, reply)
                elif text and not text.startswith("!"):
                    await self.search_history(author, text, reply)
        except (ValueError, RuntimeError, TimeoutError, OSError, sqlite3.Error) as e:
            await self.report_error(f"コマンド処理失敗: {command[:40]}", e)
            if reply != self.say:
                await reply("処理に失敗しました。詳細を音楽受付チャンネルに送信しました。")

        finally:
            await self.refresh_prefetch()
            await self.update_dashboard()

    async def playback_loop(self):
        await self.wait_until_ready()
        while True:
            self.wake.clear()
            async with self.prefetch_lock:
                track = None if self.stopped else self.queue.next()
                preparation, self.prefetch = self.prefetch, None
                self.current = track
                self.phase = "準備中" if track else "待機中"
                if preparation is not None and preparation.track is not track:
                    await self.dispose_preparation(preparation)
                    preparation = None
                if track is not None:
                    preparation = preparation or self.start_preparation(track)
                    self.skip_event = preparation.skip
            if track is None:
                await self.wake.wait()
                continue
            try:
                await self.update_dashboard()
                if not preparation.task.done():
                    await self.say(f"準備中: {track.title[:150]}\n投稿者: {track.user_name}")
                audio = await preparation.task
                root = Path(preparation.directory.name)
                unique = "discord-music-" + uuid.uuid4().hex
                self.ipc_path = "\\\\.\\pipe\\" + unique if os.name == "nt" else str(root / "mpv.sock")
                if self.skip_event.is_set():
                    raise Skipped()
                self.phase = "再生中"
                try:
                    self.history.remember(self.channel_id, track)
                except (sqlite3.Error, OSError) as e:
                    await self.report_error("再生履歴の保存失敗", e, repeat=True)
                await self.refresh_prefetch()
                await self.update_dashboard()
                await self.say(f"再生開始: {track.title[:150]}\n投稿者: {track.user_name}")
                await self.run_process([
                    self.mpv, "--no-config", "--vid=no", "--ytdl=no", "--volume=50",
                    "--input-terminal=no", "--input-ipc-server=" + self.ipc_path, "--", str(audio)
                ], skip=self.skip_event)
            except Skipped:
                pass
            except (RuntimeError, TimeoutError, OSError) as e:
                await self.report_error(f"再生失敗: {track.title[:100]}\n次の予約へ進みます。", e)
            finally:
                self.current, self.skip_event, self.ipc_path = None, None, None
                self.phase = "待機中"
                await self.dispose_preparation(preparation)
                await self.update_dashboard()


def main():
    mpv, deno, ffmpeg = executable("mpv"), executable("deno"), executable("ffmpeg")
    if not mpv or not deno or not ffmpeg:
        print("mpv・Deno・FFmpegが必要です。見つからないツール: " +
              ", ".join(name for name, path in (("mpv", mpv), ("Deno", deno), ("FFmpeg", ffmpeg)) if not path))
        print("各exeをmusic_bot.pyと同じフォルダー、または .tools/ツール名/bin に配置してください。")
        return
    raw = input("音楽受付チャンネルのIDを貼り付けてEnter: ").strip()
    if not raw.isdigit() or int(raw) <= 0:
        print("チャンネルIDは数字だけを入力してください。")
        return
    token = getpass.getpass("Botトークンを貼り付けてEnter（入力は表示されません）: ").strip()
    if not token:
        print("トークンが空です。")
        return
    client = MusicBot(int(raw), mpv, deno, ffmpeg)
    try:
        client.run(token)
    except discord.LoginFailure:
        print("ログインできません。Bot画面で発行したトークンを確認してください。")
    except discord.PrivilegedIntentsRequired:
        print("Developer PortalのBot画面でMessage Content Intentを有効にしてください。")


if __name__ == "__main__":
    main()
