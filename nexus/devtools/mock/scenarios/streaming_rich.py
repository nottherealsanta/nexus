"""Rich markdown, wide lines, unicode and escape-looking text, with thinking."""
from ..checks import no_unexpected_errors
from ..dsl import Scenario, verdict

_BODY = """# Streaming showcase

## Lists and emphasis
1. **bold**, *italic*, ~~struck~~, `inline code`
   - nested bullet with a [link](https://example.invalid/never-fetched)
   - another one
2. Second item

> A block quote
> spanning two lines.

## Table
| tool | mutates | concurrency |
|------|---------|-------------|
| read | no | parallel |
| write | yes | exclusive |

## Code
```python
def fib(n: int) -> int:
    return n if n < 2 else fib(n - 1) + fib(n - 2)
```

```diff
- old line
+ new line
```

## Wide line
""" + ("0123456789" * 40) + """

## Unicode
日本語のテキスト · Ελληνικά · مرحبا بالعالم · 🚀🔥✅ · é combining

## Literal escapes (must render as text)
`\\x1b[31mnot red\\x1b[0m` and <script>alert(1)</script> and [[not a widget]] and {{ template }}
"""

SCENARIO = Scenario(
    name="streaming-rich",
    summary="Markdown, tables, code, 400-col lines, CJK/emoji/RTL, literal escapes, thinking",
    tags=("render",),
    est_seconds=8,
    prompt="Show me everything the renderer can do.",
    actors={
        "main": [
            verdict("streaming-rich", [no_unexpected_errors(0)], intro=_BODY),
        ]
    },
)
