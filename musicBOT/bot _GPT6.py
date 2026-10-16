"""Discord接続の確認用。!pingへの返信とYouTube URLの受付を行います。
この段階では音声再生や予約の保存は行いません。
実行: py -3.13 bot.py
"""
import getpass
import json
from pathlib import Path
import sys
import re

import discord


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
    try:
        settings = load_settings()
    except (ValueError, OSError) as error:
        print(f"設定の読み込み・保存に失敗しました: {error}")
        return
    channel_id = settings["channel_id"]
    token = settings["token"].strip()

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())

    @client.event
    async def on_ready():
        print(f"接続成功: {client.user}")
        print("指定チャンネルに !ping またはYouTubeのURLを送ってください。")
        print("終了するには、この画面で Ctrl+C を押します。")

    @client.event
    async def on_message(message):
        if message.author.bot or message.guild is None or message.channel.id != channel_id:
            return
        if message.content.strip() == "!ping":
            await message.channel.send("接続OK！")
            return
        if re.search(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)(?:/|\?)", message.content, re.IGNORECASE):
            await message.channel.send(
                f"{message.author.display_name}さんのYouTube URLを受け取りました。\n"
                "現在は接続確認用なので、まだ音声は再生しません。"
            )
            print(f"URL受付: ユーザーID={message.author.id}")

    try:
        client.run(token)
    except discord.LoginFailure:
        print("トークンを変更する場合は、起動コマンドの最後に --setup を付けて再起動してください。")
        print("ログインできませんでした。Bot画面で発行したトークンを確認してください。")
    except discord.PrivilegedIntentsRequired:
        print("Developer PortalのBot画面でMessage Content Intentを有効にしてください。")


if __name__ == "__main__":
    from single_instance import run_single_instance
    run_single_instance(main)
