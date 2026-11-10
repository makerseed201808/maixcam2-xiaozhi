import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('siot_topic_catalog', ROOT / 'xiaozhi/service/siot_topic_catalog.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Opener:
    def __init__(self, replies):
        self.replies = replies
        self.requests = []
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception): raise reply
        return io.BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode('utf-8'))


def login(): return dict(code=0, data=dict(token='SECRET-test-token'))
def topics(names, total=None):
    return dict(code=0, data=dict(topics=[dict(name=name, id=i+1, private='hidden') for i, name in enumerate(names)],
                                 total=len(names) if total is None else total))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'monitor.json'
        self.config = dict(host='localhost', username='fixture', password='SECRET-password', http_port=8080)
        self.save()
    def save(self): self.path.write_text(json.dumps(self.config), encoding='utf-8')
    def catalog(self, replies):
        self.opener = Opener(replies)
        return module.SiotTopicCatalog(str(self.path), opener=self.opener)

    def test_existing_server_topics_are_not_local_subscription_count(self):
        result = self.catalog([login(), topics(['fixture/camera', 'fixture/温度'] + ['fixture/t'+str(i) for i in range(7)], 9)]).list_topics(2)
        self.assertEqual(result['topics'], ['fixture/camera', 'fixture/温度'])
        self.assertEqual((result['total'], result['returned'], result['has_more']), (9, 2, True))
        self.assertEqual(result['source'], 'server_catalog')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('hidden', json.dumps(result))
        first, second = self.opener.requests
        self.assertEqual(first[0].get_method(), 'POST')
        self.assertEqual(first[0].full_url, 'http://localhost:8080/api/v2/login')
        self.assertEqual(second[0].full_url, 'http://localhost:8080/api/v2/topics')
        self.assertEqual(second[0].get_header('Authorization'), 'SECRET-test-token')
        self.assertEqual(first[1], 3)
        self.assertEqual(second[1], 3)

    def test_server_prefixed_token_with_space_is_preserved(self):
        self.catalog([dict(code=0,data=dict(token='Bearer fixture-token')),topics(['fixture/t'])]).list_topics()
        self.assertEqual(self.opener.requests[1][0].get_header('Authorization'),'Bearer fixture-token')

    def test_limit_validation_precedes_network(self):
        catalog = self.catalog([])
        for value in [True, 0, 65, 1.5, '20', None]:
            with self.assertRaises(ValueError): catalog.list_topics(value)
        self.assertEqual(self.opener.requests, [])

    def test_empty_catalog_and_transient_token(self):
        catalog = self.catalog([login(), topics([]), login(), topics([])])
        self.assertEqual(catalog.list_topics()['total'], 0)
        self.assertFalse(catalog.list_topics()['has_more'])
        self.assertEqual([r.get_method() for r, _ in self.opener.requests], ['POST', 'GET', 'POST', 'GET'])

    def test_ipv6_host_is_bracketed(self):
        self.config['host'] = '::1'; self.save()
        self.catalog([login(), topics([])]).list_topics()
        self.assertEqual(self.opener.requests[0][0].full_url, 'http://[::1]:8080/api/v2/login')

    def test_bad_host_and_port_are_rejected_before_network(self):
        for bad in ['http://localhost', 'user:pass@host', 'localhost/path', 'localhost:80', ' host', 'host\n', '[::1]/x']:
            self.config['host'] = bad; self.save()
            catalog = self.catalog([])
            with self.assertRaises(RuntimeError): catalog.list_topics()
            self.assertEqual(self.opener.requests, [])
        self.config['host'] = 'localhost'
        for bad in [True, '8080', 0, 65536]:
            self.config['http_port'] = bad; self.save()
            with self.assertRaises(RuntimeError): self.catalog([]).list_topics()

    def test_http_host_override_is_validated(self):
        self.config['http_host'] = 'user:pass@host'; self.save()
        with self.assertRaises(RuntimeError): self.catalog([]).list_topics()

    def test_oversized_config_response_and_malformed_json(self):
        self.path.write_bytes(b' ' * 65537)
        with self.assertRaises(RuntimeError): self.catalog([]).list_topics()
        self.save()
        for reply in [b' ' * (512*1024+1), b'not json', [], {'code': False, 'data': {}}, {'code': 1}]:
            with self.assertRaises(RuntimeError): self.catalog([reply]).list_topics()

    def test_invalid_token_is_not_used_in_header(self):
        for token in ['', 'evil\nheader', 'evil\x00', 5, 'x'*8193]:
            with self.assertRaises(RuntimeError):
                self.catalog([dict(code=0, data=dict(token=token))]).list_topics()
            self.assertEqual(len(self.opener.requests), 1)

    def test_malformed_catalog_does_not_return_partial_results(self):
        invalid = [topics(['good'], 0), topics(['good'], True), topics(['x/#']),
                   topics(['a', 'a']), dict(code=0, data=dict(topics=['bad'], total=1)),
                   dict(code=0, data=dict(topics=[], total=-1))]
        for reply in invalid:
            with self.assertRaises(RuntimeError): self.catalog([login(), reply]).list_topics()
        self.assertEqual(self.catalog([login(), topics(['a', 'b'])]).list_topics(1)['topics'], ['a'])

    def test_supplied_frontend_schema_without_total_and_local_pagination(self):
        payload = topics(['a', 'b', 'c']); del payload['data']['total']
        result = self.catalog([login(), payload]).list_topics(limit=1, offset=1)
        self.assertEqual(result['topics'], ['b'])
        self.assertEqual(result['total'], 3)
        self.assertTrue(result['has_more'])

    def test_invalid_offset_is_rejected_before_network(self):
        catalog = self.catalog([])
        for value in [True, -1, 513, '0']:
            with self.assertRaises(ValueError): catalog.list_topics(offset=value)
        self.assertEqual(self.opener.requests, [])

    def test_errors_never_echo_credentials_or_body(self):
        for exc in [RuntimeError('SECRET-password'), urllib.error.HTTPError('http://SECRET', 401, 'SECRET-token', {}, None)]:
            with self.assertRaises(RuntimeError) as raised: self.catalog([exc]).list_topics()
            self.assertNotIn('SECRET', str(raised.exception))

    def test_redirect_handler_refuses_all_destinations(self):
        handler = module.NoRedirect()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(None, None, 302, 'redirect', {}, 'http://other/steal')


if __name__ == '__main__': unittest.main()
