"""Event handlers for the slopcord bot.

Orchestrates on_ready and on_message, delegating to other modules.
"""

import asyncio
from collections import defaultdict
import io
import logging
import time
from typing import Any

import discord
import openai

from . import constants, formatters, globals, llm, messages, permissions, tools

log = logging.getLogger(__name__)


async def on_ready(ctx: globals.BotContext) -> None:
    """Handle bot ready event."""
    ctx.config.reload()

    if client_id := ctx.config.data.get("client_id", None):
        log.info(
            "READY: invite URL: https://discord.com/oauth2/authorize?client_id=%s&permissions=412317191168&scope=bot",
            client_id)

    await ctx.bot.tree.sync()


async def on_message(ctx: globals.BotContext, msg: discord.Message) -> None:
    """Handle incoming messages."""
    # Reload config for permissions
    ctx.config.reload()

    # Permission checks (includes mention/DM/author checks)
    perm_data = ctx.config.data.get("permissions", {})
    allow_dms = ctx.config.data.get("allow_dms", True)

    if not permissions.is_allowed(msg, ctx.bot.user, perm_data, allow_dms):
        return

    # Setup model and provider
    provider, model = ctx.model_name.removesuffix(":vision").split("/", 1)

    provider_config = ctx.config.data.get("providers", {}).get(provider, {})
    base_url = provider_config.get("base_url", "")
    api_key = provider_config.get("api_key", "sk-no-key-required")
    openai_client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)

    model_parameters: dict[str, Any] = ctx.config.data.get("models", {}).get(ctx.model_name, {})

    # TODO
    extra_headers = provider_config.get("extra_headers")
    extra_query = provider_config.get("extra_query")
    accept_images = any(tag in ctx.model_name.lower()
                        for tag in constants.VISION_MODEL_TAGS)

    system_prompt = ctx.config.get_system_prompt(ctx.config.system_prompt_name)

    try:
        cur_reply: discord.Message | None = None
        cur_reply_lock = asyncio.Lock()
        last_update_time = 0.0
        tool_messages = []
        parent_msg = msg
        start_time = time.time()

        for i in range(5):
            tool_names: defaultdict[str, str] = defaultdict(str)
            tool_args: defaultdict[str, io.StringIO] = defaultdict(io.StringIO)

            # Fetch message chain
            msg_chain = messages.MessageChain(ctx, parent_msg)
            await msg_chain.build()
            log.info(
                "iteration %d: handling message (%d attachments, %d chained) <@%d>: %s",
                i,
                len(parent_msg.attachments),
                len(msg_chain.messages),
                parent_msg.author.id,
                parent_msg.content)

            async with parent_msg.channel.typing():
                text = io.StringIO()
                tool_id = ""

                finish_reason: str | None = None
                usage: tuple[int, int] | None = None

                async def _update(embed: discord.Embed, final_edit: bool = False) -> None:
                    nonlocal parent_msg, cur_reply, last_update_time

                    # Avoid updating messages too frequently
                    cur_time = time.monotonic()
                    if (last_update_time and
                        (wait_time := cur_time - last_update_time) < constants.RATE_LIMIT_SECONDS):
                        log.info("waiting %d ms to update", wait_time * 1000)
                        await asyncio.sleep(wait_time)

                    # Reply or edit existing reply, avoid cancelling inflight HTTP requests
                    async with cur_reply_lock:
                        if cur_reply:
                            cur_reply = await asyncio.shield(cur_reply.edit(embed=embed))
                        else:
                            cur_reply = await asyncio.shield(parent_msg.reply(embed=embed, silent=True))
                        if final_edit:
                            parent_msg = cur_reply
                            cur_reply = None
                        last_update_time = cur_time

                async for response in llm.generate(
                    client=openai_client,
                    model_name=model,
                    model_params=model_parameters,
                    system_prompt=system_prompt,
                    messages=msg_chain.messages,
                    tool_defs=ctx.tools_config,
                    tool_messages=tool_messages
                ):
                    if response.finish_reason:
                        finish_reason = response.finish_reason
                    if response.usage:
                        usage = response.usage

                    # Accumulate content parts until we run out or hit the message length limit, then send an update
                    while response.content:
                        part = response.content.popleft()

                        # If adding this part would exceed Discord's length limit, finish this and start a new one
                        if (text.tell() + tool_args[tool_id].tell() + len(part)) > constants.MAX_MESSAGE_LENGTH:
                            log.info("Splitting at message limit with %d chars", text.tell())
                            await _update(
                                formatters.format_embed(
                                    text.getvalue(), "message_split", usage,
                                    tool_names=tool_names, tool_args=tool_args),
                                final_edit=True)
                            text = io.StringIO()
                            tool_args[tool_id] = io.StringIO()
                        text.write(part)

                    # Tool calls and arguments
                    for id, args in response.tool_calls.items():
                        tool_id = id
                        if tool_id:
                            while args:
                                part = args.popleft()

                                # If adding this part would exceed Discord's length limit, finish this and start a new one
                                if (text.tell() + tool_args[tool_id].tell() + len(part)) > constants.MAX_MESSAGE_LENGTH:
                                    log.info("Splitting at message limit with %d chars", text.tell())
                                    await _update(
                                        formatters.format_embed(
                                            text.getvalue(), "message_split", usage,
                                            tool_names=tool_names, tool_args=tool_args),
                                        final_edit=True)
                                    text = io.StringIO()
                                    tool_args[tool_id] = io.StringIO()
                                tool_args[tool_id].write(part)

                            if response.tool_names:
                                tool_names[tool_id] = response.tool_names[id]

                    cur_time = time.monotonic()
                    if not last_update_time or (cur_time - last_update_time) > constants.RATE_LIMIT_SECONDS:
                        # Enqueue an update with the current (partial) response
                        log.info("Updating message with %d chars", text.tell())
                        await _update(
                            formatters.format_embed(
                                text.getvalue(), finish_reason, usage,
                                tool_names=tool_names, tool_args=tool_args))

                # Trigger a final update to avoid missing any updates that were rate limited
                elapsed = time.time() - start_time
                log.info("LLM completion done in %.2f s, finishing update of %d chars", elapsed, text.tell())
                await _update(
                    formatters.format_embed(
                        text.getvalue(), finish_reason, usage, elapsed,
                        tool_names, tool_args))
                if cur_reply:
                    parent_msg = cur_reply
                    cur_reply = None

                # If the LLM called tools, evaluate those now so we can loop again and re-trigger generation with the tool outputs
                if response.finish_reason == "tool_calls":
                    for id, name in tool_names.items():
                        log.info("handling tool call %s for tool %s: %s", id, name, tool_args[id].getvalue())
                        message = tools.call_tool(ctx, name, tool_args[id].getvalue())
                        message_lines = message.split('\n')
                        log.info("tool %s returned message: %s%s", name, message_lines[0], f" ({len(message_lines) - 1} more lines)" if len(message_lines) > 1 else "")
                        tool_messages.append(dict(role="tool", content=message, tool_call_id=id))
                else:
                    break

    except Exception:
        log.exception("Error while generating response")

    # Prune old msg_nodes
    await messages.prune_cache()
