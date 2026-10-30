import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/codex_context_handoff.py'
SPEC = importlib.util.spec_from_file_location('queued_handoff',SCRIPT)
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)


class RecoveryQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = h.session_dir(self.root,'source')
        self.config = {'enabled':True,'auto_continue':True,'queue':{'enabled':True},
                       'task_recovery':{'enabled':True,'poll_seconds':15}}
        h.atomic_json(self.root/'settings.json',self.config)
        h.atomic_json(self.directory/'state.json',{'session_id':'source','prompt_epoch':1})
        self.queue = h.RecoveryQueue(self.root)

    def enqueue(self, kind='handoff'):
        return self.queue.enqueue(kind,'source',1,{},now=100)

    def test_pending_job_survives_reopening_and_only_one_connection_claims_it(self):
        key = self.enqueue()
        other = h.RecoveryQueue(self.root)
        first = self.queue.claim(now=100,lease_seconds=30)
        self.assertEqual(first['job_key'],key)
        self.assertIsNone(other.claim(now=100,lease_seconds=30))
        self.assertEqual(other.rows()[0]['status'],'leased')
        self.assertEqual((self.root/'queue.sqlite3').stat().st_mode & 0o777,0o600)
        with sqlite3.connect(self.root/'queue.sqlite3') as db:
            self.assertEqual(db.execute('pragma journal_mode').fetchone()[0],'wal')

    def test_expired_worker_lease_is_reclaimed_and_old_owner_cannot_acknowledge(self):
        self.enqueue()
        old = self.queue.claim(now=100,lease_seconds=10)
        new = h.RecoveryQueue(self.root).claim(now=111,lease_seconds=10)
        self.assertNotEqual(old['owner'],new['owner'])
        self.assertFalse(self.queue.finish(old,'done','old receipt',now=112))
        self.assertTrue(self.queue.finish(new,'done','completed',now=112))

    def test_heartbeat_extends_only_the_current_unexpired_owner(self):
        self.enqueue();job = self.queue.claim(now=100,lease_seconds=10)
        self.assertTrue(self.queue.heartbeat(job,now=105,lease_seconds=20))
        self.assertIsNone(self.queue.claim(now=112,lease_seconds=10))
        self.assertFalse(self.queue.heartbeat(job,now=126,lease_seconds=20))

    def test_duplicate_enqueue_never_reopens_a_completed_or_uncertain_job(self):
        self.enqueue();job = self.queue.claim(now=100,lease_seconds=30)
        self.queue.finish(job,'attention','uncertain send; reconcile',now=101)
        self.enqueue()
        self.assertIsNone(self.queue.claim(now=102,lease_seconds=30))
        self.assertEqual(self.queue.rows()[0]['status'],'attention')

    def test_newer_input_cancels_a_persisted_job_without_calling_the_handler(self):
        self.enqueue();h.atomic_json(self.directory/'state.json',{'session_id':'source','prompt_epoch':2})
        calls=[]
        h.run_queue_once(self.root,handler=lambda job:calls.append(job),now=100)
        self.assertFalse(calls)
        self.assertEqual(self.queue.rows()[0]['status'],'cancelled')

    def test_safe_pre_dispatch_failures_retry_with_a_bound(self):
        self.enqueue()
        def fail(job):raise RuntimeError('Synthetic pre-dispatch failure')
        for now in (100,200,300):h.run_queue_once(self.root,handler=fail,now=now)
        row=self.queue.rows()[0]
        self.assertEqual(row['status'],'attention')
        self.assertEqual(row['attempts'],3)

    def test_lost_send_receipt_is_never_replayed_after_queue_restart(self):
        self.enqueue();sends=[]
        def send_then_crash(job):
            sends.append(1)
            h.update_state(self.directory,auto_continuation={'status':'desktop_dispatching','dispatching_at':100})
            raise RuntimeError('Receipt lost after send')
        h.run_queue_once(self.root,handler=send_then_crash,now=100)
        self.assertEqual(self.queue.rows()[0]['status'],'attention')
        self.assertFalse(h.run_queue_once(self.root,handler=send_then_crash,now=200))
        self.assertEqual(len(sends),1)

    def test_process_death_after_send_recovers_expired_lease_without_another_send(self):
        self.enqueue()
        script = ('import importlib.util,os\nfrom pathlib import Path\n'
                  f's=importlib.util.spec_from_file_location("h",{str(SCRIPT)!r});h=importlib.util.module_from_spec(s);s.loader.exec_module(h)\n'
                  f'root=Path({str(self.root)!r})\n'
                  'h.RecoveryQueue(root).claim(now=100,lease_seconds=10)\n'
                  'h.update_state(root/"sessions/source",auto_continuation={"status":"desktop_dispatching","dispatching_at":100})\n'
                  '(root/"send-count").write_text("sent\\n")\nos._exit(17)\n')
        self.assertEqual(subprocess.run([sys.executable,'-c',script]).returncode,17)
        with patch.dict(h.os.environ,{'CODEX_APP_TOOLS_PIPE_PATH':'fixture-pipe'}):
            self.assertTrue(h.run_queue_once(self.root,now=111))
        self.assertEqual(self.queue.rows()[0]['status'],'attention')
        self.assertEqual((self.root/'send-count').read_text(),'sent\n')
        self.assertFalse(h.run_queue_once(self.root,now=200))

    def test_repeated_lease_expiry_has_a_visible_retry_limit(self):
        self.enqueue()
        for now in (100,111,122):
            self.assertIsNotNone(self.queue.claim(now=now,lease_seconds=10))
        self.assertIsNone(self.queue.claim(now=133,lease_seconds=10))
        self.assertEqual(self.queue.rows()[0]['outcome'],'lease_retries_exhausted')

    def test_capacity_backoff_is_persisted_without_blocking_other_jobs(self):
        self.enqueue('task')
        result={'status':'capacity_backoff','delay':60,'turn_id':'failed-turn'}
        h.run_queue_once(self.root,handler=lambda job:result,now=100)
        row=self.queue.rows()[0]
        self.assertEqual(row['status'],'pending')
        self.assertEqual(row['due'],160)
        self.assertIsNone(self.queue.claim(now=150,lease_seconds=30))
        self.assertEqual(self.queue.claim(now=160,lease_seconds=30)['payload']['capacity_ready_turn'],'failed-turn')

    def test_queue_scheduling_creates_no_per_task_child_process(self):
        state={'session_id':'source','prompt_epoch':1,'successor_ready':True,'successor_prompt_epoch':1,
               'source_stopped_epoch':1,'auto_continuation':{}}
        h.atomic_json(self.directory/'state.json',state)
        with patch.object(h.subprocess,'Popen') as launch:
            self.assertTrue(h.schedule_continuation(self.root,'source'))
            self.assertTrue(h.schedule_task_watch(self.root,'source',caller_thread_id='source',caller_turn_id='turn'))
        launch.assert_not_called()
        self.assertEqual({row['kind'] for row in self.queue.rows()},{'handoff','task'})

    def test_desktop_environment_snapshot_excludes_credentials(self):
        with patch.dict(h.os.environ,{'CODEX_APP_TOOLS_PIPE_PATH':'fixture-pipe','WSL_INTEROP':'fixture-interop',
                                     'GITHUB_TOKEN':'secret-token','OPENAI_API_KEY':'secret-key'}):
            h.capture_queue_environment(self.root)
        env=json.loads((self.root/'queue-environment.json').read_text())
        self.assertEqual(env['CODEX_APP_TOOLS_PIPE_PATH'],'fixture-pipe')
        self.assertNotIn('GITHUB_TOKEN',env)
        self.assertNotIn('OPENAI_API_KEY',env)


if __name__=='__main__':unittest.main()
