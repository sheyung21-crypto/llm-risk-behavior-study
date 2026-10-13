"""Read-only integrity and aggregate consistency checks; no research estimation."""
from pathlib import Path, PurePosixPath
import argparse,csv,hashlib,io,json,re,sys,xml.etree.ElementTree as ET
import importlib.util
ROOT=Path(__file__).resolve().parents[1]
def sha(data):return hashlib.sha256(data).hexdigest()
def rows(root,rel):return list(csv.DictReader(io.StringIO((root/rel).read_text(encoding='utf-8-sig'))))
def is_link(p):return p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction())
def package_paths(root):
 # A normal clone has local Git metadata; it is not a distributed package file.
 return [p for p in root.rglob('*') if p.relative_to(root).parts[0]!='.git']
def safe_rel(s):
 p=PurePosixPath(s)
 return bool(s) and not p.is_absolute() and '..' not in p.parts and '\\' not in s and ':' not in s and str(p)==s
def release_status_valid(status):
 if status.get('published') is False:
  return status.get('publication_ready') is False and bool(status.get('unresolved'))
 return (status.get('published') is True and status.get('publication_ready') is True
         and status.get('external_publication_authorized') is True
         and status.get('visibility')=='public'
         and status.get('repository_url')=='https://github.com/sheyung21-crypto/llm-risk-behavior-study'
         and status.get('scope')=='allowlisted_public_package_only_no_internal_history_or_linked_human_data'
         and status.get('open_reuse_license_assigned') is False
         and 'project_code_and_document_licenses' in status.get('unresolved',[]))
def privacy_flags(name,data):
 flags=[]
 p=PurePosixPath(name)
 if p.suffix.lower() in {'.dta','.parquet','.sqlite','.db','.zip','.exe','.dll','.ttf','.ttc','.otf'}:flags.append('forbidden_file_type')
 if set(p.parts)&{'.git','.codex','.agents','.venv','__pycache__','runtime','private','restricted'}:flags.append('forbidden_directory')
 if p.name.startswith('.env'):flags.append('environment_file')
 if p.suffix.lower() in {'.png','.pdf'}:return flags
 try:s=data.decode('utf-8-sig')
 except UnicodeDecodeError:return flags+['unrecognized_binary']
 patterns={
 'participant_key_value':r'\bp_[0-9a-f]{12,64}\b',
 'local_user_path':r'(?:[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"\']+|/(?:home|Users)/[^/\s"\']+)',
 'credential_candidate':r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|AKIA[A-Z0-9]{16})\b',
 'private_key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
 }
 for label,pattern in patterns.items():
  if re.search(pattern,s,re.I):flags.append(label)
 if name.startswith('data/aggregate/') and p.suffix=='.csv':
  header=next(csv.reader(io.StringIO(s)),[])
  if any(c.lower() in {'participant_key','participant_id','job_id','session_id','response','answer'} for c in header):flags.append('individual_data_header')
 if p.suffix=='.svg':
  try:
   tree=ET.fromstring(s)
   if any(n.tag.split('}')[-1] in {'script','image','foreignObject'} for n in tree.iter()):flags.append('embedded_or_executable_svg')
  except ET.ParseError:flags.append('invalid_svg')
 return flags
def verify(root):
 checks=[]
 def check(name,passed,detail=None):
  checks.append({'name':name,'passed':bool(passed),**({'detail':detail} if detail is not None else {})})
 if is_link(root):raise ValueError('Linked package root.')
 root=root.resolve()
 manifest=json.loads((root/'MANIFEST.json').read_text(encoding='utf-8'))
 expected=manifest['files']
 check('manifest_paths_safe',all(safe_rel(s) for s in expected))
 if not checks[-1]['passed']:return checks
 paths=package_paths(root)
 links=[p.relative_to(root).as_posix() for p in paths if is_link(p)]
 check('no_symlinks_or_junctions',not links,links)
 if links:return checks
 actual={p.relative_to(root).as_posix():p for p in paths if p.is_file()}
 check('exact_allowlist',set(actual)==set(expected)|{'MANIFEST.json'}, {'extra':sorted(set(actual)-set(expected)-{'MANIFEST.json'}),'missing':sorted(set(expected)-set(actual))})
 bad=[name for name,h in expected.items() if name not in actual or sha(actual[name].read_bytes())!=h]
 check('all_file_hashes',not bad,bad)
 if bad or not checks[-2]['passed']:return checks
 flagged={n:privacy_flags(n,p.read_bytes()) for n,p in actual.items()}
 flagged={n:v for n,v in flagged.items() if v}
 check('privacy_pattern_and_file_type_scan',not flagged,flagged)
 broken=[]
 for name,p in actual.items():
  if p.suffix!='.md':continue
  for target in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)',p.read_text(encoding='utf-8-sig')):
   target=target.strip('<>').split('#')[0]
   if not target or re.match(r'^[a-z][a-z0-9+.-]*:',target,re.I):continue
   linked=(p.parent/target).resolve()
   if not linked.is_relative_to(root) or not linked.is_file():broken.append({'file':name,'target':target})
 check('relative_markdown_links',not broken,broken)
 def js(n):return json.loads((root/n).read_text(encoding='utf-8-sig'))
 result=js('data/aggregate/frozen_results.json')
 pins=js('provenance/plot_inputs.json')
 check('plot_inputs_pinned',all(safe_rel(n) and sha((root/n).read_bytes())==h for n,h in pins.items()))
 assets=js('paper/asset_manifest.json')
 check('six_figures_seven_tables',len(assets)==13 and sum(a['kind']=='figure' for a in assets)==6 and sum(a['kind']=='table' for a in assets)==7)
 table=rows(root,'paper/tables/table_2_results.csv')
 good=[]
 for condition,row in zip(['C1','C2','C3'],table):
  v=result['conditions'][condition];low,high=result['condition_bootstrap_95_ci'][condition]['spearman']
  good.append(list(row.values())[1:]==[f"{v['spearman']:.4f}\n[{low:.4f}, {high:.4f}]",*[f"{v[k]:.4f}" for k in ['pearson','mae','bias','sd_ratio']]])
 check('main_table_matches_frozen_numbers',len(table)==3 and all(good))
 points=rows(root,'data/aggregate/plot_points.csv')
 h8={r['R_item']:r for r in rows(root,'data/aggregate/stage1_h8_item_results.csv')}
 k={r['K']:r for r in rows(root,'data/aggregate/k_stability_results.csv')}
 paired={r['condition']:r for r in rows(root,'data/aggregate/paired_condition_comparisons.csv')}
 ok=[]
 for p in points:
  name,series,metric=p['asset'],p['series'],p['metric']
  if metric=='delta_spearman':
   r=paired[series[:2]];values=[float(r[x]) for x in ['rho_delta_other_minus_c1','rho_delta_ci_low','rho_delta_ci_high']]
  elif name=='figure_s1_h8':
   r=h8[series];values=[float(r[x]) for x in ['spearman','spearman_ci_low','spearman_ci_high']]
  elif metric=='reliability':
   r=k[series];values=[float(r[x]) for x in ['reliability','ci_low','ci_high']]
  else:values=[result['conditions'][series][metric],*result['condition_bootstrap_95_ci'][series][metric]]
  ok.append([float(p[x]) for x in ['estimate','ci_low','ci_high']]==values)
 check('all_19_point_estimates_and_intervals',len(points)==19 and all(ok))

 check('h8_and_reliability_sample_sizes',result['stage1_h8']['n']==342 and result['k_stability']['participants']==30)
 condition_rows=rows(root,'methods/condition_membership.csv')
 check('condition_item_counts',[sum(r['included']=='true' for r in condition_rows if r['condition']==c) for c in ['C1_broad_condition','C2_strict_condition','C3_task_proximal_condition']]==[46,44,47])
 schedule=rows(root,'data/aggregate/table_s2_list_schedule.csv')
 check('list_schedule_21_rows',len(schedule)==21)
 for number in [1,2]:
  source=rows(root,f'methods/lists/list_{number}.csv')
  check(f'list_{number}_same_points',len(source)==21 and all(float(a['safe_points'])==float(b[f'list_{number}_safe_points']) for a,b in zip(source,schedule)))
 check('all_24_references',len((root/'paper/manuscript.md').read_text(encoding='utf-8').split('## 參考文獻')[1].strip().split('\n\n'))==24)
 check('no_individual_array_in_aggregate_json',not has_large_array(result))
 status=js('RELEASE_STATUS.json')
 check('release_limits_explicit',release_status_valid(status))
 spec=importlib.util.spec_from_file_location('public_answer_schema',root/'tools/model_answers.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 summary=js('provenance/model_answer_export.json')
 for source,count in module.COUNTS.items():
  rel=f'data/model_answers/{source}.jsonl'
  try:
   records=[module.strict_json(line) for line in (root/rel).read_text(encoding='utf-8').splitlines()]
   module.validate_collection(records,source)
   ok=summary['counts'][source]==count and summary['files'][f'{source}.jsonl']==sha((root/rel).read_bytes())
  except (ValueError,TypeError,KeyError):ok=False
  check(f'model_answers_{source}_allowlist_counts_and_hash',ok)
 check('model_answers_total',summary['total_canonical_answers']==2634)
 return checks
def has_large_array(v):
 if isinstance(v,list):return len(v)>=342 or any(has_large_array(x) for x in v)
 if isinstance(v,dict):return any(has_large_array(x) for x in v.values())
 return False
def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--root',type=Path,default=ROOT)
 args=parser.parse_args()
 try:
  checks=verify(args.root);ok=all(c['passed'] for c in checks)
  result={'status':'PASS' if ok else 'FAIL','checks':checks,'scope':'Integrity and selected disclosure/consistency checks; not legal clearance, numerical reproduction, or proof against all secret formats.'}
 except Exception as e:ok=False;result={'status':'FAIL','error':str(e)}
 print(json.dumps(result,ensure_ascii=True,indent=2))
 return 0 if ok else 2
if __name__=='__main__':sys.exit(main())
