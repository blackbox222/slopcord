"""slopcord — main entry point for a Discord smooth sloperator."""

import asyncio
import argparse
import logging
import logging.handlers
import queue
import sys

import discord
from discord.ext import commands
import httpx

from . import commands as cmd_module, configs, events, globals, tools


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--invisible", action='store_true', default=False)
    parser.add_argument("--tools_config", default="tools.yaml")
    args = parser.parse_args(argv)

    # Configure logging with a queue to avoid blocking the event loop
    log_queue = queue.Queue(-1)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    log_handler = logging.StreamHandler()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] [%(taskName)s]: %(message)s",
        handlers=(queue_handler,),
    )

    with logging.handlers.QueueListener(log_queue, log_handler):
        config = configs.Config(args.config)

        # Initialize bot
        status_msg = (config.data.get("status_message") or "")[:128]
        intents = discord.Intents.default()
        intents.message_content = True
        bot = commands.Bot(
            intents=intents,
            activity=discord.CustomActivity(name=status_msg),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
            status=discord.Status.invisible if args.invisible else discord.Status.online,
            command_prefix="")

        httpx_client = httpx.AsyncClient()

        # Make the first model the default
        model_name = next(iter(config.data.get("models", {})))
        bot_context = globals.BotContext(bot, config, httpx_client, model_name)
        tools.load_tools(bot_context, args.tools_config)

        # Register commands
        cmd_module.register_commands(bot_context, model_name)

        # Wire up events
        @bot.event
        async def on_ready() -> None:
            await events.on_ready(bot_context)

        @bot.event
        async def on_message(msg: discord.Message) -> None:
            await events.on_message(bot_context, msg)

        # Start bot
        await bot.start(config.data.get("bot_token", ""))

        return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main(sys.argv[1:])))
    except KeyboardInterrupt:
        pass
