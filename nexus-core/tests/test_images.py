import base64
from pathlib import Path

import pytest

from nexus.codec import message_from_json, message_to_json
from nexus.images import image_parts
from nexus.llmhub.chat import chat_messages
from nexus.llmhub.responses import ResponsesQuirks, input_items
from nexus.llmhub.types import Image, Message, Request, Text

PNG = (Path(__file__).parent / 'fixtures/clipboard.png').read_bytes()
URL = 'data:image/png;base64,' + base64.b64encode(PNG).decode()


def test_image_replays_in_both_dialects_and_codec():
    message = Message('user', (Text('Describe this'), Image(URL, 'clipboard.png')))
    assert message_from_json(message_to_json(message)) == message
    items = input_items([message], origin='codex', quirks=ResponsesQuirks())
    assert items == [{'type': 'message', 'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'Describe this'}, {'type': 'input_image', 'image_url': URL}]}]
    req = Request('m', '', (message,))
    assert chat_messages(req, origin='copilot') == [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Describe this'}, {'type': 'image_url', 'image_url': {'url': URL}}]}]
    assert image_parts([{'data_url': URL}]) == (Image(URL),)


@pytest.mark.parametrize('images', [
    {}, [None], [{'data_url': 'https://example.com/image.png'}],
    [{'data_url': 'data:image/svg+xml;base64,PHN2Zz4='}],
    [{'data_url': 'data:image/png;base64,!!!'}],
    [{'data_url': 'data:image/jpeg;base64,' + base64.b64encode(PNG).decode()}],
    [{'data_url': URL}] * 9,
])
def test_invalid_images_rejected(images):
    with pytest.raises(ValueError):
        image_parts(images)
