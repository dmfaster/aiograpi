import unittest
from unittest.mock import AsyncMock

from aiograpi import Client


class ClientLifecycleRegressionTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_aclose_closes_every_transport_once(self):
        client = Client()
        client.public._close = AsyncMock()
        client.private._close = AsyncMock()
        client.graphql._close = AsyncMock()

        await client.aclose()

        client.public._close.assert_awaited_once_with()
        client.private._close.assert_awaited_once_with()
        client.graphql._close.assert_awaited_once_with()

    async def test_async_context_manager_closes_client(self):
        client = Client()
        client.aclose = AsyncMock()

        async with client as entered:
            self.assertIs(entered, client)

        client.aclose.assert_awaited_once_with()
