"""Behavioral regressions for the independently reviewed failures; no AI calls."""
import asyncio
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
import agent_hub as hub
hub.asyncio=asyncio


class ProcessChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='agent-hub-regression-')
        self.children=[]
        self.stack=__import__('contextlib').ExitStack()
        self.stack.enter_context(patch.object(hub,'RUNTIME',self.tmp.name))
        self.stack.enter_context(patch.object(hub,'TERMINATE_GRACE',.05,create=True))
        self.stack.enter_context(patch.object(hub,'VIEWER_TIMEOUT',.15,create=True))
        self.stack.enter_context(patch.object(hub,'stop_if_empty',lambda:None))

    async def asyncTearDown(self):
        tasks=[s.kill() for s in self.children]
        await asyncio.gather(*(task for task in tasks if task is not None),return_exceptions=True)
        for s in self.children:
            if s.proc.poll() is None:
                os.kill(s.proc.pid,signal.SIGKILL)
            await asyncio.to_thread(s.proc.wait)
        await asyncio.sleep(.03)
        hub.SESSIONS.clear()
        self.stack.close()
        self.tmp.cleanup()

    async def child(self, code):
        s=hub.Session('test','claude',[sys.executable,'-c',code],self.tmp.name,dict(os.environ),'agent-a',1)
        self.children.append(s)
        hub.SESSIONS[s.name]=s
        for _ in range(100):
            if 'READY' in s.render(30):return s
            await asyncio.sleep(.01)
        self.fail('child did not become ready')

    async def test_kill_does_not_report_success_before_resistant_child_is_dead(self):
        s=await self.child("import signal,time;signal.signal(signal.SIGHUP,signal.SIG_IGN);print('READY',flush=True);time.sleep(60)")
        await hub.dispatch({'op':'kill','name':s.name,'owner':'agent-a'})
        self.assertIsNotNone(s.proc.poll(),'kill returned while the SIGHUP-resistant child was alive')

    async def test_split_escape_payload_never_appears_in_screen(self):
        s=await self.child("import os,time;os.write(1,b'\\x1b_Ga=d;');time.sleep(.05);os.write(1,b'PAYLOAD\\x1b\\\\READY');time.sleep(60)")
        self.assertNotIn('PAYLOAD',s.render(30))
        self.assertNotIn('Ga=d;',s.render(30))

    async def test_resize_retains_output_from_apps_that_do_not_redraw(self):
        s=await self.child("import time;print('READY',flush=True);time.sleep(60)")
        s.resize(40,5)
        s.resize(80,24)
        self.assertIn('READY',s.render(100))

    async def test_archived_screen_is_still_private(self):
        s=await self.child("import time;print('READY',flush=True);time.sleep(60)")
        await hub.dispatch({'op':'kill','name':s.name,'owner':'agent-a'})
        self.assertIn('READY',hub.archived(s.name,'agent-a')['text'])
        self.assertIsNone(hub.archived(s.name,'agent-b'))

    async def test_visible_spawn_failure_stops_its_new_session(self):
        req={'op':'spawn','kind':'claude','name':'bad-viewer','argv':[sys.executable,'-c',"import time;print('READY',flush=True);time.sleep(60)"],
             'cwd':self.tmp.name,'env':dict(os.environ),'owner':'agent-a','depth':0,'visible':True}
        with patch.object(hub,'TERMINAL',['/usr/bin/false']):
            try:
                with self.assertRaisesRegex((RuntimeError,OSError),'terminal|viewer|Foot'):
                    await hub.dispatch(req)
            finally:
                if 'bad-viewer' in hub.SESSIONS:
                    self.children.append(hub.SESSIONS['bad-viewer'])
        self.assertNotIn('bad-viewer',hub.SESSIONS)


class OwnerChecks(unittest.TestCase):
    def proc_file(self,path,*args,**kwargs):
        if str(path).endswith('/stat'):
            fields=['S','1']+['0']*17+['999']
            return io.StringIO('100 (node) '+' '.join(fields))
        if str(path).endswith('/cmdline'):
            return io.BytesIO(self.argv)
        raise FileNotFoundError(path)

    def owner(self,argv,env):
        self.argv=argv
        with patch('builtins.open',self.proc_file),patch.object(hub.os,'getppid',return_value=100),patch.dict(os.environ,env,clear=True),patch.object(sys.stdin,'isatty',return_value=False),patch.object(sys.stdout,'isatty',return_value=False):
            return hub.caller_owner()

    def test_distinct_codex_threads_cannot_share_an_owner(self):
        a=self.owner(b'codex\0app-server\0',{'CODEX_THREAD_ID':'thread-A'})
        b=self.owner(b'codex\0app-server\0',{'CODEX_THREAD_ID':'thread-B'})
        self.assertNotEqual(a,b)

    def test_unknown_automation_never_gets_human_access(self):
        self.assertIsNotNone(self.owner(b'node\0/unknown/agent.js\0',{}))

    def test_shared_codex_host_without_a_conversation_id_fails_closed(self):
        for env in ({},{'CODEX_SESSION_ID':'shared-host-session'}):
            with self.subTest(env=env),self.assertRaisesRegex(RuntimeError,'conversation ID'):
                self.owner(b'codex\0app-server\0',env)

    def test_stream_filter_handles_every_split_boundary(self):
        for sequence in (b'\x1b_Ga=d;PAYLOAD\x1b\\',b'\x1bPdata\x1b\\',b'\x1b[>1;2u',b'\x1b[=1u'):
            for split in range(len(sequence)+1):
                with self.subTest(sequence=sequence,split=split):
                    f=hub.ScreenFilter()
                    self.assertEqual(f.feed(sequence[:split])+f.feed(sequence[split:])+f.feed(b'READY'),b'READY')


class ConfigChecks(unittest.TestCase):
    def test_model_restore_preserves_unrelated_user_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'settings.json'
            p.write_text(json.dumps({'model':'old','unrelated':'before'}))
            with patch.dict(hub.MODEL_DEFAULTS_FILE,claude=str(p)),patch.object(hub,'RUNTIME',tmp),patch.object(hub,'SETTINGS_LOCK',str(Path(tmp)/'lock')):
                with hub.keep_user_default('claude'):
                    p.write_text(json.dumps({'model':'new','unrelated':'user-edit'}))
            self.assertEqual(json.loads(p.read_text()),{'model':'old','unrelated':'user-edit'})

    def test_model_restore_preserves_a_settings_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'real.json';target.write_text('{"model":"old"}')
            p=Path(tmp)/'settings.json';p.symlink_to(target)
            with patch.dict(hub.MODEL_DEFAULTS_FILE,claude=str(p)),patch.object(hub,'RUNTIME',tmp),patch.object(hub,'SETTINGS_LOCK',str(Path(tmp)/'lock')):
                with hub.keep_user_default('claude'):
                    p.write_text('{"model":"new"}')
            self.assertTrue(p.is_symlink())
            self.assertEqual(json.loads(target.read_text())['model'],'old')

    def test_launch_model_merges_existing_overlay(self):
        captured=[]
        def request(op,**kwargs):captured.append(kwargs);return {'name':kwargs.get('name','test')}
        for kind,key,value in [('grok','GROK_CONFIG',{'models':{'other':'keep'},'unrelated':'keep'}),('opencode','OPENCODE_CONFIG_CONTENT',{'permission':{'bash':'deny'}})]:
            with self.subTest(kind=kind),patch.dict(os.environ,{key:json.dumps(value)}),patch.object(hub,'request',request),patch.object(hub,'wait_ready',return_value='READY'),patch.object(hub.shutil,'which',return_value='/usr/bin/true'):
                hub.spawn_agent(kind,'test',cwd='/tmp',model='new',visible=False)
                config=json.loads(captured[-1]['env'][key])
                for field,expected in value.items():
                    if field=='models':self.assertEqual(config['models']['other'],'keep')
                    else:self.assertEqual(config[field],expected)

    def test_option_shaped_model_values_are_rejected(self):
        for value in ('--settings','--dangerously-skip-permissions','-c'):
            with self.subTest(value=value),self.assertRaises(ValueError):hub.check_value('model',value)

    def test_grok_toml_restore_keeps_other_model_and_user_settings(self):
        import tomllib
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'config.toml';p.write_text('[models]\ndefault="old"\nother="keep"\n[user]\nnote="before"\n')
            with patch.dict(hub.MODEL_DEFAULTS_FILE,grok=str(p)),patch.object(hub,'RUNTIME',tmp),patch.object(hub,'SETTINGS_LOCK',str(Path(tmp)/'lock')):
                with hub.keep_user_default('grok','new',''):
                    p.write_text('[models]\ndefault="new"\nother="keep"\n[user]\nnote="user-edit"\n')
            d=tomllib.loads(p.read_text())
            self.assertEqual(d['models'],{'default':'old','other':'keep'})
            self.assertEqual(d['user']['note'],'user-edit')

    def test_claude_nested_effort_and_other_settings_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'settings.json'
            p.write_text(json.dumps({'modelSettings':{'test-model':{'effortLevel':'low'}},'permissions':{'allow':[]}}))
            with patch.dict(hub.MODEL_DEFAULTS_FILE,claude=str(p)),patch.object(hub,'RUNTIME',tmp),patch.object(hub,'SETTINGS_LOCK',str(Path(tmp)/'lock')):
                with hub.keep_user_default('claude','','high'):
                    p.write_text(json.dumps({'modelSettings':{'test-model':{'effortLevel':'high'}},'permissions':{'allow':['read']}}))
            d=json.loads(p.read_text())
            self.assertEqual(d['modelSettings']['test-model']['effortLevel'],'low')
            self.assertEqual(d['permissions']['allow'],['read'])

    def test_grok_restore_handles_quoted_keys_tables_dates_and_missing_newline(self):
        import tomllib
        for header,key,end in (('[models]','"default"','\n'),('["models"]','default','\n'),('[models]','default','')):
            with self.subTest(header=header,key=key,end=end),tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)/'config.toml'
                before=f'{header}\n{key}="old"\nother="before"\ndate=2026-10-26{end}'
                after=f'{header}\n{key}="new"\nother="after"\ndate=2026-10-26{end}'
                p.write_text(before)
                with patch.dict(hub.MODEL_DEFAULTS_FILE,grok=str(p)),patch.object(hub,'RUNTIME',tmp),patch.object(hub,'SETTINGS_LOCK',str(Path(tmp)/'lock')):
                    with hub.keep_user_default('grok','new',''):p.write_text(after)
                parsed=tomllib.loads(p.read_text())
                self.assertEqual(parsed['models']['default'],'old')
                self.assertEqual(parsed['models']['other'],'after')
                self.assertEqual(str(parsed['models']['date']),'2026-10-26')

    def test_opencode_bare_name_restores_and_picker_mismatch_warns(self):
        for requested,selected in [('beam-free','beam-free'),('opencode/beam-free','beam-free-pro')]:
            with self.subTest(requested=requested,selected=selected),tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)/'model.json';before={'recent':[{'providerID':'opencode','modelID':'old'}]}
                p.write_text(json.dumps(before));hub.WARNINGS.clear()
                with patch.dict(hub.MODEL_DEFAULTS_FILE,opencode=str(p)),patch.object(hub,'RUNTIME',tmp),patch.object(hub,'SETTINGS_LOCK',str(Path(tmp)/'lock')):
                    with hub.keep_user_default('opencode',requested,''):
                        p.write_text(json.dumps({'recent':[{'providerID':'opencode','modelID':selected}]}))
                if selected=='beam-free':self.assertEqual(json.loads(p.read_text()),before)
                else:
                    self.assertTrue(hub.WARNINGS)
                    self.assertEqual(json.loads(p.read_text())['recent'][0]['modelID'],selected)


if __name__=='__main__':unittest.main(verbosity=2)
