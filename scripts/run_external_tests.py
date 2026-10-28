#!/usr/bin/env python3
"""Test a committed snapshot locally; optionally publish an external GitHub status."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile


CONTEXT = 'external/tests'
RESULTS_BRANCH = 'test-results'


def command(args, cwd=None, **kwargs):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True, **kwargs).stdout.strip()


def github(path, payload=None):
    args = ['gh', 'api', path]
    if payload is not None:
        args += ['--method', 'POST', '--input', '-']
    return json.loads(command(args, input=json.dumps(payload) if payload is not None else None))


def run_tests(repository, revision='HEAD'):
    repository = Path(repository).resolve()
    sha = command(['git', 'rev-parse', revision + '^{commit}'], cwd=repository)
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix='codex-external-tests-') as temporary:
        snapshot = Path(temporary)/'snapshot'
        command(['git', 'clone', '--quiet', '--no-hardlinks', '--no-checkout', str(repository), str(snapshot)])
        command(['git', 'checkout', '--quiet', '--detach', sha], cwd=snapshot)
        env = os.environ.copy()
        for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'OPENAI_API_KEY', 'CODEX_APP_TOOLS_PIPE_PATH', 'PYTHONPATH'):
            env.pop(name, None)
        env['CODEX_HOME'] = str(Path(temporary)/'codex-home')
        summary_path = Path(temporary)/'summary.json'
        compile_result = subprocess.run([sys.executable, '-m', 'compileall', '-q', 'scripts', 'tests'],
                                        cwd=snapshot, env=env, text=True, capture_output=True)
        program = (
            'import json,sys,unittest\n'
            'suite=unittest.defaultTestLoader.discover("tests",pattern="test_*.py")\n'
            'result=unittest.TextTestRunner(verbosity=2).run(suite)\n'
            'summary={"tests_run":result.testsRun,"failures":len(result.failures),'
            '"errors":len(result.errors),"skipped":len(result.skipped),'
            '"passed":result.wasSuccessful() and result.testsRun>0}\n'
            'with open(sys.argv[1],"w") as output:json.dump(summary,output)\n'
            'sys.exit(0 if summary["passed"] else 1)\n'
        )
        tests = subprocess.run([sys.executable, '-c', program, str(summary_path)],
                               cwd=snapshot, env=env, text=True, capture_output=True)
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {'passed': False, 'tests_run': 0}
        passed = compile_result.returncode == 0 and tests.returncode == 0 and summary['passed']
        report = {'commit': sha, 'state': 'success' if passed else 'failure', 'context': CONTEXT,
                  'runner': 'external Linux/WSL process', 'python': platform.python_version(),
                  'started_at': started, 'completed_at': datetime.now(timezone.utc).isoformat(),
                  'compile_exit_code': compile_result.returncode, 'test_exit_code': tests.returncode,
                  'summary': summary}
        log = (compile_result.stdout + compile_result.stderr + tests.stdout + tests.stderr)
        # Never publish the actual local checkout/home paths in test traces.
        for source, replacement in ((temporary, '<temporary>'), (str(repository), '<repository>'),
                                    (str(Path.home()), '<home>')):
            log = log.replace(source, replacement)
        return report, log


def publish_report(repository, slug, report, log):
    sha = report['commit']
    relative = 'reports/' + sha
    with tempfile.TemporaryDirectory(prefix='codex-test-report-') as temporary:
        root = Path(temporary)
        command(['git', 'init', '--quiet', '-b', RESULTS_BRANCH], cwd=root)
        command(['git', 'remote', 'add', 'origin', 'https://github.com/' + slug + '.git'], cwd=root)
        if command(['git', 'ls-remote', 'origin', 'refs/heads/' + RESULTS_BRANCH], cwd=root):
            command(['git', 'fetch', '--quiet', '--depth=1', 'origin', RESULTS_BRANCH], cwd=root)
            command(['git', 'checkout', '--quiet', '-B', RESULTS_BRANCH, 'FETCH_HEAD'], cwd=root)
        (root/'reports').mkdir(exist_ok=True)
        (root/(relative + '.json')).write_text(json.dumps(report, indent=2) + '\n')
        (root/(relative + '.txt')).write_text(log)
        (root/'README.md').write_text(
            '# External test results\n\n'
            'These reports come from manually invoked Linux/WSL test runs of exact source commits.\n'
            'They are separate from GitHub Actions and do not claim automatic coverage of future pushes.\n\n'
            'Latest tested commit: [' + sha + '](https://github.com/' + slug + '/commit/' + sha + ')\n\n'
            '[Report](' + relative + '.json) · [Test log](' + relative + '.txt)\n')
        command(['git', 'add', 'README.md', relative + '.json', relative + '.txt'], cwd=root)
        author = github('user')
        email = str(author['id']) + '+' + author['login'] + '@users.noreply.github.com'
        command(['git', '-c', 'user.name=' + author['login'], '-c', 'user.email=' + email,
                 'commit', '--quiet', '-m', 'External test result for ' + sha[:12] + ' [skip ci]'], cwd=root)
        report_sha = command(['git', 'rev-parse', 'HEAD'], cwd=root)
        command(['git', 'push', '--quiet', 'origin', 'HEAD:refs/heads/' + RESULTS_BRANCH], cwd=root)
    return 'https://github.com/' + slug + '/blob/' + report_sha + '/' + relative + '.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='HEAD', help='Committed revision to test; dirty edits are excluded')
    parser.add_argument('--publish', action='store_true', help='Publish a sanitized report and external/tests commit status')
    parser.add_argument('--repo', help='GitHub owner/name; defaults to current repository')
    args = parser.parse_args()
    repository = Path(command(['git', 'rev-parse', '--show-toplevel']))
    sha = command(['git', 'rev-parse', args.revision + '^{commit}'], cwd=repository)
    slug = args.repo
    if args.publish:
        slug = slug or json.loads(command(['gh', 'repo', 'view', '--json', 'nameWithOwner'], cwd=repository))['nameWithOwner']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', slug):
            parser.error('Expected GitHub owner/name')
        github('repos/' + slug + '/git/commits/' + sha)  # Refuse unpublished or wrong-repository commits.
        github('repos/' + slug + '/statuses/' + sha,
               {'state': 'pending', 'context': CONTEXT, 'description': 'External Linux/WSL snapshot tests running'})
    report, log = run_tests(repository, sha)
    output = repository/'.local/external-results'/report['commit']
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    output.with_suffix('.txt').write_text(log)
    if args.publish:
        report['target_url'] = publish_report(repository, slug, report, log)
        count = report['summary']['tests_run']
        github('repos/' + slug + '/statuses/' + report['commit'],
               {'state': report['state'], 'context': CONTEXT, 'target_url': report['target_url'],
                'description': f'Python {report["python"]}: {count} tests; compile and tests {report["state"]}'})
    print(json.dumps(report, indent=2))
    return 0 if report['state'] == 'success' else 1


if __name__ == '__main__':
    sys.exit(main())
