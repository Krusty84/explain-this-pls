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
from src.reports.document_rendering import materialize_study, recover_sections
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.runtime.errors import AuditError, UnsafeRepository
from src.analysis.repository import Repository
from src.runtime.runner import Runner
from src.runtime.process import cli_env, process
from src.runtime.config import load_config
from src.runtime.execution import repository_lock
from src.runtime.artifacts import slug
from src.runtime.reporting import Reporter
from src.backends import codex
from src.contracts.contracts import ContractError, review_verdict, strict_json, validate_result
from fixtures.ledger_response import response
from fixtures.architecture_checks import assert_architecture
from src.analysis.ledger import review_context, prepare_result

BASE = {'completion_status': 'COMPLETE',
        'report_markdown': '# Report\nC-001\n', 'limitations': []}

def doc(branch, commit):
    return response({'branch': branch, 'source_commit': commit})

def review(branch, commit, context=None):
    context = context or review_context(materialize_study(doc(branch, commit)), {'branch': branch, 'source_commit': commit})
    return response(context)

class ContractTests(unittest.TestCase):
    def test_wire_contract_rejects_unexpected_fields(self):
        git_context = {'branch': 'master', 'source_commit': 'abc'}
        folder_context = {'source_directory': '/source', 'source_fingerprint': 'abc'}
        comparison = dict(BASE, task='architecture_comparison', baseline_branch='master',
            baseline_commit='abc', compared_branches=[], unresolved_branches=['master'], differences=[],
            completion_status='PARTIAL', limitations=['Unpublished wire fixtures'])
        comparison_context = {'baseline_branch': 'master', 'baseline_commit': 'abc',
                              'requested_branches': ['master'], 'branches': [
                                  {'branch': 'master', 'study': doc('master', 'abc'), 'review': review('master', 'abc')}]}
        cases = [('compare', comparison, comparison_context, 'git')]
        for mode, context in (('git', git_context), ('folder', folder_context)):
            for stage, data in (('study', doc('master', 'abc')), ('review', review('master', 'abc'))):
                if mode == 'folder':
                    del data['branch']; del data['source_commit']
                    data.update(context)
                if stage == 'review':
                    document = response(context)
                    review_ctx = review_context(materialize_study(document), context)
                    data = response(review_ctx)
                    cases.append((stage, data, review_ctx, mode))
                else:
                    cases.append((stage, data, context, mode))
        for stage, data, context, mode in cases:
            with self.subTest(stage=stage, mode=mode):
                validate_result(stage, data, context, mode)
                for key in ('unexpected_field', 'contract_id', 'artifact_format', 'context_format'):
                    self.assertNotIn(key, data)
                    with self.assertRaises(ContractError) as caught:
                        validate_result(stage, data | {key: 'unexpected'}, context, mode)
                    self.assertEqual(caught.exception.failure_kind, 'SCHEMA_ERROR')
                    self.assertEqual(caught.exception.details['path'], '$')
                    self.assertEqual(caught.exception.details['extra_key_count'], 1)

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ContractError):
            strict_json('{"x":1,"x":2}')
    def test_nonfinite_rejected(self):
        with self.assertRaises(ContractError):
            strict_json('{"x":NaN}')
    def test_codex_native_json(self):
        self.assertEqual(codex.parse_output(json.dumps(doc('master', 'abc')))[0]['branch'], 'master')
    def test_fenced_output_rejected(self):
        with self.assertRaises(ContractError):
            codex.parse_output('```json\n{}\n```')
    def test_wrong_commit_rejected(self):
        with self.assertRaises(ContractError):
            validate_result('study',doc('master','wrong'),{'branch':'master','source_commit':'abc'})
    def test_partial_review_cannot_pass(self):
        context = review_context(materialize_study(doc('master', 'abc')), {'branch':'master','source_commit':'abc'})
        data = response(context)
        data.update(completion_status='PARTIAL',limitations=['coverage incomplete'])
        validate_result('review',data,context)
        self.assertEqual(review_verdict(data),'INCONCLUSIVE')
    def test_complete_review_cannot_have_unchecked_claim(self):
        context = review_context(materialize_study(doc('master', 'abc')), {'branch':'master','source_commit':'abc'})
        data=response(context)
        data['claims'][0].update(outcome='NOT_CHECKED',limitation='not inspected')
        validate_result('review',data,context)
        self.assertEqual(review_verdict(data), 'INCONCLUSIVE')
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
        return {'result_policy':'strict', 'mode':'git', 'reports_dir':str(reports),
            'execution': {'review_enabled': True},
            'git_mode':{'repository':str(self.repo_path),
                'branches':['master','test01','dev_01_customerA'],'baseline_branch':'master'},
            'output_language':'Russian','project_description':'ERP-система 1995 года.',
            'priority_scenarios':[],'continue_on_error':True,
            '_agents':{s:dict(agent) for s in ('study','review','compare')},
            '_prompt_paths':{s:str(Path(__file__).resolve().parents[1]/'prompts'/f'{s}.md') for s in ('study','review','revise','compare')}}
    def test_grouped_git_configuration_runs_and_restores(self):
        cfg=self.config()
        cfg['folder_mode']={'path':'/missing/inactive/folder'}
        result,code=FakeRunner(cfg,self.base/'grouped-run').run()
        self.assertEqual(code,0)
        self.assertEqual(result['status'],'COMPLETE')
        self.assertEqual(result['contract_id'],'evidence-ledger')
        self.assertEqual(result['artifact_format'],'architecture-artifacts')
        self.assertEqual(result['comparison']['compared_branches'],['test01','dev_01_customerA'])
        self.assertEqual(self.repo.symbolic(),'master')
        self.assertEqual(self.repo.head(),self.master)
        self.repo.clean()
    def test_pins_then_detached_checkout_and_restore(self):
        before = (self.repo_path / '.git/HEAD').read_bytes()
        pins = self.repo.preflight(['master', 'test01'])
        self.assertEqual(before, (self.repo_path / '.git/HEAD').read_bytes())
        try:
            self.repo.checkout(pins['test01'])
            self.assertIn('test01', (self.repo_path / 'app.py').read_text())
            self.assertIsNone(self.repo.symbolic())
        finally:
            self.assertTrue(self.repo.restore()['restored'])
        self.assertEqual(self.repo.symbolic(), 'master')
    def test_dirty_worktree_rejected(self):
        (self.repo_path/'app.py').write_text('changed')
        with self.assertRaises(UnsafeRepository):self.repo.clean()
    def test_untracked_rejected(self):
        (self.repo_path/'old-report.md').write_text('untracked')
        with self.assertRaises(UnsafeRepository):self.repo.clean()
    def test_ignored_files_accepted(self):
        (self.repo_path/'.gitignore').write_text('cache.tmp\n')
        self.git('add','.gitignore');self.git('commit','-m','ignore')
        (self.repo_path/'cache.tmp').write_text('stale data')
        self.repo.clean()
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
        self.repo.preflight(['master'])
        pin=self.repo.text('rev-parse','test01')
        self.git('update-ref','refs/heads/master',pin)
        with self.assertRaises(UnsafeRepository):self.repo.assert_expected()
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
        self.assertEqual(result['isolation'],'in-place-checkout; cli-native-permissions')
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
                config=self.config();config['git_mode']['branches']=['master']
                config['continue_on_error']=continue_on_error
                dest=self.base/'reports'/str(index)
                fake=FakeRunner(config,dest)
                invoke=fake.invoke
                def change_result(stage,context,destination):
                    if stage==failed_stage and outcome=='error':
                        raise AuditError('simulated CLI failure', code='CLI_FAILED', failure_layer='backend')
                    data,meta=invoke(stage,context,destination)
                    if stage==failed_stage:
                        data.update(completion_status=outcome,limitations=['Incomplete coverage.'])
                        if stage=='review':data['verdict']='INCONCLUSIVE'
                    return data,meta
                fake.invoke=change_result
                with patch.object(fake.session.repo,'delta',side_effect=AssertionError('Unexpected comparison')):
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
            assert (self.session.source_path / '.git').exists()
            assert self.session.repo.head() == context['source_commit']
            assert self.session.repo.symbolic_ref() is None
            assert context['branch'] in (self.session.source_path/'app.py').read_text()
            if context['branch']==self.fail_branch:raise AuditError('simulated CLI failure', code='CLI_FAILED', failure_layer='backend')
            data=response(dict(context, stage=stage))
        elif stage=='review':
            assert 'branches' not in context
            assert context['architecture_document']['branch']==context['branch']
            data=review(context['branch'],context['source_commit'],context)
        else:
            unresolved=[b['branch'] for b in context['branches'] if not b['study'] or not b['review']]
            data=dict(BASE,task='architecture_comparison',baseline_branch=context['baseline_branch'],
                baseline_commit=context['baseline_commit'],
                compared_branches=[b for b in context['requested_branches'] if b!=context['baseline_branch']],
                unresolved_branches=unresolved,differences=[])
            if unresolved:data.update(completion_status='PARTIAL',limitations=['missing input'])
        validate_result(stage,data,context)
        data = prepare_result(stage, data, context)
        self.publisher.publish_result(stage, data, destination)
        from src.contracts.contracts import CONTRACT_ID, ARTIFACT_FORMAT
        return data,{'TEST_ONLY':'mock invocation', 'publication_complete': True,
                     'contract_id': CONTRACT_ID, 'artifact_format': ARTIFACT_FORMAT}

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
        self.value={'mode':'git','reports_dir':'reports', 'execution': {'review_enabled': True},
            'git_mode':{'repository':'repo','branches':['master','test01'],'baseline_branch':'master'},
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
                self.assertEqual(cfg['git_mode']['repository'],str(self.base/'repo'))
                self.assertEqual(cfg['reports_dir'],str(self.base/'reports'))
        del self.value['project_description']
        self.assertEqual(self.load()['project_description'],'')
    def test_description_rejects_non_strings(self):
        for raw in (None,42,False,[],{}):
            with self.subTest(raw=raw):
                self.value['project_description']=raw
                with self.assertRaisesRegex(AuditError,'project_description must be a string'):
                    self.load()
    def test_single_branch_ignores_compare_settings(self):
        self.value['git_mode']['branches']=['master']
        self.value.update(
            stage_agents={'compare':{'backend':'codex','executable':'/missing/cli'}},
            prompts={'compare':'/missing/prompt'})
        cfg=self.load()
        self.assertEqual(set(cfg['_agents']),{'study','review'})
        self.assertEqual(set(cfg['_prompt_paths']),{'study','review','revise'})
    def test_git_branches_and_baseline_validation(self):
        for branches,baseline in (([],'master'),(['master','master'],'master'),
                ([''],'master'),([None],'master'),('master','master'),
                (['master'],'other'),(['master','test01'],'other')):
            with self.subTest(branches=branches,baseline=baseline):
                self.value['git_mode'].update(branches=branches,baseline_branch=baseline)
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
    def test_removed_fields_are_unknown_keys(self):
        for name in ('api_key_env','provider_key_env'):
            for location in ('agent','stage_agents'):
                with self.subTest(name=name,location=location):
                    original=copy.deepcopy(self.value)
                    if location=='agent':self.value['agent'][name]='OLD_KEY'
                    else:self.value['stage_agents']={'review':{name:'OLD_KEY'}}
                    with self.assertRaisesRegex(AuditError,'Unknown agent configuration field'):self.load()
                    self.value=original
        self.value['additional_runtime_read_paths']=[]
        with self.assertRaisesRegex(AuditError,'Unknown configuration keys:.*additional_runtime_read_paths'):self.load()
    def test_stage_overrides_preserve_explicit_model_and_version(self):
        self.value['agent'].update(model='base-model',expected_version='test-version')
        self.value['stage_agents']={'review':{'backend':'codex','model':None}}
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
        self.assertEqual(len(paths), 1)
        for path in paths:
            with self.subTest(path=path.name), patch.dict(os.environ,{},clear=True), \
                 patch('src.runtime.config.shutil.which',return_value=sys.executable):
                cfg=load_config(path)
                self.assertTrue(cfg['project_description'])
                self.assertEqual(cfg['_agents']['study']['model'], cfg['agent'].get('model'))

class AdapterCommandTests(unittest.TestCase):
    setUp=RepoFixture.setUp
    tearDown=RepoFixture.tearDown
    git=RepoFixture.git
    config=RepoFixture.config
    def command(self,backend,stage='study',env=None,model=None):
        cfg=self.config();runner=Runner(cfg,self.base/'reports'/'run')
        agent=cfg['_agents'][stage];agent.update(backend=backend,model=model)
        return runner.invoker.command(stage,self.base,agent,self.base/'schema.json',env if env is not None else {})
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
    def test_explicit_model_overrides_codex_cli_default(self):
        for backend in ('codex',):
            cmd=self.command(backend,model='chosen-model')
            self.assertEqual(cmd[cmd.index('--model')+1],'chosen-model')
    def test_environment_preserves_profiles_path_and_credentials(self):
        inherited={'HOME':str(self.base),'PATH':'/opt/homebrew/bin:/custom/bin',
            'XDG_CONFIG_HOME':'/custom/config','XDG_DATA_HOME':'/custom/data',
            'XDG_CACHE_HOME':'/custom/cache','XDG_STATE_HOME':'/custom/state',
            'XDG_RUNTIME_DIR':'/custom/runtime','TMPDIR':'/custom/tmp',
            'CODEX_HOME':'/custom/codex',
            'HTTPS_PROXY':'http://proxy.invalid',
            'OPENAI_API_KEY':'test-existing-key'}
        # Construct the Git runtime before intentionally replacing PATH. This test
        # checks child environment preservation, not Git discovery in a fake PATH.
        cfg=self.config(); runner=Runner(cfg,self.base/'reports'/'env-run')
        self.addCleanup(runner.session.repo.close)
        with patch.dict(os.environ,inherited,clear=True):
            env=cli_env(self.repo_path)
            for backend in ('codex',):
                agent=cfg['_agents']['study'] | {'backend':backend}
                runner.invoker.command('study',self.base,agent,self.base/'schema.json',env)
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
        cli.write_text('#!'+sys.executable+'\n'+('import sys; sys.path.insert(0, ' + repr(str(root / 'tests/fixtures')) + ')' + '\n' + (root/'tests/fixtures/fake_cli.py').read_text()))
        cli.chmod(0o700)
        calls_path=self.base/'calls.jsonl'
        env={'HOME':str(home),'PATH':os.environ.get('PATH',os.defpath),
             'AUDIT_TEST_CALL_LOG':str(calls_path),'PYTHONIOENCODING':'utf-8'}
        for backend in ('codex',):
            version = 'fixture-cli 1.0'
            env['AUDIT_TEST_CLI_VERSION'] = version
            for check_only in (True,False):
                with self.subTest(backend=backend,check_only=check_only):
                    calls_path.write_text('')
                    cfg={'mode':'git','reports_dir':str(self.base/'reports'), 'execution': {'review_enabled': True},
                         'git_mode':{'repository':str(self.repo_path),
                             'branches':['master','test01','dev_01_customerA'],'baseline_branch':'master'},
                         'agent':{'backend':backend,'executable':str(cli),'expected_version':version}}
                    # Cover both startup warning and non-ASCII description propagation.
                    if not check_only:cfg['project_description']='ERP-система 1995 года.'
                    config_path=self.base/'config.json';config_path.write_text(json.dumps(cfg))
                    cmd=[sys.executable,'-B',str(root/'explain.py'),'--config',str(config_path)]
                    if check_only:cmd.append('--check')
                    result=subprocess.run(cmd,cwd=self.base,env=env,capture_output=True,text=True,timeout=30)
                    self.assertEqual(result.returncode,0,result.stderr)
                    output=json.loads(result.stdout)
                    self.assertEqual(output['status'],'PREFLIGHT_OK' if check_only else 'COMPLETE')
                    run_dir=Path(output['manifest']).parent
                    manifest=json.loads((run_dir/'manifest.json').read_text())
                    self.assertEqual(manifest['contract_id'],'evidence-ledger')
                    self.assertEqual(manifest['artifact_format'],'architecture-artifacts')
                    snapshot=json.loads((run_dir/'config.snapshot.json').read_text())
                    self.assertEqual(snapshot['project_description'],cfg.get('project_description',''))
                    self.assertEqual(result.stderr.count('Project description is missing.'),int(check_only))
                    calls=[json.loads(line) for line in calls_path.read_text().splitlines()]
                    invocations=[call for call in calls if 'context' in call]
                    self.assertEqual(len(calls),2 if check_only else 9)
                    self.assertEqual(len(invocations),0 if check_only else 7)
                    for call in invocations:
                        for field in ('contract_id', 'artifact_format', 'context_format'):
                            self.assertNotIn(field,call['schema']['properties'])
                            self.assertNotIn(field,call['schema']['required'])
                        self.assertEqual(call['home'],str(home))
                        self.assertEqual(call['context']['project_description'],cfg['project_description'])
                        compare='baseline_branch' in call['context']
                        self.assertEqual(Path(call['cwd']) == self.repo_path, not compare)
                        if not compare: self.assertEqual(call['cwd'], call['context']['repository'])
                    if not check_only:
                        results=[run_dir/'comparison'/'compare.json']
                        bundle=json.loads((run_dir/'comparison'/'inputs.json').read_text())
                        self.assertEqual(invocations[-1]['context']['requested_branches'],bundle['requested_branches'])
                        for index,branch in enumerate(manifest['branches']):
                            self.assertNotIn('document',branch)
                            self.assertNotIn('document_invocation',branch)
                            self.assertEqual(branch['study_invocation']['stage'],'study')
                            self.assertEqual(bundle['branches'][index]['study'],branch['study'])
                            self.assertNotIn('document',bundle['branches'][index])
                            self.assertNotIn('architecture_document',invocations[index*2]['context'])
                            review_context=invocations[index*2+1]['context']
                            self.assertEqual(review_context['architecture_document']['report_markdown'],branch['study']['report_markdown'])
                            branch_dir=run_dir/branch['directory']
                            self.assertNotIn('document_sha256', review_context)
                            bindings=json.loads((branch_dir/'revisions/001/review.logs/attempt-001/binding.json').read_text())
                            target=next(mapping['identity'] for mapping in bindings['mappings']
                                        if mapping['id']==review_context['review_target_id'])
                            self.assertEqual(target['document_sha256'],branch['study']['review_plan']['document_sha256'])
                            assert_architecture(self, branch_dir, branch['study'], branch['study_invocation'],
                                                cfg.get('output_language', 'Russian'))
                            self.assertEqual(json.loads((branch_dir/'study.json').read_text()),branch['study'])
                            meta=json.loads((branch_dir/'revisions/001/study.logs/invocation.json').read_text())
                            self.assertEqual(meta['stage'],'study')
                            self.assertFalse((branch_dir/'document.json').exists())
                            self.assertFalse((branch_dir/'document.logs').exists())
                            results.extend(run_dir/branch['directory']/f'{stage}.json'
                                           for stage in ('study','review'))
                        for path in results:
                            data=json.loads(path.read_text())
                            for field in ('contract_id', 'artifact_format', 'context_format'):
                                self.assertNotIn(field,data)
                    self.assertEqual(self.repo.symbolic(),'master')
                    self.assertEqual(self.repo.head(),self.master)
                    self.repo.clean()
                    self.assertEqual(json.loads((home/'audit-profile.json').read_text()),{'model':'configured-model'})

if __name__=='__main__':unittest.main()
