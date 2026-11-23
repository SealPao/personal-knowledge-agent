import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import core,ai_ui,organize_batch as b
class TestBatch(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();r=Path(self.tmp.name);self.src=r/'source';self.src.mkdir();self.doc=self.src/'boox.md';self.doc.write_text('# 前光\nBOOX 控制中心可以調整前光。',encoding='utf8');self.before=self.doc.read_bytes();self.db=r/'app'/'knowledge.sqlite';self.db.parent.mkdir();core.scan(str(self.src),str(self.db));self.cfg={**ai_ui.DEFAULT,'provider':'local','local_model':'fake','max_tokens':100};ai_ui.write_config(self.db.parent/'ai-settings.json',self.cfg)
 def tearDown(self):self.assertEqual(self.doc.read_bytes(),self.before);self.tmp.cleanup()
 def test_csv_grouping_preserves_rows_and_limits_size(self):
  rows=[(f'CSV 第 {i} 列',f'商品{i},營業時間') for i in range(1,101)]
  packed=b.pack_csv_chunks(rows)
  self.assertLess(len(packed),len(rows))
  self.assertTrue(all(len(text)<=3000 for _,text in packed))
  combined='\n'.join(text for _,text in packed)
  for locator,body in rows:
   self.assertIn(f'[{locator}] {body}',combined)

 def test_compact_preserves_finished_and_merges_only_pending(self):
  job=b.create(self.db,self.cfg,100000);db=b.connect(self.db)
  with db:
   did=db.execute('SELECT id FROM documents WHERE path=?',(str(self.doc.resolve()),)).fetchone()[0]
   db.execute("UPDATE documents SET extension='.csv' WHERE id=?",(did,))
   db.execute("UPDATE organize_items SET state='review',result='已保存草稿' WHERE job_id=?",(job,))
   row=db.execute('SELECT path,sha,size,mtime FROM organize_items WHERE job_id=?',(job,)).fetchone()
   for i in range(60):db.execute("INSERT INTO organize_items(job_id,document_id,path,sha,size,mtime,locator,text,kind,state) VALUES(?,?,?,?,?,?,?,?,?,?)",(job,did,*row,str(i),'row'+str(i),'text','pending'))
   b.compact_pending_csv(db,job)
  self.assertEqual(db.execute("SELECT result FROM organize_items WHERE job_id=? AND state='review'",(job,)).fetchone()[0],'已保存草稿')
  pending=db.execute("SELECT text FROM organize_items WHERE job_id=? AND state='pending'",(job,)).fetchall();self.assertLess(len(pending),60)
  for i in range(60):self.assertIn(f'[{i}] row{i}', '\n'.join(x[0] for x in pending))
  db.close()

 def test_change_cap_preserves_reserved_usage(self):
  job=b.create(self.db,self.cfg,5000);db=b.connect(self.db)
  with db:db.execute('UPDATE organize_jobs SET reserved=2000 WHERE id=?',(job,))
  db.close();b.set_limit(self.db,job,12000);self.assertEqual(b.stats(self.db,job)['reserved'],2000);self.assertEqual(b.stats(self.db,job)['limit'],12000)
  with self.assertRaises(ValueError):b.set_limit(self.db,job,1000)

 def test_html_grouping_and_round_robin_across_documents(self):
  html=self.src/'manual.html';html.write_text('<h1>使用說明</h1>'+''.join(f'<p>步驟{i}：開啟設定選單。</p>' for i in range(250)),encoding='utf8')
  other=self.src/'other.md';other.write_text('# 其他\n這是另一份文件。',encoding='utf8')
  core.scan(str(self.src),str(self.db));job=b.create(self.db,self.cfg,1000000);db=b.connect(self.db)
  try:
   original=db.execute('SELECT count(*) FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.path=?',(str(html.resolve()),)).fetchone()[0]
   grouped=db.execute('SELECT count(*) FROM organize_items WHERE job_id=? AND path=?',(job,str(html.resolve()))).fetchone()[0]
  finally:db.close()
  self.assertGreater(original,100);self.assertLess(grouped,original//2)
  names=[]
  def answer(question,sources,*args,**kwargs):
   names.append(sources[0]['path']);return ('草稿 [1]',sources,{})
  with patch.object(b.ai_service,'answer',side_effect=answer):b.run(self.db,job,lambda:len(names)>=3)
  self.assertEqual(len(set(names)),3)

 def test_prompt_documents_are_separate_and_not_sent(self):
  prompt=self.src/'Prompt 提示詞.txt';prompt.write_text('這是一份提示詞。',encoding='utf8');core.scan(str(self.src),str(self.db));job=b.create(self.db,self.cfg,100000)
  db=b.connect(self.db)
  try:row=db.execute('SELECT state,kind FROM organize_items WHERE job_id=? AND path=?',(job,str(prompt.resolve()))).fetchone()
  finally:db.close()
  self.assertEqual(row,('skipped','prompt'))

 def test_skip_unknown_preserves_reservation(self):
  job=b.create(self.db,self.cfg,100000);db=b.connect(self.db)
  with db:
   db.execute("UPDATE organize_jobs SET reserved=6000 WHERE id=?",(job,))
   db.execute("UPDATE organize_items SET state='unknown',detail='未確認' WHERE job_id=?",(job,))
  db.close();self.assertEqual(b.skip_unknown(self.db,job),1)
  self.assertEqual(b.stats(self.db,job)['reserved'],6000);self.assertEqual(b.stats(self.db,job)['counts']['skipped'],1)

 def test_incomplete_answer_accounts_reported_usage(self):
  config={**self.cfg,'provider':'openai','network_enabled':True};ai_ui.write_config(self.db.parent/'ai-settings.json',config)
  with patch.object(b.ai_service,'load_key',return_value='fake'):
   job=b.create(self.db,config,100000)
   error=b.ai_service.IncompleteResponse('未完成',{'input_tokens':100,'output_tokens':20})
   with patch.object(b.budget,'reserve',return_value='ticket'),patch.object(b.budget,'finish') as finish,patch.object(b.budget,'uncertain') as uncertain,patch.object(b.ai_service,'answer',side_effect=error):b.run(self.db,job)
  finish.assert_called_once_with(self.db,'ticket',error.usage);uncertain.assert_not_called();self.assertEqual(b.stats(self.db,job)['counts']['failed'],1)

 def test_budget_rejection_releases_unused_job_reservation(self):
  config={**self.cfg,'provider':'openai','network_enabled':True};ai_ui.write_config(self.db.parent/'ai-settings.json',config)
  with patch.object(b.ai_service,'load_key',return_value='fake'):
   job=b.create(self.db,config,100000)
   with patch.object(b.budget,'reserve',side_effect=ValueError('超過月預算')),patch.object(b.ai_service,'answer') as answer:b.run(self.db,job)
  answer.assert_not_called();self.assertEqual(b.stats(self.db,job)['reserved'],0);self.assertEqual(b.stats(self.db,job)['counts']['failed'],1)

 def test_output_limit_can_change_without_restarting_job(self):
  job=b.create(self.db,self.cfg,100000);ai_ui.write_config(self.db.parent/'ai-settings.json',{**self.cfg,'max_tokens':200})
  with patch.object(b.ai_service,'answer',return_value=('草稿 [1]',[{'id':1}],{})) as answer:b.run(self.db,job)
  self.assertEqual(answer.call_args.args[6],200);self.assertEqual(b.stats(self.db,job)['counts']['review'],1)

 def test_review_publishes_searchable_card_and_tracks_original(self):
  import ai_service
  job=b.create(self.db,self.cfg,100000)
  with patch.object(b.ai_service,'answer',return_value=('BOOX 前光在控制中心調整 [1]',[{'id':1}],{})):b.run(self.db,job)
  db=b.connect(self.db);item=db.execute("SELECT id FROM organize_items WHERE job_id=? AND state='review'",(job,)).fetchone()[0];db.close()
  card=b.publish_review(self.db,job,item,'BOOX 前光操作','BOOX 前光在控制中心調整。')
  self.assertTrue(Path(card).is_file());self.assertEqual(b.stats(self.db,job)['counts']['published'],1)
  with self.assertRaises(ValueError):b.publish_review(self.db,job,item,'BOOX 前光操作','BOOX 前光在控制中心調整。')
  refs=ai_service.retrieve(str(self.db),'BOOX 前光')
  cards=[x for x in refs if x['path']==card];self.assertTrue(cards,refs)
  self.assertEqual(cards[0]['origin']['path'],str(self.doc.resolve()));self.assertIn('控制中心',cards[0]['origin']['excerpt'])
  self.doc.write_text('# 前光\n新的不同內容。',encoding='utf8')
  self.assertTrue(ai_service.source_state(cards[0]))
  self.assertFalse(any(x['path']==card for x in ai_service.retrieve(str(self.db),'BOOX 前光')))
  self.doc.write_bytes(self.before)

 def test_multi_document_cards_retrieval_and_ai_citations(self):
  import ai_service
  other=self.src/'other.md';other.write_text('# 前光\n開啟前光後拖動亮度滑桿。',encoding='utf8')
  core.scan(str(self.src),str(self.db));job=b.create(self.db,self.cfg,100000)
  with patch.object(b.ai_service,'answer',return_value=('根據手冊調整前光 [1]',[{'id':1}],{})):b.run(self.db,job)
  db=b.connect(self.db)
  try:rows=db.execute("SELECT id,path FROM organize_items WHERE job_id=? AND state='review' ORDER BY id",(job,)).fetchall()
  finally:db.close()
  self.assertEqual(len(rows),2)
  card_paths=[]
  for item,source in rows:
   card_paths.append(b.publish_review(self.db,job,item,'前光調整 '+Path(source).stem,'前光調整：進入控制中心或拖動亮度滑桿。'))
  refs=[x for x in ai_service.retrieve(str(self.db),'前光調整') if x['path'] in card_paths]
  self.assertEqual(len(refs),2);self.assertEqual({x['origin']['path'] for x in refs},{str(self.doc.resolve()),str(other.resolve())})
  for i,x in enumerate(refs,1):x['id']=i
  def fake(url,headers,payload):return {'output':[{'type':'message','content':[{'type':'output_text','text':'控制中心調整 [1]；滑桿調整 [2]'}]}]}
  result,chosen,_=ai_service.answer('怎麼調整前光？',refs,'openai','fake','fake',post=fake,network_enabled=True)
  self.assertIn('[1]',result);self.assertIn('[2]',result);self.assertEqual({x['origin']['path'] for x in chosen},{str(self.doc.resolve()),str(other.resolve())})

 def test_source_changes_before_approval_are_rejected(self):
  job=b.create(self.db,self.cfg,100000)
  with patch.object(b.ai_service,'answer',return_value=('草稿 [1]',[{'id':1}],{})):b.run(self.db,job)
  db=b.connect(self.db);item=db.execute('SELECT id FROM organize_items WHERE job_id=?',(job,)).fetchone()[0];db.close()
  self.doc.write_text('已變更的內容',encoding='utf8')
  with self.assertRaises(ValueError):b.publish_review(self.db,job,item,'標題','內容')
  self.doc.write_bytes(self.before)

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
