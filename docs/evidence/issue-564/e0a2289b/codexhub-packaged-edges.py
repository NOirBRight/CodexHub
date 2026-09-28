from pathlib import Path
import argparse, hashlib, json, os, platform, queue, re, secrets, shutil, subprocess, sys, tempfile, threading, time, uuid
from urllib.request import urlopen, Request
from urllib.error import HTTPError

parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,default=Path(tempfile.gettempdir())/'codexhub-real-switch-evidence.json')
parser.add_argument('--bin',type=Path,required=True)
parser.add_argument('--candidate-sha',required=True)
parser.add_argument('--source-root',type=Path,required=True)
parser.add_argument('--claude-cli',type=Path,required=True)
parser.add_argument('--claude-credentials',type=Path,required=True)
parser.add_argument('--codex-auth',type=Path,required=True)
parser.add_argument('--model-catalog',type=Path,required=True)
parser.add_argument('--deepseek-credentials',type=Path,required=True)
parser.add_argument('--third-responses',action='store_true')
parser.add_argument('--supplemental',action='store_true')
parser.add_argument('--thinking-max',action='store_true')
parser.add_argument('--edge-cases',action='store_true')
args=parser.parse_args()
REPO=args.source_root.expanduser().resolve()
sys.path.insert(0,str(REPO))
sys.path.insert(0,str(REPO/'src-python'))
try:
 from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
 from python_runtime_contract import require_python_313
require_python_313(__file__)
from scripts.e2e_claude_live_routes import snapshot_claude_subscription, free_port, provider_fixture, verify_candidate_binding, build_isolated_e2e_environment

def command_for(path,*arguments):
 executable=Path(path)
 values=[str(executable),*(str(value) for value in arguments)]
 if os.name=='nt' and executable.suffix.lower() in {'.cmd','.bat'}:
  return [os.environ.get('COMSPEC','cmd.exe'),'/d','/s','/c',subprocess.list2cmdline(values)]
 return values

def run_cli_version(path):
 result=subprocess.run(command_for(path,'--version'),capture_output=True,text=True,timeout=10)
 version=result.stdout.strip() or result.stderr.strip()
 if result.returncode or not version:raise RuntimeError('claude_cli_version_unavailable')
 return version[:120]

def private_file(path):
 if os.name!='nt':path.chmod(0o600)

def read_deepseek_key(path):
 try:payload=json.loads(path.read_text(encoding='utf-8'))
 except (OSError,UnicodeError,json.JSONDecodeError):raise RuntimeError('deepseek_credential_unreadable') from None
 value=payload.get('api_key') if isinstance(payload,dict) and payload.get('schema')=='codexhub.real-client-deepseek.v1' else None
 if not isinstance(value,str) or not value.strip() or any(character.isspace() for character in value):
  raise RuntimeError('deepseek_credential_invalid')
 return value.strip()

SOURCE_ROOT=REPO
CLI=args.claude_cli.expanduser().resolve()
SOURCE_SHA=args.candidate_sha
BINARY=args.bin.expanduser().resolve()
CLAUDE_CREDENTIALS=args.claude_credentials.expanduser().resolve()
CODEX_AUTH=args.codex_auth.expanduser().resolve()
MODEL_CATALOG=args.model_catalog.expanduser().resolve()
DEEPSEEK_CREDENTIALS=args.deepseek_credentials.expanduser().resolve()
for label,path in {'Claude CLI':CLI,'Claude credentials':CLAUDE_CREDENTIALS,'Codex auth':CODEX_AUTH,'model catalog':MODEL_CATALOG,'DeepSeek credentials':DEEPSEEK_CREDENTIALS,'candidate binary':BINARY}.items():
 if not path.is_file():raise SystemExit(f'{label} input is missing or not a file')
verify_candidate_binding(BINARY,BINARY.parent,SOURCE_ROOT,SOURCE_SHA)
if args.output.exists():raise SystemExit('output must be new')
REPORT={'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'candidate_sha':SOURCE_SHA,'package_binary_sha256':hashlib.sha256(BINARY.read_bytes()).hexdigest(),'platform':platform.platform(),'cli_version':run_cli_version(CLI),
        'implementation':'exact-SHA packaged Gateway; real CLI; real HTTPS upstreams',
        'scenario':'mapped_family_upstream_error_cli_cancel' if args.edge_cases else 'thinking_max_all_protocols' if args.thinking_max else 'supplemental_luna_third_party_responses' if args.supplemental else 'main_switch_compact_resume',
        'effort':'max' if args.thinking_max else 'low',
        'thinking_enabled':args.thinking_max,
        'bounds':{'overall_seconds':900,'turn_seconds':120,'gateway_requests':32,'requested_output_tokens':2048,'cli_environment_output_limit':2048,'provider_enforced_output_limit':'unknown','usage_output_check':'observed usage is compared with the requested limit; absent usage remains unknown'},
        'turns':[],'status':'running'}
watched={'claude_subscription':CLAUDE_CREDENTIALS,'codex_auth':CODEX_AUTH,'model_catalog':MODEL_CATALOG,'deepseek_credentials':DEEPSEEK_CREDENTIALS}
def fingerprint(p):return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
before={label:fingerprint(path) for label,path in watched.items()}
def save():
 args.output.parent.mkdir(parents=True,exist_ok=True)
 args.output.write_text(json.dumps(REPORT,ensure_ascii=False,indent=2)+'\n');private_file(args.output)
def stop(p):
 if p is None:return
 if p.poll() is None:
  if os.name=='nt':
   subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
  else:p.terminate()
  try:p.wait(5)
  except subprocess.TimeoutExpired:p.kill();p.wait()
def read_events(path):
 if not path.exists():return []
 out=[]
 for line in path.read_text().splitlines():
  try:out.append(json.loads(line))
  except ValueError:pass
 return out
SAFE_FIELDS=('event','request_id','model','upstream','status','upstream_status','inbound_format','upstream_format','route_provider_id','route_upstream_model','route_endpoint_url','request_observability_scope','request_observability_upstream_protocol','request_observability_attempt_index','usage_source','usage_missing_reason','usage_input_tokens','usage_output_tokens','usage_total_tokens','usage_cached_input_tokens','usage_cache_write_input_tokens','usage_reasoning_tokens','input_tokens','output_tokens','error_type','error_category','error_code','policy','field')
def safe_events(events):return [{k:e[k] for k in SAFE_FIELDS if k in e} for e in events if e.get('event') in {'request_start','request_complete','request_error','usage_observed','upstream_protocol_fallback','protocol_adaptation','response_usage'}]

with tempfile.TemporaryDirectory(prefix='codexhub-real-switch-') as directory:
 root=Path(directory)
 if os.name!='nt':root.chmod(0o700)
 home=root/'home';work=root/'work';codex=root/'codex';claude=home/'.claude'
 user_dirs=(work,claude,home/'tmp',home/'AppData/Roaming',home/'AppData/Local',codex/'proxy/config',codex/'model-catalogs',root/'config',root/'cache',root/'data')
 for p in user_dirs:p.mkdir(parents=True,exist_ok=True)
 event_path=codex/'proxy/codex-proxy-events.jsonl'
 providers=codex/'proxy/config/providers.toml'
 settings=codex/'proxy/settings.json'
 key=secrets.token_hex(24);port=free_port();deadline=time.monotonic()+900
 snapshot_claude_subscription(CLAUDE_CREDENTIALS,claude/'.credentials.json',minimum_remaining_seconds=1200)
 oauth=json.loads((claude/'.credentials.json').read_text())['claudeAiOauth']['accessToken']
 if os.name!='nt':(claude/'.credentials.json').chmod(0o600)
 auth=json.loads(CODEX_AUTH.read_text(encoding='utf-8'))
 if not isinstance(auth,dict) or not isinstance(auth.get('tokens'),dict):raise RuntimeError('codex_auth_input_invalid')
 auth['tokens'].pop('refresh_token',None)
 (codex/'auth.json').write_text(json.dumps(auth));private_file(codex/'auth.json')
 shutil.copy2(MODEL_CATALOG,codex/'model-catalogs/codexhub-model-catalog.json')
 dskey=read_deepseek_key(DEEPSEEK_CREDENTIALS)
 def configure(protocol):
  providers.write_text(provider_fixture().replace('upstream_format = "auto"',f'upstream_format = "{protocol}"').replace('available_upstream_formats = ["responses", "chat_completions", "anthropic_messages"]',f'available_upstream_formats = ["{protocol}"]'))
 configure('chat_completions')
 settings.write_text(json.dumps({'auto_sync_clients':False,'auto_start_gateway':False,'gateway_bind_address':'127.0.0.1','gateway_client_key':key,'gateway_enable_models':True,'gateway_enable_responses':True,'gateway_enable_chat_completions':True,'include_official_models':True,'official_disabled_models':[],'gateway_auto_retry_enabled':False,'gateway_auto_retry_max_attempts':1,'gateway_main_generation_retry_max_attempts':1,'gateway_compact_retry_max_attempts':1,'gateway_request_timeout_seconds':100,'proxy_port':port}))
 onboarding={'hasCompletedOnboarding':True,'projects':{str(work):{'hasTrustDialogAccepted':True}}}
 for p in (home/'.claude.json',claude/'.claude.json'):p.write_text(json.dumps(onboarding))
 (claude/'settings.json').write_text(json.dumps({'autoMemoryEnabled':False}))
 (root/'xdg-runtime').mkdir(mode=0o700)
 env=build_isolated_e2e_environment(dict(os.environ),root=home,codex_home=codex,claude_home=claude,runtime_home=codex,xdg_runtime=root/'xdg-runtime',resource_root=BINARY.parent,claude_bin=CLI,deepseek_key=dskey)
 env['CODEX_PROXY_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS']='1'
 cli_env=dict(env);cli_env.pop('DEEPSEEK_API_KEY',None)
 cli_env.update({'CLAUDE_CONFIG_DIR':str(claude),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{port}','ANTHROPIC_AUTH_TOKEN':oauth,'ANTHROPIC_CUSTOM_HEADERS':f'x-codexhub-gateway-key: {key}','CLAUDE_CODE_DISABLE_CLAUDE_MDS':'1','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','CLAUDE_CODE_DISABLE_THINKING':'1','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'2048','DISABLE_TELEMETRY':'1','DISABLE_AUTOUPDATER':'1','DISABLE_ERROR_REPORTING':'1','HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1','http_proxy':'http://127.0.0.1:1','https_proxy':'http://127.0.0.1:1'})
 if args.edge_cases:cli_env['ANTHROPIC_DEFAULT_HAIKU_MODEL']='claude-codexhub-deepseek-deepseek-flash'
 if args.thinking_max:cli_env.pop('CLAUDE_CODE_DISABLE_THINKING',None)
 gateway=client=None
 session=str(uuid.uuid4()); memory='MEM_'+secrets.token_hex(10)
 REPORT['isolation']={'temporary_home_config_work':True,'access_tokens_only':True,'refresh_tokens_excluded':True,'cli_restricted_read_only':True,'operator_gateway_untouched':True}
 def budget():
  if time.monotonic()>deadline:raise RuntimeError('overall_timeout')
  all_events=read_events(event_path)
  requests={e.get('request_id') for e in all_events if e.get('event')=='request_start'}
  if any(int(e.get('usage_output_tokens') or e.get('output_tokens') or 0)>2048 for e in all_events):raise RuntimeError('observed_output_budget_exceeded')
  if len(requests)>=32:raise RuntimeError('gateway_request_budget_exceeded')
 def launch(resume=False):
  command=[str(CLI),'-p','--input-format','stream-json','--output-format','stream-json','--verbose','--model','claude-opus-5-5','--restricted','--strict-mcp-config','--tools','Read','--allowedTools','Read','--permission-prompts','none','--effort','max' if args.thinking_max else 'low','--max-turns','24']
  if args.edge_cases:command+=['--include-partial-messages']
  command+=['--resume',session] if resume else ['--session-id',session]
  proc=subprocess.Popen(command_for(CLI,*command[1:]),cwd=work,env=cli_env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
  q=queue.Queue()
  def read():
   for line in proc.stdout:
    try:q.put(json.loads(line))
    except ValueError:pass
   q.put({'type':'process_end'})
  threading.Thread(target=read,daemon=True).start()
  return proc,q
 def send(value):client.stdin.write(json.dumps(value)+'\n');client.stdin.flush()
 def wait(kind,received):
  end=min(deadline,time.monotonic()+120)
  while time.monotonic()<end:
   budget()
   try:event=q.get(timeout=.25)
   except queue.Empty:continue
   received.append(event)
   if event.get('type')=='process_end':raise RuntimeError('cli_exited')
   if event.get('type')==kind:return event
  raise RuntimeError('turn_timeout')
 def turn(label,selected,protocol,initial=False,change=True):
  start=len(read_events(event_path)); observed=[]
  row={'label':label,'selected':selected,'expected_protocol':protocol,'status':'running'};REPORT['turns'].append(row);save()
  if selected.startswith('claude-codexhub-deepseek'):configure(protocol)
  if change:
   send({'type':'control_request','request_id':label,'request':{'subtype':'set_model','model':selected}})
   ack=wait('control_response',observed);row['model_control_success']=ack.get('response',{}).get('subtype')=='success'
   if not row['model_control_success']:
    row['control_error_statuses']=re.findall(r'\b[45]\d\d\b',str(ack.get('response',{}).get('error','')))[:3]
    raise RuntimeError('model_control_failed')
  value='FILE_'+secrets.token_hex(10);filename=f'{label}.txt';(work/filename).write_text(value+'\n')
  prompt=(f'Remember this session marker for all later turns: {memory}. ' if initial else 'Recall the session marker from our first turn. ')+f'Use the Read tool to read {filename} in the current directory. Then reply with exactly two tokens: the original session marker and this file content. Do not read any other file. Keep the session marker in any future summary.'
  send({'type':'user','session_id':session,'message':{'role':'user','content':prompt},'parent_tool_use_id':None})
  result=wait('result',observed)
  output=str(result.get('result',''))
  calls=[];results=[]
  for event in observed:
   content=event.get('message',{}).get('content',[])
   if not isinstance(content,list):continue
   for block in content:
    if block.get('type')=='tool_use':calls.append(block)
    if block.get('type')=='tool_result':results.append(block)
  row.update({'same_session':result.get('session_id')==session,'memory_recalled':memory in output,'fresh_file_recalled':value in output,'read_calls':sum(c.get('name')=='Read' for c in calls),'tool_results':len(results),'tool_ids_match':bool(calls) and {c.get('id') for c in calls}=={r.get('tool_use_id') for r in results},'cli_is_error':result.get('is_error'),'cli_subtype':result.get('subtype')})
  # Flush completion events before checking executed route evidence.
  time.sleep(.25)
  events=read_events(event_path)[start:];row['gateway_events']=safe_events(events)
  completed=[e for e in events if e.get('event')=='request_complete']
  row['executed_protocols']=sorted({e.get('request_observability_upstream_protocol',e.get('upstream_format','unknown')) for e in completed})
  row['status']='passed' if all((not result.get('is_error'),row['same_session'],row['memory_recalled'],row['fresh_file_recalled'],row['read_calls']>0,row['tool_ids_match'],protocol in row['executed_protocols'])) else 'failed'
  if row['status']=='failed':
   row['safe_error_statuses']=re.findall(r'\b[45]\d\d\b',output)[:3]
  save();print(json.dumps({k:v for k,v in row.items() if k!='gateway_events'}),flush=True)
  if row['status']!='passed':raise RuntimeError('turn_verification_failed')
 try:
  started=subprocess.run([str(BINARY),'start'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
  if started.returncode:raise RuntimeError('packaged_gateway_start_failed')
  for _ in range(100):
   try:
    with urlopen(f'http://127.0.0.1:{port}/health',timeout=1) as r: assert r.status==200
    break
   except OSError:
    if time.monotonic()>deadline:raise RuntimeError('packaged_gateway_health_timeout')
    time.sleep(.1)
  else:raise RuntimeError('packaged_gateway_health_timeout')
  client,q=launch()
  if args.edge_cases:
   start=len(read_events(event_path))
   request=Request(f'http://127.0.0.1:{port}/v1/messages',json.dumps({'model':'claude-opus-5-5','max_tokens':32,'messages':[{'role':'user','content':'Reply OK.'}]}).encode(),{'Content-Type':'application/json','Authorization':'Bearer sk-ant-oat01-synthetic-invalid-not-a-credential','x-codexhub-gateway-key':key,'anthropic-version':'2023-06-01'})
   status=None
   try:
    with urlopen(request,timeout=30) as response:status=response.status;response.read(4096)
   except HTTPError as error:status=error.code;error.close()
   time.sleep(.25)
   errors=read_events(event_path)[start:]
   REPORT['upstream_auth_error']={'http_status':status,'gateway_events':safe_events(errors)}
   if status!=401 or not any(e.get('event')=='request_complete' and e.get('status')==401 and e.get('upstream')=='anthropic_native' and e.get('request_observability_scope')=='executed_attempt' for e in errors):raise RuntimeError('native_upstream_error_not_verified')
   turn('native_initial','claude-opus-5-5','anthropic_messages',initial=True,change=False)
   configure('anthropic_messages')
   turn('mapped_haiku_to_deepseek','haiku','anthropic_messages')
   turn('native_after_mapping','claude-opus-5-5','anthropic_messages')
   start=len(read_events(event_path));observed=[]
   send({'type':'user','session_id':session,'message':{'role':'user','content':'Write a long numbered list of 1000 distinct common nouns. Start immediately; no tools or explanations.'},'parent_tool_use_id':None})
   streamed=False
   while not streamed:
    event=wait('stream_event',observed).get('event',{})
    streamed=event.get('type')=='content_block_delta' and event.get('delta',{}).get('type')=='text_delta'
   stop(client)
   until=min(deadline,time.monotonic()+15)
   while time.monotonic()<until:
    events=read_events(event_path)[start:]
    if any(e.get('event')=='request_complete' and e.get('status')==499 for e in events):break
    time.sleep(.1)
   REPORT['cli_cancellation']={'text_delta_before_termination':streamed,'gateway_events':safe_events(events),'status':'passed' if any(e.get('event')=='request_complete' and e.get('status')==499 for e in events) else 'failed'}
   if REPORT['cli_cancellation']['status']!='passed':raise RuntimeError('cli_cancellation_not_verified')
  elif args.thinking_max:
   turn('native_initial','claude-opus-5-5','anthropic_messages',initial=True,change=False)
   turn('codex_luna_max','claude-codexhub-gpt-6-luna','responses')
   turn('deepseek_responses_max','claude-codexhub-deepseek-deepseek-flash','responses')
   turn('deepseek_chat_max','claude-codexhub-deepseek-deepseek-flash','chat_completions')
   turn('deepseek_messages_max','claude-codexhub-deepseek-deepseek-flash','anthropic_messages')
   turn('native_return','claude-opus-5-5','anthropic_messages')
  elif args.supplemental:
   turn('native_initial','claude-opus-5-5','anthropic_messages',initial=True,change=False)
   turn('codex_luna','claude-codexhub-gpt-6-luna','responses')
   turn('deepseek_responses','claude-codexhub-deepseek-deepseek-flash','responses')
   turn('native_return','claude-opus-5-5','anthropic_messages')
  else:
   turn('native_initial','claude-opus-5-5','anthropic_messages',initial=True,change=False)
   turn('codex_luna','claude-codexhub-gpt-6-luna','responses')
   turn('deepseek_chat','claude-codexhub-deepseek-deepseek-flash','chat_completions')
   turn('deepseek_messages','claude-codexhub-deepseek-deepseek-flash','anthropic_messages')
   turn('native_return','claude-opus-5-5','anthropic_messages')
   # Manual compact is a client command; require its explicit boundary, not an answer.
   observed=[];start=len(read_events(event_path))
   send({'type':'user','session_id':session,'message':{'role':'user','content':'/compact Preserve the original session marker and note that it must be repeated on request.'},'parent_tool_use_id':None})
   result=wait('result',observed)
   boundaries=[e for e in observed if e.get('subtype')=='compact_boundary']
   REPORT['manual_compaction']={'boundary_observed':bool(boundaries),'cli_is_error':result.get('is_error'),'gateway_events':safe_events(read_events(event_path)[start:])};save()
   if not boundaries:raise RuntimeError('manual_compaction_boundary_missing')
   turn('post_compact','claude-opus-5-5','anthropic_messages',change=False)
   stop(client);client,q=launch(resume=True)
   turn('explicit_resume','claude-opus-5-5','anthropic_messages',change=False)
   if args.third_responses:
    turn('deepseek_responses','claude-codexhub-deepseek-deepseek-flash','responses')
  REPORT['status']='passed'
 except Exception as error:
  REPORT['status']='failed';REPORT['failure_class']=type(error).__name__;REPORT['failure']=str(error) if isinstance(error,RuntimeError) else type(error).__name__
  REPORT['gateway_tail']=safe_events(read_events(event_path)[-50:])
 finally:
  stop(client)
  stopped=subprocess.run([str(BINARY),'stop'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
  REPORT['packaged_gateway_stop_succeeded']=stopped.returncode==0
  events=read_events(event_path)
  REPORT['gateway_request_count']=len({e.get('request_id') for e in events if e.get('event')=='request_start'})
  REPORT['source_files_unchanged']={label:fingerprint(path)==before[label] for label,path in watched.items()}
  REPORT['host_state_unchanged']=all(REPORT['source_files_unchanged'].values())
  if not REPORT['host_state_unchanged'] or not REPORT['packaged_gateway_stop_succeeded']:
   REPORT['status']='failed';REPORT['failure']='cleanup_or_source_integrity_failed'
  save()
print(json.dumps({'status':REPORT['status'],'failure':REPORT.get('failure'),'gateway_requests':REPORT.get('gateway_request_count'),'host_state_unchanged':REPORT['host_state_unchanged'],'evidence':str(args.output)}),flush=True)
sys.exit(0 if REPORT['status']=='passed' else 1)
