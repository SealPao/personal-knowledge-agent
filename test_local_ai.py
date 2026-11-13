import hashlib
import io
import json
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
        def response(request,timeout):
            url=request.full_url; calls.append(url)
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
    def test_cancel_preserves_partial_for_resume(self):
        files=(('model.gguf',4,hashlib.sha256(b'good').hexdigest()),)
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai,'FILES',files),patch.object(local_ai.urllib.request,'urlopen',return_value=io.BytesIO(b'good')):
            root=Path(td)
            with self.assertRaises(RuntimeError): local_ai.download(root=root,cancelled=lambda:True)
            self.assertTrue((root/'models'/'model.gguf.download').exists())
    def test_resume_uses_range_and_verifies_full_contents(self):
        files=(('model.gguf',8,hashlib.sha256(b'abcdefgh').hexdigest()),)
        class Response(io.BytesIO):
            status=206
            headers={'Content-Range':'bytes 3-7/8'}
        def response(request,timeout):
            self.assertEqual(request.get_header('Range'),'bytes=3-')
            return Response(b'defgh')
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai,'FILES',files),patch.object(local_ai.urllib.request,'urlopen',side_effect=response):
            root=Path(td); (root/'models').mkdir(); (root/'models'/'model.gguf.download').write_bytes(b'abc')
            local_ai.download(root=root)
            self.assertEqual((root/'models'/'model.gguf').read_bytes(),b'abcdefgh')
    def test_bad_range_never_appends_to_partial(self):
        files=(('model.gguf',8,hashlib.sha256(b'abcdefgh').hexdigest()),)
        class Response(io.BytesIO):
            status=206
            headers={'Content-Range':'bytes 0-4/8'}
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai,'FILES',files),patch.object(local_ai.urllib.request,'urlopen',return_value=Response(b'abcde')):
            root=Path(td); (root/'models').mkdir(); part=root/'models'/'model.gguf.download'; part.write_bytes(b'abc')
            with self.assertRaises(RuntimeError): local_ai.download(root=root)
            self.assertEqual(part.read_bytes(),b'abc')
    def test_server_ignoring_range_restarts_safely(self):
        files=(('model.gguf',8,hashlib.sha256(b'abcdefgh').hexdigest()),)
        class Response(io.BytesIO):
            status=200
            headers={}
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai,'FILES',files),patch.object(local_ai.urllib.request,'urlopen',return_value=Response(b'abcdefgh')):
            root=Path(td); (root/'models').mkdir(); (root/'models'/'model.gguf.download').write_bytes(b'abc')
            local_ai.download(root=root)
            self.assertEqual((root/'models'/'model.gguf').read_bytes(),b'abcdefgh')
    def test_download_lock_rejects_concurrent_request(self):
        with tempfile.TemporaryDirectory() as td:
            with local_ai._download_file_lock(Path(td)):
                with self.assertRaisesRegex(RuntimeError,'另一個 APP'):
                    with local_ai._download_file_lock(Path(td)): pass
    def test_cancel_model_start_terminates_process(self):
        class Process:
            terminated=False
            def poll(self): return None
            def terminate(self): self.terminated=True
            def wait(self,timeout): return 0
        proc=Process(); calls=iter((False,True))
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai.subprocess,'Popen',return_value=proc):
            root=Path(td); (root/'runtime').mkdir(); (root/'models').mkdir()
            (root/'runtime'/'llama-server.exe').touch()
            for name,_,_ in local_ai.FILES: (root/'models'/name).touch()
            with self.assertRaises(local_ai.StartCancelled): local_ai.start(root=root,cancelled=lambda:next(calls))
            self.assertTrue(proc.terminated); self.assertFalse(local_ai.running())
    def test_model_status_reports_partial_and_ready_without_reading_contents(self):
        files=(('model.gguf',8,'unused'),('vision.gguf',4,'unused'))
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai,'FILES',files):
            root=Path(td); (root/'models').mkdir(); (root/'runtime').mkdir(); (root/'runtime'/'llama-server.exe').touch()
            (root/'models'/'model.gguf.download').write_bytes(b'abc')
            state=local_ai.model_status(root); self.assertFalse(state['ready']); self.assertEqual(state['present'],3); self.assertTrue(state['runtime'])
            (root/'models'/'model.gggguf').write_bytes(b'ignored')
            (root/'models'/'model.gguf').write_bytes(b'abcdefgh'); (root/'models'/'vision.gguf').write_bytes(b'abcd')
            self.assertTrue(local_ai.model_status(root)['ready'])
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
            return {'choices':[{'message':{'content':'{"answer":"設定在系統選單","citations":[1],"evidence":[{"source_id":1,"quote":"設定在系統選單"}]}'}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,citations,_=ai_service.answer('設定在哪裡',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertIn('[1]',text); self.assertIn('供核對的來源片段文字',text); self.assertIn('設定在系統選單',text); self.assertEqual(citations,sources)
    def test_builtin_retries_and_rejects_fabricated_quote(self):
        sources=[{'id':1,'excerpt':'進入設定頁面'}]; calls=[]
        def post(url,headers,payload):
            calls.append(1)
            return {'choices':[{'message':{'content':'{"answer":"完成","citations":[1],"evidence":[{"source_id":1,"quote":"已執行重置"}]}'}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            with self.assertRaisesRegex(RuntimeError,'來源原文'):
                ai_service.answer('問題',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertEqual(len(calls),2)
    def test_quote_must_match_the_corresponding_source(self):
        sources=[{'id':1,'excerpt':'第一台長按'},{'id':2,'excerpt':'第二台短按'}]
        raw='{"answer":"操作 [1]","citations":[1],"evidence":[{"source_id":1,"quote":"第二台短按"}]}'
        with self.assertRaises(ValueError): ai_service._checked_evidence(raw,sources)
    def test_builtin_retries_then_displays_verified_excerpt(self):
        sources=[{'id':1,'excerpt':'請長按電源鍵以關機。'}]; calls=[]
        def post(url,headers,payload):
            calls.append(1)
            quote='按一下電源鍵' if len(calls)==1 else '長按電源鍵以關機'
            content=json.dumps({'answer':'長按電源鍵 [1]','citations':[1],'evidence':[{'source_id':1,'quote':quote}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,cited,_=ai_service.answer('如何關機',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertEqual(len(calls),2); self.assertIn('長按電源鍵以關機',text); self.assertNotIn('按一下電源鍵',text); self.assertEqual(cited,sources)
    def test_number_in_source_quote_is_not_treated_as_citation(self):
        sources=[{'id':1,'excerpt':'原文標記 [99] 為文件編號'}]
        def post(url,headers,payload):
            content=json.dumps({'answer':'文件編號見來源 [1]','citations':[1],'evidence':[{'source_id':1,'quote':'原文標記 [99] 為文件編號'}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,cited,_=ai_service.answer('文件編號',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertIn('[99]',text); self.assertEqual(cited,sources)
    def test_warns_when_long_press_or_selection_is_omitted(self):
        sources=[{'id':1,'excerpt':'長按電源鍵，選擇關機。'}]
        def post(url,headers,payload):
            content=json.dumps({'answer':'按下電源鍵 [1]','citations':[1],'evidence':[{'source_id':1,'quote':'長按電源鍵，選擇關機。'}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,_,_=ai_service.answer('如何關機',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertIn('回答可能遺漏',text); self.assertIn('長按',text); self.assertIn('選擇',text)

if __name__=='__main__': unittest.main()
