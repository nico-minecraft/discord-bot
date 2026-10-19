"""Discord bot that reports this machine's usage, battery (if any) and Pi-hole stats."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from replies import ReplyTable
from status import collect_status, format_status

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("device-status")
LOCK_PATH = Path(__file__).resolve().parent / ".bot.lock"


def _acquire_singleton_lock():
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            "Another bot.py is already running. Stop the extra one with Ctrl+C "
            "in that terminal, then start only once."
        )
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def _allowed_ids() -> set[int]:
    raw = os.getenv("ALLOWED_USER_IDS", "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ids.add(int(part))
    return ids


def _device_name() -> str | None:
    name = os.getenv("DEVICE_NAME", "").strip()
    return name or None


async def _status_report() -> str:
    # collect_status() blocks (CPU sampling, network checks), so keep it off the event loop.
    status = await asyncio.to_thread(collect_status)
    return format_status(status, _device_name())


class StatusBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.allowed_ids = _allowed_ids()
        self.replies = ReplyTable()

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def on_ready(self) -> None:
        user = self.user
        log.info("Logged in as %s (%s)", user, user.id if user else "?")
        if not self.allowed_ids:
            log.warning("ALLOWED_USER_IDS is empty; nobody will receive status")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        text = message.content.strip()
        lowered = text.lower()
        if lowered in {"status", "!status", "/status"}:
            if message.author.id not in self.allowed_ids:
                log.info("Ignored status request from %s", message.author.id)
                return
            await message.channel.send(await _status_report())
            return
        reply = self.replies.lookup(text)
        if reply:
            await message.channel.send(reply)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set (copy .env.example to .env)")

    lock = _acquire_singleton_lock()
    bot = StatusBot()

    @bot.tree.command(name="status", description="Usage, battery (if any) and Pi-hole stats for the host this bot is running on")
    async def status_command(interaction: discord.Interaction) -> None:
        if interaction.user.id not in bot.allowed_ids:
            await interaction.response.send_message(
                "You are not allowed to request this.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await _status_report(), ephemeral=True)

    bot.run(token)


if __name__ == "__main__":
    main()
