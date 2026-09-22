import math

from nexus.model.capabilities import Capabilities
from nexus.model.tokenizer import DEFAULT_TOKENIZER, HeuristicTokenizer, Tokenizer


def test_conservative_capabilities():
    caps = Capabilities.conservative()
    assert caps.tools is False
    assert caps.parallel_tool_calls is False
    assert caps.thinking is False
    assert caps.streaming is True
    assert caps.max_context_tokens == 0
    assert caps.degradation == {}


def test_capabilities_frozen():
    caps = Capabilities(tools=True)
    try:
        caps.tools = False
        assert False
    except AttributeError:
        pass


def test_heuristic_tokenizer():
    tokenizer = HeuristicTokenizer()
    assert tokenizer.count_tokens("") == 0
    assert tokenizer.count_tokens("x") == 1

    prose = "hello world " * 100
    assert tokenizer.count_tokens(prose) == math.ceil(len(prose) / 3.7)
    assert tokenizer.count_tokens(prose, code=True) == math.ceil(len(prose) / 2.9)
    assert isinstance(tokenizer, Tokenizer)
    assert DEFAULT_TOKENIZER.count_tokens("abc") >= 1
