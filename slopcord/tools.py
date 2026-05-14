"""Handlers for tools and tool calls."""
import glob
import io
import json
import logging
import yaml

from . import globals


log = logging.getLogger(__name__)


def load_tools(ctx: globals.BotContext, filename: str):
    """Read the tools config file."""
    with open(filename, 'rt') as f:
        ctx.tools_config = yaml.safe_load(f.read())


def call_tool(ctx: globals.BotContext, tool_name: str, tool_args: str) -> str:
    """Invoke a tool with the specified args as a JSON string."""
    ctx.config.reload()
    enabled_tools = ctx.config.data.get('enabled_tools', [])
    allowed_dirs = ctx.config.data.get('allowed_dirs', [])
    if tool_name not in enabled_tools:
        return f"ERROR: tool not found: {tool_name}"

    try:
        allowed_paths = set()
        for d in allowed_dirs:
            for g in sorted(glob.glob(f"../{d}/**", recursive=True)):
                allowed_paths.add(g[3:])
        params = json.loads(tool_args)
    except Exception:
        log.exception("invalid args")
        return "ERROR: Invalid arguments"
    match tool_name:
        case "read_file":
            if 'filePath' not in params:
                return "ERROR: `filePath` is required"
            path = params['filePath'][1:] if params['filePath'].startswith('/') else params['filePath']
            log.info('read_file: path %s', path)
            if path not in allowed_paths:
                return f"ERROR: File `{path}` does not exist"
            else:
                with open('../' + params['filePath'], 'rb') as f:
                    contents = f.read().decode('utf-8', 'replace')
                    return contents
        case "list_dir":
            out = io.StringIO()
            for d in allowed_dirs:
                out.write('\n'.join(g[3:] for g in sorted(glob.glob(f"../{d}/**", recursive=True))))
                out.write('\n')
            return out.getvalue()
        case "file_search":
            return "FIXME: file_search is unimplemented."
        case "grep_search":
            return "FIXME: grep_search is unimplemented."
    return "ERROR"


def describe_tool_call(tool_name: str, tool_args: str) -> tuple[str, str]:
    """Provide a description for a tool call to use in formatting messages."""
    try:
        params = json.loads(tool_args)
    except:
        params = {}
    match tool_name:
        case "read_file":
            name = params.get('filePath', '')
            if name:
                name = f" `📄 {name}`"
            start_line = int(params.get('startLine', 0))
            end_line = int(params.get('endLine', 0))
            lines = f" (lines {start_line}-{end_line})" if start_line and end_line else ""
            return (f"reading{name}{lines}", "")
        case "list_dir":
            path = params.get('path', '')
            if path:
                path = f" `📁 {path}`"
            return (f"listing directory{path}", "")

    return (f"calling `{tool_name}`", tool_args)
