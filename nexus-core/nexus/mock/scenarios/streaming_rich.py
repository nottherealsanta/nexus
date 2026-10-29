"""Rich streaming showcase adapted from old Nexus streaming_rich."""

from ..dsl import Scenario, verdict

_BODY = """# Streaming showcase\n\n## Lists and emphasis\n1. **bold**, *italic*, ~~struck~~, `inline code`\n\n> A block quote\n\n| tool | mutates |\n|---|---|\n| read | no |\n| write | yes |\n\n```python\ndef fib(n): return n if n < 2 else fib(n-1)+fib(n-2)\n```\n\n日本語 · Ελληνικά · مرحبا · 🚀🔥✅\n"""
SCENARIO = Scenario(
    "streaming_rich",
    "Markdown, code, tables and Unicode",
    {"main": [verdict("streaming-rich", [], intro=_BODY)]},
    prompt="Show me everything the renderer can do.",
)
