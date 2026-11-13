import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
import core,document_ui,import_history,package_service,workspace

class ImportHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.root=Path(self.tmp.name).resolve()
        self.source=self.root/'source'; self.source.mkdir(); self.manual=self.source/'manual.txt'; self.manual.write_text('原始資料',encoding='utf8'); self.before=self.manual.read_bytes()
        self.db=self.root/'data'/'knowledge.sqlite3'; self.db.parent.mkdir(); core.scan(str(self.source),str(self.db))
        self.incoming=self.root/'incoming.txt'; self.incoming.write_text('重試後可以搜尋的文字',encoding='utf8'); self.incoming_before=self.incoming.read_bytes()
    def tearDown(self): self.tmp.cleanup()
    def failed_batch(self):
        with import_history.ImportHistory(self.db) as history:
            batch=history.create([self.incoming])
            with patch.object(core,'parse',side_effect=ValueError('模擬解析失敗')):
                with import_history.batch_lock(self.db,batch):
                    document_ui.import_batch(self.db,[self.incoming],on_item=lambda i,item:history.update(batch,i,item))
            items=history.load(batch)
        self.assertEqual(items[0]['state'],'副本已保存，解析失敗'); return batch,items
    def test_reopen_and_retry_same_copy(self):
        batch,items=self.failed_batch(); target=Path(items[0]['target']); original_open=open
        def guarded(path,*args,**kwargs):
            if str(path)==str(self.incoming): raise AssertionError('重試已保存副本不應讀取原始檔')
            return original_open(path,*args,**kwargs)
        with import_history.ImportHistory(self.db) as history,import_history.batch_lock(self.db,batch),patch('builtins.open',side_effect=guarded):
            reopened=history.load(batch)
            document_ui.retry_import_batch(self.db,reopened,on_item=lambda i,item:history.update(batch,i,item))
        with import_history.ImportHistory(self.db) as history:
            after=history.load(batch); self.assertEqual(after[0]['state'],'匯入成功'); self.assertEqual(after[0]['target'],str(target))
            self.assertEqual(len(history.batches()),1)
            self.assertEqual([event[2] for event in history.events(batch)],['副本已保存，解析失敗','匯入成功'])
        self.assertEqual(self.incoming.read_bytes(),self.incoming_before); self.assertEqual(self.manual.read_bytes(),self.before)
    def test_different_process_can_read_and_retry_history(self):
        batch,items=self.failed_batch()
        script=('import sys,import_history,document_ui; '
                'db,batch=sys.argv[1:]; '
                'h=import_history.ImportHistory(db); '
                'items=h.load(batch); '
                'document_ui.retry_import_batch(db,items,on_item=lambda i,x:h.update(batch,i,x)); '
                'print(h.load(batch)[0]["state"]); h.db.close()')
        result=subprocess.run([sys.executable,'-c',script,str(self.db),batch],cwd=str(Path(__file__).parent),capture_output=True,timeout=30,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace')); self.assertIn('匯入成功',result.stdout.decode('utf8'))
        with import_history.ImportHistory(self.db) as history: self.assertEqual(history.load(batch)[0]['target'],items[0]['target'])
    def test_interrupted_batch_retains_pending_items(self):
        other=self.root/'other.txt'; other.write_text('其他內容',encoding='utf8')
        with import_history.ImportHistory(self.db) as history:
            batch=history.create([self.incoming,other])
            def save_then_interrupt(index,item):
                history.update(batch,index,item); raise RuntimeError('模擬中斷')
            with self.assertRaises(RuntimeError): document_ui.import_batch(self.db,[self.incoming,other],on_item=save_then_interrupt)
        with import_history.ImportHistory(self.db) as history:
            items=history.load(batch); self.assertEqual([x['state'] for x in items],['匯入成功','未處理'])
            document_ui.retry_import_batch(self.db,items,on_item=lambda i,item:history.update(batch,i,item))
            self.assertEqual([x['state'] for x in history.load(batch)],['匯入成功','匯入成功'])
    def test_abrupt_process_exit_keeps_committed_item_and_releases_lock(self):
        other=self.root/'other.txt'; other.write_text('另一份內容',encoding='utf8')
        with import_history.ImportHistory(self.db) as history: batch=history.create([self.incoming,other])
        script='''import os,sys,import_history,document_ui
db,batch,first,second=sys.argv[1:]
with import_history.batch_lock(db,batch),import_history.ImportHistory(db) as history:
 def checkpoint(index,item):
  history.update(batch,index,item)
  os._exit(23)
 document_ui.import_batch(db,[first,second],on_item=checkpoint)
'''
        result=subprocess.run([sys.executable,'-c',script,str(self.db),batch,str(self.incoming),str(other)],cwd=str(Path(__file__).parent),capture_output=True,timeout=30)
        self.assertEqual(result.returncode,23,result.stderr.decode('utf8','replace'))
        with import_history.batch_lock(self.db,batch),import_history.ImportHistory(self.db) as history:
            items=history.load(batch); self.assertEqual([x['state'] for x in items],['匯入成功','未處理'])
            document_ui.retry_import_batch(self.db,items,on_item=lambda i,item:history.update(batch,i,item))
            self.assertEqual([x['state'] for x in history.load(batch)],['匯入成功','匯入成功'])
        self.assertEqual(self.incoming.read_bytes(),self.incoming_before); self.assertEqual(self.manual.read_bytes(),self.before)
    def test_same_batch_cannot_be_retried_concurrently(self):
        batch,_=self.failed_batch()
        with import_history.batch_lock(self.db,batch):
            with self.assertRaisesRegex(RuntimeError,'另一個'): 
                with import_history.batch_lock(self.db,batch): pass
    def test_multiple_batches_are_preserved_and_ordered(self):
        with import_history.ImportHistory(self.db) as history:
            first=history.create([self.incoming]); second=history.create([self.manual])
            self.assertEqual([row[0] for row in history.batches()],[second,first])
            self.assertEqual(history.load(first)[0]['source'],str(self.incoming))
            self.assertEqual(history.load(second)[0]['source'],str(self.manual))
    def test_selected_retry_leaves_unselected_items_and_sources_untouched(self):
        other=self.root/'other.txt'; other.write_text('另一份操作',encoding='utf8'); other_before=other.read_bytes()
        with import_history.ImportHistory(self.db) as history:
            batch=history.create([self.incoming,other])
            with patch.object(core,'parse',side_effect=ValueError('暫時失敗')):
                document_ui.import_batch(self.db,[self.incoming,other],on_item=lambda i,item:history.update(batch,i,item))
            items=history.load(batch); untouched=dict(items[1]); original_open=open
            def guarded(path,*args,**kwargs):
                if str(path) in (str(other),untouched['target']): raise AssertionError('不可處理未選項目')
                return original_open(path,*args,**kwargs)
            with patch('builtins.open',side_effect=guarded):
                document_ui.retry_import_batch(self.db,items,on_item=lambda i,item:history.update(batch,i,item),positions=[0])
            after=history.load(batch); self.assertEqual(after[0]['state'],'匯入成功'); self.assertEqual(after[1],untouched)
            self.assertEqual([event[2] for event in history.events(batch)],['副本已保存，解析失敗','副本已保存，解析失敗','匯入成功'])
        self.assertEqual(other.read_bytes(),other_before); self.assertEqual(self.manual.read_bytes(),self.before)
    def test_cancel_marks_only_selected_pending_items(self):
        with import_history.ImportHistory(self.db) as history:
            batch=history.create([self.incoming,self.manual]); before=history.load(batch)
            history.cancel_pending(batch,positions=[1]); after=history.load(batch)
            self.assertEqual(after[0],before[0]); self.assertIn('已取消',after[1]['detail'])
            self.assertEqual(len(history.events(batch)),1)
    def test_invalid_selected_positions_rejected(self):
        item={'source':'x','state':'未處理','detail':'','target':''}
        for positions in ([],[-1],[1],[True]):
            with self.subTest(positions=positions),self.assertRaises(ValueError):
                document_ui.retry_import_batch(self.db,[item],positions=positions)
    def test_viewing_pending_history_does_not_open_source(self):
        original_open=open
        def guarded(path,*args,**kwargs):
            if str(path)==str(self.incoming): raise AssertionError('查看匯入紀錄不可讀取來源')
            return original_open(path,*args,**kwargs)
        with patch('builtins.open',side_effect=guarded),import_history.ImportHistory(self.db) as history:
            batch=history.create([self.incoming]); self.assertEqual(history.load(batch)[0]['state'],'未處理')
    def test_existing_database_gets_additive_history_without_changing_documents(self):
        db=core.connect(str(self.db))
        try: before=db.execute('SELECT path,sha256,status FROM documents ORDER BY id').fetchall(); version=db.execute('PRAGMA user_version').fetchone()[0]
        finally: db.close()
        with import_history.ImportHistory(self.db) as history:
            self.assertEqual(history.batches(),[]); self.assertEqual(history.db.execute('SELECT path,sha256,status FROM documents ORDER BY id').fetchall(),before)
            self.assertEqual(history.db.execute('PRAGMA user_version').fetchone()[0],version)
        self.assertEqual(self.manual.read_bytes(),self.before)
    def test_interruption_after_parse_before_history_update_is_idempotent(self):
        batch,items=self.failed_batch(); target=items[0]['target']
        core.retry_import_copy(self.db,target,str(self.incoming))
        with import_history.ImportHistory(self.db) as history:
            unchanged=history.load(batch); self.assertEqual(unchanged[0]['state'],'副本已保存，解析失敗')
            document_ui.retry_import_batch(self.db,unchanged,on_item=lambda i,item:history.update(batch,i,item))
            result=history.load(batch)[0]; self.assertEqual(result['state'],'已有副本'); self.assertEqual(result['target'],target)
        self.assertEqual(len(list((self.db.parent/'imported').glob('*.txt'))),1)
    def test_missing_saved_copy_never_falls_back_to_original(self):
        batch,items=self.failed_batch(); Path(items[0]['target']).unlink()
        with import_history.ImportHistory(self.db) as history:
            loaded=history.load(batch); self.assertTrue(loaded[0]['target_missing'])
            with patch.object(core,'import_copy',side_effect=AssertionError('must not read original')):
                after=document_ui.retry_import_batch(self.db,loaded)
        self.assertTrue(after[0]['target_missing']); self.assertEqual(self.incoming.read_bytes(),self.incoming_before)
    def test_retry_pending_cloud_placeholder_does_not_open_it(self):
        with import_history.ImportHistory(self.db) as history:
            batch=history.create([self.incoming]); items=history.load(batch)
            original_stat=os.stat; original_open=open
            def fake_stat(path,*args,**kwargs):
                value=original_stat(path,*args,**kwargs)
                if str(path)==str(self.incoming): return SimpleNamespace(st_mode=value.st_mode,st_size=value.st_size,st_mtime_ns=value.st_mtime_ns,st_file_attributes=core.PLACEHOLDER)
                return value
            def guarded(path,*args,**kwargs):
                if str(path)==str(self.incoming): raise AssertionError('不可下載或讀取雲端佔位檔')
                return original_open(path,*args,**kwargs)
            with patch.object(core.os,'stat',side_effect=fake_stat),patch('builtins.open',side_effect=guarded):
                result=document_ui.retry_import_batch(self.db,items,on_item=lambda i,item:history.update(batch,i,item))
            self.assertEqual(result[0]['state'],'未完成'); self.assertIn('雲端佔位檔',result[0]['detail'])
    def test_pka_restore_and_portable_move_rebind_copy_reference(self):
        batch,_=self.failed_batch(); archive=self.root/'backup.pka'; package_service.create_package(self.db,archive)
        a=self.root/'machine-a'; parent=workspace.library_parent(a); parent.mkdir(parents=True)
        restored,_,_=package_service.restore_package(archive,parent,self.source); old_db=restored/'data'/'knowledge.sqlite3'
        workspace.activate(a,old_db)
        b=self.root/'machine-b'; shutil.copytree(a,b); moved=workspace.active_database(b)
        with import_history.ImportHistory(moved) as history:
            items=history.load(batch); self.assertTrue(core.contained(items[0]['target'],b)); self.assertFalse(items[0]['target_missing'])
            self.assertEqual([event[2] for event in history.events(batch)],['副本已保存，解析失敗'])
            original_open=open
            def guarded(path,*args,**kwargs):
                if str(path)==str(self.incoming): raise AssertionError('搬移後重試已保存副本不應讀取舊來源')
                return original_open(path,*args,**kwargs)
            with import_history.batch_lock(moved,batch),patch('builtins.open',side_effect=guarded):
                document_ui.retry_import_batch(moved,items,on_item=lambda i,item:history.update(batch,i,item))
            self.assertEqual(history.load(batch)[0]['state'],'匯入成功')
            self.assertEqual([event[2] for event in history.events(batch)],['副本已保存，解析失敗','匯入成功'])
            self.assertEqual(Path(history.load(batch)[0]['target']).read_bytes(),self.incoming_before)
        self.assertEqual(self.manual.read_bytes(),self.before); self.assertEqual(self.incoming.read_bytes(),self.incoming_before)

if __name__=='__main__': unittest.main()
