import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(
        command="/opt/anaconda3/bin/python",
        args=["mcp_server.py"],
        cwd="/Users/kenhglee/projects/supplychain-ai-agent",
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("=== Registered tools ===")
            for t in tools.tools:
                print(f"  {t.name}: {t.description.splitlines()[0]}")

            print("\n=== evaluate_github_event_risk (push to main) ===")
            result = await session.call_tool("evaluate_github_event_risk", {
                "event_type": "push",
                "repository": "org/repo",
                "ref": "refs/heads/main",
            })
            print(result.content)

            print("\n=== evaluate_github_event_risk (PR to main) ===")
            result = await session.call_tool("evaluate_github_event_risk", {
                "event_type": "pull_request",
                "repository": "org/repo",
                "base_ref": "main",
                "pr_number": 42,
                "pr_title": "Add new supplier integration",
            })
            print(result.content)

            print("\n=== get_recent_risk_decisions (limit=3) ===")
            result = await session.call_tool("get_recent_risk_decisions", {"limit": 3})
            print(result.content)

            print("\n=== get_decisions_requiring_review (limit=5) ===")
            result = await session.call_tool("get_decisions_requiring_review", {"limit": 5})
            print(result.content)


asyncio.run(main())
