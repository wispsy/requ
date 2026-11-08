import asyncio
import unittest
from requ import AsyncClient, ClientClosedError, encode_json
import test_transport


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        test_transport.TransportTests.setUpClass()
        cls.base = test_transport.TransportTests.base

    @classmethod
    def tearDownClass(cls):
        test_transport.TransportTests.tearDownClass()

    async def test_async_methods_reuse_pool_and_close(self):
        async with AsyncClient(max_connections=1) as client:
            first = (await client.get(self.base)).json()['port']
            for method in ('post', 'put', 'patch', 'delete'):
                response = await getattr(client, method)(self.base, json={'x': 1})
                self.assertEqual(response.json()['port'], first)
                self.assertEqual(response.json()['method'], method.upper())
            self.assertEqual((await client.head(self.base)).body, b'')
        with self.assertRaises(ClientClosedError):
            await client.get(self.base)
        await client.aclose()

    async def test_cancelled_request_keeps_lease_until_worker_finishes(self):
        server = test_transport.TransportTests.server
        server.entered.clear()
        server.release.clear()
        client = AsyncClient(max_connections=1)
        task = asyncio.create_task(client.post(self.base + '/hold', json={'x': 1}))
        try:
            self.assertTrue(await asyncio.to_thread(server.entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await client.aclose()
            with self.assertRaises(ClientClosedError):
                await client.get(self.base)
        finally:
            server.release.set()
            await client.aclose()
            # IsolatedAsyncioTestCase joins its executor before closing the loop.

    def test_outbound_json_does_not_coerce_keys_or_collections(self):
        for value in ({1: 'x'}, (1, 2), {'nested': {None: True}}):
            with self.assertRaises(TypeError):
                encode_json(value)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(ValueError):
            encode_json(cyclic)
        self.assertEqual(encode_json({'x': [None, True, 1.5]}), b'{"x":[null,true,1.5]}')
