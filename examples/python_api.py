"""Run from the project root: python3 -m examples.python_api"""
import asyncio
from contextlib import aclosing
from nexus import Agent


async def main():
    agent = Agent(".")
    async with aclosing(agent.stream("Explain this repository.", session="example")) as events:
        async for event in events:
            # A GUI/TUI can dispatch these events to its own renderer.
            if event.type == "message":
                print(event.data["text"])


if __name__ == "__main__":
    asyncio.run(main())
