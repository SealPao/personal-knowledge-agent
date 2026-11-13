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

if __name__=='__main__': unittest.main()
