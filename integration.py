"""Private-daemon integration checks. Real PTYs/systemd/Foot; no model requests."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import signal
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

    def spawn(self,name,code,visible=False):
        result=self.h.request('spawn',name=name,kind='claude',argv=[sys.executable,'-c',code],
                              cwd=self.tmp.name,env=dict(os.environ),depth=0,parent='',visible=visible)
        name=result['name']
        self.wait_for(lambda:'READY' in self.h.request('screen',name=name,lines=30)['text'])
        return name

    def test_daemon_survives_first_caller_scope_and_keeps_foot_viewer(self):
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


if __name__=='__main__':unittest.main(verbosity=2)
