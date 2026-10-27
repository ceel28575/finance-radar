import unittest,tempfile,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'collector'))
from cloud_sync import sync_exports,SITE_URL
class Response:
 status_code=200
 def __init__(self,items):self.items=items
 def raise_for_status(self):pass
 def json(self):return {'ok':True,'accepted_ids':[x['id'] for x in self.items]}
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
 def test_no_config_local_only(self):
  with tempfile.TemporaryDirectory() as t:self.assertIsNone(sync_exports(Path(t)))
if __name__=='__main__':unittest.main()
