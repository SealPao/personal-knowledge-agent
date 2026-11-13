import tempfile
import unittest
from pathlib import Path
import adb_research,ai_service,budget,core,package_service

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.base=Path(self.tmp.name)
        self.src=self.base/'source'; self.src.mkdir(); (self.src/'manual.txt').write_text('original',encoding='utf-8')
        self.db=self.base/'data'/'knowledge.sqlite3'; self.db.parent.mkdir(); core.scan(str(self.src),str(self.db)); self.calls=[]
        self.config={'provider':'openai','openai_model':'test-model','network_enabled':True,'max_tokens':800}
    def tearDown(self): self.tmp.cleanup()
    def post(self,url,headers,payload):
        self.calls.append((url,headers,payload)); return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'前光指令尚未確認。','annotations':[{'type':'url_citation','url':'https://example.org/source','title':'來源','start_index':0,'end_index':4},{'type':'url_citation','url':'javascript:alert(1)','title':'bad','end_index':4}]}]}]}
    def test_citations_notes_backup_no_commands_executed(self):
        result=adb_research.research(self.db,'BOOX','','','前光指令',self.config,'test-key',post=self.post)
        self.assertEqual(len(result['sources']),1); self.assertIn('[1]',result['text']); self.assertEqual(self.calls[0][2]['tools'],[{'type':'web_search','search_context_size':'low'}]); self.assertEqual(self.calls[0][2]['max_tool_calls'],1); self.assertFalse(self.calls[0][2]['store'])
        self.assertEqual(budget.recent(self.db)[0][2],'unknown')
        path=adb_research.save(self.db,result); self.assertTrue(core.search(str(self.db),'前光指令'))
        self.assertTrue(any('指令未經裝置驗證' in x['kind'] for x in ai_service.retrieve(str(self.db),'前光指令')))
        archive=self.base/'backup.pka'; package_service.create_package(self.db,archive)
        restored,_,_=package_service.restore_package(archive,self.base,self.src)
        self.assertTrue(core.search(str(restored/'data'/'knowledge.sqlite3'),'前光指令')); self.assertEqual((self.src/'manual.txt').read_text(encoding='utf-8'),'original')
    def test_network_and_budget_fail_closed(self):
        self.config['network_enabled']=False
        with self.assertRaises(ValueError): adb_research.research(self.db,'HyRead','','','前光',self.config,'key',post=self.post)
        self.config['network_enabled']=True; budget.save_settings(self.db,'1','2','test-model','1','1')
        with self.assertRaises(ValueError): adb_research.research(self.db,'Meebook','','','刷新',self.config,'key',post=self.post)
        self.assertFalse(self.calls)
    def test_arbitrary_brand_and_no_brand(self):
        result=adb_research.research(self.db,'自訂品牌','','','刷新方式',self.config,'key',post=self.post)
        self.assertIn('自訂品牌',result['query'])
        with self.assertRaises(ValueError): adb_research.research(self.db,'','','','刷新方式',self.config,'key',post=self.post)
    def test_search_cost_is_reserved_and_estimated(self):
        budget.save_settings(self.db,'1','2','test-model','1','2','0.01')
        def post(url,headers,payload):
            response=self.post(url,headers,payload)
            response['output'].insert(0,{'type':'web_search_call','status':'completed','action':{'type':'search'}})
            response['usage']={'input_tokens':100,'output_tokens':50}
            return response
        adb_research.research(self.db,'BOOX','','','前光',self.config,'key',post=post)
        row=budget.recent(self.db)[0]; self.assertEqual(row[2],'complete'); self.assertEqual(row[5],10200000)
    def test_web_fee_and_conservative_reservation_block_before_network(self):
        budget.save_settings(self.db,'0.01','0','test-model','1','2','0.01')
        with self.assertRaises(ValueError): adb_research.research(self.db,'BOOX','','','前光',self.config,'key',post=self.post)
        self.assertFalse(self.calls)

if __name__=='__main__': unittest.main()
