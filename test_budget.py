import datetime
import tempfile
import threading
import unittest
from pathlib import Path
import budget,core

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.path=Path(self.tmp.name)/'test.db'
        core.connect(str(self.path)).close()
        self.sources=[{'id':1,'excerpt':'充電手冊','kind':'來源文件'}]; self.now=datetime.datetime(2026,11,13,12,tzinfo=datetime.timezone.utc)
    def tearDown(self): self.tmp.cleanup()
    def test_budget_reservation_and_actual_usage(self):
        budget.save_settings(self.path,'0.01','0.02','model','1','2')
        ident=budget.reserve(self.path,'充電方式',self.sources,'model',100,now=self.now)
        budget.finish(self.path,ident,{'input_tokens':100,'output_tokens':50})
        row=budget.recent(self.path)[0]; self.assertEqual(row[2],'complete'); self.assertEqual(row[5],200000)
    def test_cap_blocks_before_request_and_wrong_model(self):
        budget.save_settings(self.path,'0.00001','0','model','1','2')
        with self.assertRaises(ValueError): budget.reserve(self.path,'問題',self.sources,'model',100,now=self.now)
        with self.assertRaises(ValueError): budget.reserve(self.path,'問題',self.sources,'different',100,now=self.now)
        self.assertFalse(budget.recent(self.path))
    def test_unknown_and_unpriced_are_not_misreported_as_free(self):
        ident=budget.reserve(self.path,'問題',self.sources,'model',100,now=self.now)
        budget.finish(self.path,ident,{'input_tokens':10,'output_tokens':3})
        self.assertEqual(budget.recent(self.path)[0][2],'unpriced')
        budget.save_settings(self.path,'1','2','model','1','2')
        with self.assertRaises(ValueError): budget.reserve(self.path,'問題',self.sources,'model',100,now=self.now)
        budget.exclude(self.path,ident); self.assertTrue(budget.reserve(self.path,'問題',self.sources,'model',100,now=self.now))
    def test_simultaneous_reservations_are_serialized(self):
        budget.save_settings(self.path,'0.0015','0','model','1','1')
        barrier=threading.Barrier(2); results=[]
        def work():
            barrier.wait()
            try: budget.reserve(self.path,'問題',self.sources,'model',100,now=self.now); results.append('accepted')
            except ValueError: results.append('blocked')
        threads=[threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sorted(results),['accepted','blocked'])
    def test_invalid_values(self):
        with self.assertRaises(ValueError): budget.save_settings(self.path,'NaN','2','model','1','1')
        with self.assertRaises(ValueError): budget.save_settings(self.path,'1','2','model','0','0')
    def test_image_reservation_counts_extra_input(self):
        budget.save_settings(self.path,'0.005','0','model','1','1')
        with self.assertRaises(ValueError): budget.reserve(self.path,'目標',self.sources,'model',100,now=self.now,extra_input_tokens=8192)
        self.assertFalse(budget.recent(self.path))
        with self.assertRaises(ValueError): budget.reserve(self.path,'目標',self.sources,'model',100,now=self.now,extra_input_tokens=-1)
    def test_tool_pricing_is_preserved_and_missing_count_stays_uncertain(self):
        budget.save_settings(self.path,'1','2','model','1','2','0.01')
        budget.save_settings(self.path,'1','2','model','1','2')
        self.assertEqual(budget.get_settings(self.path)['web_search_price'],'0.01')
        ticket=budget.reserve(self.path,'目標',self.sources,'model',100,now=self.now,extra_input_tokens=131072,tool_kind='web_search',tool_calls=1)
        budget.finish(self.path,ticket,{'input_tokens':100,'output_tokens':10})
        self.assertEqual(budget.recent(self.path)[0][2],'unknown')
        self.assertGreater(budget.recent(self.path)[0][5],10000000)

if __name__=='__main__': unittest.main()
