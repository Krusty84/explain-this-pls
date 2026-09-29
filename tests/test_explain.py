# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

from __future__ import annotations
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from explain import AuditError, Repository, Runner, UnsafeRepository, cli_env, load_config, process, repository_lock, slug
from reporting import Reporter
from opencode import prepare_environment
from contracts import ContractError, parse_backend, review_verdict, strict_json, validate_result

BASE = {'completion_status': 'COMPLETE',
        'report_markdown': '# Report\nC-001\n', 'limitations': []}

def doc(branch, commit):
    return dict(BASE, task='architecture_documentation', branch=branch, source_commit=commit)

def review(branch, commit):
    return dict(BASE, task='architecture_review', branch=branch, source_commit=commit,
        verdict='PASS', claim_inventory_complete=True,
        claims=[{'id': 'C-001', 'location': 'overview', 'statement': 'Has an entry point',
        'outcome': 'SUPPORTED', 'evidence': ['app.py:main'], 'limitation': '', 'finding_ids': []}], findings=[])

class ContractTests(unittest.TestCase):
    def test_results_without_schema_version_and_rejection_of_legacy_field(self):
        git_context = {'branch': 'master', 'source_commit': 'abc'}
        folder_context = {'source_directory': '/source', 'source_fingerprint': 'abc'}
        comparison = dict(BASE, task='architecture_comparison', baseline_branch='master',
            baseline_commit='abc', compared_branches=[], unresolved_branches=[], differences=[])
        comparison_context = {'baseline_branch': 'master', 'baseline_commit': 'abc',
                              'requested_branches': ['master'], 'branches': []}
        cases = [('compare', comparison, comparison_context, 'git')]
        for mode, context in (('git', git_context), ('folder', folder_context)):
            for stage, data in (('study', doc('master', 'abc')), ('review', review('master', 'abc'))):
                if mode == 'folder':
                    del data['branch']; del data['source_commit']
                    data.update(context)
                cases.append((stage, data, context, mode))
        for stage, data, context, mode in cases:
            with self.subTest(stage=stage, mode=mode):
                validate_result(stage, data, context, mode)
                with self.assertRaisesRegex(ContractError, 'missing/extra keys'):
                    validate_result(stage, data | {'schema_version': '3.0' if mode == 'folder' else '2.0'},
                                    context, mode)

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ContractError):
            strict_json('{"x":1,"x":2}')
    def test_nonfinite_rejected(self):
        with self.assertRaises(ContractError):
            strict_json('{"x":NaN}')
    def test_codex_native_json(self):
        self.assertEqual(parse_backend('codex', json.dumps(doc('master', 'abc')))[0]['branch'], 'master')
    def test_fenced_output_rejected(self):
        with self.assertRaises(ContractError):
            parse_backend('codex', '```json\n{}\n```')
    def test_claude_structured_output(self):
        transport = {'is_error': False, 'structured_output': doc('master', 'abc'), 'session_id': 's'}
        data, meta = parse_backend('claude-code', json.dumps(transport))
        self.assertEqual(data['branch'], 'master')
        self.assertEqual(meta['session_id'], 's')
    def test_claude_error_is_not_success(self):
        with self.assertRaises(ContractError):
            parse_backend('claude-code', '{"is_error":true,"result":"error"}')
    def test_opencode_old_text_transport_is_explicitly_rejected(self):
        events = [
            {'type':'text','sessionID':'s','part':{'id':'p1','messageID':'m1','text':'Planning prose'}},
            {'type':'step_finish','part':{'messageID':'m1','reason':'tool-calls'}},
            {'type':'text','sessionID':'s','part':{'id':'p2','messageID':'m2','text':json.dumps(doc('test01','abc'))}},
            {'type':'step_finish','part':{'messageID':'m2','reason':'stop'}}]
        with self.assertRaises(ContractError) as caught:
            parse_backend('opencode', '\n'.join(map(json.dumps,events)))
        self.assertEqual(caught.exception.failure_kind, 'BACKEND_INCOMPATIBLE')
    def test_opencode_truncation_rejected(self):
        with self.assertRaises(ContractError):
            parse_backend('opencode', json.dumps({'type':'text','part':{'id':'p','messageID':'m','text':'{}'}}))
    def test_wrong_commit_rejected(self):
        with self.assertRaises(ContractError):
            validate_result('study',doc('master','wrong'),{'branch':'master','source_commit':'abc'})
    def test_partial_review_cannot_pass(self):
        data = review('master','abc')
        data.update(completion_status='PARTIAL',limitations=['coverage incomplete'])
        with self.assertRaises(ContractError):
            validate_result('review',data,{'branch':'master','source_commit':'abc'})
        self.assertEqual(review_verdict(data),'INCONCLUSIVE')
    def test_complete_review_cannot_have_unchecked_claim(self):
        data=review('master','abc')
        data['claims'][0].update(outcome='NOT_CHECKED',limitation='not inspected')
        with self.assertRaises(ContractError):
            validate_result('review',data,{'branch':'master','source_commit':'abc'})
    def test_slugs_do_not_collide(self):
        self.assertNotEqual(slug('customer/a'),slug('customer_a'))
        self.assertNotIn('/',slug('../../customer/a'))

class RepoFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name).resolve()
        self.repo_path=self.base/'repo'; self.repo_path.mkdir()
        self.git('init','-b','master')
        self.git('config','user.email','test@example.invalid')
        self.git('config','user.name','Fixture')
        (self.repo_path/'app.py').write_text('def main(): return "master"\n')
        self.git('add','app.py'); self.git('commit','-m','base')
        self.master=self.git('rev-parse','HEAD').strip()
        for b in ('test01','dev_01_customerA'):
            self.git('switch','-c',b,'master')
            (self.repo_path/'app.py').write_text(f'def main(): return "{b}"\n')
            self.git('add','app.py'); self.git('commit','-m',b)
        self.git('switch','master')
        self.repo=Repository(self.repo_path)
    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()
    def git(self,*args):
        r=subprocess.run(['git','-C',str(self.repo_path),*args],stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,check=True)
        return r.stdout.decode()
    def config(self):
        reports=self.base/'reports'; reports.mkdir(exist_ok=True)
        agent={'backend':'codex','executable':str(Path(sys.executable).resolve()),
               'model':None}
        return {'repository':str(self.repo_path),'reports_dir':str(reports),
            'branches':['master','test01','dev_01_customerA'],'baseline_branch':'master',
            'output_language':'Russian','project_description':'ERP-система 1995 года.',
            'priority_scenarios':[],'continue_on_error':True,
            '_agents':{s:dict(agent) for s in ('study','review','compare')},
            '_prompt_paths':{s:str(Path(__file__).resolve().parents[1]/'prompts'/f'{s}.md') for s in ('study','review','compare')}}
    def test_grouped_git_configuration_runs_and_restores(self):
        cfg=self.config()
        cfg['mode']='git'
        cfg['git_mode']={key:cfg.pop(key) for key in ('repository','branches','baseline_branch')}
        cfg['folder_mode']={'path':'/missing/inactive/folder'}
        result,code=FakeRunner(cfg,self.base/'grouped-run').run()
        self.assertEqual(code,0)
        self.assertEqual(result['status'],'COMPLETE')
        self.assertNotIn('schema_version',result)
        self.assertEqual(result['comparison']['compared_branches'],['test01','dev_01_customerA'])
        self.assertEqual(self.repo.symbolic(),'master')
        self.assertEqual(self.repo.head(),self.master)
        self.repo.clean()
    def test_pins_and_restore(self):
        pins=self.repo.preflight(['master','test01'])
        self.repo.checkout(pins['test01'])
        self.assertIsNone(self.repo.symbolic())
        self.repo.restore('master',self.master)
        self.assertEqual(self.repo.symbolic(),'master')
    def test_dirty_worktree_rejected(self):
        (self.repo_path/'app.py').write_text('changed')
        with self.assertRaises(UnsafeRepository):self.repo.clean()
    def test_untracked_rejected(self):
        (self.repo_path/'old-report.md').write_text('must not affect next run')
        with self.assertRaises(UnsafeRepository):self.repo.clean()
    def test_ignored_files_rejected(self):
        (self.repo_path/'.gitignore').write_text('cache.tmp\n')
        self.git('add','.gitignore');self.git('commit','-m','ignore')
        (self.repo_path/'cache.tmp').write_text('stale data')
        with self.assertRaises(UnsafeRepository):self.repo.clean()
    def test_filter_rejected(self):
        self.git('config','filter.danger.smudge','echo bad')
        with self.assertRaises(AuditError):self.repo.preflight(['master','test01'])
    def test_git_delta_orientation(self):
        pin=self.repo.text('rev-parse','test01')
        delta=self.repo.delta(self.master,pin)
        self.assertEqual(delta['changes'][0]['path'],'app.py')
        self.assertEqual(delta['changes'][0]['status'],'M')
        self.assertFalse(delta['identical_trees'])
    def test_changed_original_branch_is_not_reset(self):
        self.repo.checkout(self.master)
        pin=self.repo.text('rev-parse','test01')
        self.git('update-ref','refs/heads/master',pin)
        with self.assertRaises(UnsafeRepository):self.repo.restore('master',self.master)
        self.assertEqual(self.repo.text('rev-parse','master'),pin)
    def test_symlink_repository_is_canonicalized_and_uses_same_lock(self):
        alias=self.base/'alias';alias.symlink_to(self.repo_path, target_is_directory=True)
        repo=Repository(alias)
        self.assertEqual(repo.preflight(['master','test01'])['master'],self.master)
        with repository_lock(self.repo_path):
            with self.assertRaises(AuditError):
                with repository_lock(alias):pass
    def test_all_three_branches_pipeline_and_context_separation(self):
        config=self.config(); dest=self.base/'reports'/'run';dest.mkdir()
        fake=FakeRunner(config,dest)
        result,code=fake.run()
        self.assertEqual(code,0)
        self.assertEqual(result['status'],'COMPLETE')
        self.assertEqual(len(fake.calls),7)
        for stage,context in fake.calls:
            self.assertEqual(context['project_description'],config['project_description'],stage)
        self.assertEqual(result['isolation'],'cli-native-permissions')
        self.assertNotIn('isolation_probe',result)
        self.assertEqual(self.repo.symbolic(),'master')
        self.assertEqual(self.repo.head(),self.master)
        self.assertTrue((dest/'comparison'/'inputs.json').exists())
    def test_one_branch_failure_does_not_pollute_or_stop_next_branch(self):
        config=self.config();dest=self.base/'reports'/'run';dest.mkdir()
        fake=FakeRunner(config,dest);fake.fail_branch='test01'
        result,code=fake.run()
        self.assertEqual(code,1)
        self.assertEqual(result['status'],'FAILED')
        self.assertIn('test01',result['comparison']['unresolved_branches'])
        self.assertTrue(result['branches'][2]['accepted'])
        self.assertEqual(self.repo.symbolic(),'master')
    def test_single_branch_status_and_restoration(self):
        cases=[('study','PARTIAL',True,'PARTIAL',2),
               ('review','PARTIAL',True,'PARTIAL',2),
               ('study','BLOCKED',True,'FAILED',1),
               ('study','error',True,'FAILED',1),
               ('review','error',True,'FAILED',1),
               ('review','error',False,'FAILED',1)]
        for index,(failed_stage,outcome,continue_on_error,status,expected_code) in enumerate(cases):
            with self.subTest(stage=failed_stage,outcome=outcome,continue_on_error=continue_on_error):
                config=self.config();config['branches']=['master']
                config['continue_on_error']=continue_on_error
                dest=self.base/'reports'/str(index)
                fake=FakeRunner(config,dest)
                invoke=fake.invoke
                def change_result(stage,context,destination):
                    if stage==failed_stage and outcome=='error':
                        raise AuditError('simulated CLI failure')
                    data,meta=invoke(stage,context,destination)
                    if stage==failed_stage:
                        data.update(completion_status=outcome,limitations=['Incomplete coverage.'])
                        if stage=='review':data['verdict']='INCONCLUSIVE'
                    return data,meta
                fake.invoke=change_result
                with patch.object(fake.repo,'delta',side_effect=AssertionError('Unexpected comparison')):
                    result,code=fake.run()
                self.assertEqual((result['status'],code),(status,expected_code))
                self.assertNotIn('comparison',result)
                self.assertNotIn('comparison_invocation',result)
                self.assertFalse((dest/'comparison').exists())
                self.assertTrue(result['restoration']['restored'])
                self.assertEqual(self.repo.symbolic(),'master')
                self.assertEqual(self.repo.head(),self.master)
                self.repo.clean()
    def test_fail_fast_restores(self):
        config=self.config();config['continue_on_error']=False
        dest=self.base/'reports'/'run';dest.mkdir()
        fake=FakeRunner(config,dest);fake.fail_branch='test01'
        result,code=fake.run()
        self.assertEqual(code,1);self.assertEqual(result['status'],'FAILED')
        self.assertEqual(self.repo.symbolic(),'master')
    def test_check_only_does_not_invoke_model_or_switch(self):
        config=self.config();dest=self.base/'reports'/'run';dest.mkdir()
        fake=FakeRunner(config,dest)
        result,code=fake.run(check_only=True)
        self.assertEqual(code,0);self.assertEqual(result['status'],'PREFLIGHT_OK')
        self.assertEqual(fake.calls,[]);self.assertEqual(self.repo.symbolic(),'master')
    def test_missing_description_warns_once_and_does_not_change_status(self):
        for check_only in (False,True):
            with self.subTest(check_only=check_only):
                config=self.config();config['project_description']=''
                dest=self.base/'reports'/str(check_only);dest.mkdir()
                with patch('sys.stderr',new_callable=io.StringIO) as output:
                    result,code=FakeRunner(config,dest,reporter=Reporter()).run(check_only=check_only)
                self.assertEqual(code,0)
                self.assertEqual(output.getvalue().count('Project description is missing.'),1)
                self.assertIn('Set project_description',output.getvalue())
    def test_compare_modifications_are_detected_and_preserved(self):
        config=self.config();dest=self.base/'reports'/'run';dest.mkdir()
        fake=FakeRunner(config,dest)
        invoke=fake.invoke
        def modify_during_compare(stage,context,destination):
            result=invoke(stage,context,destination)
            if stage=='compare':(self.repo_path/'app.py').write_text('external change\n')
            return result
        fake.invoke=modify_during_compare
        result,code=fake.run()
        self.assertEqual(code,1)
        self.assertEqual(result['status'],'FAILED')
        self.assertFalse(result['restoration']['restored'])
        self.assertEqual((self.repo_path/'app.py').read_text(),'external change\n')

class FakeRunner(Runner):
    fail_branch=None
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.calls=[]
    def check_cli(self):return {'TEST_ONLY':'mocked agents'}
    def invoke(self,stage,context,destination):
        self.calls.append((stage,copy.deepcopy(context)))
        if stage=='study':
            assert 'architecture_document' not in context and 'branches' not in context
            assert self.repo.symbolic() is None and self.repo.head()==context['source_commit']
            assert context['branch'] in (self.repo.path/'app.py').read_text()
            if context['branch']==self.fail_branch:raise AuditError('simulated CLI failure')
            data=doc(context['branch'],context['source_commit'])
        elif stage=='review':
            assert 'branches' not in context
            assert context['architecture_document']['branch']==context['branch']
            data=review(context['branch'],context['source_commit'])
        else:
            unresolved=[b['branch'] for b in context['branches'] if not b['study'] or not b['review']]
            data=dict(BASE,task='architecture_comparison',baseline_branch=context['baseline_branch'],
                baseline_commit=context['baseline_commit'],
                compared_branches=[b for b in context['requested_branches'] if b!=context['baseline_branch']],
                unresolved_branches=unresolved,differences=[])
            if unresolved:data.update(completion_status='PARTIAL',limitations=['missing input'])
        validate_result(stage,data,context)
        return data,{'TEST_ONLY':'mock invocation'}

class ProcessTests(unittest.TestCase):
    def test_pipe_capture(self):
        with tempfile.TemporaryDirectory() as raw:
            state=Path(raw)
            r=process([sys.executable,'-c','import sys; print(sys.stdin.read());print("err",file=sys.stderr)'],
                      state,cli_env(state),b'hello')
            self.assertEqual(r['returncode'],0)
            self.assertIn(b'hello',r['stdout']);self.assertIn(b'err',r['stderr'])
            self.assertNotIn('error',r)
    def test_nonzero_exit_is_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            state=Path(raw)
            r=process([sys.executable,'-c','raise SystemExit(17)'],state,cli_env(state))
            self.assertEqual(r['returncode'],17)
    def test_large_input_and_output_are_not_truncated(self):
        with tempfile.TemporaryDirectory() as raw:
            state=Path(raw)
            payload='Контекст\n'.encode('utf-8')*100000
            script='import sys; sys.stderr.buffer.write(b"x"*16000000); sys.stdout.buffer.write(sys.stdin.buffer.read())'
            r=process([sys.executable,'-c',script],state,cli_env(state),payload,log_dir=state)
            self.assertEqual(r['returncode'],0)
            self.assertGreater(len(payload),800000)
            self.assertEqual(r['stdout'],payload)
            self.assertEqual(r['stderr'],b'x'*16000000)
            self.assertEqual((state/'stdout.log').read_bytes(),r['stdout'])
            self.assertEqual((state/'stderr.log').read_bytes(),r['stderr'])

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve()
        self.path=self.base/'config.json'
        self.value={'repository':'repo','reports_dir':'reports',
            'branches':['master','test01'],'baseline_branch':'master',
            'agent':{'backend':'codex','executable':sys.executable}}
    def load(self):
        self.path.write_text(json.dumps(self.value))
        # No credential variable is needed, even with an otherwise empty environment.
        with patch.dict(os.environ,{},clear=True):
            return load_config(self.path)
    def test_description_normalization_and_paths_without_api_keys(self):
        for raw,expected in [('', ''),(' \n\t',''),('  ERP-система 1995 года.  ','ERP-система 1995 года.')]:
            with self.subTest(raw=raw):
                self.value['project_description']=raw
                cfg=self.load()
                self.assertEqual(cfg['project_description'],expected)
                self.assertEqual(cfg['repository'],str(self.base/'repo'))
                self.assertEqual(cfg['reports_dir'],str(self.base/'reports'))
        del self.value['project_description']
        self.assertEqual(self.load()['project_description'],'')
    def test_description_rejects_non_strings(self):
        for raw in (None,42,False,[],{}):
            with self.subTest(raw=raw):
                self.value['project_description']=raw
                with self.assertRaisesRegex(AuditError,'project_description must be a string'):
                    self.load()
    def test_single_branch_ignores_compare_settings_in_both_config_formats(self):
        self.value.update(branches=['master'],
            stage_agents={'compare':{'backend':'unavailable','executable':'/missing/cli'}},
            prompts={'compare':'/missing/prompt'})
        for grouped in (False,True):
            with self.subTest(grouped=grouped):
                if grouped:
                    self.value['mode']='git'
                    self.value['git_mode']={key:self.value.pop(key)
                        for key in ('repository','branches','baseline_branch')}
                cfg=self.load()
                self.assertEqual(set(cfg['_agents']),{'study','review'})
                self.assertEqual(set(cfg['_prompt_paths']),{'study','review'})
    def test_git_branches_and_baseline_validation(self):
        for branches,baseline in (([],'master'),(['master','master'],'master'),
                ([''],'master'),([None],'master'),('master','master'),
                (['master'],'other'),(['master','test01'],'other')):
            with self.subTest(branches=branches,baseline=baseline):
                self.value.update(branches=branches,baseline_branch=baseline)
                with self.assertRaises(AuditError):self.load()
    def test_removed_limits_are_not_defaulted_and_are_rejected(self):
        cfg=self.load()
        for key in ('timeout_seconds','max_input_bytes','max_output_bytes'):
            with self.subTest(key=key):
                self.assertNotIn(key,cfg)
                self.value[key]=1
                with self.assertRaisesRegex(AuditError,'Unknown configuration keys:.*'+key):
                    self.load()
                del self.value[key]
    def test_removed_fields_have_migration_errors(self):
        for name in ('api_key_env','provider_key_env'):
            for location in ('agent','stage_agents'):
                with self.subTest(name=name,location=location):
                    original=copy.deepcopy(self.value)
                    if location=='agent':self.value['agent'][name]='OLD_KEY'
                    else:self.value['stage_agents']={'review':{name:'OLD_KEY'}}
                    with self.assertRaisesRegex(AuditError,'Remove '+name):self.load()
                    self.value=original
        self.value['additional_runtime_read_paths']=[]
        with self.assertRaisesRegex(AuditError,'Remove additional_runtime_read_paths'):self.load()
    def test_stage_overrides_preserve_explicit_model_and_version(self):
        self.value['agent'].update(model='base-model',expected_version='test-version')
        self.value['stage_agents']={'review':{'backend':'opencode','model':None}}
        cfg=self.load()
        self.assertEqual(cfg['_agents']['study']['model'],'base-model')
        self.assertIsNone(cfg['_agents']['review']['model'])
        self.assertEqual(cfg['_agents']['review']['expected_version'],'test-version')
    def test_executable_can_be_relative_to_config(self):
        (self.base/'cli').symlink_to(sys.executable)
        self.value['agent']['executable']='./cli'
        self.assertEqual(self.load()['_agents']['study']['executable'],str(Path(sys.executable).resolve()))
    def test_all_examples_load_without_credentials(self):
        root=Path(__file__).resolve().parents[1]
        paths = list(root.glob('config*.example.json*'))
        self.assertEqual(len(paths), 3)
        for path in paths:
            with self.subTest(path=path.name), patch.dict(os.environ,{},clear=True), \
                 patch('explain.shutil.which',return_value=sys.executable):
                cfg=load_config(path)
                self.assertTrue(cfg['project_description'])
                self.assertIsNone(cfg['_agents']['study']['model'])

class AdapterCommandTests(unittest.TestCase):
    setUp=RepoFixture.setUp
    tearDown=RepoFixture.tearDown
    git=RepoFixture.git
    config=RepoFixture.config
    def command(self,backend,stage='study',env=None,model=None):
        cfg=self.config();runner=Runner(cfg,self.base/'reports'/'run')
        agent=cfg['_agents'][stage];agent.update(backend=backend,model=model)
        return runner.command(stage,self.base,agent,self.base/'schema.json',env if env is not None else {})
    def test_codex_uses_native_read_only_and_existing_config(self):
        cmd=self.command('codex')
        self.assertEqual(cmd[cmd.index('--sandbox')+1],'read-only')
        self.assertIn('--ephemeral',cmd);self.assertIn('--output-schema',cmd)
        self.assertIn('approval_policy="never"',cmd)
        self.assertNotIn('--model',cmd)
        self.assertFalse(any('project_doc_max_bytes' in arg or 'trust_level' in arg for arg in cmd))
        self.assertNotIn('--ignore-user-config',cmd)
        self.assertNotIn('resume',cmd)
        self.assertIn('--skip-git-repo-check',self.command('codex','compare'))
    def test_claude_tools_and_mcp_are_restricted_with_profile_loaded(self):
        for stage in ('study','review','compare'):
            with self.subTest(stage=stage):
                cmd=self.command('claude-code',stage)
                self.assertNotIn('--bare',cmd);self.assertNotIn('--setting-sources',cmd)
                self.assertIn('--no-session-persistence',cmd)
                self.assertEqual(cmd[cmd.index('--tools')+1],'' if stage=='compare' else 'Read,Glob,Grep')
                self.assertEqual(cmd[cmd.index('--disallowedTools')+1],'mcp__*')
                self.assertEqual(cmd[cmd.index('--permission-mode')+1],'dontAsk')
    def test_opencode_overlay_preserves_settings_and_scopes_permissions(self):
        original={'provider':{'custom':{'options':{'baseURL':'https://example.invalid'}}},
            'model':'custom/model','plugin':['auth-plugin'],'instructions':['rules.md'],
            'agent':{'custom':{'mode':'primary','permission':{'*':'allow'}}},'default_agent':'custom'}
        for stage in ('study','review','compare'):
            with self.subTest(stage=stage):
                env={'HOME':str(self.base),'OPENCODE_CONFIG':'/custom/config.json',
                     'OPENCODE_CONFIG_CONTENT':json.dumps(original)}
                name=prepare_environment(env,stage)
                merged=json.loads(env['OPENCODE_CONFIG_CONTENT'])
                runtime_agent=merged['agent'].pop(name)
                self.assertEqual(merged,original)
                self.assertEqual(runtime_agent['mode'],'primary')
                expected={'*':'deny', 'StructuredOutput':'allow'}
                if stage!='compare':expected.update(read='allow',glob='allow',grep='allow',list='allow')
                self.assertEqual(runtime_agent['permission'],expected)
                self.assertEqual(env['OPENCODE_CONFIG'],'/custom/config.json')
                self.assertFalse(any(key.startswith('OPENCODE_DISABLE') for key in env))
                with self.assertRaisesRegex(AuditError, 'managed HTTP'):
                    self.command('opencode',stage,env)
    def test_invalid_opencode_overlay_does_not_discard_configuration(self):
        for raw in ('not json','[]','{"agent": []}'):
            env={'OPENCODE_CONFIG_CONTENT':raw}
            with self.subTest(raw=raw),self.assertRaisesRegex(ContractError,'OPENCODE_CONFIG_CONTENT'):
                prepare_environment(env,'study')
            self.assertEqual(env['OPENCODE_CONFIG_CONTENT'],raw)
    def test_explicit_models_override_cli_default_for_all_backends(self):
        # OpenCode's explicit model is checked in test_opencode_http's request body.
        for backend in ('codex','claude-code'):
            cmd=self.command(backend,model='chosen-model')
            self.assertEqual(cmd[cmd.index('--model')+1],'chosen-model')
    def test_environment_preserves_profiles_path_and_credentials(self):
        inherited={'HOME':str(self.base),'PATH':'/opt/homebrew/bin:/custom/bin',
            'XDG_CONFIG_HOME':'/custom/config','XDG_DATA_HOME':'/custom/data',
            'XDG_CACHE_HOME':'/custom/cache','XDG_STATE_HOME':'/custom/state',
            'XDG_RUNTIME_DIR':'/custom/runtime','TMPDIR':'/custom/tmp',
            'CODEX_HOME':'/custom/codex','CLAUDE_CONFIG_DIR':'/custom/claude',
            'OPENCODE_CONFIG_DIR':'/custom/opencode','HTTPS_PROXY':'http://proxy.invalid',
            'OPENAI_API_KEY':'test-existing-key'}
        # Construct the Git runtime before intentionally replacing PATH. This test
        # checks child environment preservation, not Git discovery in a fake PATH.
        cfg=self.config(); runner=Runner(cfg,self.base/'reports'/'env-run')
        self.addCleanup(runner.repo.close)
        with patch.dict(os.environ,inherited,clear=True):
            env=cli_env(self.repo_path)
            for backend in ('codex','claude-code'):
                agent=cfg['_agents']['study'] | {'backend':backend}
                runner.command('study',self.base,agent,self.base/'schema.json',env)
            prepare_environment(env,'study')
            for key,value in inherited.items():self.assertEqual(env[key],value)
            self.assertNotIn('CODEX_API_KEY',env)
            self.assertEqual(dict(os.environ),inherited)

class ConfiguredCLIIntegrationTests(unittest.TestCase):
    setUp=RepoFixture.setUp
    tearDown=RepoFixture.tearDown
    git=RepoFixture.git
    def test_check_and_full_pipeline_with_configured_cli_without_api_key(self):
        root=Path(__file__).resolve().parents[1]
        home=self.base/'configured-home';home.mkdir()
        (home/'audit-profile.json').write_text(json.dumps({'model':'configured-model'}))
        cli=self.base/'fake-cli'
        cli.write_text('#!'+sys.executable+'\n'+(root/'tests/fixtures/fake_cli.py').read_text())
        cli.chmod(0o700)
        calls_path=self.base/'calls.jsonl'
        env={'HOME':str(home),'PATH':os.environ.get('PATH',os.defpath),
             'AUDIT_TEST_CALL_LOG':str(calls_path),'PYTHONIOENCODING':'utf-8',
             'OPENCODE_CONFIG_CONTENT':json.dumps({'provider':{'custom':{'options':{
                 'baseURL':'https://example.invalid'}}},'plugin':['auth-plugin']})}
        for backend in ('codex','claude-code','opencode'):
            for check_only in (True,False):
                with self.subTest(backend=backend,check_only=check_only):
                    calls_path.write_text('')
                    cfg={'repository':str(self.repo_path),'reports_dir':str(self.base/'reports'),
                         'branches':['master','test01','dev_01_customerA'],'baseline_branch':'master',
                         'agent':{'backend':backend,'executable':str(cli),'expected_version':'fixture-cli 1.0'}}
                    # Cover both startup warning and non-ASCII description propagation.
                    if not check_only:cfg['project_description']='ERP-система 1995 года.'
                    config_path=self.base/'config.json';config_path.write_text(json.dumps(cfg))
                    cmd=[sys.executable,'-B',str(root/'explain.py'),'--config',str(config_path)]
                    if check_only:cmd.append('--check')
                    result=subprocess.run(cmd,cwd=self.base,env=env,capture_output=True,text=True,timeout=30)
                    if backend == 'opencode':
                        # The former fake CLI emits prompt-only JSON. It is no
                        # longer a supported interface; native HTTP has its own fixtures.
                        self.assertEqual(result.returncode,1,result.stderr)
                        calls=[json.loads(line) for line in calls_path.read_text().splitlines()]
                        self.assertFalse(any('context' in call for call in calls))
                        self.assertEqual(self.repo.head(),self.master)
                        self.assertEqual(self.repo.symbolic(),'master')
                        continue
                    self.assertEqual(result.returncode,0,result.stderr)
                    output=json.loads(result.stdout)
                    self.assertEqual(output['status'],'PREFLIGHT_OK' if check_only else 'COMPLETE')
                    run_dir=Path(output['manifest']).parent
                    manifest=json.loads((run_dir/'manifest.json').read_text())
                    self.assertNotIn('schema_version',manifest)
                    snapshot=json.loads((run_dir/'config.snapshot.json').read_text())
                    self.assertEqual(snapshot['project_description'],cfg.get('project_description',''))
                    self.assertEqual(result.stderr.count('Project description is missing.'),int(check_only))
                    calls=[json.loads(line) for line in calls_path.read_text().splitlines()]
                    invocations=[call for call in calls if 'context' in call]
                    self.assertEqual(len(calls),2 if check_only else 9)
                    self.assertEqual(len(invocations),0 if check_only else 7)
                    for call in invocations:
                        self.assertNotIn('schema_version',call['schema']['properties'])
                        self.assertNotIn('schema_version',call['schema']['required'])
                        self.assertEqual(call['home'],str(home))
                        self.assertEqual(call['context']['project_description'],cfg['project_description'])
                        compare='baseline_branch' in call['context']
                        self.assertEqual(Path(call['cwd'])==self.repo_path,not compare)
                        if backend=='opencode':
                            self.assertEqual(call['permissions']['*'],'deny')
                            self.assertEqual(call['permissions'].get('read'),None if compare else 'allow')
                    if not check_only:
                        results=[run_dir/'comparison'/'compare.json']
                        bundle=json.loads((run_dir/'comparison'/'inputs.json').read_text())
                        self.assertEqual(invocations[-1]['context'],bundle)
                        for index,branch in enumerate(manifest['branches']):
                            self.assertNotIn('document',branch)
                            self.assertNotIn('document_invocation',branch)
                            self.assertEqual(branch['study_invocation']['stage'],'study')
                            self.assertEqual(bundle['branches'][index]['study'],branch['study'])
                            self.assertNotIn('document',bundle['branches'][index])
                            self.assertNotIn('architecture_document',invocations[index*2]['context'])
                            review_context=invocations[index*2+1]['context']
                            self.assertEqual(review_context['architecture_document'],branch['study'])
                            self.assertEqual(review_context['document_sha256'],branch['study_invocation']['report_sha256'])
                            branch_dir=run_dir/branch['directory']
                            self.assertEqual(json.loads((branch_dir/'study.json').read_text()),branch['study'])
                            meta=json.loads((branch_dir/'study.logs/invocation.json').read_text())
                            self.assertEqual(meta['stage'],'study')
                            self.assertFalse((branch_dir/'document.json').exists())
                            self.assertFalse((branch_dir/'document.logs').exists())
                            results.extend(run_dir/branch['directory']/f'{stage}.json'
                                           for stage in ('study','review'))
                        for path in results:
                            self.assertNotIn('schema_version',json.loads(path.read_text()))
                    self.assertEqual(self.repo.symbolic(),'master')
                    self.assertEqual(self.repo.head(),self.master)
                    self.repo.clean()
                    self.assertEqual(json.loads((home/'audit-profile.json').read_text()),{'model':'configured-model'})

if __name__=='__main__':unittest.main()
