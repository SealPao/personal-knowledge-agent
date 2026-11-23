import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import core,ai_ui,organize_batch as b
class TestBatch(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();r=Path(self.tmp.name);self.src=r/'source';self.src.mkdir();self.doc=self.src/'boox.md';self.doc.write_text('# 前光\nBOOX 控制中心可以調整前光。',encoding='utf8');self.before=self.doc.read_bytes();self.db=r/'app'/'knowledge.sqlite';self.db.parent.mkdir();core.scan(str(self.src),str(self.db));self.cfg={**ai_ui.DEFAULT,'provider':'local','local_model':'fake','max_tokens':100};ai_ui.write_config(self.db.parent/'ai-settings.json',self.cfg)
 def tearDown(self):self.assertEqual(self.doc.read_bytes(),self.before);self.tmp.cleanup()
 def test_pause_resume_and_review(self):
  job=b.create(self.db,self.cfg,100000);b.run(self.db,job,lambda:True);self.assertEqual(b.stats(self.db,job)['counts']['pending'],1)
  with patch.object(b.ai_service,'answer',return_value=('整理 [1]',[{'id':1}],{})) as answer:b.run(self.db,job);self.assertEqual(answer.call_count,1)
  self.assertEqual(b.stats(self.db,job)['counts']['review'],1)
  b.run(self.db,job)
 def test_budget_stops_before_request(self):
  job=b.create(self.db,self.cfg,1000)
  with patch.object(b.ai_service,'answer') as answer:b.run(self.db,job);answer.assert_not_called()
  self.assertEqual(b.stats(self.db,job)['counts']['pending'],1)
 def test_interruption_never_retries(self):
  job=b.create(self.db,self.cfg,100000);db=b.connect(self.db)
  with db:db.execute("UPDATE organize_items SET state='running' WHERE job_id=?",(job,))
  db.close();b.recover(self.db,job);self.assertEqual(b.stats(self.db,job)['counts']['unknown'],1)
  with self.assertRaises(ValueError):b.run(self.db,job)
if __name__=='__main__':unittest.main()
