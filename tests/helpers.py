"""Utilitaires partagés des tests (client HTTP asynchrone, fausse WebSocket)."""
from __future__ import annotations

import asyncio

import httpx
from httpx import ASGITransport
from starlette.websockets import WebSocketDisconnect


def run_app(app, coro_fn):
    """Exécute ``coro_fn(client)`` dans le contexte de l'app (lifespan inclus).

    ``coro_fn`` est une coroutine async prenant en argument le
    ``httpx.AsyncClient`` prêt à l'emploi (transport ASGI, pas de réseau réel).
    """
    async def _inner():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                return await coro_fn(client)

    return asyncio.run(_inner())


def run_async(coro):
    """Lance une coroutine dans l'événement loop courant (encapsule asyncio.run)."""
    return asyncio.run(coro)


class FakeWebSocket:
    """Double minimal de ``starlette.websockets.WebSocket`` pour ``ws_status``.

    ``messages`` : pile de messages renvoyés par ``receive_text`` dans l'ordre ;
    une fois épuisée, ``receive_text`` lève ``WebSocketDisconnect``.
    """

    def __init__(self, messages=()):
        self.accepted = False
        self.closed = None
        self.sent = []
        self._messages = list(messages)

    async def close(self, code=1000):
        self.closed = code

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_text(self):
        if self._messages:
            return self._messages.pop(0)
        raise WebSocketDisconnect()
