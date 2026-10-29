import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1]/'scripts/codex_context_handoff.py'
SPEC = importlib.util.spec_from_file_location('recovery_handoff', SCRIPT)
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)


class TaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base/'private'
        self.sid = 'fixture-task'
        self.directory = h.session_dir(self.root, self.sid)
        self.cwd = self.base/'workspace';self.cwd.mkdir()
        self.config = {'enabled': True, 'prepare_tokens': 350000, 'rollover_tokens': 400000,
            'task_recovery': {'enabled': True, 'capacity_delay': 0, 'poll_seconds': 0}}
        self.state = {'session_id': self.sid, 'prompt_epoch': 1, 'context_tokens': 100,
                      'config_home': str(self.base/'codex-home'), 'cwd': str(self.cwd),
                      'transcript_path': str(self.base/'transcript.jsonl'),
                      'last_prompt_turn_id': 'source-turn'}
        h.atomic_json(self.root/'settings.json', self.config)
        h.atomic_json(self.directory/'state.json', self.state)
        self.value = {'status': 'active', 'goal': 'Finish the whole fixture', 'constraints': ['No deployment'],
            'remaining': ['Implement fixture', 'Verify installed outcome'],
            'acceptance': ['Fixture tested', 'Installed outcome verified'], 'evidence': {},
            'next_action': 'Inspect the fixture'}
        self.schedule = patch.object(h, 'schedule_task_watch', return_value=True).start()
        self.addCleanup(patch.stopall)
        h.register_task(self.root, self.sid, self.value)
        self.task = h.load_json(self.directory/'task.json')
        self.control = h.load_json(self.directory/'task-control.json')

    def snapshot(self, status='failed', error=None, thread_status='idle'):
        return {'thread': {'id': self.sid, 'status': {'type': thread_status, 'activeFlags': []}},
                'turns': [{'id': 'failed-turn', 'status': status,
                           'error': error or {'message': 'Model provider is currently at capacity'}}]}

    def desktop(self, snapshots, reject=False, crash_file=None):
        calls = []
        snapshots = list(snapshots)
        class Desktop:
            def __init__(self, **kwargs): calls.append(('connect', kwargs))
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def call(self, name, arguments):
                calls.append((name, arguments))
                if name == 'read_thread':
                    return copy.deepcopy(snapshots.pop(0) if len(snapshots) > 1 else snapshots[0])
                if reject: raise RuntimeError('Synthetic lost receipt')
                return {'status': 'accepted'}
        return Desktop, calls

    def test_completion_requires_evidence_for_every_full_goal_criterion(self):
        value = dict(self.value, status='complete', remaining=[], evidence={'Fixture tested': ['test output']})
        with self.assertRaisesRegex(ValueError, 'every criterion'): h.validate_task(value)
        value['evidence']['Installed outcome verified'] = ['runtime receipt']
        self.assertEqual(h.validate_task(value)['status'], 'complete')

    def test_milestone_cannot_shrink_the_original_goal_or_acceptance(self):
        with self.assertRaisesRegex(ValueError,'original goal'):
            h.register_task(self.root,self.sid,dict(self.value,goal='Just the intermediate test'))
        with self.assertRaisesRegex(ValueError,'original goal'):
            h.register_task(self.root,self.sid,dict(self.value,acceptance=['Fixture tested']))

    def test_premature_stop_continues_but_unchanged_work_has_a_retry_limit(self):
        for _ in range(3):
            result = h.task_recovery_hook({'hook_event_name': 'Stop', 'stop_hook_active': True},
                                         self.root, self.directory, self.state, self.config)
            self.assertEqual(result['decision'], 'block')
            self.assertIn('intermediate test, PR', result['reason'])
        self.assertIn('paused',h.task_recovery_hook({'hook_event_name': 'Stop'},
            self.root, self.directory, self.state, self.config)['systemMessage'])
        self.assertEqual(h.load_json(self.directory/'task-control.json')['status'], 'premature_retries_exhausted')

    def test_actual_progress_resets_premature_guard_budget(self):
        for _ in range(3):h.task_recovery_hook({'hook_event_name': 'Stop'},self.root,self.directory,self.state,self.config)
        value = dict(self.value, remaining=['Verify installed outcome'], evidence={'Fixture tested': ['tests passed']})
        h.register_task(self.root, self.sid, value)
        self.assertEqual(h.task_recovery_hook({'hook_event_name': 'Stop'},
            self.root,self.directory,self.state,self.config)['decision'],'block')

    def test_complete_blocked_and_paused_tasks_are_allowed_to_stop(self):
        for status in ('complete', 'blocked', 'paused'):
            value = dict(self.value, status=status, reason='Operator decision', remaining=[] if status=='complete' else self.value['remaining'],
                         evidence={v:['recorded proof'] for v in self.value['acceptance']})
            h.register_task(self.root,self.sid,value)
            self.assertEqual(h.task_recovery_hook({'hook_event_name':'Stop'},self.root,self.directory,self.state,self.config),{})

    def test_only_exact_generated_continuation_keeps_task_across_prompt_epoch(self):
        first=h.task_recovery_hook({'hook_event_name':'Stop'},self.root,self.directory,self.state,self.config)
        state=dict(self.state,prompt_epoch=2)
        h.task_recovery_hook({'hook_event_name':'UserPromptSubmit','prompt':first['reason']},
                             self.root,self.directory,state,self.config)
        self.assertEqual(h.load_json(self.directory/'task.json')['prompt_epoch'],2)
        self.assertEqual(h.task_recovery_hook({'hook_event_name':'Stop'},
            self.root,self.directory,state,self.config)['decision'],'block')

    def test_genuine_new_user_input_does_not_inherit_old_task(self):
        h.task_recovery_hook({'hook_event_name':'Stop'},self.root,self.directory,self.state,self.config)
        state=dict(self.state,prompt_epoch=2)
        h.task_recovery_hook({'hook_event_name':'UserPromptSubmit','prompt':'A different task'},
                             self.root,self.directory,state,self.config)
        self.assertEqual(h.load_json(self.directory/'task.json')['prompt_epoch'],1)
        self.assertEqual(h.task_recovery_hook({'hook_event_name':'Stop'},self.root,self.directory,state,self.config),{})

    def test_capacity_error_classifier_excludes_quota_permissions_and_context(self):
        for error in ({'message':'Provider reached capacity'}, {'codexErrorInfo':{'httpStatusCode':503}},
                      {'message':'Server overloaded'}, {'message':'Temporarily unavailable'}):
            self.assertTrue(h.capacity_error(error),error)
        for error in ({'message':'Quota reached capacity'}, {'codexErrorInfo':'UsageLimitExceeded'},
                      {'message':'Invalid request','codexErrorInfo':'BadRequest'}, {'message':'Context window at capacity'},
                      {'message':'Payment required'}, {'message':'User interrupted'}, {'message':'Random unknown failure'}):
            self.assertFalse(h.capacity_error(error),error)

    def test_active_approval_or_interrupted_turn_is_never_restarted(self):
        for snapshot in (self.snapshot(thread_status='active'), self.snapshot(status='interrupted')):
            decision=h.recovery_decision(self.task,self.control,self.state,snapshot,self.config,0)
            self.assertNotEqual(decision,'retry_capacity')
        snapshot=self.snapshot();snapshot['thread']['status']['activeFlags']=['waitingOnApproval']
        self.assertEqual(h.recovery_decision(self.task,self.control,self.state,snapshot,self.config,0),
                         'running_or_waiting_for_input')

    def test_capacity_retries_once_and_duplicate_failed_snapshot_is_not_replayed(self):
        desktop,calls=self.desktop([self.snapshot()])
        h.watch_task(self.root,self.sid,desktop_factory=desktop,sleeper=lambda _:None,max_polls=2)
        sends=[v for n,v in calls if n=='send_message_to_thread']
        self.assertEqual(len(sends),1)
        self.assertEqual(set(sends[0]),{'threadId','hostId','prompt'})
        self.assertIn('reference data',sends[0]['prompt'])
        self.assertEqual(h.load_json(self.directory/'task-control.json')['capacity_attempts'],1)

    def test_native_system_error_capacity_failure_retries_once(self):
        # Native read_thread reports provider-failed threads as systemError.
        snapshot = self.snapshot(thread_status='systemError', error={
            'message': 'Selected model is at capacity. Please try a different model.',
            'additionalDetails': None})
        snapshot['thread']['status'].pop('activeFlags')
        desktop,calls = self.desktop([snapshot])
        h.watch_task(self.root,self.sid,desktop_factory=desktop,sleeper=lambda _:None,max_polls=2)
        self.assertEqual(sum(name=='send_message_to_thread' for name,_ in calls),1)
        self.assertEqual(h.load_json(self.directory/'task-control.json')['capacity_attempts'],1)

    def test_system_error_still_requires_terminal_capacity_failure_without_approval(self):
        snapshots = [self.snapshot(thread_status='systemError',status=status)
                     for status in ('inProgress','completed','interrupted')]
        snapshots += [self.snapshot(thread_status='systemError',error={'message': message})
                      for message in ('Quota exceeded','Unauthorized','Context window exceeded','Unknown error')]
        waiting = self.snapshot(thread_status='systemError')
        waiting['thread']['status']['activeFlags'] = ['waitingOnApproval']
        snapshots.append(waiting)
        snapshots.append(self.snapshot(thread_status='unrecognized'))
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                self.assertNotEqual(h.recovery_decision(self.task,self.control,self.state,
                                                       snapshot,self.config,0),'retry_capacity')

    def test_capacity_limit_exhaustion_stops_without_a_send(self):
        h.atomic_json(self.directory/'task-control.json',dict(self.control,capacity_attempts=5))
        desktop,calls=self.desktop([self.snapshot()])
        result=h.watch_task(self.root,self.sid,desktop_factory=desktop,sleeper=lambda _:None,max_polls=1)
        self.assertEqual(result['status'],'capacity_retries_exhausted')
        self.assertFalse(any(n=='send_message_to_thread' for n,_ in calls))
        self.assertEqual(h.load_json(self.directory/'task.json')['status'],'blocked')
        h.register_task(self.root,self.sid,self.value)
        self.assertEqual(h.load_json(self.directory/'task-control.json')['capacity_attempts'],0)

    def test_new_input_or_disable_during_backoff_prevents_dispatch(self):
        for change in ('input','disable','allowlist','pause','reenroll'):
            h.atomic_json(self.directory/'state.json',self.state);h.atomic_json(self.root/'settings.json',self.config)
            h.register_task(self.root,self.sid,self.value)
            desktop,calls=self.desktop([self.snapshot()])
            def pause(_):
                if change=='input':h.atomic_json(self.directory/'state.json',dict(self.state,prompt_epoch=2))
                elif change=='disable':h.atomic_json(self.root/'settings.json',dict(self.config,enabled=False))
                elif change=='allowlist':h.atomic_json(self.root/'settings.json',dict(self.config,allowed_sessions=['other']))
                elif change=='pause':h.register_task(self.root,self.sid,dict(self.value,status='paused',reason='Operator paused'))
                elif change=='reenroll':
                    h.atomic_json(self.directory/'state.json',dict(self.state,prompt_epoch=2))
                    h.register_task(self.root,self.sid,self.value)
            h.watch_task(self.root,self.sid,desktop_factory=desktop,sleeper=pause,max_polls=1)
            self.assertFalse(any(n=='send_message_to_thread' for n,_ in calls))

    def test_manual_interrupt_during_backoff_is_preserved(self):
        desktop,calls=self.desktop([self.snapshot(),self.snapshot(status='interrupted')])
        result=h.watch_task(self.root,self.sid,desktop_factory=desktop,sleeper=lambda _:None,max_polls=2)
        self.assertEqual(result['status'],'manual_or_unknown_interruption')
        self.assertFalse(any(n=='send_message_to_thread' for n,_ in calls))

    def test_ambiguous_send_is_durably_non_retryable(self):
        desktop,calls=self.desktop([self.snapshot()],reject=True)
        self.assertEqual(h.watch_task(self.root,self.sid,desktop_factory=desktop,sleeper=lambda _:None,max_polls=1)['status'],
                         'capacity_dispatch_ambiguous')
        self.assertEqual(h.watch_task(self.root,self.sid,desktop_factory=desktop,sleeper=lambda _:None,max_polls=1)['status'],
                         'dispatch_already_attempted')
        self.assertEqual(sum(n=='send_message_to_thread' for n,_ in calls),1)
        self.assertEqual(h.task_recovery_hook({'hook_event_name':'Stop'},
            self.root,self.directory,self.state,self.config),{})

    def test_successor_seed_preserves_full_goal_without_launching_a_watcher(self):
        successor=h.session_dir(self.root,'successor')
        h.atomic_json(successor/'state.json',{'session_id':'successor','prompt_epoch':0})
        checkpoint={'status':'active','goal':'A short summary','constraints':['No deployment'],
                    'remaining':['Verify installed outcome'],'next_action':'Inspect runtime'}
        self.schedule.reset_mock()
        self.assertTrue(h.seed_successor_task(self.root,dict(self.state,successor_id='successor'),checkpoint))
        task=h.load_json(successor/'task.json')
        self.assertEqual(task['goal'],'Finish the whole fixture')
        self.assertEqual(task['acceptance'],self.value['acceptance'])
        self.assertEqual(task['remaining'],checkpoint['remaining'])
        self.schedule.assert_not_called()
        self.assertFalse(h.seed_successor_task(self.root,dict(self.state,successor_id='successor'),checkpoint))

    def test_native_acceptance_starts_successor_watch_with_genuine_source_routing(self):
        desktop,calls=self.desktop([self.snapshot()])
        state=dict(self.state,successor_id='successor',successor_recovery_seeded=True)
        self.schedule.reset_mock()
        with patch.object(h,'checkpoint_is_current',return_value=True):
            result=h.dispatch_desktop(self.root,self.directory,state,{'status':'active'},'checkpoint.json',
                                      self.config,desktop_factory=desktop)
        self.assertEqual(result['status'],'desktop_dispatched')
        self.schedule.assert_called_once_with(self.root,'successor',caller_thread_id=self.sid,
                                              caller_turn_id='source-turn')
        self.assertEqual(sum(n=='send_message_to_thread' for n,_ in calls),1)

    def test_handoff_prompt_preserves_inherited_task_in_successor_epoch(self):
        successor=h.session_dir(self.root,'successor')
        successor_state=dict(self.state,session_id='successor')
        h.atomic_json(successor/'state.json',successor_state)
        checkpoint={'status':'active','goal':'Short summary','constraints':['No deployment'],
                    'remaining':['Verify installed outcome'],'next_action':'Inspect runtime'}
        state=dict(self.state,successor_id='successor',successor_recovery_seeded=True)
        h.seed_successor_task(self.root,state,checkpoint)
        desktop,calls=self.desktop([self.snapshot()])
        with patch.object(h,'checkpoint_is_current',return_value=True):
            h.dispatch_desktop(self.root,self.directory,state,checkpoint,'checkpoint.json',
                               self.config,desktop_factory=desktop)
        prompt=next(args['prompt'] for name,args in calls if name=='send_message_to_thread')
        successor_state['prompt_epoch']+=1
        h.task_recovery_hook({'hook_event_name':'UserPromptSubmit','prompt':prompt},
                             self.root,successor,successor_state,self.config)
        task=h.load_json(successor/'task.json')
        self.assertEqual(task['prompt_epoch'],successor_state['prompt_epoch'])
        self.assertEqual(task['goal'],self.value['goal'])
        self.assertEqual(task['acceptance'],self.value['acceptance'])
        self.assertEqual(h.task_recovery_hook({'hook_event_name':'Stop'},self.root,successor,
                                             successor_state,self.config)['decision'],'block')

    def test_disable_during_final_native_read_prevents_dispatch(self):
        desktop,calls=self.desktop([self.snapshot()])
        original=desktop.call
        reads=0
        def read_then_disable(instance,name,arguments):
            nonlocal reads
            result=original(instance,name,arguments)
            if name=='read_thread':
                reads+=1
                if reads==2:h.atomic_json(self.root/'settings.json',dict(self.config,enabled=False))
            return result
        desktop.call=read_then_disable
        h.watch_task(self.root,self.sid,desktop_factory=desktop,sleeper=lambda _:None,max_polls=1)
        self.assertFalse(any(name=='send_message_to_thread' for name,_ in calls))

    def test_observer_failure_records_error_and_never_uses_cached_status(self):
        desktop,calls=self.desktop([self.snapshot()])
        def unavailable(*args,**kwargs):raise RuntimeError('Observer offline')
        desktop.call=unavailable
        result=h.watch_task(self.root,self.sid,desktop_factory=desktop,sleeper=lambda _:None,max_polls=1)
        self.assertEqual(result['status'],'observer_unavailable')
        self.assertEqual(h.load_json(self.directory/'task-control.json')['error_type'],'RuntimeError')
        self.assertFalse(any(n=='send_message_to_thread' for n,_ in calls))

    def test_retry_after_hint_and_jitter_are_respected(self):
        delay=h.capacity_delay({'message':'Retry after 120 seconds'},0,{'capacity_delay':30})
        self.assertGreaterEqual(delay,120)
        self.assertGreaterEqual(h.capacity_delay({},1,{'capacity_delay':30}),60)
        self.assertLessEqual(h.capacity_delay({},1,{'capacity_delay':30}),69)

    def test_bootstrap_has_no_recovery_instruction_or_stop_guard(self):
        transcript=self.base/'transcript.jsonl'
        transcript.write_text(json.dumps({'type':'session_meta','payload':{'id':self.sid,'source':'vscode'}})+'\n'+
            json.dumps({'type':'token_usage_record','payload':{'usage':{'input_tokens':100,'output_tokens':0}}})+'\n')
        h.atomic_json(self.directory/'state.json',dict(self.state,handoff_initializing=True))
        for event in ('PostToolUse','Stop'):
            self.assertEqual(h.hook({'session_id':self.sid,'cwd':str(self.cwd),'transcript_path':str(transcript),
                                    'hook_event_name':event},self.root),{})

    def test_hard_exit_after_send_cannot_create_a_second_task_turn(self):
        count=self.base/'send-count'
        wrapper=self.base/'crash.py'
        snapshot=self.snapshot()
        wrapper.write_text('import os,sys,json\nfrom pathlib import Path\n'+
            f'sys.path.insert(0,{str(SCRIPT.parent)!r})\nimport codex_context_handoff as h\n'+
            'class Desktop:\n def __init__(self,**kwargs):pass\n def __enter__(self):return self\n'+
            ' def __exit__(self,*args):pass\n def call(self,name,args):\n'+
            f'  if name=="read_thread":return json.loads({json.dumps(snapshot)!r})\n'+
            f'  with Path({str(count)!r}).open("a") as output:output.write("sent\\n")\n'+
            '  os._exit(17)\nh.DesktopTools=Desktop\nh.main()\n')
        args=[sys.executable,str(wrapper),'watch-task','--root',str(self.root),'--session',self.sid]
        self.assertEqual(subprocess.run(args,capture_output=True,text=True).returncode,17)
        result=subprocess.run(args,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['status'],'dispatch_already_attempted')
        self.assertEqual(count.read_text(),'sent\n')


if __name__=='__main__':
    unittest.main()
