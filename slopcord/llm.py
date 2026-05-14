"""LLM streaming interface to the OpenAI API.

Handles chat.completions.create streaming and chunk processing.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
import logging
from typing import Any, AsyncGenerator

import openai
from openai.types import chat

from . import constants, tools

log = logging.getLogger(__name__)


@dataclass
class Response:
    """State for an ongoing response generation."""
    content: deque[str] = field(default_factory=deque)
    finish_reason: str | None = None
    usage: tuple[int, int] | None = None
    tool_calls: defaultdict[str, deque[str]] = field(default_factory=lambda: defaultdict(deque))
    tool_names: defaultdict[str, str] = field(default_factory=lambda: defaultdict(str))


async def generate(
    client: openai.AsyncOpenAI,
    model_name: str,
    model_params: dict[str, Any],
    system_prompt: str,
    messages: list[dict[str, Any]],
    tool_defs: list[Any],
    tool_messages: list[dict[str, Any]],
) -> AsyncGenerator[Response]:
    """Generate responses from the LLM, yielding updated Response objects as new chunks arrive."""
    system_turns = [dict(content=system_prompt, role="system")]
    response = Response()

    cur_tool_id = ""

    async for chunk in await client.chat.completions.create(
        messages=system_turns + messages[::-1] + tool_messages[::-1],
        model=model_name,
        max_completion_tokens=constants.MAX_TOKENS,
        max_tokens=constants.MAX_TOKENS,
        response_format="json_object",
        stream=True,
        stream_options=dict(include_usage=True),
        tools=tool_defs,
        tool_choice="auto",

        **(model_params or {}),
    ):
        chunk: chat.ChatCompletionChunk

        if choice := chunk.choices[0] if chunk.choices else None:
            if choice.delta.content:
                response.content.append(choice.delta.content)

            if choice.delta.tool_calls:
                for tool in choice.delta.tool_calls:
                    if tool.id:
                        cur_tool_id = tool.id
                    if tool.function:
                        if tool.function.name:
                            response.tool_names[cur_tool_id] = tool.function.name
                        if tool.function.arguments:
                            response.tool_calls[cur_tool_id].append(tool.function.arguments)

            if choice.finish_reason:
                response.finish_reason = choice.finish_reason

        if chunk.usage:
            response.usage = (chunk.usage.prompt_tokens, chunk.usage.completion_tokens)

        yield response
