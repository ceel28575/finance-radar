import unittest,tempfile,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'collector'))
from cloud_sync import sync_exports,SITE_URL
class Response:
 status_code=200
 def __init__(self,items,reports=0):self.items=items;self.reports=reports
 def raise_for_status(self):pass
 def json(self):return {'ok':True,'accepted_ids':[x['id'] for x in self.items],'reports':self.reports}
class SyncTests(unittest.TestCase):
 def test_retry_dedupe_and_update(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'exports').mkdir();(root/'cloud-config.json').write_text(json.dumps({'site_url':SITE_URL,'site_token':'test-fixture-not-a-real-secret'}))
   p=root/'exports/2026-10-27.json';p.write_text(json.dumps({'items':[{'id':'a','content':'old'}]}));calls=[]
   def fail(*a,**kw):raise OSError('offline')
   with self.assertRaises(OSError):sync_exports(root,post=fail)
   def ok(url,**kw):
    self.assertEqual(url,SITE_URL+'/api/radar/ingest');self.assertFalse(kw['allow_redirects']);calls.append(kw['json']['items']);return Response(calls[-1])
   self.assertEqual(sync_exports(root,post=ok),1);self.assertEqual(sync_exports(root,post=ok),0)
   p.write_text(json.dumps({'items':[{'id':'a','content':'updated'}]}));self.assertEqual(sync_exports(root,post=ok),1)
   self.assertEqual([len(c) for c in calls],[1,0,1])
 def test_ai_report_sync_and_dedupe(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'exports').mkdir();(root/'cloud-config.json').write_text(json.dumps({'site_url':SITE_URL,'site_token':'test-fixture-not-a-real-secret'}))
   report={'date':'2026-10-30','generated_at':'2026-10-30T15:10:00+08:00','bull_probability':62,'summary':'test','risk':'test','stocks':[]}
   (root/'exports/2026-10-30.json').write_text(json.dumps({'items':[],'reports':[report]}))
   calls=[]
   def ok(url,**kw):
    self.assertEqual(url,SITE_URL+'/api/radar/ingest')
    payload=kw['json'];calls.append(payload)
    return Response(payload.get('items',[]),len(payload.get('reports',[])))
   self.assertEqual(sync_exports(root,post=ok),1)
   self.assertEqual(sync_exports(root,post=ok),0)
   self.assertEqual(len(calls),2)
   self.assertEqual(calls[0]['reports'][0]['date'],'2026-10-30')
 def test_no_config_local_only(self):
  with tempfile.TemporaryDirectory() as t:self.assertIsNone(sync_exports(Path(t)))
if __name__=='__main__':unittest.main()
