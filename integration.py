"""Private-daemon integration checks. Real PTYs/systemd/terminals; no model requests."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import agent_hub as installed


class DaemonChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='agent-hub-integration-')
        self.root=Path(self.tmp.name)
        self.entry=self.root/'entry.py'
        self.ready=self.root/'caller-ready'
        self.entry.write_text(f'''import os,sys,time
sys.path.insert(0,{str(Path(installed.__file__).parent)!r})
import agent_hub as h
h.RUNTIME={str(self.root/'rt')!r}
h.SOCK=h.RUNTIME+'/hub.sock'
h.LOG=h.RUNTIME+'/hub.log'
h.SETTINGS_LOCK=h.RUNTIME+'/settings.lock'
h.IDLE_SECONDS=1
h.TERMINATE_GRACE=1.5
h.__file__=__file__
if sys.argv[1:] == ['first-caller']:
    h.connect(autostart=True).close()
    open({str(self.ready)!r},'w').write('ready')
    time.sleep(60)
else:
    h.main()
''')
        spec=importlib.util.spec_from_file_location('private_hub_'+self.root.name,installed.__file__)
        self.h=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.h)
        self.h.RUNTIME=str(self.root/'rt');self.h.SOCK=self.h.RUNTIME+'/hub.sock'
        self.h.LOG=self.h.RUNTIME+'/hub.log';self.h.SETTINGS_LOCK=self.h.RUNTIME+'/settings.lock'
        self.h.__file__=str(self.entry)
        self.owner=f'proc:{os.getpid()}:{self.h.process_start(os.getpid())}:integration'
        self.h.OWNER=self.owner
        self.caller_unit='agent-hub-caller-'+self.root.name+'.scope'
        self.processes=[]

    def tearDown(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):proc.wait(timeout=3)
            if proc.stderr:
                proc.stderr.close()
        subprocess.run(['systemctl','--user','stop',self.caller_unit,self.h.daemon_unit()],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=12)
        self.tmp.cleanup()

    def wait_for(self,predicate,timeout=10):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if predicate():return
            time.sleep(.03)
        self.fail('condition did not become true')

    def spawn(self,name,code,visible=False,terminal='auto',workspace='special:agent-hub-integration',parent=''):
        result=self.h.request('spawn',name=name,kind='claude',argv=[sys.executable,'-c',code],
                              cwd=self.tmp.name,env=dict(os.environ),depth=0,parent=parent,visible=visible,terminal=terminal,
                              workspace=workspace)
        name=result['name']
        self.wait_for(lambda:'READY' in self.h.request('screen',name=name,lines=30)['text'])
        return name

    def test_daemon_survives_first_caller_scope_and_keeps_viewer(self):
        proc=subprocess.Popen(['systemd-run','--user','--scope','--quiet','--unit='+self.caller_unit,
                               sys.executable,str(self.entry),'first-caller'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        self.processes.append(proc)
        self.wait_for(self.ready.exists)
        name=self.spawn('scope-check',"import time;print('READY',flush=True);time.sleep(60)",visible=True)
        info=self.h.request('info',name=name)
        cgroup=Path(f"/proc/{info['pid']}/cgroup").read_text()
        self.assertIn(self.h.daemon_unit()+'.service',cgroup)
        self.assertNotIn(self.caller_unit,cgroup)
        self.assertEqual(self.h.request('list')['agents'][0]['viewers'],1)
        subprocess.run(['systemctl','--user','stop',self.caller_unit],check=True,timeout=8)
        self.assertTrue(self.h.request('info',name=name)['alive'])
        self.assertEqual(self.h.request('list')['agents'][0]['viewers'],1)
        self.h.request('kill',name=name)
        self.wait_for(lambda:not Path(self.h.SOCK).exists())
        self.assertIn('READY',self.h.request('screen',name=name,lines=30)['text'])
        self.assertFalse(Path(self.h.SOCK).exists(),'reading archived output restarted a daemon')

    def test_kill_finishes_before_idle_exit_and_cache_survives(self):
        name=self.spawn('resistant',"import signal,time;signal.signal(signal.SIGHUP,signal.SIG_IGN);print('READY',flush=True);time.sleep(60)")
        pid=self.h.request('info',name=name)['pid']
        time.sleep(.8)
        self.h.request('kill',name=name)
        self.assertFalse(Path(f'/proc/{pid}').exists(),'kill returned before child reaping')
        self.wait_for(lambda:not Path(self.h.SOCK).exists())
        self.assertIn('READY',self.h.request('screen',name=name)['text'])
        self.assertEqual(self.h.request('list')['agents'],[])
        self.assertFalse(Path(self.h.SOCK).exists())

    def test_stale_socket_list_never_starts_a_daemon(self):
        import socket
        Path(self.h.RUNTIME).mkdir()
        sock=socket.socket(socket.AF_UNIX);sock.bind(self.h.SOCK);sock.close()
        self.assertEqual(self.h.request('list')['agents'],[])
        result=subprocess.run(['systemctl','--user','is-active','--quiet',self.h.daemon_unit()])
        self.assertNotEqual(result.returncode,0)

    def test_spawn_retries_while_previous_unit_is_deactivating(self):
        from unittest.mock import patch
        real_run=subprocess.run
        def with_slow_stop(args,**kwargs):
            if args[0]=='systemd-run':
                index=args.index(sys.executable)
                args=args[:index]+['--property=ExecStopPost=/usr/bin/sleep 0.3']+args[index:]
            return real_run(args,**kwargs)
        with patch.object(self.h.subprocess,'run',with_slow_stop):
            for index in range(3):
                name=self.spawn('restart-'+str(index),"import time;print('READY',flush=True);time.sleep(60)")
                self.h.request('kill',name=name)
                self.wait_for(lambda:not Path(self.h.SOCK).exists())

    def test_children_forked_during_shutdown_or_natural_exit_do_not_escape(self):
        for trigger in ('kill','natural'):
            with self.subTest(trigger=trigger):
                pidfile=self.root/(trigger+'.pid')
                code='''import os,signal,time
def leave(*args):
    signal.signal(signal.SIGHUP,signal.SIG_IGN)
    if os.fork()==0:
        os.setsid()
        fd=os.open('/dev/null',os.O_RDWR)
        for n in (0,1,2):os.dup2(fd,n)
        open(PIDFILE,'w').write(str(os.getpid()))
        time.sleep(60)
        os._exit(0)
    while not os.path.exists(PIDFILE):time.sleep(.001)
    os._exit(0)
'''.replace('PIDFILE',repr(str(pidfile)))
                code+=("signal.signal(signal.SIGHUP,leave)\nprint('READY',flush=True)\ntime.sleep(60)" if trigger=='kill'
                       else "print('READY',flush=True)\ntime.sleep(.2)\nleave()")
                name=self.spawn('late-fork-'+trigger,code)
                if trigger=='kill':self.h.request('kill',name=name)
                else:self.wait_for(lambda:not self.h.request('info',name=name)['alive'],timeout=10)
                self.assertTrue(pidfile.exists())
                child=int(pidfile.read_text())
                path=Path(f'/proc/{child}/stat')
                self.wait_for(lambda:not path.exists() or path.read_text().rsplit(')',1)[1].split()[0]=='Z')
                self.assertFalse(self.h.request('info',name=name)['alive'])


    def test_each_installed_terminal_attaches_and_closes(self):
        available = [name for name in self.h.TERMINALS if shutil.which(name)
                     and (name != 'foot' or os.environ.get('WAYLAND_DISPLAY'))]
        if not available:
            self.skipTest('no supported terminal installed')
        for terminal in available:
            with self.subTest(terminal=terminal):
                before = json.loads(self.h.hyprland_request(['-j', 'activewindow'], dict(os.environ))) if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') else None
                name = self.spawn('viewer-' + terminal, "import time;print('READY',flush=True);time.sleep(60)",
                                  visible=True, terminal=terminal)
                self.assertEqual(self.h.request('list')['agents'][0]['viewers'], 1)
                if before is not None:
                    token = 'agent-hub-' + self.h.hashlib.sha256(b'special:agent-hub-integration').hexdigest()[:16]
                    windows = [w for w in self.h.hyprland_clients(dict(os.environ)) if w['initialTitle'] == token]
                    self.assertEqual(len(windows), 1)
                    self.assertEqual(windows[0]['workspace']['name'], 'special:agent-hub-integration')
                    after = json.loads(self.h.hyprland_request(['-j', 'activewindow'], dict(os.environ)))
                    self.assertEqual(after.get('address'), before.get('address'), 'helper stole focus')
                self.h.request('kill', name=name)
                self.assertFalse(self.h.request('info', name=name)['alive'])

    @unittest.skipUnless(os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') and shutil.which('foot'), 'needs Hyprland and Foot')
    def test_nested_helper_follows_moved_parent_without_taking_focus(self):
        env = dict(os.environ)
        before = json.loads(self.h.hyprland_request(['-j', 'activewindow'], env))
        code = "import time;print('READY',flush=True);time.sleep(60)"
        parent = self.spawn('workspace-parent', code, visible=True, terminal='foot')
        token = 'agent-hub-' + self.h.hashlib.sha256(b'special:agent-hub-integration').hexdigest()[:16]
        window = next(w for w in self.h.hyprland_clients(env) if w['initialTitle'] == token)
        moved = 'special:agent-hub-moved'
        reply = self.h.hyprland_request(['eval', 'hl.dispatch(hl.dsp.window.move({window=' +
            json.dumps('address:' + window['address']) + ',workspace=' + json.dumps(moved) + ',follow=false}))'], env)
        self.assertEqual(reply, 'ok')
        child = self.spawn('workspace-child', code, visible=True, terminal='foot', parent=parent)
        self.assertEqual(self.h.request('info', name=child)['workspace'], moved)
        child_token = 'agent-hub-' + self.h.hashlib.sha256(moved.encode()).hexdigest()[:16]
        child_window = next(w for w in self.h.hyprland_clients(env) if w['initialTitle'] == child_token)
        self.assertEqual(child_window['workspace']['name'], moved)
        after = json.loads(self.h.hyprland_request(['-j', 'activewindow'], env))
        self.assertEqual(after.get('address'), before.get('address'), 'nested helper stole focus')
        self.h.request('kill', name=parent)

    @unittest.skipUnless(os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') and shutil.which('foot'), 'needs Hyprland and Foot')
    def test_terminal_caller_is_found_on_an_inactive_workspace(self):
        report = self.root / 'origin'
        script = self.root / 'origin.py'
        script.write_text('import os,sys,time\nsys.path.insert(0,' + repr(str(Path(installed.__file__).parent)) +
                          ')\nimport agent_hub as h\nopen(' + repr(str(report)) +
                          ',"w").write(h.caller_workspace(dict(os.environ)))\ntime.sleep(60)\n')
        env = dict(os.environ)
        env.pop('AGENT_HUB_WORKSPACE', None)
        terminal = self.h.background_terminal(self.h.terminal_command('foot', env), 'special:agent-hub-origin', env)
        proc = subprocess.Popen([*terminal, sys.executable, str(script)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(proc)
        self.wait_for(lambda: report.exists() and bool(report.read_text()))
        self.assertEqual(report.read_text(), 'special:agent-hub-origin')

    def test_named_accounts_reach_real_children_and_keep_their_identity(self):
        from unittest.mock import patch
        launcher = self.root / 'fake-agent'
        launcher.write_text('#!' + sys.executable + '\n' + '''import json,os,time
print('\\x1b[?2004hREADY',flush=True)
for key,value in [('cwd',os.getcwd()),('claude',os.getenv('CLAUDE_CONFIG_DIR')),('codex',os.getenv('CODEX_HOME'))]:
    print(key+'='+str(value),flush=True)
time.sleep(60)
''')
        launcher.chmod(0o700)
        profiles = self.root / 'profiles.toml'
        entries = []
        for kind in ('claude', 'codex'):
            for account in ('first', 'second'):
                name = kind + '-' + account
                entries.append(f'[profiles.{name}]\nkind="{kind}"\ncommand={json.dumps(str(launcher))}\nhome={json.dumps(str(self.root/name))}\n')
        profiles.write_text('\n'.join(entries))
        with patch.dict(os.environ, {'AGENT_HUB_PROFILES':str(profiles)}):
            for kind in ('claude', 'codex'):
                for account in ('first', 'second'):
                    name = kind + '-' + account
                    self.h.spawn_agent(name, name, self.tmp.name, visible=False)
                    info = self.h.request('info', name=name)
                    screen = self.h.request('screen', name=name, lines=30)['text']
                    self.assertEqual(info['profile'], name)
                    self.assertEqual(info['kind'], kind)
                    self.assertIn(str(self.root/name), screen)
                    self.assertIn(self.tmp.name, screen)
                    if kind == 'claude':
                        self.assertEqual(info['settings_file'], str(self.root/name/'settings.json'))
            self.assertEqual({s['profile'] for s in self.h.request('list')['agents']},
                             {'claude-first','claude-second','codex-first','codex-second'})
            for name in ('claude-first','claude-second','codex-first','codex-second'):
                self.h.request('kill', name=name)
                self.assertEqual(self.h.request('info', name=name)['profile'], name)


if __name__=='__main__':unittest.main(verbosity=2)
