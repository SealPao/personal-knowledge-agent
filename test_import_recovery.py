import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import core,document_ui,import_history,package_service

class ImportRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.root=Path(self.tmp.name).resolve()
        self.source=self.root/'source'; self.source.mkdir(); self.manual=self.source/'manual.txt'; self.manual.write_text('原始知識',encoding='utf8'); self.original=self.manual.read_bytes()
        self.incoming=self.root/'incoming.txt'; self.incoming.write_text('需要安全復原的文件內容',encoding='utf8'); self.incoming_bytes=self.incoming.read_bytes()
        self.db=self.root/'data'/'knowledge.sqlite3'; self.db.parent.mkdir(); core.scan(str(self.source),str(self.db))
    def tearDown(self): self.tmp.cleanup()
    def crash_import(self,phase):
        with import_history.ImportHistory(self.db) as history: batch=history.create([self.incoming])
        script='''import os,sys,builtins,import_history,document_ui,core
from unittest.mock import patch
db,batch,source,phase=sys.argv[1:]
real_open=builtins.open
class Partial:
 def __init__(self,path): self.file=real_open(path,'xb')
 def __enter__(self): return self
 def __exit__(self,*args): self.file.close()
 def write(self,data):
  self.file.write(data[:max(1,len(data)//2)]); self.file.flush(); os.fsync(self.file.fileno()); os._exit(23)
 def flush(self): self.file.flush()
 def fileno(self): return self.file.fileno()
def intercepted(path,*args,**kwargs):
 if phase=='partial' and str(path).endswith('.tmp') and args and args[0]=='xb': return Partial(path)
 return real_open(path,*args,**kwargs)
def die(*args,**kwargs): os._exit(23)
with import_history.batch_lock(db,batch),import_history.ImportHistory(db) as history:
 with patch('builtins.open',side_effect=intercepted),patch.object(core,'_publish_import_temp',side_effect=die if phase=='before_rename' else core._publish_import_temp),patch.object(core,'_record',side_effect=die if phase=='before_index' else core._record):
  document_ui.import_batch(db,[source],on_item=lambda i,x:history.update(batch,i,x),on_prepare=lambda i,t,p,h,n:history.prepare(batch,i,t,p,h,n))
'''
        result=subprocess.run([sys.executable,'-c',script,str(self.db),batch,str(self.incoming),phase],cwd=str(Path(__file__).parent),capture_output=True,timeout=30)
        self.assertEqual(result.returncode,23,result.stderr.decode('utf8','replace'))
        with import_history.ImportHistory(self.db) as history: item=history.load(batch)[0]
        self.assertTrue(item['planned']); self.assertEqual(item['state'],'副本待復原'); return batch,item
    def test_complete_temp_recovers_without_original_or_duplicate(self):
        batch,item=self.crash_import('before_rename'); self.assertTrue(Path(item['temp']).is_file()); self.assertFalse(Path(item['target']).exists())
        with self.assertRaisesRegex(ValueError,'可復原'): core.cleanup_import_stage(self.db,item['target'],item['temp'],item['expected_sha256'],item['expected_size'])
        original_open=open
        def guard(path,*args,**kwargs):
            if str(path)==str(self.incoming): raise AssertionError('復原不應讀取原始檔')
            return original_open(path,*args,**kwargs)
        with import_history.batch_lock(self.db,batch),import_history.ImportHistory(self.db) as history,patch('builtins.open',side_effect=guard):
            results=document_ui.retry_import_batch(self.db,history.load(batch),on_item=lambda i,x:history.update(batch,i,x),positions=[0])
            self.assertEqual(results[0]['state'],'匯入成功'); self.assertFalse(history.load(batch)[0]['planned'])
        self.assertEqual(Path(item['target']).read_bytes(),self.incoming_bytes); self.assertFalse(Path(item['temp']).exists())
        self.assertEqual(self.manual.read_bytes(),self.original); self.assertEqual(self.incoming.read_bytes(),self.incoming_bytes)
    def test_crash_before_source_read_leaves_only_pending_record(self):
        with import_history.ImportHistory(self.db) as history: batch=history.create([self.incoming])
        script='''import builtins,os,sys,import_history,document_ui
from unittest.mock import patch
db,batch,source=sys.argv[1:]; original=builtins.open
def intercept(path,*args,**kwargs):
 if str(path)==source: os._exit(23)
 return original(path,*args,**kwargs)
with import_history.batch_lock(db,batch),import_history.ImportHistory(db) as h,patch('builtins.open',side_effect=intercept):
 document_ui.import_batch(db,[source],on_item=lambda i,x:h.update(batch,i,x),on_prepare=lambda i,t,p,s,n:h.prepare(batch,i,t,p,s,n))
'''
        proc=subprocess.run([sys.executable,'-c',script,str(self.db),batch,str(self.incoming)],cwd=str(Path(__file__).parent),capture_output=True,timeout=30)
        self.assertEqual(proc.returncode,23,proc.stderr.decode('utf8','replace'))
        with import_history.ImportHistory(self.db) as history:
            item=history.load(batch)[0]; self.assertEqual(item['state'],'未處理'); self.assertFalse(item['planned'])
        self.assertFalse(list((self.db.parent/'imported').glob('*'))); self.assertEqual(self.incoming.read_bytes(),self.incoming_bytes)
    def test_published_copy_recovers_after_crash_before_index(self):
        batch,item=self.crash_import('before_index'); self.assertTrue(Path(item['target']).is_file()); self.assertFalse(Path(item['temp']).exists())
        with import_history.batch_lock(self.db,batch),import_history.ImportHistory(self.db) as history:
            result=document_ui.retry_import_batch(self.db,history.load(batch),on_item=lambda i,x:history.update(batch,i,x),positions=[0])
            self.assertEqual(result[0]['state'],'匯入成功')
        self.assertTrue(core.search(str(self.db),'安全復原的文件內容')); self.assertEqual(self.incoming.read_bytes(),self.incoming_bytes)
    def test_partial_temp_is_cleaned_without_touching_unrelated_files(self):
        batch,item=self.crash_import('partial'); temp=Path(item['temp']); self.assertTrue(temp.exists()); self.assertNotEqual(temp.read_bytes(),self.incoming_bytes)
        unrelated=temp.parent/'unrelated.tmp'; unrelated.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError,'不完整'): core.recover_import_stage(self.db,item['target'],item['temp'],item['expected_sha256'],item['expected_size'],item['source'])
        with import_history.batch_lock(self.db,batch),import_history.ImportHistory(self.db) as history:
            self.assertEqual(core.cleanup_import_stage(self.db,item['target'],item['temp'],item['expected_sha256'],item['expected_size']),'已清理')
            history.clear_stage(batch,0,'已清理不完整暫存')
            self.assertEqual(history.load(batch)[0]['state'],'未處理'); self.assertFalse(history.load(batch)[0]['planned'])
        self.assertFalse(temp.exists()); self.assertEqual(unrelated.read_bytes(),b'keep'); self.assertEqual(self.incoming.read_bytes(),self.incoming_bytes)
    def test_invalid_or_outside_stage_is_never_deleted(self):
        batch,item=self.crash_import('partial'); temp=Path(item['temp']); before=temp.read_bytes()
        with self.assertRaises(ValueError): core.cleanup_import_stage(self.db,self.manual,temp,item['expected_sha256'],item['expected_size'])
        self.assertEqual(temp.read_bytes(),before); self.assertEqual(self.manual.read_bytes(),self.original)
    def test_existing_temp_name_collision_is_not_deleted(self):
        folder=self.db.parent/'imported'; folder.mkdir(); existing=folder/(('2'*32)+'.tmp'); existing.write_bytes(b'keep')
        ids=[SimpleNamespace(hex='1'*32),SimpleNamespace(hex='2'*32)]; prepared=[]
        with patch.object(core.uuid,'uuid4',side_effect=ids),self.assertRaises(FileExistsError):
            core.import_copy(self.db,self.incoming,on_prepare=lambda *args:prepared.append(args))
        self.assertEqual(existing.read_bytes(),b'keep'); self.assertEqual(prepared,[]); self.assertEqual(self.incoming.read_bytes(),self.incoming_bytes)
    def test_existing_target_name_collision_is_not_overwritten(self):
        folder=self.db.parent/'imported'; folder.mkdir(); existing=folder/(('1'*32)+'--incoming.txt'); existing.write_bytes(b'keep')
        ids=[SimpleNamespace(hex='1'*32),SimpleNamespace(hex='2'*32)]
        with patch.object(core.uuid,'uuid4',side_effect=ids),self.assertRaises(FileExistsError): core.import_copy(self.db,self.incoming)
        self.assertEqual(existing.read_bytes(),b'keep'); self.assertEqual(self.incoming.read_bytes(),self.incoming_bytes)
    def test_extra_temp_is_cleaned_only_when_complete_target_is_verified(self):
        batch,item=self.crash_import('before_index'); target=Path(item['target']); temp=Path(item['temp']); temp.write_bytes(b'partial')
        self.assertEqual(core.cleanup_import_stage(self.db,target,temp,item['expected_sha256'],item['expected_size']),'保留副本')
        self.assertTrue(target.exists()); self.assertFalse(temp.exists()); self.assertEqual(target.read_bytes(),self.incoming_bytes)
        with import_history.ImportHistory(self.db) as history:
            result=document_ui.retry_import_batch(self.db,history.load(batch),on_item=lambda i,x:history.update(batch,i,x),positions=[0])
            self.assertEqual(result[0]['state'],'匯入成功')
    def test_changed_target_prevents_cleanup(self):
        batch,item=self.crash_import('before_index'); target=Path(item['target']); temp=Path(item['temp']); target.write_bytes(b'changed'); temp.write_bytes(b'partial')
        with self.assertRaisesRegex(ValueError,'已變更'): core.cleanup_import_stage(self.db,target,temp,item['expected_sha256'],item['expected_size'])
        self.assertEqual(target.read_bytes(),b'changed'); self.assertEqual(temp.read_bytes(),b'partial'); self.assertEqual(self.manual.read_bytes(),self.original)
    def test_backup_does_not_run_during_managed_import(self):
        archive=self.root/'backup.pka'
        with import_history.operation_lock(self.db):
            with self.assertRaisesRegex(RuntimeError,'正在進行'): package_service.create_package(self.db,archive)
        self.assertFalse(archive.exists()); self.assertEqual(self.manual.read_bytes(),self.original)
    def test_lock_rejects_database_inside_source_before_creating_files(self):
        unsafe=self.source/'unsafe.sqlite3'; db=core.connect(str(unsafe))
        try:
            with db: db.execute("INSERT INTO settings(key,value) VALUES('root',?)",(str(self.source),))
        finally: db.close()
        with self.assertRaisesRegex(ValueError,'不可位於原始來源'):
            with import_history.operation_lock(unsafe): pass
        self.assertFalse((self.source/'import-locks').exists()); self.assertEqual(self.manual.read_bytes(),self.original)
    def test_backup_refuses_unpacked_stage_until_recovered(self):
        batch,item=self.crash_import('before_rename'); archive=self.root/'backup.pka'
        with self.assertRaisesRegex(ValueError,'匯入暫存'): package_service.create_package(self.db,archive)
        self.assertFalse(archive.exists()); self.assertTrue(Path(item['temp']).exists())
        with import_history.ImportHistory(self.db) as history:
            document_ui.retry_import_batch(self.db,history.load(batch),on_item=lambda i,x:history.update(batch,i,x),positions=[0])
        package_service.create_package(self.db,archive); restored,_,_=package_service.restore_package(archive,self.root,self.source)
        self.assertTrue(core.search(str(restored/'data'/'knowledge.sqlite3'),'安全復原的文件內容')); self.assertEqual(self.incoming.read_bytes(),self.incoming_bytes)

if __name__=='__main__': unittest.main()
