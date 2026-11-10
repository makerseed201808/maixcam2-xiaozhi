import json
import threading
import unittest
from unittest.mock import Mock, patch, ANY

from test_mcp_hardware import service, Socket


class SiotMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset = patch.object(service, '_siot_api', None)
        reset.start()
        self.addCleanup(reset.stop)

    async def test_schema_and_dispatch_keep_network_off_event_loop(self):
        names = {tool.name: tool for tool in service.MCP_TOOLS}
        for name in ('get_status', 'publish', 'read_latest', 'list_topics', 'get_history',
                     'subscribe', 'unsubscribe', 'list_subscriptions', 'create_topic',
                     'clear_history', 'delete_topic', 'media.get_status', 'media.start',
                     'media.stop', 'media.show', 'device.list_devices', 'device.command'):
            self.assertIn('self.siot.' + name, names)
        schema = names['self.siot.publish'].to_json()['inputSchema']
        self.assertEqual(schema['required'], ['topic', 'message'])
        threads = []
        backend = Mock()
        def publish(**args):
            threads.append(threading.get_ident())
            return {'status': 'local_sent', 'topic': args['topic']}
        backend.publish.side_effect = publish
        with patch.object(service, '_get_siot_tools', return_value=backend):
            result = json.loads(await names['self.siot.publish'].call(
                {'topic': 'siot/test', 'message': 'hello'}))
        self.assertFalse(result['isError'])
        self.assertNotEqual(threads, [threading.get_ident()])
        backend.publish.assert_called_once_with(topic='siot/test', message='hello', save=False, cancel_event=ANY)

    async def test_dispatch_redacts_topic_and_message(self):
        ws = Socket()
        server = service.McpServer(ws, lambda: 'test')
        backend = Mock()
        backend.publish.return_value = {'status': 'local_sent'}
        with patch.object(service, '_get_siot_tools', return_value=backend), \
                patch.object(service, 'log') as log, \
                patch.object(service, 'feedback'):
            await server.parse_message({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'self.siot.publish', 'arguments': {
                    'topic': 'siot/private-topic', 'message': 'private-message'}}})
        self.assertNotIn('private-message', str(log.call_args_list))
        self.assertNotIn('private-topic', str(log.call_args_list))
        self.assertFalse(ws.messages[-1]['payload']['result']['isError'])
        await server.close()

    async def test_missing_message_is_rejected_before_network(self):
        tool = next(t for t in service.MCP_TOOLS if t.name == 'self.siot.publish')
        with patch.object(service, '_get_siot_tools') as backend:
            result = json.loads(await tool.call({'topic': 'siot/test'}))
        self.assertTrue(result['isError'])
        backend.assert_not_called()

    async def test_status_distinguishes_server_topics_from_local_subscriptions(self):
        import siot_topic_catalog
        backend = Mock()
        backend.get_status.return_value = {'connected': True, 'local_subscription_count': 0}
        catalog = Mock()
        catalog.list_topics.return_value = {'total': 12}
        with patch.object(service, '_get_siot_tools', return_value=backend), \
                patch.object(siot_topic_catalog, 'SiotTopicCatalog', return_value=catalog):
            result = await service._siot_callback('get_status')()
        self.assertEqual(result['local_subscription_count'], 0)
        self.assertEqual(result['server_topics_total'], 12)
        self.assertTrue(result['server_catalog_available'])

    async def test_catalog_unavailable_is_not_zero_server_topics(self):
        import siot_topic_catalog
        backend = Mock()
        backend.get_status.return_value = {'connected': True, 'local_subscription_count': 0}
        catalog = Mock()
        catalog.list_topics.side_effect = RuntimeError('unavailable')
        with patch.object(service, '_get_siot_tools', return_value=backend), \
                patch.object(siot_topic_catalog, 'SiotTopicCatalog', return_value=catalog):
            result = await service._siot_callback('get_status')()
        self.assertIsNone(result['server_topics_total'])
        self.assertFalse(result['server_catalog_available'])

    async def test_real_tool_inventory_paginates_without_loss(self):
        ws = Socket()
        server = service.McpServer(ws, lambda: 'inventory')
        cursor = ''
        names = []
        for page in range(10):
            await server.parse_message({'jsonrpc': '2.0', 'id': page, 'method': 'tools/list',
                                       'params': {'cursor': cursor}})
            packet = ws.messages[-1]
            self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False).encode()), 8000)
            result = packet['payload']['result']
            names.extend(t['name'] for t in result['tools'])
            cursor = result.get('nextCursor', '')
            if not cursor:break
        self.assertEqual(len(names), 50)          # 46 + 4 个显微镜工具（feature/microscope）
        self.assertEqual(len(set(names)), 50)
        self.assertEqual(sum(n.startswith('self.siot.') for n in names), 17)
        self.assertEqual(sum(n.startswith('self.microscope.') for n in names), 4)
        await server.close()
