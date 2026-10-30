import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import queue
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/codex_context_handoff.py'
SPEC = importlib.util.spec_from_file_location('context_handoff', SCRIPT)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
INSTALL_SPEC = importlib.util.spec_from_file_location('handoff_installer', SCRIPT.with_name('install_codex_context_handoff.py'))
import sys
sys.modules['codex_context_handoff'] = m
installer = importlib.util.module_from_spec(INSTALL_SPEC)
INSTALL_SPEC.loader.exec_module(installer)


class FakeServer:
    calls = []
    answer = None
    title = None
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def rpc(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == 'thread/start': return {'thread': {'id': 'successor-1'}}
        if method == 'thread/read': return {'thread': {'turns': [], 'name': self.title}}
        if method == 'thread/list': return {'data': [{'id': 'successor-1'}]}
        return {}
    def seed(self, *args, **kwargs):
        self.calls.append(('seed', args))
        return [json.dumps(self.answer)]


class FakeWorkerServer(FakeServer):
    needs_approval = False
    def rpc(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == 'turn/start': return {'turn': {'id': 'automatic-turn'}}
        if method == 'thread/resume': return {'thread': {'turns': []}}
        return {}
    def receive(self, timeout):
        if self.needs_approval:
            return {'id': 99, 'method': 'item/commandExecution/requestApproval', 'params': {}}
        return {'method': 'turn/completed', 'params': {'threadId': 'successor-1',
                'turn': {'id': 'automatic-turn', 'status': 'completed'}}}


class FakeDesktop:
    calls = []
    reject_send = False
    def __init__(self, **kwargs): self.calls.append(('connect', kwargs))
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'send_message_to_thread' and self.reject_send:
            raise RuntimeError('Desktop rejected the action')
        return {'threadId': 'successor-1', 'hostId': 'local', 'status': 'accepted'}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'state'
        self.cwd = self.base/'work'; self.cwd.mkdir()
        self.sid = 'source-1'
        self.directory = m.session_dir(self.root, self.sid)
        self.transcript = self.base/'rollout.jsonl'
        self.settings = {'enabled': True, 'prepare_tokens': 350000, 'rollover_tokens': 400000,
                         'refresh_tokens': 20000, 'allowed_sessions': []}
        m.atomic_json(self.root/'settings.json', self.settings)
        FakeServer.calls = []
        FakeServer.answer = None
        FakeServer.title = None
        FakeWorkerServer.needs_approval = False
        FakeDesktop.calls = []
        FakeDesktop.reject_send = False
        self.payload = {'session_id': self.sid, 'transcript_path': str(self.transcript),
            'cwd': str(self.cwd), 'model': 'gpt-6.1-sol', 'hook_event_name': 'PostToolUse'}

    def transcript_at(self, tokens, source='vscode', extra=()):
        rows = [{'type':'session_meta','payload':{'id':self.sid,'source':source}},
                {'type':'token_usage_record','payload':{'usage':{'input_tokens':tokens,'output_tokens':0},
                   'thread_token_usage':{'input_tokens':999999999}}}, *extra]
        self.transcript.write_text(''.join(json.dumps(row)+'\n' for row in rows))

    def checkpoint(self, status='active', jobs=()):
        state = m.load_json(self.directory/'state.json')
        value = {'version':1,'source_thread_id':self.sid,'cwd':str(self.cwd),'status':status,
            'user_prompt_epoch':state['prompt_epoch'],'goal':'Continue the synthetic task safely',
            'constraints':['Do not deploy; preserve unrelated edits'],'completed':['Read the fixture'],
            'remaining':['Implement the fixture'] if status=='active' else [],'files':[],
            'evidence':['verification.txt'],'running_operations':list(jobs),'next_action':'Inspect the fixture diff'}
        m.atomic_json(self.directory/'handoff.json',value)
        FakeServer.answer = {key:value[key] for key in ('source_thread_id','goal','constraints','next_action')}
        return value

    def test_queue_service_is_owned_supervised_and_uses_the_existing_loader(self):
        calls=[];units=self.base/'units'
        report=installer.install_queue_service(self.root,self.base/'loader.py',unit_directory=units,
            runner=lambda args,**kwargs:calls.append(args))
        unit=Path(report['unit']).read_text()
        self.assertIn('queue-worker',unit)
        self.assertIn('Restart=on-failure',unit)
        self.assertIn('UMask=0077',unit)
        self.assertTrue(m.load_json(self.root/'settings.json')['queue']['enabled'])
        self.assertEqual(calls[-1],['systemctl','--user','restart','codex-context-handoff-queue.service'])


    def test_queue_installer_preserves_an_unowned_existing_unit(self):
        units=self.base/'units';units.mkdir()
        path=units/'codex-context-handoff-queue.service';path.write_text('Unrelated existing service\n')
        with self.assertRaisesRegex(ValueError,'unrecognized'):
            installer.install_queue_service(self.root,self.base/'loader.py',unit_directory=units,
                                             runner=lambda *args,**kwargs:self.fail('Unexpected service action'))
        self.assertEqual(path.read_text(),'Unrelated existing service\n')


    def test_tool_directory_change_preserves_accepted_handoff_workspace(self):
        self.settings['continuation_backend'] = 'desktop'
        self.automatic_fixture()
        other = self.base/'other-tool-directory';other.mkdir()
        self.payload.update(cwd=str(other),hook_event_name='PostToolUse')
        m.hook(self.payload,self.root)
        state = m.load_json(self.directory/'state.json')
        self.assertEqual(state['cwd'],str(self.cwd))
        self.assertEqual(state['checkpoint']['cwd'],str(self.cwd))
        self.assertTrue(m.checkpoint_is_current(self.directory,state))
        result = m.continue_work(self.root,self.sid,desktop_factory=FakeDesktop)
        self.assertEqual(result['status'],'desktop_dispatched')
        self.assertEqual(sum(name=='send_message_to_thread' for name,_ in FakeDesktop.calls),1)


    def test_stop_from_another_directory_keeps_the_verified_workspace(self):
        self.settings['continuation_backend'] = 'desktop'
        self.automatic_fixture(stopped=False)
        other = self.base/'stop-directory';other.mkdir()
        self.payload.update(cwd=str(other),hook_event_name='Stop')
        with patch.object(m,'schedule_continuation',return_value=True):
            m.hook(self.payload,self.root)
        state = m.load_json(self.directory/'state.json')
        self.assertEqual(state['cwd'],str(self.cwd))
        self.assertTrue(m.checkpoint_is_current(self.directory,state))
        result = m.continue_work(self.root,self.sid,desktop_factory=FakeDesktop)
        self.assertEqual(result['status'],'desktop_dispatched')


    def test_new_user_directory_is_observed_and_still_cancels_old_handoff(self):
        self.automatic_fixture()
        other = self.base/'new-request-directory';other.mkdir()
        self.payload.update(cwd=str(other),hook_event_name='UserPromptSubmit',turn_id='new-source-request')
        m.hook(self.payload,self.root)
        self.assertEqual(m.load_json(self.directory/'state.json')['cwd'],str(other))
        self.assertEqual(m.continue_work(self.root,self.sid,desktop_factory=FakeDesktop)['status'],
                         'cancelled_new_source_input')
        self.assertFalse(any(name=='send_message_to_thread' for name,_ in FakeDesktop.calls))


    def test_real_workspace_change_is_not_hidden_by_pinned_directory(self):
        subprocess.run(['git','init','-q',str(self.cwd)],check=True)
        file = self.cwd/'pending.txt';file.write_text('before')
        self.automatic_fixture()
        file.write_text('actual source change')
        other = self.base/'other-directory';other.mkdir()
        self.payload.update(cwd=str(other),hook_event_name='PostToolUse')
        m.hook(self.payload,self.root)
        self.assertEqual(m.continue_work(self.root,self.sid,desktop_factory=FakeDesktop)['status'],
                         'checkpoint_changed')
        self.assertFalse(any(name=='send_message_to_thread' for name,_ in FakeDesktop.calls))


    def test_queued_recovery_restores_legacy_callback_directory_only_for_exact_accepted_state(self):
        self.settings['continuation_backend']='desktop'
        self.automatic_fixture()
        state=m.load_json(self.directory/'state.json');state['checkpoint'].pop('cwd')
        state['cwd']=str(self.base/'wrong-callback-directory')
        state['auto_continuation']={'status':'checkpoint_changed'}
        m.atomic_json(self.directory/'state.json',state)
        self.assertTrue(m.restore_handoff_workspace(self.directory))
        self.assertEqual(m.load_json(self.directory/'state.json')['cwd'],str(self.cwd))
        self.assertEqual(m.continue_work(self.root,self.sid,desktop_factory=FakeDesktop)['status'],'desktop_dispatched')


    def test_workspace_restoration_never_accepts_changed_files_or_newer_input(self):
        self.automatic_fixture()
        state=m.load_json(self.directory/'state.json');state['cwd']=str(self.base/'wrong-directory')
        state['prompt_epoch']+=1;m.atomic_json(self.directory/'state.json',state)
        self.assertFalse(m.restore_handoff_workspace(self.directory))
        state['prompt_epoch']-=1;m.atomic_json(self.directory/'state.json',state)
        (self.directory/'handoff.json').write_text((self.directory/'handoff.json').read_text()+' ')
        self.assertFalse(m.restore_handoff_workspace(self.directory))


    def test_disabled_is_a_noop_even_without_a_transcript(self):
        self.settings['enabled']=False;m.atomic_json(self.root/'settings.json',self.settings)
        self.assertEqual(m.hook(self.payload,self.root),{})

    def test_numbered_successor_preserves_original_title_and_links(self):
        FakeServer.title = 'Continue: a deliberate custom title · 12345678'
        self.transcript_at(400000);m.hook(self.payload,self.root);self.checkpoint()
        result = m.ready(self.root,self.sid,FakeServer)
        self.assertEqual(result['successor_title'], 'Part 2 · ' + FakeServer.title)
        successor = m.load_json(self.root/'sessions/successor-1/state.json')
        self.assertEqual((successor['root_thread_id'], successor['previous_thread_id'], successor['part']),
                         (self.sid, self.sid, 2))
        prompt = next(args[1] for name,args in FakeServer.calls if name == 'seed')
        self.assertTrue(prompt.startswith('[Original chat](codex://threads/source-1) · [Previous part]'))
        self.assertIn('Do not modify files', prompt)
        state = m.load_json(self.directory/'state.json')
        self.assertTrue(m.continuation_prompt('checkpoint.json',state).startswith('[Original chat]'))
        self.assertNotIn('thread/resume', [name for name,_ in FakeServer.calls])

    def test_legacy_edges_recover_part_without_parsing_repeated_titles(self):
        for source,target in [('original','middle'),('middle',self.sid)]:
            m.atomic_json(m.session_dir(self.root,source)/'state.json', {'successor_id':target})
        lineage = m.lineage_for(self.root,self.sid,lambda ident: {'name':'The original task'})
        self.assertEqual(lineage, {'root_title':'The original task','root_thread_id':'original',
                                  'part':3,'previous_thread_id':'middle'})
        m.atomic_json(m.session_dir(self.root,'original')/'state.json',
                      {'successor_id':'middle','root_title':'The original task'})
        self.assertEqual(m.lineage_for(self.root,self.sid,lambda _: self.fail('Saved title must be stable')),lineage)

    def test_ambiguous_or_cyclic_edges_fail_without_renaming(self):
        for source in ['one','two']:
            m.atomic_json(m.session_dir(self.root,source)/'state.json', {'successor_id':self.sid})
        with self.assertRaisesRegex(ValueError,'Ambiguous'):
            m.lineage_for(self.root,self.sid,lambda _: {})
        m.atomic_json(m.session_dir(self.root,'two')/'state.json', {'successor_id':'one'})
        m.atomic_json(self.directory/'state.json', {'successor_id':'two'})
        with self.assertRaisesRegex(ValueError,'Cyclic'):
            m.lineage_for(self.root,self.sid,lambda _: {})

    def test_lineage_survives_successor_hook_and_old_transfers_still_work(self):
        lineage = {'root_title':'Original','root_thread_id':'root','part':2,'previous_thread_id':'root'}
        m.atomic_json(self.directory/'state.json', dict(lineage,session_id=self.sid,prompt_epoch=0))
        self.transcript_at(100);m.hook(self.payload,self.root)
        state = m.load_json(self.directory/'state.json')
        self.assertEqual({key:state[key] for key in lineage},lineage)
        self.assertEqual(m.lineage_header({}),'')

    def test_prepare_threshold_and_instruction_throttling(self):
        self.transcript_at(349999);self.assertEqual(m.hook(self.payload,self.root),{})
        self.transcript_at(350000);first=m.hook(self.payload,self.root)
        self.assertIn('prepare a concise',first['hookSpecificOutput']['additionalContext'])
        self.assertEqual(m.hook(self.payload,self.root),{})
        self.transcript_at(400000);last=m.hook(self.payload,self.root)
        self.assertIn('finish a safe handoff',last['hookSpecificOutput']['additionalContext'])

    def test_never_uses_cumulative_usage_or_embedded_tool_text(self):
        self.transcript_at(1000,extra=[{'type':'response_item','payload':{'type':'custom_tool_call_output',
            'output':json.dumps({'type':'token_usage_record','payload':{'usage':{'input_tokens':900000}}})}}])
        self.assertEqual(m.hook(self.payload,self.root),{})
        self.assertEqual(m.load_json(self.directory/'state.json')['context_tokens'],1000)

    def test_subagents_and_mismatched_session_metadata_are_ignored(self):
        self.transcript_at(400000,source={'subagent':{'parent_thread_id':'parent'}})
        self.assertEqual(m.hook(self.payload,self.root),{})
        self.transcript_at(400000)
        data=self.transcript.read_text().replace('source-1','another-session')
        self.transcript.write_text(data);self.assertEqual(m.hook(self.payload,self.root),{})

    def test_stop_requests_one_continuation_but_never_loops_forever(self):
        self.transcript_at(400000);self.payload['hook_event_name']='Stop'
        self.assertEqual(m.hook(self.payload,self.root)['decision'],'block')
        self.payload['stop_hook_active']=True
        result=m.hook(self.payload,self.root)
        self.assertNotIn('decision',result);self.assertIn('not forced',result['systemMessage'])

    def test_checkpoint_accepted_before_rollover_without_creating_a_chat(self):
        self.transcript_at(350000);m.hook(self.payload,self.root);self.checkpoint()
        self.assertEqual(m.ready(self.root,self.sid,FakeServer)['status'],'checkpoint_ready')
        self.assertEqual(FakeServer.calls,[])

    def test_finished_task_below_threshold_and_running_operations_do_not_start_successors(self):
        self.transcript_at(350000);m.hook(self.payload,self.root)
        self.checkpoint(status='complete')
        self.assertEqual(m.ready(self.root,self.sid,FakeServer)['status'],'checkpoint_ready')
        self.transcript_at(400000);m.hook(self.payload,self.root)
        self.checkpoint(jobs=[{'pid':123,'purpose':'still running'}])
        self.assertEqual(m.ready(self.root,self.sid,FakeServer)['status'],'deferred_running_operations')
        self.assertEqual(FakeServer.calls,[])

    def test_finished_chat_also_rolls_over_at_400k_without_inventing_pending_work(self):
        self.transcript_at(400000);m.hook(self.payload,self.root);self.checkpoint(status='complete')
        result=m.ready(self.root,self.sid,FakeServer)
        self.assertEqual(result['status'],'successor_ready')
        self.assertEqual(m.load_json(self.directory/'handoff.json')['remaining'],[])

    def test_new_user_request_invalidates_old_checkpoint(self):
        self.transcript_at(350000);m.hook(self.payload,self.root);self.checkpoint()
        m.ready(self.root,self.sid,FakeServer)
        self.payload.update(hook_event_name='UserPromptSubmit',turn_id='new-turn')
        m.hook(self.payload,self.root)
        self.assertFalse(m.checkpoint_is_current(self.directory,m.load_json(self.directory/'state.json')))
        with self.assertRaisesRegex(ValueError,'latest user request'):
            m.ready(self.root,self.sid,FakeServer)

    def test_complete_checkpoint_cannot_discard_remaining_work(self):
        self.transcript_at(400000);m.hook(self.payload,self.root)
        data=self.checkpoint();data['status']='complete';m.atomic_json(self.directory/'handoff.json',data)
        with self.assertRaisesRegex(ValueError,'remaining work'):m.ready(self.root,self.sid,FakeServer)

    def test_earlier_unverified_checkpoint_is_reset_after_compaction(self):
        self.transcript_at(400000);m.hook(self.payload,self.root);self.checkpoint()
        FakeServer.answer={}
        with self.assertRaises(ValueError):m.ready(self.root,self.sid,FakeServer)
        self.transcript_at(100000);m.hook(self.payload,self.root)
        self.assertNotIn('checkpoint',m.load_json(self.directory/'state.json'))

    def test_successor_is_verified_once_and_source_is_never_archived(self):
        self.transcript_at(400000);m.hook(self.payload,self.root);self.checkpoint()
        first=m.ready(self.root,self.sid,FakeServer)
        second=m.ready(self.root,self.sid,FakeServer)
        self.assertEqual(first['successor_id'],second['successor_id'])
        self.assertEqual(sum(name=='thread/start' for name,_ in FakeServer.calls),1)
        self.assertFalse(any(name=='thread/archive' for name,_ in FakeServer.calls))
        self.payload['hook_event_name']='Stop'
        self.assertFalse(m.hook(self.payload,self.root)['continue'])

    def test_completed_handoff_prevents_old_turn_tools_but_preserves_new_user_requests(self):
        self.transcript_at(400000);m.hook(self.payload,self.root);self.checkpoint()
        m.ready(self.root,self.sid,FakeServer)
        self.payload['hook_event_name']='PreToolUse'
        self.assertEqual(m.hook(self.payload,self.root)['hookSpecificOutput']['permissionDecision'],'deny')
        self.payload.update(hook_event_name='UserPromptSubmit',turn_id='genuine-new-user-turn')
        m.hook(self.payload,self.root)
        self.payload['hook_event_name']='PreToolUse';self.assertEqual(m.hook(self.payload,self.root),{})
        self.payload['hook_event_name']='PreCompact';self.assertEqual(m.hook(self.payload,self.root),{})

    def test_bad_initialization_never_marks_handoff_ready_or_blocks_compaction(self):
        self.transcript_at(400000);m.hook(self.payload,self.root);self.checkpoint()
        FakeServer.answer={'goal':'wrong'}
        with self.assertRaises(ValueError):m.ready(self.root,self.sid,FakeServer)
        state=m.load_json(self.directory/'state.json');self.assertFalse(state.get('successor_ready',False))
        self.payload['hook_event_name']='PreCompact'
        self.assertNotIn('continue',m.hook(self.payload,self.root))

    def test_rejects_wrong_chat_workspace_credentials_and_oversized_files(self):
        self.transcript_at(400000);m.hook(self.payload,self.root)
        state=m.load_json(self.directory/'state.json')
        for field,value in [('source_thread_id','other'),('cwd',str(self.base/'other')),('goal','sk-'+'a'*30)]:
            data=self.checkpoint();data[field]=value;m.atomic_json(self.directory/'handoff.json',data)
            with self.assertRaises(ValueError):m.validate_checkpoint(self.directory/'handoff.json',state)
        self.checkpoint();(self.directory/'handoff.json').write_text(' '*13000)
        with self.assertRaises(ValueError):m.validate_checkpoint(self.directory/'handoff.json',state)

    def test_dirty_file_content_changes_invalidate_workspace_identity(self):
        subprocess.run(['git','init','-q',str(self.cwd)],check=True)
        file=self.cwd/'file.txt';file.write_text('first')
        before=m.workspace_fingerprint(self.cwd)
        file.write_text('other value')
        self.assertNotEqual(before,m.workspace_fingerprint(self.cwd))

    def test_windows_transcript_paths_translate_without_shell_execution(self):
        self.assertEqual(str(m.local_path(r'C:\Users\Example\file.jsonl')),'/mnt/c/Users/Example/file.jsonl')
        with self.assertRaises(ValueError):m.session_dir(self.root,'../other')

    def test_dead_appserver_fails_promptly_instead_of_waiting_a_minute(self):
        start=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'closed its connection'):
            m.AppServer(binary='/bin/false')
        self.assertLess(time.monotonic()-start,2)

    def test_bootstrap_timeout_interrupts_its_own_turn(self):
        server=object.__new__(m.AppServer);calls=[]
        def rpc(method,params):
            calls.append(method)
            return {'turn':{'id':'test-turn'}} if method=='turn/start' else {}
        def receive(timeout):raise queue.Empty
        server.rpc=rpc;server.receive=receive
        with self.assertRaises(TimeoutError):server.seed('test-thread','read-only init',timeout=.01)
        self.assertEqual(calls,['turn/start','turn/interrupt'])

    def test_nested_workspace_uses_repository_root_for_dirty_paths(self):
        subprocess.run(['git','init','-q',str(self.cwd)],check=True)
        nested=self.cwd/'sub';nested.mkdir()
        file=self.cwd/'root.txt';file.write_text('first')
        before=m.workspace_fingerprint(nested)
        file.write_text('changed')
        self.assertNotEqual(before,m.workspace_fingerprint(nested))

    def test_reinstalled_hooks_preserve_existing_rtk_and_other_handlers(self):
        existing={'hooks':{'PostToolUse':[{'hooks':[{'type':'command','command':'existing'}]}],
                           'PreToolUse':[{'matcher':'Bash','hooks':[{'type':'command','command':'rtk hook codex'}]}]}}
        original=json.loads(json.dumps(existing))
        lib=Path('/private/handoff')
        merged=installer.merge_hooks(existing,'/usr/bin/python3 /private/handoff/v1.py hook',lib)
        merged=installer.merge_hooks(merged,'/usr/bin/python3 /private/handoff/v2.py hook',lib)
        self.assertEqual(existing,original)
        self.assertEqual(merged['hooks']['PreToolUse'][0],original['hooks']['PreToolUse'][0])
        self.assertEqual(len(merged['hooks']['PreToolUse']),2)
        self.assertEqual(merged['hooks']['PostToolUse'][0],original['hooks']['PostToolUse'][0])
        self.assertEqual(len(merged['hooks']['PostToolUse']),2)

    def test_reinstall_removes_unsupported_context_limit_only_from_owned_hooks(self):
        lib=Path('/private/handoff')
        existing={'hooks':{'Stop':[{'hooks':[
            {'type':'command','command':'other','additionalContextLimit':25},
            {'type':'command','command':'/private/handoff/v1.py hook','additionalContextLimit':800}]}],
            'PreCompact':[{'hooks':[{'type':'command','command':'/private/handoff/v1.py hook',
                                    'additionalContextLimit':800}]}]}}
        merged=installer.merge_hooks(existing,'/usr/bin/python3 /private/handoff/v2.py hook',lib)
        self.assertEqual(merged['hooks']['Stop'][0]['hooks'][0]['additionalContextLimit'],25)
        self.assertNotIn('additionalContextLimit',merged['hooks']['Stop'][0]['hooks'][1])
        self.assertNotIn('additionalContextLimit',merged['hooks']['PreCompact'][0]['hooks'][0])

    def test_trust_updates_only_this_hooks_state_and_preserves_model_settings(self):
        import tomllib
        text='model = "gpt-6.1-sol"\nmodel_context_window = 500000\n[hooks.state."existing"]\nenabled = true\ntrusted_hash = "old"\n'
        rows=[{'key':'new','currentHash':'sha256:abc'}]
        once=installer.trust_config(text,rows)
        twice=installer.trust_config(once,[{'key':'new','currentHash':'sha256:def'}])
        data=tomllib.loads(twice)
        self.assertEqual(data['model_context_window'],500000)
        self.assertEqual(data['hooks']['state']['existing']['trusted_hash'],'old')
        self.assertEqual(data['hooks']['state']['new']['trusted_hash'],'sha256:def')

    def test_failed_initialization_retains_source_and_all_checkpoint_bytes(self):
        self.transcript_at(400000);m.hook(self.payload,self.root);self.checkpoint()
        original=(self.directory/'handoff.json').read_bytes()
        FakeServer.answer={}
        with self.assertRaises(ValueError):m.ready(self.root,self.sid,FakeServer)
        self.assertEqual((self.directory/'handoff.json').read_bytes(),original)
        self.assertFalse(any(method=='thread/archive' for method,_ in FakeServer.calls))
        self.assertEqual(next(self.directory.glob('handoff-*.json')).read_bytes(),original)

    def automatic_fixture(self, status='active', stopped=True):
        self.transcript_at(400000);m.hook(self.payload,self.root);self.checkpoint(status=status)
        m.ready(self.root,self.sid,FakeServer)
        self.settings.update(auto_continue=True,auto_open=True,source_stop_timeout=0)
        m.atomic_json(self.root/'settings.json',self.settings)
        if stopped:
            self.payload['hook_event_name']='Stop'
            with patch.object(m,'schedule_continuation',return_value=True):
                m.hook(self.payload,self.root)

    def test_scheduling_waits_for_stop_without_a_worker_or_timeout(self):
        self.automatic_fixture(stopped=False)
        with patch.object(m.subprocess,'Popen') as launch:
            self.assertTrue(m.schedule_continuation(self.root,self.sid))
            self.assertEqual(m.load_json(self.directory/'state.json')['auto_continuation']['status'],
                             'waiting_for_source_stop')
            launch.assert_not_called()

    def test_late_stop_restarts_timed_out_dispatch_exactly_once(self):
        self.automatic_fixture(stopped=False)
        self.assertEqual(m.continue_work(self.root,self.sid,FakeWorkerServer)['status'],
                         'source_stop_not_confirmed')
        with patch.object(m.subprocess,'Popen') as launch, patch.object(m,'process_identity',
                side_effect=lambda pid: 'start-1' if pid == 12345 else None):
            launch.return_value.pid = 12345
            self.payload['hook_event_name']='Stop'
            self.assertFalse(m.hook(self.payload,self.root)['continue'])
            m.hook(self.payload,self.root)
            self.assertEqual(launch.call_count,1)
            self.assertIn('supervise',launch.call_args.args[0])
        self.settings['continuation_backend']='desktop';m.atomic_json(self.root/'settings.json',self.settings)
        self.assertEqual(m.continue_work(self.root,self.sid,desktop_factory=FakeDesktop)['status'],
                         'desktop_dispatched')
        with patch.object(m.subprocess,'Popen') as launch:
            m.hook(self.payload,self.root)
            launch.assert_not_called()
            self.assertEqual(sum(n=='send_message_to_thread' for n,_ in FakeDesktop.calls),1)

    def test_crashed_coordinator_restarts_but_reused_pid_does_not_suppress_recovery(self):
        self.automatic_fixture()
        m.update_state(self.directory,auto_continuation={'status':'scheduled'},
                       continuation_coordinator={'pid':12345,'identity':'old-start'})
        with patch.object(m.subprocess,'Popen') as launch, patch.object(m,'process_identity',return_value='new-start'):
            launch.return_value.pid=12345
            self.assertTrue(m.schedule_continuation(self.root,self.sid))
            self.assertTrue(m.schedule_continuation(self.root,self.sid))
            self.assertEqual(launch.call_count,1)

    def test_failed_launch_can_retry_on_a_later_stop(self):
        self.automatic_fixture()
        with patch.object(m.subprocess,'Popen',side_effect=OSError('synthetic')):
            self.assertFalse(m.schedule_continuation(self.root,self.sid))
        self.assertEqual(m.load_json(self.directory/'state.json')['auto_continuation']['status'],'launch_failed')
        with patch.object(m.subprocess,'Popen') as launch:
            launch.return_value.pid=999999999
            self.assertTrue(m.schedule_continuation(self.root,self.sid))
            launch.assert_called_once()

    def test_supervisor_retries_predispatch_crash_then_dispatches_once(self):
        self.automatic_fixture();self.settings['continuation_backend']='desktop'
        m.atomic_json(self.root/'settings.json',self.settings)
        m.update_state(self.directory,auto_continuation={'status':'scheduled'})
        calls=[]
        def attempt(*args,**kwargs):
            calls.append(args)
            if len(calls)==1:raise OSError('synthetic worker launch failure')
            m.continue_work(self.root,self.sid,desktop_factory=FakeDesktop)
        delays=[]
        result=m.supervise_continuation(self.root,self.sid,runner=attempt,sleeper=delays.append)
        self.assertEqual(result['status'],'desktop_dispatched')
        self.assertEqual(len(calls),2);self.assertEqual(delays,[1])
        self.assertEqual(sum(n=='send_message_to_thread' for n,_ in FakeDesktop.calls),1)

    def test_supervisor_stops_after_three_crashes_and_does_not_loop_on_stop(self):
        self.automatic_fixture();m.update_state(self.directory,auto_continuation={'status':'scheduled'})
        attempts=[]
        result=m.supervise_continuation(self.root,self.sid,runner=lambda *a,**k: attempts.append(a),sleeper=lambda _:None)
        self.assertEqual(len(attempts),3)
        self.assertEqual(result['status'],'pre_dispatch_retries_exhausted')
        with patch.object(m.subprocess,'Popen') as launch:
            self.assertFalse(m.schedule_continuation(self.root,self.sid));launch.assert_not_called()

    def test_supervisor_never_retries_lost_send_receipt(self):
        self.automatic_fixture();m.update_state(self.directory,auto_continuation={'status':'scheduled'})
        def uncertain(*args,**kwargs):
            m.update_state(self.directory,auto_continuation={
                'status':'desktop_dispatch_failed_or_ambiguous','dispatching_at':time.time()})
        result=m.supervise_continuation(self.root,self.sid,runner=uncertain,sleeper=lambda _:self.fail('No retry'))
        self.assertEqual(result['status'],'desktop_dispatch_failed_or_ambiguous')
        with patch.object(m.subprocess,'Popen') as launch:
            self.assertTrue(m.schedule_continuation(self.root,self.sid));launch.assert_not_called()

    def test_supervisor_cancels_when_new_input_arrives_during_backoff(self):
        self.automatic_fixture();m.update_state(self.directory,auto_continuation={'status':'scheduled'})
        attempts=[]
        def newer(_):
            state=m.load_json(self.directory/'state.json');m.update_state(self.directory,prompt_epoch=state['prompt_epoch']+1)
        result=m.supervise_continuation(self.root,self.sid,runner=lambda *a,**k:attempts.append(a),sleeper=newer)
        self.assertEqual(len(attempts),1)
        self.assertEqual(result['status'],'source_stop_not_confirmed_or_request_changed')

    def test_manual_successor_work_prevents_recovery_from_sending_another_turn(self):
        self.automatic_fixture()
        successor=m.session_dir(self.root,'successor-1')
        state=m.load_json(successor/'state.json');m.update_state(successor,prompt_epoch=state['prompt_epoch']+1)
        result=m.continue_work(self.root,self.sid,desktop_factory=FakeDesktop)
        self.assertEqual(result['status'],'successor_already_used')
        self.assertEqual(FakeDesktop.calls,[])

    def test_process_identity_rejects_missing_pid_and_identifies_this_process(self):
        import os
        self.assertIsNone(m.process_identity(None))
        self.assertIsNone(m.process_identity(999999999))
        self.assertTrue(m.process_identity(os.getpid()))

    def test_detached_stop_recovery_survives_real_child_crash_and_sends_once(self):
        import os
        import signal
        self.automatic_fixture(stopped=False)
        self.settings['continuation_backend']='desktop';m.atomic_json(self.root/'settings.json',self.settings)
        attempts=self.base/'attempts';sends=self.base/'sends'
        wrapper=self.base/'dispatcher.py'
        wrapper.write_text(
            'import sys,os\nfrom pathlib import Path\n'
            f'sys.path.insert(0,{str(SCRIPT.parent)!r})\nimport codex_context_handoff as h\n'
            'h.__file__=__file__\n'
            f'attempts=Path({str(attempts)!r})\nsends=Path({str(sends)!r})\n'
            'class Desktop:\n'
            ' def __init__(self,**kwargs):pass\n'
            ' def __enter__(self):return self\n'
            ' def __exit__(self,*args):pass\n'
            ' def call(self,name,args):\n'
            '  if name=="send_message_to_thread":\n'
            '   with sends.open("a") as f:f.write("accepted\\n")\n'
            '  return {"status":"accepted","threadId":"successor-1"}\n'
            'h.DesktopTools=Desktop\n'
            'if sys.argv[1]=="continue":\n'
            ' count=int(attempts.read_text())+1 if attempts.exists() else 1\n'
            ' attempts.write_text(str(count))\n'
            ' if count==1:os._exit(17)\n'
            'h.main()\n')
        m.update_state(self.directory,auto_continuation={'status':'source_stop_not_confirmed'})
        self.payload['hook_event_name']='Stop'
        result=subprocess.run([sys.executable,str(wrapper),'hook','--root',str(self.root)],
                              input=json.dumps(self.payload),capture_output=True,text=True,check=True)
        self.assertFalse(json.loads(result.stdout)['continue'])
        coordinator=m.load_json(self.directory/'state.json')['continuation_coordinator']
        def cleanup():
            if m.process_identity(coordinator['pid'])==coordinator['identity']:
                os.killpg(coordinator['pid'],signal.SIGTERM)
        self.addCleanup(cleanup)
        deadline=time.monotonic()+10
        while m.process_identity(coordinator['pid'])==coordinator['identity'] and time.monotonic()<deadline:
            time.sleep(.05)
        self.assertIsNone(m.process_identity(coordinator['pid']))
        state=m.load_json(self.directory/'state.json')
        self.assertEqual(state['auto_continuation']['status'],'desktop_dispatched',
                         (self.directory/'continuation-worker.log').read_text())
        self.assertEqual(attempts.read_text(),'2')
        self.assertEqual(sends.read_text(),'accepted\n')
        subprocess.run([sys.executable,str(wrapper),'supervise','--root',str(self.root),'--session',self.sid],
                       check=True,capture_output=True,text=True)
        self.assertEqual(attempts.read_text(),'2')
        self.assertEqual(sends.read_text(),'accepted\n')

    def test_automatic_work_requires_confirmed_source_stop(self):
        self.automatic_fixture(stopped=False)
        count=len(FakeServer.calls)
        result=m.continue_work(self.root,self.sid,FakeWorkerServer)
        self.assertEqual(result['status'],'source_stop_not_confirmed')
        self.assertEqual(len(FakeServer.calls),count)

    def test_automatic_work_opens_and_dispatches_once_without_another_user_message(self):
        self.automatic_fixture()
        with patch.object(m,'open_continuation',return_value='accepted') as opener:
            first=m.continue_work(self.root,self.sid,FakeWorkerServer)
            second=m.continue_work(self.root,self.sid,FakeWorkerServer)
        self.assertEqual(first['status'],'turn_completed')
        self.assertEqual(second['status'],'already_dispatched_or_ambiguous')
        opener.assert_called_once_with('successor-1')
        starts=[params for method,params in FakeServer.calls if method=='turn/start']
        self.assertEqual(len(starts),1)
        self.assertIn('continue its remaining work now',starts[0]['input'][0]['text'])

    def test_completed_handoff_opens_without_inventing_a_new_work_turn(self):
        self.automatic_fixture(status='complete')
        result=m.continue_work(self.root,self.sid,FakeWorkerServer,opener=lambda _: 'accepted')
        self.assertEqual(result['status'],'no_remaining_work')
        self.assertFalse(any(method=='turn/start' for method,_ in FakeServer.calls))

    def test_new_user_input_cancels_automatic_dispatch_of_older_request(self):
        self.automatic_fixture()
        self.payload.update(hook_event_name='UserPromptSubmit',turn_id='newer-user-request')
        m.hook(self.payload,self.root)
        result=m.continue_work(self.root,self.sid,FakeWorkerServer)
        self.assertEqual(result['status'],'cancelled_new_source_input')
        self.assertFalse(any(method=='turn/start' for method,_ in FakeServer.calls))

    def test_automatic_work_interrupts_its_own_turn_on_required_approval(self):
        self.automatic_fixture();FakeWorkerServer.needs_approval=True
        result=m.continue_work(self.root,self.sid,FakeWorkerServer,opener=lambda _: 'accepted')
        self.assertEqual(result['status'],'needs_user_input')
        interrupts=[params for method,params in FakeServer.calls if method=='turn/interrupt']
        self.assertEqual(interrupts,[{'threadId':'successor-1','turnId':'automatic-turn'}])

    def test_workspace_change_prevents_automatic_dispatch(self):
        self.automatic_fixture()
        (self.directory/'handoff.json').write_text((self.directory/'handoff.json').read_text()+' ')
        result=m.continue_work(self.root,self.sid,FakeWorkerServer)
        self.assertEqual(result['status'],'checkpoint_changed')
        self.assertFalse(any(method=='turn/start' for method,_ in FakeServer.calls))

    def test_new_input_during_bootstrap_is_not_overwritten_or_marked_ready(self):
        self.transcript_at(400000);m.hook(self.payload,self.root);self.checkpoint()
        directory=self.directory
        class NewInputServer(FakeServer):
            def seed(self,*args,**kwargs):
                state=m.load_json(directory/'state.json');m.update_state(directory,prompt_epoch=state['prompt_epoch']+1)
                return super().seed(*args,**kwargs)
        with self.assertRaisesRegex(ValueError,'New user input'):
            m.ready(self.root,self.sid,NewInputServer)
        state=m.load_json(self.directory/'state.json')
        self.assertEqual(state['prompt_epoch'],1)
        self.assertFalse(state.get('successor_ready'))

    def test_deep_link_rejects_non_uuid_without_running_windows_commands(self):
        with patch.object(m.subprocess,'run') as run:
            with self.assertRaises(ValueError):m.open_continuation("thread'; arbitrary")
            run.assert_not_called()

    def test_desktop_backend_leaves_turn_ownership_with_app_and_exits_after_receipt(self):
        self.automatic_fixture()
        self.settings['continuation_backend']='desktop';m.atomic_json(self.root/'settings.json',self.settings)
        count=len(FakeServer.calls)
        first=m.continue_work(self.root,self.sid,FakeWorkerServer,desktop_factory=FakeDesktop)
        second=m.continue_work(self.root,self.sid,FakeWorkerServer,desktop_factory=FakeDesktop)
        self.assertEqual(first['status'],'desktop_dispatched')
        self.assertEqual(second['status'],'already_dispatched_or_ambiguous')
        self.assertEqual(len(FakeServer.calls),count)
        self.assertEqual([name for name,_ in FakeDesktop.calls],
                         ['connect','set_thread_title','navigate_to_codex_page','send_message_to_thread'])
        self.assertEqual(FakeDesktop.calls[1][1], {'threadId':self.sid,'source':'codex',
                         'title':'Part 1 · ' + self.cwd.name})
        args=FakeDesktop.calls[-1][1]
        self.assertEqual(args['threadId'],'successor-1')
        self.assertNotIn('model',args);self.assertNotIn('thinking',args)
        self.assertEqual(FakeDesktop.calls[0][1]['caller_thread_id'],self.sid)

    def test_desktop_rejection_is_preserved_without_fallback_or_duplicate_dispatch(self):
        self.automatic_fixture();FakeDesktop.reject_send=True
        self.settings['continuation_backend']='desktop';m.atomic_json(self.root/'settings.json',self.settings)
        count=len(FakeServer.calls)
        with self.assertRaises(RuntimeError):
            m.continue_work(self.root,self.sid,FakeWorkerServer,desktop_factory=FakeDesktop)
        result=m.continue_work(self.root,self.sid,FakeWorkerServer,desktop_factory=FakeDesktop)
        self.assertEqual(result['status'],'already_dispatched_or_ambiguous')
        self.assertEqual(len(FakeServer.calls),count)
        self.assertEqual(m.load_json(self.directory/'state.json')['auto_continuation']['status'],
                         'desktop_dispatch_failed_or_ambiguous')

    def test_desktop_complete_checkpoint_only_navigates(self):
        self.automatic_fixture(status='complete')
        self.settings['continuation_backend']='desktop';m.atomic_json(self.root/'settings.json',self.settings)
        result=m.continue_work(self.root,self.sid,FakeWorkerServer,desktop_factory=FakeDesktop)
        self.assertEqual(result['status'],'no_remaining_work')
        self.assertNotIn('send_message_to_thread',[name for name,_ in FakeDesktop.calls])

    def test_cached_entrypoint_preserves_original_bytes_and_forwards_arguments(self):
        import hashlib
        library=self.base/'lib';library.mkdir();backups=self.base/'backup';backups.mkdir()
        original=b'print("old version")\n'
        alias=library/('handoff-'+hashlib.sha256(original).hexdigest()[:16]+'.py');alias.write_bytes(original)
        target=library/'new.py';target.write_text('import sys,json;print(json.dumps(sys.argv[1:]))\n')
        installer.compatibility_entrypoint(alias,target,library,backups)
        self.assertEqual((backups/('entrypoint-'+alias.name)).read_bytes(),original)
        result=subprocess.run(['/usr/bin/python3',str(alias),'status','--session','source-1'],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout),['status','--session','source-1'])

    def test_cached_entrypoint_never_replaces_an_unrecognized_file(self):
        library=self.base/'lib';library.mkdir();alias=library/'custom.py';alias.write_text('preserve this')
        with self.assertRaises(ValueError):installer.compatibility_entrypoint(alias,library/'new.py',library,self.base)
        self.assertEqual(alias.read_text(),'preserve this')

    def test_portable_default_home_respects_environment_without_fixed_user_paths(self):
        with patch.dict(m.os.environ,{'CODEX_HOME':str(self.base/'custom-home')}):
            self.assertEqual(installer.default_homes(),(self.base/'custom-home',))
        with patch.dict(m.os.environ,{},clear=True), patch.object(installer.Path,'home',return_value=self.base):
            self.assertEqual(installer.default_homes(),(self.base/'.codex',))

    def test_install_into_temporary_home_is_disabled_trusted_and_repeatable(self):
        import hashlib
        import tomllib
        home=self.base/'codex-home';home.mkdir()
        config='model = "example-model"\n[hooks.state."unrelated"]\nenabled = true\ntrusted_hash = "existing"\n'
        (home/'config.toml').write_text(config)
        existing={'hooks':{'PreToolUse':[{'hooks':[{'type':'command','command':'existing hook'}]}]}}
        m.atomic_json(home/'hooks.json',existing)
        def hook_rows(selected,command,binary):
            return [{'key':str(selected)+'/hooks.json:'+event,'eventName':event,'enabled':True,
                     'currentHash':'sha256:'+hashlib.sha256((command+event).encode()).hexdigest()}
                    for event in installer.EVENTS]
        library=self.base/'library'
        with patch.object(installer.shutil,'which',return_value=sys.executable), \
                patch.object(installer,'hook_rows',side_effect=hook_rows):
            first=installer.install(self.root,homes=[home],library=library)
            second=installer.install(self.root,homes=[home],library=library)
        self.assertEqual(first['command'],second['command'])
        settings=m.load_json(self.root/'settings.json')
        # The fixture already owns settings: installation must preserve its enablement.
        self.assertTrue(settings['enabled'])
        parsed=tomllib.loads((home/'config.toml').read_text())
        self.assertEqual(parsed['model'],'example-model')
        self.assertEqual(parsed['hooks']['state']['unrelated']['trusted_hash'],'existing')
        self.assertEqual(m.load_json(home/'hooks.json')['hooks']['PreToolUse'][0],existing['hooks']['PreToolUse'][0])
        self.assertEqual(len(m.load_json(home/'hooks.json')['hooks']['PreToolUse']),2)
        self.assertTrue(Path(settings['hook_entrypoint']).is_file())
        fresh=self.base/'fresh-state';new_home=self.base/'new-codex-home'
        with patch.object(installer.shutil,'which',return_value=sys.executable), \
                patch.object(installer,'hook_rows',side_effect=hook_rows):
            installer.install(fresh,homes=[new_home],library=self.base/'fresh-library')
        settings=m.load_json(fresh/'settings.json')
        self.assertFalse(settings['enabled']);self.assertFalse(settings['auto_continue'])
        self.assertEqual(settings['config_home'],str(new_home))
        self.assertEqual(settings['continuation_backend'],'desktop')
        self.assertTrue((new_home/'config.toml').is_file())


if __name__=='__main__':unittest.main()
