import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import core,installer_core,workspace

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.base=Path(self.tmp.name); self.source=self.base/'original'; self.source.mkdir()
        (self.source/'guide.md').write_text('# 手冊\n原始知識內容',encoding='utf-8')
        self.proto=self.base/'prototype'; db=self.proto/'data'/'knowledge.sqlite3'; db.parent.mkdir(parents=True); core.scan(str(self.source),str(db))
        self.card=core.save_card(str(db),'筆記','已校對的內容'); self.payload=self.base/'runtime.exe'; self.payload.write_bytes(b'APP-v1')
        self.program=self.base/'Programs'/'Knowledge'; self.home=self.base/'AppData'/'Knowledge'
    def tearDown(self): self.tmp.cleanup()
    def test_install_migrates_copy_and_update_preserves_data(self):
        before=(self.source/'guide.md').read_bytes(); result=installer_core.install(self.payload,self.program,self.home,prototype=self.proto)
        self.assertTrue(result['migrated']); db=self.home/'data'/'knowledge.sqlite3'
        self.assertTrue(core.search(str(db),'原始知識')); card=core.search(str(db),'已校對')[0][0]; self.assertTrue(core.contained(card,self.home))
        self.assertEqual((self.source/'guide.md').read_bytes(),before)
        self.payload.write_bytes(b'APP-v2'); updated=installer_core.install(self.payload,self.program,self.home,prototype=self.proto)
        self.assertTrue(updated['existing']); self.assertEqual((self.program/'PersonalKnowledgeAgent.exe').read_bytes(),b'APP-v2')
        self.assertTrue(core.search(str(db),'已校對')); self.assertEqual((self.source/'guide.md').read_bytes(),before)
        self.assertTrue(list(self.program.glob('PersonalKnowledgeAgent.backup-*.exe')))
        with patch.object(workspace.sys,'frozen',True,create=True), patch.object(workspace.sys,'executable',str(self.program/'PersonalKnowledgeAgent.exe')):
            self.assertEqual(workspace.application_base(),self.home.resolve())
    def test_reject_install_under_original_or_overlapping_data(self):
        with self.assertRaises(ValueError): installer_core.install(self.payload,self.source/'program',self.home,prototype=self.proto)
        with self.assertRaises(ValueError): installer_core.install(self.payload,self.program,self.program/'data',prototype=self.proto)
        with self.assertRaises(ValueError): installer_core.install(self.payload,self.source/'program',self.home,protection_base=self.proto)
        self.assertFalse((self.source/'program').exists())
    def test_does_not_overwrite_partial_existing_data(self):
        data=self.home/'data'; data.mkdir(parents=True); marker=data/'valuable.txt'; marker.write_text('keep')
        with self.assertRaises(ValueError): installer_core.install(self.payload,self.program,self.home,prototype=self.proto)
        self.assertEqual(marker.read_text(),'keep')
    def test_installed_model_and_foreign_program_protection(self):
        modeldir=self.base/'model'; modeldir.mkdir(); model=modeldir/'chi_sim.traineddata'; model.write_bytes(b'model'); (modeldir/'LICENSE.txt').write_text('license')
        installer_core.install(self.payload,self.program,self.home,model_path=model,prototype=self.proto)
        self.assertEqual((self.home/'data'/'ocr'/'tessdata'/'chi_sim.traineddata').read_bytes(),b'model')
        self.assertTrue((self.home/'data'/'ocr'/'tessdata'/'LICENSE.txt').exists())
        other=self.base/'other'; other.mkdir(); (other/'PersonalKnowledgeAgent.exe').write_bytes(b'foreign')
        with self.assertRaises(ValueError): installer_core.install(self.payload,other,self.base/'newdata',prototype=self.proto)
        self.assertEqual((other/'PersonalKnowledgeAgent.exe').read_bytes(),b'foreign')

if __name__=='__main__': unittest.main()
