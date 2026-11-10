import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import test_siot_topic_catalog as fixtures

login, topics, module = fixtures.login, fixtures.topics, fixtures.module


def history(content='hello', identity=1, timestamp=1700000000, total=1):
    return dict(code=0, data=dict(messages=[] if total == 0 else [
        dict(id=identity, content=content, timestamp=timestamp)], total=total))


class RestTests(unittest.TestCase):
    setUp = fixtures.CatalogTests.setUp
    save = fixtures.CatalogTests.save
    catalog = fixtures.CatalogTests.catalog
    def test_create_is_idempotent_and_registered_before_return(self):
        catalog = self.catalog([login(), topics(['fixture/test'])])
        result = catalog.ensure_topic('fixture/test')
        self.assertFalse(result['created'])
        self.assertEqual(len(self.opener.requests), 2)
        catalog = self.catalog([login(), topics([]), {'code': 0, 'data': ''}, topics(['fixture/test'])])
        self.assertTrue(catalog.create_topic('fixture/test')['created'])
        write = self.opener.requests[2][0]
        self.assertEqual(write.get_method(), 'PUT')
        self.assertEqual(json.loads(write.data), {'name': 'test'})

    def test_ownership_and_cancellation_block_creation(self):
        catalog = self.catalog([login()])
        with self.assertRaises(RuntimeError): catalog.create_topic('another/test')
        self.assertEqual(len(self.opener.requests), 1)
        cancelled = threading.Event(); cancelled.set()
        catalog = self.catalog([])
        with self.assertRaises(RuntimeError): catalog.create_topic('fixture/test', cancel_event=cancelled)
        self.assertEqual(self.opener.requests, [])

    def test_mutation_unknown_outcome_not_retried(self):
        catalog = self.catalog([login(), topics([]), RuntimeError('SECRET')])
        with self.assertRaises(RuntimeError) as exc: catalog.create_topic('fixture/test')
        self.assertNotIn('SECRET', str(exc.exception))
        self.assertEqual(sum(r.get_method() == 'PUT' for r, _ in self.opener.requests), 1)

    def test_history_has_source_time_and_utf8_truncation(self):
        catalog = self.catalog([login(), history('测' * 2000)])
        result = catalog.get_history('fixture/test', limit=1, start_time=1, end_time=1700000010)
        record = result['messages'][0]
        self.assertTrue(record['truncated'])
        self.assertEqual(record['bytes'], 6000)
        self.assertLessEqual(len(record['message'].encode()), 4096)
        self.assertEqual(record['timestamp'], 1700000000)
        self.assertEqual(record['source'], 'history')
        self.assertEqual(result['order'], 'newest_first_server_contract')
        self.assertEqual(result['order_evidence'], 'supplied_2618_frontend')
        request = self.opener.requests[1][0]
        self.assertEqual(json.loads(request.data), {'topic': 'fixture/test'})
        self.assertIn('startTime=1&endTime=1700000010', request.full_url)

    def test_history_rejects_malformed_data_and_invalid_ranges(self):
        for response in [history(timestamp=float('nan')), history(content=5), history(identity=0),
                         dict(code=0, data=dict(messages=[], total='1'))]:
            with self.assertRaises(RuntimeError): self.catalog([login(), response]).get_history('fixture/test')
        for arguments in [dict(limit=31), dict(offset=-1), dict(start_time=True), dict(start_time=5,end_time=2)]:
            catalog = self.catalog([])
            with self.assertRaises(ValueError): catalog.get_history('fixture/test', **arguments)
            self.assertEqual(self.opener.requests, [])

    def test_delete_requires_preparation_revalidates_id_and_single_use(self):
        catalog = self.catalog([login(), topics(['fixture/test']), login(), topics(['fixture/test']),
                                {'code': 0, 'data': None}, topics([])])
        ready = catalog.prepare_change('delete', 'fixture/test')
        self.assertTrue(ready['confirmation_required'])
        self.assertEqual(len(self.opener.requests), 2)
        result = catalog.confirm_change(ready['confirmation_token'])
        self.assertEqual(result['status'], 'deleted')
        self.assertEqual(self.opener.requests[4][0].get_method(), 'DELETE')
        with self.assertRaises(ValueError): catalog.confirm_change(ready['confirmation_token'])

    def test_topic_replacement_blocks_confirmation(self):
        replaced = topics(['fixture/test']); replaced['data']['topics'][0]['id'] = 7
        catalog = self.catalog([login(), topics(['fixture/test']), login(), replaced])
        token = catalog.prepare_change('delete', 'fixture/test')['confirmation_token']
        with self.assertRaises(RuntimeError): catalog.confirm_change(token)
        self.assertFalse(any(r.get_method() == 'DELETE' for r, _ in self.opener.requests))

    def test_expiration_and_unknown_tokens_do_not_write(self):
        catalog = self.catalog([login(), topics(['fixture/test'])])
        with patch.object(module.time, 'monotonic', return_value=10):
            token = catalog.prepare_change('clear', 'fixture/test')['confirmation_token']
        with patch.object(module.time, 'monotonic', return_value=131):
            with self.assertRaises(ValueError): catalog.confirm_change(token)
        with self.assertRaises(ValueError): catalog.confirm_change('random')
        self.assertEqual(len(self.opener.requests), 2)

    def test_server_change_and_cancel_consume_confirmation_without_write(self):
        catalog = self.catalog([login(), topics(['fixture/test']), login()])
        token = catalog.prepare_change('delete', 'fixture/test')['confirmation_token']
        self.config['host'] = 'other'; self.save()
        with self.assertRaises(RuntimeError): catalog.confirm_change(token)
        self.assertEqual(len(self.opener.requests), 3)
        self.config['host'] = 'localhost'; self.save()
        catalog = self.catalog([login(), topics(['fixture/test'])])
        token = catalog.prepare_change('clear', 'fixture/test')['confirmation_token']
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(RuntimeError): catalog.confirm_change(token, cancel_event=cancel)
        with self.assertRaises(ValueError): catalog.confirm_change(token)

    def test_clear_keeps_topic_and_verifies_empty_history(self):
        catalog = self.catalog([login(), topics(['fixture/test']), login(), topics(['fixture/test']),
            {'code': 0, 'data': ''}, topics(['fixture/test']), history(total=0)])
        token = catalog.prepare_change('clear', 'fixture/test')['confirmation_token']
        result = catalog.confirm_change(token)
        self.assertEqual((result['status'], result['topic_id']), ('cleared', 1))
        self.assertEqual(self.opener.requests[4][0].get_method(), 'POST')
        self.assertTrue(self.opener.requests[4][0].full_url.endswith('/messages/delMsgByTopic'))

    def test_new_messages_after_clear_are_uncertain_not_automatically_recleared(self):
        catalog = self.catalog([login(), topics(['fixture/test']), login(), topics(['fixture/test']),
            {'code': 0, 'data': ''}, topics(['fixture/test']), history('new message', identity=2)])
        token = catalog.prepare_change('clear', 'fixture/test')['confirmation_token']
        with self.assertRaises(RuntimeError): catalog.confirm_change(token)
        self.assertEqual(sum('/delMsgByTopic' in r.full_url for r, _ in self.opener.requests), 1)

    def test_local_http_transport_matches_catalog_without_total(self):
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                observed.append((self.path, body))
                self.respond(login())
            def do_GET(self):
                observed.append((self.path, self.headers.get('authorization')))
                self.respond({'code': 0, 'data': {'topics': [
                    {'id': 1, 'name': 'fixture/one'}, {'id': 2, 'name': 'fixture/two'}]}})
            def respond(self, body):
                raw = json.dumps(body).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        runner = threading.Thread(target=server.serve_forever, daemon=True); runner.start()
        try:
            self.config.update(host='127.0.0.1', http_port=server.server_address[1]); self.save()
            result = module.SiotTopicCatalog(str(self.path)).list_topics(limit=1, offset=1)
            self.assertEqual((result['topics'], result['total']), (['fixture/two'], 2))
            self.assertEqual(observed[0][0], '/api/v2/login')
            self.assertEqual(observed[1], ('/api/v2/topics', 'SECRET-test-token'))
        finally:
            server.shutdown(); server.server_close(); runner.join(timeout=2)


if __name__ == '__main__': unittest.main()
