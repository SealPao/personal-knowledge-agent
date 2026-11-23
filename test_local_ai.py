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

def claim_response(post):
    # Adapt historical quote fixtures to request-scoped claim IDs.
    def wrapped(url,headers,payload):
        response=post(url,headers,payload)
        content=response['choices'][0]['message']['content']; parsed=json.loads(content)
        if 'items' in parsed: return response
        source_input=json.loads(payload['messages'][1]['content'])
        segments={piece['segment_id']:(source['id'],piece['text']) for source in source_input['sources'] for piece in source['segments']}
        evidence=parsed.get('evidence',[]); selected=[]
        for quote in evidence:
            selected.append(next((i for i,(sid,text) in segments.items() if sid==quote['source_id'] and quote['quote'] in text),999))
        if not selected: selected=parsed.get('evidence_ids',[])
        answer=__import__('re').sub(r'\s*\[\d+\]','',parsed['answer']).strip()
        response['choices'][0]['message']['content']=json.dumps({'items':[{'text':answer,'evidence_ids':selected}]},ensure_ascii=False)
        return response
    return wrapped

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
    def test_existing_model_verification_reports_progress(self):
        files=(('model.gguf',4,hashlib.sha256(b'good').hexdigest()),)
        with tempfile.TemporaryDirectory() as td,patch.object(local_ai,'FILES',files):
            root=Path(td); (root/'models').mkdir(); (root/'models'/'model.gguf').write_bytes(b'good')
            progress=[]; phases=[]
            local_ai.download(lambda done,total:progress.append((done,total)),root=root,phase=phases.append)
            self.assertIn('驗證已存在的模型檔',phases)
            self.assertIn((0,4),progress); self.assertIn((4,4),progress)
    def test_segment_evidence_rejects_unknown_or_wrong_source(self):
        sources=[{'id':1,'excerpt':'進入系統設定後點選畫面。'}, {'id':2,'excerpt':'按下返回鍵。'}]
        content,segments=ai_service._local_segment_input('如何設定畫面',sources)
        self.assertEqual(json.loads(content)['sources'][0]['segments'][0]['text'],sources[0]['excerpt'])
        def raw(ids,evidence): return json.dumps({'answer':'設定 [1]','citations':ids,'evidence_ids':evidence})
        with self.assertRaises(ValueError): ai_service._checked_segment_evidence(raw([1],[999]),segments)
        with self.assertRaises(ValueError): ai_service._checked_segment_evidence(raw([1],[2]),segments)
        body,ids,quotes=ai_service._checked_segment_evidence(raw([1],[1]),segments)
        self.assertEqual(quotes,[(1,sources[0]['excerpt'])]); self.assertEqual(ids,[1])
    def test_local_segments_limit_context_without_modifying_originals(self):
        original='前言'*400+'亮度設定，請選擇亮度。'+'後記'*400
        sources=[{'id':1,'excerpt':original}]
        content,segments=ai_service._local_segment_input('如何調整亮度設定',sources)
        sent=''.join(quote for _,quote in segments.values())
        self.assertLessEqual(len(sent),1200); self.assertIn('亮度設定',sent)
        self.assertTrue(all(part in original for _,part in segments.values())); self.assertEqual(sources[0]['excerpt'],original)
    def test_builtin_uses_segment_ids_without_recopying_quote(self):
        sources=[{'id':1,'excerpt':'長按電源鍵，選擇關機。'}]; calls=[]
        def post(url,headers,payload):
            calls.append(payload)
            content=json.dumps({'answer':'長按電源鍵，選擇關機 [1]','citations':[1],'evidence_ids':[1]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,refs,_=ai_service.answer('如何關機',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=claim_response(post))
        self.assertEqual(len(calls),1); self.assertEqual(refs,sources); self.assertIn(sources[0]['excerpt'],text)
    def test_repeated_irrelevant_text_does_not_hide_relevant_steps(self):
        noise='本文件介紹閱讀器設定與使用說明。'*35
        original=noise+'BOOX 顯示設定：開啟應用程式最佳化，選擇全螢幕顯示。'+noise
        content,segments=ai_service._local_segment_input('BOOX 閱讀器如何讓 APP 全畫面運作？',[{'id':1,'excerpt':original}])
        texts=[text for _,text in segments.values()]
        self.assertTrue(any('選擇全螢幕顯示' in text for text in texts))
        self.assertEqual(sum('本文件介紹閱讀器設定與使用說明。'==t for t in texts),1)
        self.assertTrue(all(t in original for t in texts)); self.assertLess(len(content),len(original))
    def test_claims_require_valid_per_item_evidence(self):
        segments={1:(1,'Era 長按電源鍵。'),2:(2,'Verse 短按電源鍵。')}
        def raw(items): return json.dumps({'items':items},ensure_ascii=False)
        with self.assertRaises(ValueError): ai_service._checked_claims(raw([{'text':'Era 長按','evidence_ids':[]}]),segments)
        with self.assertRaises(ValueError): ai_service._checked_claims(raw([{'text':'Era 長按','evidence_ids':[999]}]),segments)
        with self.assertRaises(ValueError): ai_service._checked_claims(raw([{'text':'Era 長按 [2]','evidence_ids':[1]}]),segments)
        claims=ai_service._checked_claims(raw([{'text':'Era 長按','evidence_ids':[1]},{'text':'Verse 短按','evidence_ids':[2]}]),segments)
        self.assertEqual(claims[0][1],[(1,'Era 長按電源鍵。')]); self.assertEqual(claims[1][1],[(2,'Verse 短按電源鍵。')])
        self.assertTrue(ai_service._claim_warning('長按電源鍵',[(2,'Verse 短按電源鍵。')]))
    def test_same_source_strong_match_repairs_wrong_segment_without_crossing_document(self):
        segments={1:(1,'在快捷選單使用前光滑桿調整亮度。'),2:(1,'注意不要將裝置浸入水中。'),3:(2,'注意不要將裝置浸入水中。')}
        quotes,changed=ai_service._claim_anchor('注意事項：不要將裝置浸入水中。',[segments[1]],segments)
        self.assertTrue(changed); self.assertEqual(quotes,[segments[2]])
        self.assertTrue(ai_service._claim_warning('不要浸水',[segments[1]]))
        self.assertNotIn('限制或警告未在所選原文中確認',ai_service._claim_warning('不要浸水',quotes))
    def test_ambiguous_same_source_does_not_guess_anchor(self):
        segments={1:(1,'請開啟設定。'),2:(1,'不要浸入水中。'),3:(1,'請不要浸入水中。')}
        quotes,changed=ai_service._claim_anchor('不要浸入水中',[segments[1]],segments)
        self.assertFalse(changed); self.assertEqual(quotes,[segments[1]])
    def test_unique_model_name_repairs_wrong_document(self):
        sources=[{'id':1,'path':'PocketBook Era.md'}, {'id':2,'path':'PocketBook Verse.md'}]
        segments={1:(1,'PocketBook Era：長按電源鍵，選擇關機。'),2:(2,'PocketBook Verse：短按電源鍵，選擇關機。')}
        quotes,changed=ai_service._claim_model_anchor('PocketBook Verse：短按電源鍵，選擇關機。',[segments[1]],segments,sources)
        self.assertTrue(changed); self.assertEqual(quotes,[segments[2]])
        quotes,changed=ai_service._claim_model_anchor('PocketBook：短按電源鍵，選擇關機。',[segments[1]],segments,sources)
        self.assertFalse(changed); self.assertEqual(quotes,[segments[1]])
        quotes,changed=ai_service._claim_model_anchor('PocketBook Era 和 Verse 如何關機？',[segments[1]],segments,sources)
        self.assertFalse(changed); self.assertEqual(quotes,[segments[1]])
    def test_heading_or_irrelevant_title_does_not_become_an_answer(self):
        sources=[{'id':1,'path':'BOOX 使用指南.html','excerpt':'BOOX 平板提供前光燈。'}]
        def post(url,headers,payload):
            content=json.dumps({'items':[{'text':'BOOX 使用指南 - E-INK 平板閱讀器','evidence_ids':[1]},{'text':'### AI-SAFE-ANSWER 安全回答範例','evidence_ids':[1]}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            with self.assertRaises(ai_service.EvidenceError):
                ai_service.answer('BOOX 前光怎麼調整',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
    def test_product_titles_do_not_answer_frontlight_question(self):
        title='【新機上市】文石 BOOX Go 10.3 吋 (Gen II) 電子閱讀器 - 白色 [現貨]'
        sources=[{'id':1,'path':'BOOX-Go-10.3-Gen-II.md','excerpt':title}]
        def post(url,headers,payload):
            content=json.dumps({'items':[{'text':title,'evidence_ids':[1]}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            with self.assertRaises(ai_service.EvidenceError):
                ai_service.answer('我要怎麼調整 boox go 10 的前光',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
    def test_echoed_question_does_not_hide_explicit_missing_information(self):
        sources=[{'id':1,'excerpt':'BOOX Go 7 可從設定進入重置頁面。此文件沒有說明重置是否刪除書籍。'}]
        def post(url,headers,payload):
            content=json.dumps({'items':[{'text':'BOOX Go 7 重置會刪除哪些書籍？','evidence_ids':[1]}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,cited,_=ai_service.answer('BOOX Go 7 重置會刪除哪些書籍？',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertIn('此文件沒有說明重置是否刪除書籍',text);self.assertEqual(cited,sources)
        self.assertNotIn('重置會刪除哪些書籍？ [1]',text)
    def test_question_echo_is_not_shown_as_a_claim(self):
        sources=[{'id':1,'excerpt':'PocketBook Era：長按電源鍵，選擇關機。'}]
        q='比較 PocketBook Era 和 Verse 的關機方式'
        def post(url,headers,payload):
            content=json.dumps({'items':[{'text':q,'evidence_ids':[1]},{'text':'長按電源鍵，選擇關機。','evidence_ids':[1]}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,_,_=ai_service.answer(q,sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertNotIn(q,text);self.assertIn('長按電源鍵',text)
    def test_missing_information_claim_has_corresponding_source_sentence(self):
        sources=[{'id':1,'excerpt':'BOOX Go 7 可從設定進入重置頁面。此文件沒有說明重置是否刪除書籍。'}]
        def post(url,headers,payload):
            content=json.dumps({'items':[{'text':'可以進入重置頁面，但文件沒有說明是否刪除書籍。','evidence_ids':[1]}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,_,_=ai_service.answer('BOOX Go 7 重置會刪除哪些書籍？',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertIn('原文 [1]：此文件沒有說明重置是否刪除書籍。',text)
    def test_duplicate_with_introductory_phrase_is_not_repeated(self):
        sources=[{'id':1,'excerpt':'對於文字型 PDF，可以在閱讀選單中選擇文字重新排版，進而調整字體大小。'}]
        def post(url,headers,payload):
            text='對於文字型 PDF，可以在閱讀選單中選擇文字重新排版，進而調整字體大小。'
            content=json.dumps({'items':[{'text':text,'evidence_ids':[1]},{'text':'文件記載：'+text,'evidence_ids':[1]}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,_,_=ai_service.answer('如何調整 PDF 字體？',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertIn('1. ',text);self.assertNotIn('2. ',text)
    def test_extractive_fallback_uses_only_relevant_actionable_original_text(self):
        q='BOOX Go 7 如何調整前光？'
        sources=[{'id':1,'path':'BOOX 手冊.md','kind':'BOOX 通用文件（未確認適用 go7）',
                  'excerpt':'# 前光\nBOOX 前光可以調整。\n從螢幕頂端向下滑動，打開控制中心。\n# Wi-Fi\n開啟設定並選擇 Wi-Fi。'}]
        result=ai_service.extract_relevant_steps(q,sources)
        self.assertIsNotNone(result);text,refs=result
        self.assertIn('尚未確認適用',text);self.assertIn('向下滑動',text)
        self.assertNotIn('Wi-Fi',text);self.assertEqual(refs,sources)
        self.assertIsNone(ai_service.extract_relevant_steps(q,[{'id':2,'path':'x.md','excerpt':'前光介紹。具體操作如下。'}]))
    def test_pdf_extractive_fallback_keeps_relevant_text_and_omits_other_topics(self):
        sources=[{'id':1,'path':'BOOX 手冊.md','kind':'BOOX 通用文件（未確認適用 go7）',
                  'excerpt':'# PDF 閱讀\n文字型 PDF：在閱讀選單選擇文字重新排版，再調整字體大小。\n# 網路\n開啟設定並選擇 Wi-Fi。'}]
        text,refs=ai_service.extract_relevant_steps('BOOX Go 7 如何調整 PDF 字體？',sources)
        self.assertIn('重新排版',text);self.assertNotIn('Wi-Fi',text);self.assertEqual(refs,sources)
    def test_invalid_local_response_uses_relevant_original_without_second_generation(self):
        sources=[{'id':1,'path':'BOOX 手冊.md','kind':'BOOX 通用文件（未確認適用 go7）',
                  'excerpt':'# 前光\nBOOX 前光設定。\n從螢幕頂端向下滑動，打開控制中心。'}];calls=[]
        def post(url,headers,payload):
            calls.append(1);return {'choices':[{'message':{'content':'{invalid'}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,cited,_=ai_service.answer('BOOX Go 7 如何調整前光？',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertEqual(len(calls),1);self.assertIn('向下滑動',text);self.assertEqual(cited,sources)
    def test_multi_fact_claim_gets_matching_sentences_from_same_source(self):
        segments={1:(1,'BOOX Go 7 在文字型 PDF 的閱讀選單選擇文字重新排版，再調整字體大小。'),2:(1,'掃描型 PDF 可能不支援文字重新排版。'),3:(2,'另一台裝置的文字重新排版，再調整字體大小。')}
        body='選擇文字重新排版，再調整字體大小。掃描型 PDF 可能不支援文字重新排版。'
        quotes,added=ai_service._supporting_source_sentences(body,[segments[2]],segments)
        self.assertTrue(added);self.assertIn(segments[1],quotes);self.assertNotIn(segments[3],quotes)
    def test_no_claims_returns_clear_abstention(self):
        sources=[{'id':1,'excerpt':'僅有充電說明。'}]
        def post(url,headers,payload): return {'choices':[{'message':{'content':'{"items":[]}'}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,refs,_=ai_service.answer('如何重置',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=post)
        self.assertIn('不足以支持回答',text); self.assertEqual(refs,[])
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
            text,citations,_=ai_service.answer('設定在哪裡',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=claim_response(post))
        self.assertIn('[1]',text); self.assertIn('原文 [1]',text); self.assertIn('設定在系統選單',text); self.assertEqual(citations,sources)
    def test_builtin_retries_and_rejects_fabricated_quote(self):
        sources=[{'id':1,'excerpt':'進入設定頁面'}]; calls=[]
        def post(url,headers,payload):
            calls.append(1)
            return {'choices':[{'message':{'content':'{"answer":"完成","citations":[1],"evidence":[{"source_id":1,"quote":"已執行重置"}]}'}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            with self.assertRaisesRegex(RuntimeError,'逐項來源'):
                ai_service.answer('問題',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=claim_response(post))
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
            text,cited,_=ai_service.answer('如何關機',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=claim_response(post))
        self.assertEqual(len(calls),2); self.assertIn('長按電源鍵以關機',text); self.assertNotIn('按一下電源鍵',text); self.assertEqual(cited,sources)
    def test_number_in_source_quote_is_not_treated_as_citation(self):
        sources=[{'id':1,'excerpt':'原文標記 [99] 為文件編號'}]
        def post(url,headers,payload):
            content=json.dumps({'answer':'文件編號見來源 [1]','citations':[1],'evidence':[{'source_id':1,'quote':'原文標記 [99] 為文件編號'}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            text,cited,_=ai_service.answer('文件編號',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=claim_response(post))
        self.assertIn('[99]',text); self.assertEqual(cited,sources)
    def test_warns_when_long_press_or_selection_is_omitted(self):
        sources=[{'id':1,'excerpt':'長按電源鍵，選擇關機。'}]
        def post(url,headers,payload):
            content=json.dumps({'answer':'按下電源鍵 [1]','citations':[1],'evidence':[{'source_id':1,'quote':'長按電源鍵，選擇關機。'}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            with self.assertRaises(ai_service.EvidenceError):
                ai_service.answer('如何關機',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=claim_response(post))
    def test_checks_relevant_sentence_even_if_model_quotes_only_part(self):
        sources=[{'id':1,'excerpt':'長按電源鍵，選擇關機。不要浸水。'}]
        missing=ai_service._missing_operation_markers('按下電源鍵並選擇關機',[(1,'電源鍵，選擇關機')],'如何關機',sources)
        self.assertIn('長按',missing); self.assertNotIn('不要',missing)
    def test_unrelated_source_does_not_trigger_marker_warning(self):
        sources=[{'id':1,'excerpt':'長按電源鍵，選擇關機。不要浸水。'}, {'id':2,'excerpt':'不要打開裝置。'}]
        missing=ai_service._missing_operation_markers('長按電源鍵，選擇關機',[(1,'長按電源鍵，選擇關機')],'如何關機',sources)
        self.assertEqual(missing,[])
    def test_warns_when_model_claims_missing_steps_despite_relevant_action(self):
        sources=[{'id':1,'excerpt':'裝置電源。長按電源鍵，選擇關機。'}]
        def post(url,headers,payload):
            content=json.dumps({'answer':'來源並無記載如何關機 [1]','citations':[1],'evidence':[{'source_id':1,'quote':'裝置電源'}]},ensure_ascii=False)
            return {'choices':[{'message':{'content':content}}]}
        with patch.object(local_ai,'headers_for',return_value={'Authorization':'Bearer test'}):
            with self.assertRaises(ai_service.EvidenceError):
                ai_service.answer('如何關機',sources,'local',local_ai.MODEL,base='http://127.0.0.1:1234/v1',post=claim_response(post))
    def test_unrelated_source_does_not_trigger_missing_steps_warning(self):
        sources=[{'id':1,'excerpt':'使用前光亮度滑桿調整。'}]
        self.assertFalse(ai_service._possible_missed_steps('來源未記載重置會否刪除書籍','重置會清除書籍嗎',sources,[1]))
    def test_model_did_not_include_steps_wording_triggers_check(self):
        sources=[{'id':1,'excerpt':'長按電源鍵，選擇關機。'}]
        self.assertTrue(ai_service._possible_missed_steps('來源並未包含具體的關機步驟','如何關機',sources,[1]))

if __name__=='__main__': unittest.main()
