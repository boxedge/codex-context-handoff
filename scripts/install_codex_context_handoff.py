#!/usr/bin/env python3
"""Install only the context-handoff hooks; preserve existing hooks and model settings."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import time
import tomllib

from codex_context_handoff import AppServer, DEFAULT_ROOT, atomic_bytes, atomic_json, load_json, local_path, capture_queue_environment, adopt_queue_jobs, RecoveryQueue

EVENTS = ('PreToolUse', 'PostToolUse', 'UserPromptSubmit', 'Stop', 'PreCompact')
CONTEXT_EVENTS = {'PreToolUse', 'PostToolUse', 'UserPromptSubmit'}
def default_homes():
    return (local_path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))),)


def merge_hooks(document, command, install_directory):
    result = copy.deepcopy(document)
    events = result.setdefault('hooks', {})
    for event in EVENTS:
        found = False
        for group in events.setdefault(event, []):
            for handler in group.get('hooks', []):
                if str(install_directory) + '/' in handler.get('command', ''):
                    handler.update(command=command, timeout=8)
                    if event in CONTEXT_EVENTS:
                        handler['additionalContextLimit'] = 800
                    else:
                        handler.pop('additionalContextLimit', None)
                    found = True
        if not found:
            handler = {'type':'command', 'command':command, 'timeout':8}
            if event in CONTEXT_EVENTS:
                handler['additionalContextLimit'] = 800
            events[event].append({'hooks': [handler]})
    return result


def trust_config(text, rows):
    original = tomllib.loads(text)
    for row in rows:
        key = json.dumps(row['key'])
        header = '[hooks.state.' + key + ']'
        block = header + '\nenabled = true\ntrusted_hash = ' + json.dumps(row['currentHash']) + '\n'
        pattern = r'(?ms)^' + re.escape(header) + r'\s*\n.*?(?=^\[|\Z)'
        if re.search(pattern, text):
            text = re.sub(pattern, lambda _: block + '\n', text)
        else:
            text = text.rstrip() + '\n\n' + block
    updated = tomllib.loads(text)
    # Every previous setting and hook state must remain identical except this integration's states.
    before = copy.deepcopy(original)
    after = copy.deepcopy(updated)
    for value in (before, after):
        states = value.get('hooks', {}).get('state', {})
        for row in rows:
            states.pop(row['key'], None)
        if not states and 'hooks' in value:
            value['hooks'].pop('state', None)
        if not value.get('hooks'):
            value.pop('hooks', None)
    if before != after:
        raise ValueError('Unexpected change outside the handoff hook trust states')
    return text


def hook_rows(home, command, binary):
    env = os.environ.copy(); env['CODEX_HOME'] = str(home)
    with AppServer(binary=binary, env=env) as server:
        response = server.rpc('hooks/list', {'cwds': [str(Path.cwd())]})
    return [hook for item in response['data'] for hook in item['hooks'] if hook.get('command') == command]


def compatibility_entrypoint(alias, installed, library, backups):
    alias, installed, library = map(Path, (alias, installed, library))
    if alias.resolve().parent != library.resolve() or alias == installed:
        raise ValueError('Compatibility entrypoint must be a separate managed helper')
    if alias.exists():
        original = alias.read_bytes()
        match = re.fullmatch(r'handoff-([a-f0-9]{16})\.py', alias.name)
        owned_version = match and hashlib.sha256(original).hexdigest().startswith(match[1])
        if not owned_version and not original.startswith(b'# Codex context handoff compatibility loader\n'):
            raise ValueError('Refusing to replace an unrecognized entrypoint')
        atomic_bytes(Path(backups)/('entrypoint-' + alias.name), original)
    loader = ('# Codex context handoff compatibility loader\n'
              '# Cached hooks retain their command and trust hash; running processes are untouched.\n'
              'import runpy\nrunpy.run_path(' + repr(str(installed)) + ', run_name="__main__")\n')
    atomic_bytes(alias, loader.encode())
    return alias


def install_queue_service(root, executable, unit_directory=None, runner=None):
    runner = runner or subprocess.run
    directory = Path(unit_directory or Path.home()/'.config/systemd/user')
    directory.mkdir(parents=True,exist_ok=True)
    path = directory/'codex-context-handoff-queue.service'
    marker = '# Managed by codex-context-handoff queue\n'
    if path.exists() and not path.read_text().startswith(marker):
        raise ValueError('Refusing to replace an unrecognized queue service')
    command = '/usr/bin/python3 ' + shlex.join([str(executable),'queue-worker','--root',str(root)])
    unit = (marker + '[Unit]\nDescription=Durable Codex continuation queue\nStartLimitIntervalSec=0\n\n'
            '[Service]\nType=simple\nUMask=0077\nExecStart=' + command.replace('%','%%') +
            '\nRestart=on-failure\nRestartSec=2\nTimeoutStopSec=15\n\n[Install]\nWantedBy=default.target\n')
    atomic_bytes(path,unit.encode())
    settings = load_json(Path(root)/'settings.json')
    settings['queue'] = dict(settings.get('queue',{}),enabled=True)
    atomic_json(Path(root)/'settings.json',settings)
    capture_queue_environment(root)
    RecoveryQueue(root)
    adopted = adopt_queue_jobs(root)
    for command in (['systemctl','--user','daemon-reload'],
                    ['systemctl','--user','enable','codex-context-handoff-queue.service'],
                    ['systemctl','--user','restart','codex-context-handoff-queue.service']):
        runner(command,check=True,capture_output=True,text=True)
    return {'unit':str(path),'database':str(Path(root)/'queue.sqlite3'),'adopted_jobs':adopted}



def install(root=DEFAULT_ROOT, homes=None, library=None, entrypoint=None, enable_queue=False):
    root = Path(root)
    homes = tuple(dict.fromkeys(local_path(p).expanduser().resolve() for p in
                                (homes if homes is not None else default_homes())))
    if not homes:
        raise ValueError('Select at least one Codex configuration home')
    library = Path(library or Path.home()/'.local/lib/codex-context-handoff')
    library.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = Path(__file__).with_name('codex_context_handoff.py').read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    installed = library / ('handoff-' + digest[:16] + '.py')
    if installed.exists() and installed.read_bytes() != source:
        raise ValueError('Installed version path has unexpected contents')
    if not installed.exists():
        atomic_bytes(installed, source)
    binary = shutil.which('codex')
    if not binary:
        raise ValueError('Codex binary is not available')
    settings = root/'settings.json'
    if not settings.exists():
        atomic_json(settings, {'version':1,'enabled':False,'prepare_tokens':350000,'rollover_tokens':400000,
                              'refresh_tokens':20000,'bootstrap_timeout':90,'codex_binary':binary,
                              'allowed_sessions':[], 'config_home':str(homes[0]),
                              'auto_continue':False, 'auto_open':False, 'continuation_backend':'desktop'})
    backups = root/'backups'/str(time.time_ns()); backups.mkdir(parents=True, mode=0o700)
    entrypoint = entrypoint or load_json(settings).get('hook_entrypoint') or library/'handoff-entrypoint.py'
    executable = compatibility_entrypoint(entrypoint,installed,library,backups)
    configuration = load_json(settings)
    configuration['hook_entrypoint'] = str(executable)
    atomic_json(settings, configuration)
    command = shlex.join([sys.executable,str(executable),'hook','--root',str(root)])
    report = {'installed':str(installed),'entrypoint':str(executable),'sha256':digest,'command':command,'backups':str(backups),'homes':[]}
    for index, home in enumerate(map(Path, homes)):
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        hooks_path = home/'hooks.json'; config_path = home/'config.toml'
        raw_hooks = hooks_path.read_bytes() if hooks_path.exists() else None
        raw_config = config_path.read_bytes() if config_path.exists() else b''
        document = json.loads(raw_hooks) if raw_hooks else {}
        atomic_bytes(backups/f'{index}-config.toml', raw_config)
        if raw_hooks is not None:
            atomic_bytes(backups/f'{index}-hooks.json',raw_hooks)
        merged = merge_hooks(document,command,library)
        if hooks_path.exists() and hooks_path.read_bytes() != raw_hooks:
            raise ValueError('Hooks changed concurrently; installation stopped')
        atomic_json(hooks_path,merged)
        rows = hook_rows(home,command,binary)
        if len(rows) != len(EVENTS):
            raise ValueError('Installed Codex did not recognize all handoff lifecycle hooks')
        if (config_path.read_bytes() if config_path.exists() else b'') != raw_config:
            raise ValueError('Configuration changed concurrently; installation stopped')
        changed = trust_config(raw_config.decode(),rows).encode()
        atomic_bytes(config_path,changed)
        verified = hook_rows(home,command,binary)
        if not all(row.get('enabled') and row.get('currentHash') for row in verified):
            raise ValueError('Handoff hooks are not enabled')
        report['homes'].append({'home':str(home),'hooks':[{key:row.get(key) for key in ('key','eventName','enabled','currentHash','status')} for row in verified],
                                'model_settings_preserved':True})
    if enable_queue or load_json(settings).get('queue',{}).get('enabled'):
        report['queue'] = install_queue_service(root,executable)
    atomic_json(root/'installation.json',report)
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    parser.add_argument('--entrypoint',type=Path)
    parser.add_argument('--home',type=Path,action='append',dest='homes',
                        help='Codex home to configure; repeat for multiple homes. Defaults to CODEX_HOME or ~/.codex.')
    parser.add_argument('--library',type=Path,help='Versioned helper directory (default: ~/.local/lib/codex-context-handoff)')
    parser.add_argument('--enable-queue',action='store_true')
    args=parser.parse_args()
    print(json.dumps(install(args.root,homes=args.homes,library=args.library,entrypoint=args.entrypoint,enable_queue=args.enable_queue),indent=2))
