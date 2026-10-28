import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location('external_tests',
    Path(__file__).resolve().parents[1]/'scripts/run_external_tests.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class ExternalTests(unittest.TestCase):
    def fixture(self, body=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root/'tests').mkdir();(root/'scripts').mkdir()
        (root/'scripts/fixture.py').write_text('VALUE = 1\n')
        if body:
            (root/'tests/test_fixture.py').write_text(
                'import unittest\nclass Fixture(unittest.TestCase):\n def test_value(self):\n  ' + body + '\n')
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), 'add', 'scripts', 'tests'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture'], check=True)
        return root

    def test_tests_committed_snapshot_even_when_working_copy_is_dirty(self):
        root = self.fixture('self.assertTrue(True)')
        (root/'tests/test_fixture.py').write_text('raise RuntimeError("dirty edits must not run")\n')
        report, log = m.run_tests(root)
        self.assertEqual(report['state'], 'success')
        self.assertEqual(report['summary']['tests_run'], 1)
        self.assertIn('test_value', log)
        self.assertNotIn(str(root), log)

    def test_failed_tests_produce_failure_and_sanitized_trace(self):
        root = self.fixture('self.fail("expected regression")')
        report, log = m.run_tests(root)
        self.assertEqual(report['state'], 'failure')
        self.assertEqual(report['summary']['failures'], 1)
        self.assertIn('expected regression', log)
        self.assertNotIn(str(root), log)
        self.assertNotIn('/tmp/', log)

    def test_no_discovered_tests_is_a_failure(self):
        report, _ = m.run_tests(self.fixture())
        self.assertEqual(report['state'], 'failure')
        self.assertEqual(report['summary']['tests_run'], 0)


if __name__ == '__main__':
    unittest.main()
