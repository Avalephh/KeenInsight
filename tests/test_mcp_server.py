import asyncio

import mcp
import smithery
from mcp.client.websocket import websocket_client

# Create Smithery URLs for both servers
think_url = smithery.create_smithery_url("wss://server.smithery.ai/@PhillipRt/think-mcp-server/ws", {}) + "&api_key=839b4262-5b2a-4226-a967-37d2c7fa8213"
postgresql_url = smithery.create_smithery_url("wss://server.smithery.ai/@HenkDz/postgresql-mcp-server/ws", {}) + "&api_key=your-smithery-api-key"


async def test_server(url, server_name):
    print(f"\n=== Testing {server_name} ===")
    async with websocket_client(url) as streams:
        async with mcp.ClientSession(*streams) as session:
            # List available tools
            response = await session.list_tools()

            print(f"\n=== Available Tools on {server_name} ===")
            for tool in response.tools:
                print(f"\nTool Name: {tool.name}")
                print(f"Description: {tool.description}")
                print("\nInput Schema:")
                print(f"  Required parameters: {tool.inputSchema.get('required', [])}")
                print("  Properties:")
                for prop_name, prop_info in tool.inputSchema.get("properties", {}).items():
                    print(f"    - {prop_name}: {prop_info.get('type')} ({prop_info.get('description')})")


async def main():
    # Test both servers
    await test_server(think_url, "Think MCP Server")
    await test_server(postgresql_url, "PostgreSQL MCP Server")


if __name__ == "__main__":
    asyncio.run(main())
