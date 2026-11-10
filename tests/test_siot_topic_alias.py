import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
from test_siot_api import SiotApi


class AliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'config.json'
        self.path.write_text(json.dumps({'username':'alice'}))
        self.mqtt=Mock();self.rest=Mock();self.media=Mock()
        self.api=SiotApi(lambda:self.mqtt,self.rest,self.media,config_path=self.path)

    def test_short_and_explicit_names(self):
        self.assertEqual(self.api.resolve_topic('测试'),'alice/测试')
        self.assertEqual(self.api.resolve_topic('siot/demo'),'siot/demo')
        self.assertEqual(self.api.resolve_topic('alice/demo'),'alice/demo')
        for value in ('', '#', 'a/+', '\n', '$SYS'):
            with self.assertRaises(ValueError):self.api.resolve_topic(value)

    def test_all_topic_routes_and_media(self):
        for method in ('publish','subscribe','unsubscribe'):
            kw={'topic':'demo'}
            if method=='publish':kw['message']='ok'
            self.api.call(method,kw)
            self.assertEqual(getattr(self.mqtt,method).call_args.kwargs['topic'],'alice/demo')
        for method in ('create_topic','get_history'):
            self.api.call(method,{'topic':'demo'})
            self.assertEqual(getattr(self.rest,method).call_args.kwargs['topic'],'alice/demo')
        self.api.call('media.show',{'source':'topic','target':'demo'})
        self.assertEqual(self.media.show.call_args.kwargs['target'],'alice/demo')
        self.api.call('media.start',{'topic':'demo'})
        self.assertEqual(self.media.start.call_args.kwargs['topic'],'alice/demo')
        self.api.call('media.show',{'source':'url','target':'http://host/image.jpg'})
        self.assertEqual(self.media.show.call_args.kwargs['target'],'http://host/image.jpg')

    def test_confirmation_uses_canonical_name(self):
        self.rest.prepare_change.return_value={'confirmation_token':'token','topic':'alice/demo'}
        self.rest.confirm_change.return_value={'verified':True}
        self.api.call('delete_topic',{'topic':'demo'},scope='session')
        self.rest.prepare_change.assert_called_once_with('delete','alice/demo')
        self.api.call('delete_topic',{'topic':'alice/demo','confirmation_token':'token'},scope='session')
        self.rest.confirm_change.assert_called_once()

    def test_missing_config_does_not_guess_account(self):
        self.path.unlink()
        with self.assertRaises(ValueError):self.api.resolve_topic('demo')
        self.assertEqual(self.api.resolve_topic('explicit/demo'),'explicit/demo')
