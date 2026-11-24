import asyncio
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from test_mcp_hardware import service, Socket
from test_siot_tools import module, Client


class TrackedTools(module.SiotTools):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.publish_finished = threading.Event()

    def publish(self, *args, **kwargs):
        try:
            return super().publish(*args, **kwargs)
        finally:
            self.publish_finished.set()


class SiotCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_before_suback_prevents_delayed_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'monitor.json'
            config.write_text(json.dumps({
                'host': 'localhost', 'username': 'fixture', 'password': 'fixture',
            }), encoding='utf-8')
            client = Client()
            client.ack = False
            tools = TrackedTools(str(config), factory=lambda: client)
            tools.SUBSCRIBE_TIMEOUT = 2
            socket = Socket()
            server = service.McpServer(socket, lambda: 'cancellation-test')
            try:
                with patch.object(service, '_get_siot_tools', return_value=tools), \
                        patch.object(service, 'feedback'):
                    await server.dispatch({
                        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'self.siot.publish', 'arguments': {
                            'topic': 'fixture/control', 'message': 'on',
                        }},
                    })
                    deadline = asyncio.get_running_loop().time() + 1
                    while not client.subscriptions:
                        self.assertLess(asyncio.get_running_loop().time(), deadline,
                                        'Worker did not reach subscription wait')
                        await asyncio.sleep(.005)

                    await server.close()
                    self.assertEqual(client.publications, [])
                    client.on_subscribe(client, None, client.mid,
                                        [types.SimpleNamespace(is_failure=False)], None)
                    finished = await asyncio.to_thread(tools.publish_finished.wait, 1)
                    self.assertTrue(finished, 'Cancelled publish worker did not finish')
                    self.assertEqual(client.publications, [])
                    self.assertEqual(socket.messages, [])
            finally:
                await server.close()
                tools.stop()


if __name__ == '__main__':
    unittest.main()
