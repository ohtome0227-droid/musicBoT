"""DiscordからYouTube・ニコニコ動画を予約し、PCのスピーカーで再生するBot。
必要: discord.py, yt-dlp[default], mpv, Deno
実行: py -3.13 music_bot_2_GPT6.py
予約は起動中のみ保持。1投稿1URL、リスト先頭50件、1人の待ち曲100件。
音声を一時取得して再生し、終了・スキップ時に削除します。
"""
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit
import uuid

import discord
from discord import app_commands


QUEUE_CHANNEL_ID = 1559531557348450395
QUEUE_STATE = Path(__file__).with_name("queue_message.json")
PREFETCH_COUNT = 3
AUDIO_FILTER = "--af=lavfi=[loudnorm=I=-16:TP=-1.5:LRA=11]"
JST = timezone(timedelta(hours=9))
SCHEDULE_URL = "https://www.youtube.com/watch?v=OgYWssWn7uQ"


def japan_now():
    return datetime.now(JST)

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

    def clear(self):
        count = sum(len(q) for q in self.queues.values())
        self.queues.clear()
        self.turns.clear()
        self.last_user = None
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
    beside = Path(__file__).resolve().parent / (name + (".exe" if os.name == "nt" else ""))
    return str(beside) if beside.is_file() else shutil.which(name)


class MusicBot(discord.Client):
    def __init__(self, channel_id, mpv, deno):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.channel_id, self.mpv, self.deno = channel_id, mpv, deno
        self.queue = FairQueue()
        self.queue_generation = 0
        self.wake = asyncio.Event()
        self.add_lock = asyncio.Lock()
        self.current = None
        self.skip_event = None
        self.phase = "待機中"
        self.stopped = False
        self.worker = None
        self.processes = set()
        self.ipc_path = None
        self.volume = 50
        self.volume_lock = asyncio.Lock()
        self.downloads = {}
        self.download_tasks = set()
        self.tree = app_commands.CommandTree(self)
        self.dashboard_lock = asyncio.Lock()
        self.dashboard_message = None
        self.dashboard_payload = None
        self.dashboard_worker = None
        self.last_errors = {}
        self.schedule_worker = None
        self.schedule_track = None
        self.schedule_date = None
        self.schedule_started = False
        self.schedule_pending = False
        self.schedule_active = False
        self.night_finished = False
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
        self.schedule_worker = asyncio.create_task(self.schedule_loop())

    async def close(self):
        if self.schedule_worker:
            self.schedule_worker.cancel()
            await asyncio.gather(self.schedule_worker, return_exceptions=True)
        if self.dashboard_worker:
            self.dashboard_worker.cancel()
            await asyncio.gather(self.dashboard_worker, return_exceptions=True)
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        for task in list(self.downloads.values()):
            self.discard_download(task)
        self.downloads.clear()
        await asyncio.gather(*self.download_tasks, return_exceptions=True)
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
            "reset": "全員の待ち曲をリセット（サーバー管理者のみ・再生中の曲は継続）",
            "play": "YouTube・ニコニコ動画のURLから曲を追加", "playlist": "再生リスト・マイリスト・シリーズを追加"}
        descriptions.update({"volumeup": "音量を2ポイント上げる（最大100%）",
                             "volumedown": "音量を2ポイント下げる（最小0%）"})
        def callback_for(name):
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
        async def reply(text):
            await interaction.followup.send(embed=self.embed(text), ephemeral=True)
        await self.handle_command(interaction.user, f"/{name}" + (f" {url}" if url else ""), reply)

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

    async def run_process(self, args, timeout=None, skip=None):
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
            return out
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

    async def add_url(self, author, raw_url, force_playlist, reply=None):
        reply = reply or self.say
        if self.night_blocked():
            await reply("夜間停止中です。曲の受付・再生は日本時間8:00からです。")
            return
        url, is_playlist = normalize_url(raw_url, force_playlist)
        generation = self.queue_generation
        async with self.add_lock:
            if generation != self.queue_generation:
                await reply("キューがリセットされたため、この追加を取り消しました。")
                return
            await reply("再生リストを確認しています（先頭50件まで）…" if is_playlist else "動画を確認しています…")
            args = self.ytdlp() + ["--skip-download", "--dump-single-json"]
            args += ["--flat-playlist", "--playlist-end", "50", "--ignore-errors"] if is_playlist else ["--no-playlist"]
            data = json.loads((await self.run_process(args + ["--", url], timeout=120)).decode("utf-8"))
            if generation != self.queue_generation:
                await reply("キューがリセットされたため、この追加を取り消しました。")
                return
            entries = data.get("entries", []) if is_playlist else [data]
            if self.night_blocked():
                await reply("夜間停止に入ったため、曲の追加を取り消しました。")
                return
            room = 100 - len(self.queue.queues.get(author.id, []))
            tracks = []
            for entry in entries:
                if not entry or entry.get("is_live") or entry.get("duration", 0) and entry["duration"] > 2700:
                    continue
                video_url = track_url(entry, url)
                if not video_url:
                    continue
                tracks.append(Track(author.id, author.display_name, entry.get("title") or str(entry["id"]),
                                    video_url))
            accepted = tracks[:max(0, room)]
            if not accepted:
                await reply("追加できる曲がありません。公開状態・45分以内か・予約上限を確認してください。")
                return
            self.queue.add(author.id, accepted)
            self.refresh_prefetch()
            self.wake.set()
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
        text = message.content.strip()
        if text.startswith("!"):
            text = "/" + text[1:]
        await self.handle_command(message.author, text, self.say)

    async def handle_command(self, author, text, reply):
        command = text.split(maxsplit=1)[0].lower() if text else ""
        try:
            if command == "/resume" and self.night_blocked():
                await reply("夜間停止中です。日本時間8:00まで再生できません。")
                return
            if command == "/ping":
                await reply("接続OK！")
            elif command == "/help":
                await reply("/play URL: 1曲追加\n/playlist URL: リスト追加\n/queue: 順番\n/now: 現在の曲\n"
                               "/skip: 次へ\n/pause: 一時停止\n/resume: 再開\n/clear: 自分の待ち曲を削除\n"
                               "/reset: 全員の待ち曲をリセット（管理者のみ・再生中の曲は継続）\n"
                               "/volumeup: 音量を2ポイント上げる\n/volumedown: 音量を2ポイント下げる\n"
                               "音量は0～100%（初期値50%）。変更は次の曲にも引き継ぎます。\n"
                               "/stop: 停止（管理者のみ・予約は残す）\nスキップ/一時停止/再開/音量調整: 曲の本人か管理者。")
            elif command == "/queue":
                upcoming = self.queue.preview()
                count = sum(len(q) for q in self.queue.queues.values())
                listing = "\n".join(f"{i}. {t.title[:80]} — {t.user_name[:40]}" for i, t in enumerate(upcoming, 1))
                await reply(f"待ち曲: {count}曲（先頭10曲・現在の予測順）\n" + (listing or "予約はありません。"))
            elif command == "/now":
                await reply(f"{self.phase}: {self.current.title[:150]}\n投稿者: {self.current.user_name}"
                               if self.current else ("停止中です。" if self.stopped else "待機中です。"))
            elif command == "/clear":
                await reply(f"あなたの待ち曲を{self.queue.clear_user(author.id)}曲削除しました。")
            elif command == "/reset":
                if not author.guild_permissions.manage_guild:
                    await reply("/resetはサーバー管理権限のある人が使えます。")
                    return
                self.queue_generation += 1
                count = self.queue.clear()
                await reply(f"キューをリセットし、全員の待ち曲を{count}曲削除しました。再生中の曲は継続します。")
            elif command == "/stop":
                if not author.guild_permissions.manage_guild:
                    await reply("/stopはサーバー管理権限のある人が使えます。")
                    return
                self.stopped = True
                await self.skip_current()
                await reply("停止しました。待ち曲は残ります。/resumeで次の曲から再開します。")
            elif command in ("/skip", "/pause", "/resume", "/volumeup", "/volumedown"):
                if command == "/resume" and self.stopped:
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
                if not self.controller(author) and not (self.schedule_active and command in ("/volumeup", "/volumedown")):
                    await reply("この操作は、現在の曲を追加した本人かサーバー管理者が使えます。")
                    return
                if command == "/skip":
                    await self.skip_current()
                    await reply("この曲をスキップします。")
                elif command in ("/volumeup", "/volumedown"):
                    async with self.volume_lock:
                        volume = max(0, min(100, self.volume + (2 if command == "/volumeup" else -2)))
                        await self.send_ipc(["set_property", "volume", volume])
                        self.volume = volume
                    await reply(f"音量を{volume}%に設定しました。")
                else:
                    await self.send_ipc(["set_property", "pause", command == "/pause"])
                    self.phase = "一時停止中" if command == "/pause" else "再生中"
                    await reply(self.phase + "です。")
            else:
                match = re.search(r"https?://[^\s<>]+", text)
                if match:
                    await self.add_url(author, match.group().rstrip("。"), command == "/playlist", reply)
                elif command == "/playlist":
                    await reply("/playlist の後に再生リストURLを付けてください。")
        except (ValueError, RuntimeError, TimeoutError, OSError, json.JSONDecodeError) as e:
            await self.report_error(f"コマンド処理失敗: {command[:40]}", e)
            if reply != self.say:
                await reply("処理に失敗しました。詳細を音楽受付チャンネルに送信しました。")

        finally:
            self.refresh_prefetch()
            await self.update_dashboard()

    @staticmethod
    def discard_download(task):
        def cleanup(done):
            if not done.cancelled() and done.exception() is None:
                directory, _ = done.result()
                directory.cleanup()
        task.add_done_callback(cleanup)
        if not task.done():
            task.cancel()

    async def download_track(self, track):
        directory = tempfile.TemporaryDirectory(prefix="discord-music-")
        root = Path(directory.name)
        try:
            await self.run_process(self.ytdlp() + [
                "--no-playlist", "--no-progress", "--max-filesize", "100M",
                "--match-filters", "!is_live & duration <= 2700",
                "-f", "bestaudio/best", "-o", str(root / "audio.%(ext)s"), "--", track.url
            ], timeout=300)
            files = [p for p in root.glob("audio.*") if p.suffix in {
                ".m4a", ".webm", ".mp4", ".mp3", ".opus", ".ogg", ".aac", ".flac", ".wav"
            }]
            if len(files) != 1:
                raise RuntimeError("音声を取得できませんでした。ライブ配信・45分超・サイズ超過の曲は対象外です。")
            return directory, files[0]
        except BaseException:
            directory.cleanup()
            raise

    def refresh_prefetch(self):
        # 同じURLの別予約も個別に管理し、ユーザー交互の予測順に追従する。
        tracks = ([self.current] if self.current else []) + self.queue.preview(PREFETCH_COUNT)
        if self.schedule_track and all(t is not self.schedule_track for t in tracks):
            tracks.append(self.schedule_track)
        wanted = {id(track) for track in tracks}
        for key in list(self.downloads):
            if key not in wanted:
                self.discard_download(self.downloads.pop(key))
        for track in tracks:
            if id(track) not in self.downloads:
                task = asyncio.create_task(self.download_track(track))
                self.downloads[id(track)] = task
                self.download_tasks.add(task)
                task.add_done_callback(self.download_tasks.discard)
                # 先読み失敗は再生順が来たときに通知する。
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    def night_blocked(self, now=None):
        now = now or japan_now()
        minutes = now.hour * 60 + now.minute
        return minutes < 480 or minutes >= 1435

    async def schedule_tick(self, now):
        # A late start after 23:55 stays silent; never replay a missed announcement.
        minutes = now.hour * 60 + now.minute
        if self.schedule_date != now.date() and not self.schedule_active and not self.schedule_pending:
            self.schedule_started = False
        if 480 <= minutes < 1430 and self.night_finished:
            self.night_finished = False
            self.stopped = False
            self.wake.set()
        if 1430 <= minutes < 1435 and self.schedule_date != now.date():
            self.schedule_date = now.date()
            self.schedule_started = False
            self.schedule_track = Track(0, "毎日の時刻指定", "23:55の指定音源", SCHEDULE_URL)
            self.refresh_prefetch()
        if minutes == 1435 and not self.schedule_started:
            self.schedule_started = True
            self.schedule_date = now.date()
            if self.schedule_track is None:
                self.schedule_track = Track(0, "毎日の時刻指定", "23:55の指定音源", SCHEDULE_URL)
                self.refresh_prefetch()
            self.schedule_pending = True
            self.stopped = True
            self.volume = 40
            await self.skip_current()
            self.wake.set()
        elif minutes > 1435 and self.schedule_track and not self.schedule_started:
            task = self.downloads.pop(id(self.schedule_track), None)
            self.schedule_track = None
            if task:
                self.discard_download(task)
        if self.night_blocked(now) and not self.schedule_active and not self.schedule_pending:
            self.stopped = True
            self.night_finished = True
            if self.current:
                await self.skip_current()

    async def schedule_loop(self):
        await self.wait_until_ready()
        while True:
            try:
                await self.schedule_tick(japan_now())
            except Exception as error:
                await self.report_error("時刻指定処理失敗", error, repeat=True)
            await asyncio.sleep(1)

    def finish_scheduled_playback(self):
        self.schedule_active = False
        self.schedule_track = None
        self.stopped = True
        self.night_finished = True
        self.queue_generation += 1
        self.queue.clear()
        self.refresh_prefetch()

    async def playback_loop(self):
        await self.wait_until_ready()
        while True:
            self.wake.clear()
            scheduled = self.schedule_pending
            if scheduled:
                track = self.schedule_track
                self.schedule_pending = False
                self.schedule_active = True
                self.volume = 40
            else:
                track = None if self.stopped or self.night_blocked() else self.queue.next()
            if track is None:
                await self.wake.wait()
                continue
            self.current = track
            self.skip_event = asyncio.Event()
            self.refresh_prefetch()
            self.phase = "準備中"
            skip_wait = None
            try:
                await self.update_dashboard()
                await self.say(f"準備中: {track.title[:150]}\n投稿者: {track.user_name}")
                download = self.downloads[id(track)]
                skip_wait = asyncio.create_task(self.skip_event.wait())
                await asyncio.wait({download, skip_wait}, return_when=asyncio.FIRST_COMPLETED)
                if self.skip_event.is_set():
                    raise Skipped()
                directory, audio = download.result()
                root = Path(directory.name)
                unique = "discord-music-" + uuid.uuid4().hex
                self.ipc_path = "\\\\.\\pipe\\" + unique if os.name == "nt" else str(root / "mpv.sock")
                if self.skip_event.is_set():
                    raise Skipped()
                self.phase = "再生中"
                await self.update_dashboard()
                await self.say(f"再生開始: {track.title[:150]}\n投稿者: {track.user_name}")
                await self.run_process([
                    self.mpv, "--no-config", "--vid=no", "--ytdl=no", f"--volume={self.volume}",
                    AUDIO_FILTER,
                    "--input-terminal=no", "--input-ipc-server=" + self.ipc_path, "--", str(audio)
                ], skip=self.skip_event)
            except Skipped:
                pass
            except (RuntimeError, TimeoutError, OSError) as e:
                await self.report_error(f"再生失敗: {track.title[:100]}\n次の予約へ進みます。", e)
            finally:
                if skip_wait:
                    skip_wait.cancel()
                    await asyncio.gather(skip_wait, return_exceptions=True)
                download = self.downloads.pop(id(track), None)
                if download:
                    self.discard_download(download)
                self.current, self.skip_event, self.ipc_path = None, None, None
                if scheduled:
                    self.finish_scheduled_playback()
                self.phase = "待機中"
                await self.update_dashboard()


CONFIG_PATH = Path(__file__).resolve().with_name("bot_config.json")


def load_settings():
    """両方のBotで共有する設定。--setup指定時だけ再入力します。"""
    settings = {}
    if CONFIG_PATH.exists():
        try:
            settings = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(settings, dict):
                raise ValueError()
        except (ValueError, OSError):
            raise ValueError("bot_config.jsonを読み込めません。内容を修正するか、名前を変更してから再起動してください。") from None
    changed = False
    reset = "--setup" in sys.argv[1:]
    channel = settings.get("channel_id")
    if reset or not isinstance(channel, int) or isinstance(channel, bool) or channel <= 0:
        raw = input("音楽受付チャンネルのIDを貼り付けてEnter: ").strip()
        if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
            raise ValueError("チャンネルIDには数字だけを入力してください。")
        settings["channel_id"] = int(raw)
        changed = True
    token = settings.get("token")
    if reset or not isinstance(token, str) or not token.strip():
        token = getpass.getpass("Botトークンを貼り付けてEnter（入力は表示されません）: ").strip()
        if not token:
            raise ValueError("トークンが空です。")
        settings["token"] = token
        changed = True
    if "queue_channel_id" not in settings:
        settings["queue_channel_id"] = 1559531557348450395
        changed = True
    queue_channel = settings["queue_channel_id"]
    if not isinstance(queue_channel, int) or isinstance(queue_channel, bool) or queue_channel <= 0:
        raise ValueError("bot_config.jsonのqueue_channel_idには正の整数を指定してください。")
    if changed:
        # 同じフォルダの一時ファイルに保存後、置き換えて書き込み途中の破損を防ぐ。
        import tempfile
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CONFIG_PATH.parent,
                                             prefix=".bot_config_", suffix=".tmp", delete=False) as f:
                temporary = Path(f.name)
                json.dump(settings, f, ensure_ascii=False, indent=2)
                f.write("\n")
            temporary.replace(CONFIG_PATH)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print("設定をbot_config.jsonに保存しました。次回から入力は不要です。")
        print("このファイルにはトークンが含まれます。他の人に渡したり公開したりしないでください。")
    else:
        print("保存済みの設定を読み込みました。")
    return settings


def main():
    mpv, deno = executable("mpv"), executable("deno")
    if not mpv or not deno:
        print("mpvまたはDenoが見つかりません。導入後PowerShellを開き直してください。")
        print("mpv.exe/deno.exeをこのプログラムと同じフォルダーに置くこともできます。")
        return
    try:
        settings = load_settings()
    except (ValueError, OSError) as error:
        print(f"設定の読み込み・保存に失敗しました: {error}")
        return
    channel_id = settings["channel_id"]
    token = settings["token"].strip()
    global QUEUE_CHANNEL_ID
    QUEUE_CHANNEL_ID = settings["queue_channel_id"]
    client = MusicBot(channel_id, mpv, deno)

    try:
        client.run(token)
    except discord.LoginFailure:
        print("トークンを変更する場合は、起動コマンドの最後に --setup を付けて再起動してください。")
        print("ログインできません。Bot画面で発行したトークンを確認してください。")
    except discord.PrivilegedIntentsRequired:
        print("Developer PortalのBot画面でMessage Content Intentを有効にしてください。")


if __name__ == "__main__":
    from single_instance import run_single_instance
    run_single_instance(main)
