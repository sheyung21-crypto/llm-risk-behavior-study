"""Synthetic tests only: scoring boundaries and release verification failure modes."""
import importlib.util,json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
s=load('release_switching',ROOT/'tools/switching.py')
v=load('release_verify',ROOT/'tools/verify_package.py')
class Scoring(unittest.TestCase):
 def test_all_safe(self):self.assertEqual(s.score_list(['SAFE']*21),1)
 def test_all_lottery(self):self.assertEqual(s.score_list(['LOTTERY']*21),22)
 def test_single_switch(self):self.assertEqual(s.score_list(['LOTTERY']*9+['SAFE']*12),10)
 def test_multiple_switch(self):self.assertEqual(s.score_list(['LOTTERY','SAFE','LOTTERY']+['SAFE']*18),3)
 def test_reverse_only_missing(self):
  with self.assertRaises(s.NoLotteryToSafeTransitionError):s.score_list(['SAFE']*10+['LOTTERY']*11)
 def test_invalid_length(self):
  with self.assertRaises(s.InvalidChoiceSequenceError):s.score_list(['SAFE']*20)
 def test_invalid_token(self):
  with self.assertRaises(s.InvalidChoiceSequenceError):s.score_list(['SAFE']*20+['safe'])
 def test_two_list_mean(self):self.assertEqual(s.score_two_lists(['SAFE']*21,['LOTTERY']*21),11.5)
class ReleaseChecks(unittest.TestCase):
 def test_only_root_git_metadata_excluded(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/'.git').mkdir();(root/'.git/config').write_text('synthetic')
   (root/'nested/.git').mkdir(parents=True);(root/'nested/.git/config').write_text('synthetic')
   names={p.relative_to(root).as_posix() for p in v.package_paths(root)}
   self.assertNotIn('.git/config',names)
   self.assertIn('nested/.git/config',names)
 def test_authorized_publication(self):
  status={'published':True,'publication_ready':True,'external_publication_authorized':True,
          'visibility':'public','repository_url':'https://github.com/sheyung21-crypto/llm-risk-behavior-study',
          'scope':'allowlisted_public_package_only_no_internal_history_or_linked_human_data',
          'open_reuse_license_assigned':False,'unresolved':['project_code_and_document_licenses']}
  self.assertTrue(v.release_status_valid(status))
  self.assertFalse(v.release_status_valid({**status,'external_publication_authorized':False}))
  self.assertFalse(v.release_status_valid({**status,'open_reuse_license_assigned':True}))
 def test_candidate_stays_unpublished(self):
  self.assertTrue(v.release_status_valid({'published':False,'publication_ready':False,'unresolved':['license']}))
  self.assertFalse(v.release_status_valid({'published':False,'publication_ready':True,'unresolved':['license']}))
 def test_path_traversal(self):
  for path in ['../secret','/absolute','C:/secret','a\\b']:
   self.assertFalse(v.safe_rel(path))
 def test_sensitive_key(self):
  # Clearly synthetic string; not a participant value.
  self.assertIn('participant_key_value',v.privacy_flags('data/aggregate/x.csv',('p_'+'a'*32).encode()))
 def test_local_path(self):
  self.assertIn('local_user_path',v.privacy_flags('x.md',('C:'+chr(92)+'Users'+chr(92)+'example').encode()))
 def test_private_filetype(self):self.assertIn('forbidden_file_type',v.privacy_flags('x.parquet',b'PAR1'))
 def test_hash_tampering(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/'x.txt').write_bytes(b'changed')
   (root/'MANIFEST.json').write_text(json.dumps({'files':{'x.txt':v.sha(b'original')}}))
   checks=v.verify(root);self.assertTrue(any(c['name']=='all_file_hashes' and not c['passed'] for c in checks))
 def test_missing_file(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/'MANIFEST.json').write_text(json.dumps({'files':{'missing.txt':v.sha(b'x')}}))
   self.assertTrue(any(not c['passed'] for c in v.verify(root)))
 def test_extra_file(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/'extra.txt').write_text('synthetic')
   (root/'MANIFEST.json').write_text(json.dumps({'files':{}}))
   self.assertTrue(any(c['name']=='exact_allowlist' and not c['passed'] for c in v.verify(root)))
if __name__=='__main__':unittest.main(verbosity=2)
