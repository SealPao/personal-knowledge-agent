import hashlib
import json
import os
import tempfile
import unittest
import zipfile
import shutil
from pathlib import Path
import core
import package_service as package
import workspace
import budget

class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.base=Path(self.tmp.name)
        self.source=self.base/'source'; self.source.mkdir(); (self.source/'manual.md').write_text('# 手冊\n備份與充電說明',encoding='utf-8')
        self.db=self.base/'data'/'knowledge.sqlite3'; self.db.parent.mkdir(); core.scan(str(self.source),str(self.db))
        core.save_card(str(self.db),'我的筆記','用於測試的個人知識卡')
        budget.save_settings(self.db,'1','10','model','1','2')
        self.file=self.base/'backup.pka'
    def tearDown(self): self.tmp.cleanup()
    def test_complete_backup_restore_and_search(self):
        original=(self.source/'manual.md').read_bytes(); count,size=package.create_package(self.db,self.file)
        self.assertEqual(count,2); self.assertEqual((self.source/'manual.md').read_bytes(),original)
        restored,n,total=package.restore_package(self.file,self.base,self.source)
        self.assertEqual(n,2); self.assertEqual((restored/'source'/'manual.md').read_bytes(),original)
        db=restored/'data'/'knowledge.sqlite3'; self.assertTrue(core.search(str(db),'備份與充電'))
        self.assertTrue(core.search(str(db),'個人知識卡')); self.assertTrue((restored/'data'/'cards').exists())
        self.assertEqual(budget.get_settings(db)['monthly'],'10')
        workspace.activate(self.base,db); self.assertEqual(workspace.active_database(self.base),db.resolve())
    def test_external_document_and_its_explicit_copy_survive_export(self):
        original=self.source/'manual.md'; before=original.read_bytes()
        copied,created=core.import_copy(self.db,original); self.assertTrue(created)
        count,_=package.create_package(self.db,self.file); self.assertEqual(count,3)
        restored,_,_=package.restore_package(self.file,self.base,self.source)
        db=core.connect(str(restored/'data'/'knowledge.sqlite3'))
        try: paths=[Path(row[0]) for row in db.execute('SELECT path FROM documents').fetchall()]
        finally: db.close()
        restored_copy=next(path for path in paths if core.contained(path,restored/'data'/'imported'))
        self.assertEqual(restored_copy.read_bytes(),before); self.assertEqual((restored/'source'/'manual.md').read_bytes(),before)
        self.assertEqual(original.read_bytes(),before)
    def test_export_does_not_overwrite_or_write_source(self):
        with self.assertRaises(ValueError): package.create_package(self.db,self.source/'backup.pka')
        self.file.write_bytes(b'keep')
        with self.assertRaises(ValueError): package.create_package(self.db,self.file)
        self.assertEqual(self.file.read_bytes(),b'keep')
    def test_unindexed_file_and_changed_source_are_rejected(self):
        (self.source/'unindexed.txt').write_text('test',encoding='utf-8')
        with self.assertRaises(ValueError): package.create_package(self.db,self.file)
        (self.source/'unindexed.txt').unlink(); (self.source/'manual.md').write_text('changed',encoding='utf-8')
        with self.assertRaises(ValueError): package.create_package(self.db,self.file)
        self.assertFalse(self.file.exists())
    def test_same_size_and_time_content_change_is_rejected(self):
        path=self.source/'manual.md'; before=path.stat(); raw=path.read_bytes()
        changed=bytearray(raw); changed[-1]^=1; path.write_bytes(changed)
        os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.assertRaises(ValueError): package.create_package(self.db,self.file)
        self.assertFalse(self.file.exists())
    def test_tampered_asset_and_path_are_rejected(self):
        package.create_package(self.db,self.file)
        with zipfile.ZipFile(self.file) as z: data={i.filename:z.read(i.filename) for i in z.infolist()}
        obj=next(x for x in data if x.startswith('objects/')); data[obj]=b'bad'
        damaged=self.base/'damaged.pka'
        with zipfile.ZipFile(damaged,'w') as z:
            for name,content in data.items(): z.writestr(name,content)
        with self.assertRaises(ValueError): package.restore_package(damaged,self.base,self.source)
        self.assertFalse(list(self.base.glob('.pka-staging-*')))
        info=json.loads(data['manifest.json']); info['documents'][0]['path']='../../outside'; data['manifest.json']=json.dumps(info).encode()
        with zipfile.ZipFile(damaged,'w') as z:
            for name,content in data.items(): z.writestr(name,content)
        with self.assertRaises(ValueError): package.restore_package(damaged,self.base,self.source)
    def test_reject_restoring_inside_original_source(self):
        package.create_package(self.db,self.file)
        with self.assertRaises(ValueError): package.restore_package(self.file,self.source,self.source)

    def test_import_move_export_and_reimport(self):
        package.create_package(self.db,self.file)
        computer_a=self.base/'computer-a'; libraries=workspace.library_parent(computer_a); libraries.mkdir(parents=True)
        library,_,_=package.restore_package(self.file,libraries,self.source)
        workspace.activate(computer_a,library/'data'/'knowledge.sqlite3')
        copied=self.base/'computer-b'; shutil.copytree(computer_a,copied)
        # Ensure it cannot accidentally find the old files.
        shutil.rmtree(computer_a)
        db=workspace.active_database(copied)
        hits=core.search(str(db),'備份與充電'); self.assertTrue(hits)
        self.assertTrue(Path(hits[0][0]).is_file()); self.assertTrue(core.contained(hits[0][0],copied))
        self.assertTrue(core.search(str(db),'個人知識卡'))
        again=self.base/'after-move.pka'; package.create_package(db,again)
        restored,_,_=package.restore_package(again,self.base,self.source)
        self.assertTrue(core.search(str(restored/'data'/'knowledge.sqlite3'),'備份與充電'))
        self.assertEqual((self.source/'manual.md').read_text(encoding='utf-8'),'# 手冊\n備份與充電說明')

    def test_move_with_missing_document_fails_without_rewriting_database(self):
        package.create_package(self.db,self.file)
        a=self.base/'a'; libraries=workspace.library_parent(a); libraries.mkdir(parents=True)
        library,_,_=package.restore_package(self.file,libraries,self.source)
        workspace.activate(a,library/'data'/'knowledge.sqlite3')
        b=self.base/'b'; shutil.copytree(a,b); shutil.rmtree(a)
        moved=b/'data'/'libraries'/library.name; (moved/'source'/'manual.md').unlink()
        import sqlite3
        db=moved/'data'/'knowledge.sqlite3'; con=sqlite3.connect(db)
        before=con.execute("SELECT value FROM settings WHERE key='root'").fetchone()[0]; con.close()
        with self.assertRaises(RuntimeError): workspace.active_database(b)
        con=sqlite3.connect(db); after=con.execute("SELECT value FROM settings WHERE key='root'").fetchone()[0]; con.close()
        self.assertEqual(before,after)

    def test_choose_inactive_library_after_move(self):
        package.create_package(self.db,self.file)
        a=self.base/'a'; parent=workspace.library_parent(a); parent.mkdir(parents=True)
        first,_,_=package.restore_package(self.file,parent,self.source)
        second,_,_=package.restore_package(self.file,parent,self.source)
        workspace.activate(a,first/'data'/'knowledge.sqlite3'); workspace.activate(a,second/'data'/'knowledge.sqlite3')
        b=self.base/'b'; shutil.copytree(a,b); shutil.rmtree(a)
        libraries=workspace.known_libraries(b); self.assertEqual(len(libraries),2)
        inactive=next(x for x in libraries if x['database'].parent.parent.name==first.name)
        self.assertTrue(inactive['available']); workspace.activate(b,inactive['database'])
        active=workspace.active_database(b); self.assertTrue(core.search(str(active),'備份與充電'))
        self.assertTrue(core.contained(core.search(str(active),'備份與充電')[0][0],b))

    def test_external_index_moves_with_app_but_source_stays_external(self):
        a=self.base/'a'; db=workspace.external_parent(a)/'existing'/'knowledge.sqlite3'; db.parent.mkdir(parents=True)
        core.scan(str(self.source),str(db)); core.save_card(str(db),'搬移筆記','已保留工作副本')
        workspace.activate(a,db); b=self.base/'b'; shutil.copytree(a,b); shutil.rmtree(a)
        moved=workspace.active_database(b)
        self.assertTrue(core.search(str(moved),'備份與充電'))
        card=core.search(str(moved),'已保留工作副本')[0][0]; self.assertTrue(core.contained(card,b)); self.assertTrue(Path(card).exists())
        # A missing external source must not prevent the APP opening its cached index.
        self.source.rename(self.base/'temporarily-offline')
        self.assertEqual(workspace.active_database(b),moved)
        self.assertFalse(workspace.known_libraries(b)[0]['available'])
        self.assertTrue(core.search(str(moved),'備份與充電'))
        (self.base/'temporarily-offline').rename(self.source)

if __name__=='__main__': unittest.main()
