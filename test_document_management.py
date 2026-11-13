import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import ai_ui,core,document_ui

class DocumentManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.root=Path(self.tmp.name)
        self.source=self.root/'source'; self.source.mkdir(); self.manual=self.source/'manual.txt'
        self.manual.write_text('原始操作步驟',encoding='utf8'); self.original=self.manual.read_bytes()
        self.db=self.root/'data'/'knowledge.sqlite3'; self.db.parent.mkdir(); core.scan(str(self.source),str(self.db))
    def tearDown(self): self.tmp.cleanup()
    def test_copy_indexed_and_current_derived_without_modifying_source(self):
        db=core.connect(str(self.db))
        try:
            docid,sha=db.execute('SELECT id,sha256 FROM documents WHERE path=?',(str(self.manual),)).fetchone()
            with db:
                db.execute("INSERT INTO pdf_derived(document_id,source_sha256,page,kind,locator,text,updated_at) VALUES(?,?,1,'ocr','第 1 頁 OCR','校對後的充電步驟','now')",(docid,sha))
                db.execute("INSERT INTO pdf_derived(document_id,source_sha256,page,kind,locator,text,updated_at) VALUES(?,?,2,'table','舊版表格','不應包含舊版','now')",(docid,'old-hash'))
        finally: db.close()
        text=document_ui.indexed_card_copy(self.db,self.manual)
        self.assertIn('原始操作步驟',text); self.assertIn('校對後的充電步驟',text); self.assertNotIn('不應包含舊版',text)
        self.assertIn('衍生內容可能有誤',text); self.assertEqual(self.manual.read_bytes(),self.original)
    def test_changed_source_cannot_be_copied(self):
        before=self.manual.stat(); self.manual.write_text('已修改的新內容',encoding='utf8'); os.utime(self.manual,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.assertRaisesRegex(ValueError,'來源已變更'): document_ui.indexed_card_copy(self.db,self.manual)
    def test_ai_note_cannot_be_reclassified_as_user_card_by_copy(self):
        db=core.connect(str(self.db))
        try:
            with db: db.execute('UPDATE documents SET detail=? WHERE path=?',('AI 裝置觀察紀錄（尚未人工校對）',str(self.manual)))
        finally: db.close()
        with self.assertRaisesRegex(ValueError,'不能轉成一般知識卡'): document_ui.indexed_card_copy(self.db,self.manual)
    def test_source_panel_displays_origin_type(self):
        source={'path':str(self.manual),'locator':'第 1 頁','excerpt':'擷取文字','kind':'PDF OCR 衍生文字（使用者校對，可能有誤）'}
        displayed=[]; window=SimpleNamespace(cites=SimpleNamespace(selection=lambda:['0']),sources_by_row=[source],_source_text=displayed.append)
        with patch.object(ai_ui.svc,'source_state',return_value=''): ai_ui.AIWindow.source_select(window)
        self.assertIn('PDF OCR 衍生文字',displayed[0]); self.assertIn('擷取文字',displayed[0])
    def test_only_explicitly_selected_sources_are_sent(self):
        sources=[{'id':1,'excerpt':'甲'},{'id':2,'excerpt':'乙'}]
        chosen=ai_ui.selected_sources(sources,[False,True])
        self.assertEqual(chosen,[sources[1]])
        with self.assertRaises(ValueError): ai_ui.selected_sources(sources,[False,False])
        with self.assertRaises(ValueError): ai_ui.selected_sources(sources,[True])
    def test_batch_reports_success_duplicate_empty_error_and_unsupported(self):
        good=self.root/'good.txt'; good.write_text('正常內容',encoding='utf8')
        empty=self.root/'empty.txt'; empty.write_text('',encoding='utf8')
        broken=self.root/'broken.txt'; broken.write_text('解析測試',encoding='utf8')
        unsupported=self.root/'other.bin'; unsupported.write_bytes(b'data')
        original_parse=core.parse
        def parse(path,data):
            if Path(path).name.endswith('broken.txt'): raise ValueError('模擬解析失敗')
            return original_parse(path,data)
        with patch.object(core,'parse',side_effect=parse):
            items=document_ui.import_batch(self.db,[good,good,empty,broken,unsupported])
        self.assertEqual([x['state'] for x in items],['匯入成功','已有副本','副本已保存，沒有可搜尋文字','副本已保存，解析失敗','未完成'])
        self.assertTrue(Path(items[3]['target']).exists()); self.assertIn('模擬解析失敗',items[3]['detail'])
        self.assertEqual(self.manual.read_bytes(),self.original)
    def test_cancel_marks_remaining_files_unprocessed(self):
        a=self.root/'a.txt'; a.write_text('第一份內容',encoding='utf8')
        b=self.root/'b.txt'; b.write_text('第二份內容',encoding='utf8')
        count=iter((False,True)); items=document_ui.import_batch(self.db,[a,b],cancelled=lambda:next(count))
        self.assertEqual([x['state'] for x in items],['匯入成功','未處理']); self.assertTrue(b.exists())
    def test_retry_reparses_existing_copy_without_reading_or_changing_original(self):
        source=self.root/'bad.txt'; source.write_text('可恢復的操作步驟',encoding='utf8'); before=source.read_bytes()
        with patch.object(core,'parse',side_effect=ValueError('模擬暫時性解析失敗')):
            results=document_ui.import_batch(self.db,[source])
        self.assertEqual(results[0]['state'],'副本已保存，解析失敗'); target=Path(results[0]['target']); copy_before=target.read_bytes()
        original_open=open
        def guarded(path,*args,**kwargs):
            if str(path)==str(source): raise AssertionError('重試不應讀取原始文件')
            return original_open(path,*args,**kwargs)
        with patch('builtins.open',side_effect=guarded): retried=document_ui.retry_import_batch(self.db,results)
        self.assertEqual(retried[0]['state'],'匯入成功'); self.assertEqual(retried[0]['target'],str(target))
        self.assertEqual(target.read_bytes(),copy_before); self.assertEqual(source.read_bytes(),before)
        self.assertEqual(len(list((self.db.parent/'imported').glob('*.txt'))),1)
        self.assertTrue(core.search(str(self.db),'可恢復的操作步驟'))
    def test_retry_rejects_changed_copy_without_creating_new_files(self):
        source=self.root/'bad.txt'; source.write_text('原始內容',encoding='utf8')
        with patch.object(core,'parse',side_effect=ValueError('模擬失敗')): results=document_ui.import_batch(self.db,[source])
        target=Path(results[0]['target']); before=target.stat(); data=target.read_bytes(); target.write_bytes(bytes([data[0]^1])+data[1:]); os.utime(target,ns=(before.st_atime_ns,before.st_mtime_ns))
        changed=target.read_bytes(); retried=document_ui.retry_import_batch(self.db,results)
        self.assertEqual(retried[0]['state'],'未完成'); self.assertIn('已變更',retried[0]['detail'])
        self.assertEqual(target.read_bytes(),changed); self.assertEqual(len(list((self.db.parent/'imported').glob('*.txt'))),1)
    def test_retry_cancellation_preserves_other_outcomes(self):
        previous=[{'source':'a','state':'匯入成功','detail':'ok','target':'a'}, {'source':'b','state':'未處理','detail':'已取消','target':''}]
        self.assertEqual(document_ui.retry_import_batch(self.db,previous,cancelled=lambda:True),previous)

if __name__=='__main__': unittest.main()
