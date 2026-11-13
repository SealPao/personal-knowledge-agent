import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import local_ai
import ai_service
import device_agent
from PIL import Image

class LocalModelTests(unittest.TestCase):
    def test_download_verifies_and_resumes_completed_file(self):
        files=(('text.gguf',4,hashlib.sha256(b'text').hexdigest()),('vision.gguf',6,hashlib.sha256(b'vision').hexdigest()))
        calls=[]
        def response(url,timeout):
            calls.append(url)
            return io.BytesIO(b'text' if url.endswith('/text.gguf') else b'vision')
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai,'FILES',files),patch.object(local_ai.urllib.request,'urlopen',side_effect=response):
            root=Path(td); (root/'models').mkdir(); (root/'models'/'text.gguf').write_bytes(b'text')
            local_ai.download(root=root)
            self.assertEqual((root/'models'/'vision.gguf').read_bytes(),b'vision'); self.assertEqual(len(calls),1)
            self.assertIn(local_ai.REVISION,calls[0])
    def test_download_rejects_bad_hash_without_replacing_existing(self):
        files=(('model.gguf',4,hashlib.sha256(b'good').hexdigest()),)
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai,'FILES',files),patch.object(local_ai.urllib.request,'urlopen',return_value=io.BytesIO(b'evil')):
            root=Path(td)
            with self.assertRaises(RuntimeError): local_ai.download(root=root)
            self.assertFalse((root/'models'/'model.gguf').exists()); self.assertFalse((root/'models'/'model.gguf.download').exists())
    def test_cancel_cleans_partial(self):
        files=(('model.gguf',4,hashlib.sha256(b'good').hexdigest()),)
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai,'FILES',files),patch.object(local_ai.urllib.request,'urlopen',return_value=io.BytesIO(b'good')):
            root=Path(td)
            with self.assertRaises(RuntimeError): local_ai.download(root=root,cancelled=lambda:True)
            self.assertFalse((root/'models'/'model.gguf.download').exists())
    def test_auth_only_for_current_local_server(self):
        class Proc:
            def poll(self): return None
        with patch.object(local_ai,'_process',Proc()),patch.object(local_ai,'_base','http://127.0.0.1:1234/v1'),patch.object(local_ai,'_key','secret'):
            self.assertEqual(local_ai.headers_for('http://127.0.0.1:1234/v1/models'),{'Authorization':'Bearer secret'})
            self.assertEqual(local_ai.headers_for('http://127.0.0.1:12345/v1/models'),{})
            self.assertEqual(local_ai.headers_for('https://example.com/v1/models'),{})
    def test_stopped_builtin_model_does_not_send_to_stale_port(self):
        with patch.object(local_ai,'running',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'尚未啟動'):
                ai_service.answer('問題',[{'id':1,'excerpt':'內容'}],'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1')
    def test_device_plan_requires_structured_answer_for_builtin_model(self):
        image=io.BytesIO(); Image.new('RGB',(20,20),'white').save(image,format='PNG'); captured=[]
        def post(url,headers,payload):
            captured.append(payload)
            return {'choices':[{'message':{'content':'{"type":"finish","reason":"完成","observation":"設定","answer":"找到設定"}'}}]}
        config={'provider':'local','local_model':local_ai.MODEL,'local_base':'http://127.0.0.1:1234/v1','max_tokens':800}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            plan,_=device_agent.plan(image.getvalue(),'找設定',[],config,post=post)
        self.assertEqual(plan['answer'],'找到設定')
        self.assertIn('answer',captured[0]['response_format']['schema']['required'])
    def test_builtin_answer_only_accepts_provided_citation_ids(self):
        sources=[{'id':1,'excerpt':'設定在系統選單'}]
        def post(url,headers,payload):
            return {'choices':[{'message':{'content':'{"answer":"設定在系統選單","citations":[1]}'}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,citations,_=ai_service.answer('設定在哪裡',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertIn('[1]',text); self.assertEqual(citations,sources)

if __name__=='__main__': unittest.main()
