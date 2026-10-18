import json

import httpx
import pytest

from assistant import client as client_module


def reply(content, status=200):
    return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})


@pytest.fixture
def openrouter(monkeypatch):
    """Replace the network with a scripted fake OpenRouter.

    Set `fake.responses` to a list of httpx.Response objects or exceptions,
    consumed in order. Every request is recorded in `fake.requests`.
    """

    class Fake:
        responses: list = []
        requests: list = []

        def handler(self, request):
            self.requests.append(json.loads(request.content))
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        @property
        def models_called(self):
            return [r["model"] for r in self.requests]

    fake = Fake()
    fake.responses, fake.requests = [], []
    real_client = httpx.Client
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(client_module.httpx, "Client", lambda: real_client(transport=transport))
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    return fake
