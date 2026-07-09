import asyncio

from tellmphone.config import Config
from tellmphone.server import create_server


def test_mcp_tool_contract(home):
    server = create_server(Config(i_am="claude", home=home))

    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert set(tools) == {
        "call",
        "reply",
        "report_progress",
        "check_messages",
        "get_call",
        "hang_up",
        "phonebook",
    }
    assert set(tools["call"].inputSchema["properties"]) == {
        "callee",
        "message",
        "project_dir",
        "personality",
        "model",
        "context",
        "mode",
        "write",
    }
    assert set(tools["reply"].inputSchema["properties"]) == {"call_id", "message"}
    assert set(tools["report_progress"].inputSchema["properties"]) == {
        "message",
        "call_id",
    }
