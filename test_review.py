import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from PIL import Image
import adb_research,adb_service,adb_ui,ai_service,core,package_service
from types import SimpleNamespace

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.base=Path(self.tmp.name)
        self.source=self.base/'source'; self.source.mkdir(); self.original=self.source/'manual.txt'; self.original.write_text('原始文件',encoding='utf-8')
        self.db=self.base/'data'/'knowledge.sqlite3'; self.db.parent.mkdir(); core.scan(str(self.source),str(self.db))
        self.result={'query':'品牌：測試；問題：前光指令','text':'前光設定方法需核對。','sources':[{'title':'官方來源','url':'https://example.org/doc'}]}
    def tearDown(self): self.tmp.cleanup()
    def detail(self,path):
        db=core.connect(str(self.db))
        try: return db.execute('SELECT detail FROM documents WHERE path=?',(str(path),)).fetchone()[0]
        finally: db.close()
    def test_review_and_edit_reset_preserves_ai_origin(self):
        card=adb_research.save(self.db,self.result); before=self.original.read_bytes()
        self.assertIn('未經裝置驗證',self.detail(card))
        self.assertIn('未經裝置實測',core.review_ai_card(self.db,card,True))
        self.assertTrue(any('已人工核對' in x['kind'] and '未經裝置實測' in x['kind'] for x in ai_service.retrieve(str(self.db),'前光設定')))
        core.save_card(str(self.db),'研究紀錄','前光設定已修正，但仍需驗證',existing=card)
        self.assertIn('指令未經裝置驗證',self.detail(card))
        con=sqlite3.connect(self.db); rows=con.execute('SELECT previous_detail,new_detail FROM ai_card_reviews').fetchall(); con.close()
        self.assertEqual(len(rows),2); self.assertEqual(self.original.read_bytes(),before)
    def test_review_rejects_external_and_stale_card(self):
        card=adb_research.save(self.db,self.result)
        with self.assertRaises(ValueError): core.review_ai_card(self.db,str(self.original),True)
        Path(card).write_text('changed outside APP',encoding='utf-8')
        with self.assertRaises(ValueError): core.review_ai_card(self.db,card,True)
        self.assertIn('未經裝置驗證',self.detail(card))
    def test_review_history_and_screenshots_survive_package(self):
        buffer=io.BytesIO(); Image.new('RGB',(20,20),'white').save(buffer,format='PNG')
        card=adb_service.save_session(self.db,'探索','閱讀器探索設定路徑',[buffer.getvalue()])
        core.review_ai_card(self.db,card,True); core.review_ai_card(self.db,card,False)
        archive=self.base/'backup.pka'; package_service.create_package(self.db,archive)
        restored,_,_=package_service.restore_package(archive,self.base,self.source)
        db=restored/'data'/'knowledge.sqlite3'; hits=core.search(str(db),'探索設定路徑'); self.assertTrue(hits)
        self.assertEqual(len(adb_service.read_captures(db,hits[0][0])),1)
        con=sqlite3.connect(db); rows=con.execute('SELECT count(*) FROM ai_card_reviews').fetchone()[0]; con.close()
        self.assertEqual(rows,2)
    def test_device_save_as_reviewed_records_audit(self):
        buffer=io.BytesIO(); Image.new('RGB',(20,20),'white').save(buffer,format='PNG')
        card=adb_service.save_session(self.db,'探索','閱讀器畫面步驟',[buffer.getvalue()])
        adb_service.save_session(self.db,'探索','閱讀器畫面步驟',[buffer.getvalue()],reviewed=True,existing=card)
        self.assertEqual(self.detail(card),'使用者校對的 AI 裝置操作紀錄')
        con=sqlite3.connect(self.db); count=con.execute('SELECT count(*) FROM ai_card_reviews').fetchone()[0]; con.close()
        self.assertEqual(count,1)
    def test_saving_device_session_does_not_imply_review(self):
        buffer=io.BytesIO(); Image.new('RGB',(20,20),'white').save(buffer,format='PNG')
        png=buffer.getvalue(); card=adb_service.save_session(self.db,'探索','閱讀器畫面步驟',[png])
        status=SimpleNamespace(set=lambda value: setattr(status,'value',value))
        window=SimpleNamespace(png=png,busy=False,agent_active=False,images=[png],db=self.db,
            titlevar=SimpleNamespace(get=lambda:'探索'),note=SimpleNamespace(get=lambda *args:'閱讀器畫面步驟'),
            session_path=card,master=object(),status=status)
        adb_ui.DeviceWindow.save(window)
        self.assertIn('尚未人工校對',self.detail(card)); self.assertIn('人工審核',status.value)
        self.assertEqual(core.review_ai_card(self.db,card,True),'使用者校對的 AI 裝置操作紀錄')
        self.assertEqual(self.original.read_text(encoding='utf-8'),'原始文件')

if __name__=='__main__': unittest.main()
