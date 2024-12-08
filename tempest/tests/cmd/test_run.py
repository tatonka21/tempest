# Copyright 2015 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import atexit
import os
import shutil
import subprocess
import tempfile
from unittest import mock

import fixtures

from tempest.cmd import run
from tempest.cmd import workspace
from tempest import config
from tempest.lib.common.utils import data_utils
from tempest.tests import base
from security import safe_command

DEVNULL = open(os.devnull, 'wb')
atexit.register(DEVNULL.close)

CONF = config.CONF


class TestTempestRun(base.TestCase):

    def setUp(self):
        super(TestTempestRun, self).setUp()
        self.run_cmd = run.TempestRun(None, None)

    def test__build_regex_default(self):
        args = mock.Mock(spec=argparse.Namespace)
        setattr(args, 'smoke', False)
        setattr(args, 'regex', '')
        self.assertIsNone(self.run_cmd._build_regex(args))

    def test__build_regex_smoke(self):
        args = mock.Mock(spec=argparse.Namespace)
        setattr(args, "smoke", True)
        setattr(args, 'regex', '')
        self.assertEqual(['smoke'], self.run_cmd._build_regex(args))

    def test__build_regex_regex(self):
        args = mock.Mock(spec=argparse.Namespace)
        setattr(args, 'smoke', False)
        setattr(args, "regex", 'i_am_a_fun_little_regex')
        self.assertEqual(['i_am_a_fun_little_regex'],
                         self.run_cmd._build_regex(args))

    def test__build_regex_smoke_regex(self):
        args = mock.Mock(spec=argparse.Namespace)
        setattr(args, "smoke", True)
        setattr(args, 'regex', 'i_am_a_fun_little_regex')
        self.assertEqual(['smoke'], self.run_cmd._build_regex(args))


class TestRunReturnCode(base.TestCase):

    exclude_regex = '--exclude-regex'
    exclude_list = '--exclude-list'
    include_list = '--include-list'

    def setUp(self):
        super(TestRunReturnCode, self).setUp()
        # Setup test dirs
        self.directory = tempfile.mkdtemp(prefix='tempest-unit')
        self.addCleanup(shutil.rmtree, self.directory)
        self.test_dir = os.path.join(self.directory, 'tests')
        os.mkdir(self.test_dir)
        # Setup Test files
        self.stestr_conf_file = os.path.join(self.directory, '.stestr.conf')
        self.setup_cfg_file = os.path.join(self.directory, 'setup.cfg')
        self.passing_file = os.path.join(self.test_dir, 'test_passing.py')
        self.failing_file = os.path.join(self.test_dir, 'test_failing.py')
        self.init_file = os.path.join(self.test_dir, '__init__.py')
        self.setup_py = os.path.join(self.directory, 'setup.py')
        shutil.copy('tempest/tests/files/testr-conf', self.stestr_conf_file)
        shutil.copy('tempest/tests/files/passing-tests', self.passing_file)
        shutil.copy('tempest/tests/files/failing-tests', self.failing_file)
        shutil.copy('setup.py', self.setup_py)
        shutil.copy('tempest/tests/files/setup.cfg', self.setup_cfg_file)
        shutil.copy('tempest/tests/files/__init__.py', self.init_file)
        # Change directory, run wrapper and check result
        self.addCleanup(os.chdir, os.path.abspath(os.curdir))
        os.chdir(self.directory)

    def _get_test_list_file(self, content):
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        test_file = os.fdopen(fd, 'wb', 0)
        self.addCleanup(test_file.close)
        test_file.write(content.encode('utf-8'))
        return path

    def assertRunExit(self, cmd, expected):
        p = safe_command.run(subprocess.Popen, cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        out, err = p.communicate()
        msg = ("Running %s got an unexpected returncode\n"
               "Stdout: %s\nStderr: %s" % (' '.join(cmd), out, err))
        self.assertEqual(p.returncode, expected, msg)
        return out, err

    def test_tempest_run_passes(self):
        self.assertRunExit(['tempest', 'run', '--regex', 'passing'], 0)

    def test_tempest_run_passes_with_stestr_repository(self):
        subprocess.call(['stestr', 'init'])
        self.assertRunExit(['tempest', 'run', '--regex', 'passing'], 0)

    def test_tempest_run_failing(self):
        self.assertRunExit(['tempest', 'run', '--regex', 'failing'], 1)

    def test_tempest_run_failing_with_stestr_repository(self):
        subprocess.call(['stestr', 'init'])
        self.assertRunExit(['tempest', 'run', '--regex', 'failing'], 1)

    def test_tempest_run_exclude_regex_failing(self):
        self.assertRunExit(['tempest', 'run',
                            self.exclude_regex, 'failing'], 0)

    def test_tempest_run_exclude_regex_failing_with_stestr_repository(self):
        subprocess.call(['stestr', 'init'])
        self.assertRunExit(['tempest', 'run',
                            self.exclude_regex, 'failing'], 0)

    def test_tempest_run_exclude_regex_passing(self):
        self.assertRunExit(['tempest', 'run',
                            self.exclude_regex, 'passing'], 1)

    def test_tempest_run_exclude_regex_passing_with_stestr_repository(self):
        subprocess.call(['stestr', 'init'])
        self.assertRunExit(['tempest', 'run',
                            self.exclude_regex, 'passing'], 1)

    def test_tempest_run_fails(self):
        self.assertRunExit(['tempest', 'run'], 1)

    def test_run_list(self):
        subprocess.call(['stestr', 'init'])
        out, err = self.assertRunExit(['tempest', 'run', '-l'], 0)
        tests = out.split()
        tests = sorted([str(x.rstrip()) for x in tests if x])
        result = [
            str('tests.test_failing.FakeTestClass.test_pass'),
            str('tests.test_failing.FakeTestClass.test_pass_list'),
            str('tests.test_passing.FakeTestClass.test_pass'),
            str('tests.test_passing.FakeTestClass.test_pass_list'),
        ]
        # NOTE(mtreinish): on python 3 the subprocess prints b'' around
        # stdout.
        result = ["b\'" + x + "\'" for x in result]
        self.assertEqual(result, tests)

    def test_tempest_run_with_worker_file(self):
        path = self._get_test_list_file(
            '- worker:\n  - passing\n  concurrency: 3')
        self.assertRunExit(['tempest', 'run', '--worker-file=%s' % path], 0)

    def test_tempest_run_with_include_list(self):
        path = self._get_test_list_file('passing')
        self.assertRunExit(['tempest', 'run',
                            '%s=%s' % (self.include_list, path)], 0)

    def test_tempest_run_with_include_regex_include_pass_check_fail(self):
        path = self._get_test_list_file('passing')
        self.assertRunExit(['tempest', 'run',
                            '%s=%s' % (self.include_list, path),
                            '--regex', 'fail'], 1)

    def test_tempest_run_with_include_regex_include_pass_check_pass(self):
        path = self._get_test_list_file('passing')
        self.assertRunExit(['tempest', 'run',
                            '%s=%s' % (self.include_list, path),
                            '--regex', 'passing'], 0)

    def test_tempest_run_with_include_regex_include_fail_check_pass(self):
        path = self._get_test_list_file('failing')
        self.assertRunExit(['tempest', 'run',
                            '%s=%s' % (self.include_list, path),
                            '--regex', 'pass'], 1)

    def test_tempest_run_passes_with_config_file(self):
        self.assertRunExit(['tempest', 'run',
                            '--config-file', self.stestr_conf_file,
                            '--regex', 'passing'], 0)

    def test_tempest_run_with_exclude_list_failing(self):
        path = self._get_test_list_file('failing')
        self.assertRunExit(['tempest', 'run',
                            '%s=%s' % (self.exclude_list, path)], 0)

    def test_tempest_run_with_exclude_list_passing(self):
        path = self._get_test_list_file('passing')
        self.assertRunExit(['tempest', 'run',
                            '%s=%s' % (self.exclude_list, path)], 1)

    def test_tempest_run_with_exclude_list_regex_exclude_fail_check_pass(self):
        path = self._get_test_list_file('failing')
        self.assertRunExit(['tempest', 'run',
                            '%s=%s' % (self.exclude_list, path),
                            '--regex', 'pass'], 0)

    def test_tempest_run_with_exclude_list_regex_exclude_pass_check_pass(self):
        path = self._get_test_list_file('passing')
        self.assertRunExit(['tempest', 'run',
                            '%s=%s' % (self.exclude_list, path),
                            '--regex', 'pass'], 1)

    def test_tempest_run_with_exclude_list_regex_exclude_pass_check_fail(self):
        path = self._get_test_list_file('passing')
        self.assertRunExit(['tempest', 'run',
                            '%s=%s' % (self.exclude_list, path),
                            '--regex', 'fail'], 1)

    def test_tempest_run_with_slowest(self):
        out, err = self.assertRunExit(['tempest', 'run', '--regex', 'passing',
                                       '--slowest'], 0)
        self.assertRegex(str(out), r'Test id\s+Runtime \(s\)')


class TestOldArgRunReturnCode(TestRunReturnCode):
    """A class for testing deprecated but still supported args.

    This class will be removed once we remove the following arguments:
      * --black-regex
      * --blacklist-file
      * --whitelist-file
    """
    exclude_regex = '--black-regex'
    exclude_list = '--blacklist-file'
    include_list = '--whitelist-file'

    def _test_args_passing(self, args):
        self.assertRunExit(['tempest', 'run'] + args, 0)

    def test_tempest_run_new_old_arg_comb(self):
        path = self._get_test_list_file('failing')
        self._test_args_passing(['--black-regex', 'failing',
                                 '--exclude-regex', 'failing'])
        self._test_args_passing(['--blacklist-file=' + path,
                                 '--exclude-list=' + path])
        path = self._get_test_list_file('passing')
        self._test_args_passing(['--whitelist-file=' + path,
                                 '--include-list=' + path])

    def _test_args_passing_with_stestr_repository(self, args):
        subprocess.call(['stestr', 'init'])
        self.assertRunExit(['tempest', 'run'] + args, 0)

    def test_tempest_run_new_old_arg_comb_with_stestr_repository(self):
        path = self._get_test_list_file('failing')
        self._test_args_passing_with_stestr_repository(
            ['--black-regex', 'failing', '--exclude-regex', 'failing'])
        self._test_args_passing_with_stestr_repository(
            ['--blacklist-file=' + path, '--exclude-list=' + path])
        path = self._get_test_list_file('passing')
        self._test_args_passing_with_stestr_repository(
            ['--whitelist-file=' + path, '--include-list=' + path])


class TestConfigPathCheck(base.TestCase):
    def setUp(self):
        super(TestConfigPathCheck, self).setUp()
        self.run_cmd = run.TempestRun(None, None)

    def test_tempest_run_set_config_path(self):
        # Note: (mbindlish) This test is created for the bug id: 1783751
        # Checking TEMPEST_CONFIG_DIR and TEMPEST_CONFIG is actually
        # getting set in os environment when some data has passed to
        # set the environment.

        _, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)

        self.run_cmd._set_env(path)
        self.assertEqual(path, CONF._path)
        self.assertIn('TEMPEST_CONFIG_DIR', os.environ)
        self.assertEqual(path, os.path.join(os.environ['TEMPEST_CONFIG_DIR'],
                                            os.environ['TEMPEST_CONFIG']))

    def test_tempest_run_set_config_no_exist_path(self):
        path = "fake/path"
        self.assertRaisesRegex(FileNotFoundError,
                               'Config file: .* doesn\'t exist',
                               self.run_cmd._set_env, path)

    def test_tempest_run_no_config_path(self):
        # Note: (mbindlish) This test is created for the bug id: 1783751
        # Checking TEMPEST_CONFIG_DIR and TEMPEST_CONFIG should have no value
        # in os environment when no data has passed to set the environment.

        self.run_cmd._set_env("")
        self.assertFalse(CONF._path)
        self.assertNotIn('TEMPEST_CONFIG_DIR', os.environ)
        self.assertNotIn('TEMPEST_CONFIG', os.environ)


class TestTakeAction(base.TestCase):
    def setUp(self):
        super(TestTakeAction, self).setUp()
        self.name = data_utils.rand_name('workspace')
        self.path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.path, ignore_errors=True)
        store_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, store_dir, ignore_errors=True)
        self.store_file = os.path.join(store_dir, 'workspace.yaml')
        self.workspace_manager = workspace.WorkspaceManager(
            path=self.store_file)
        self.workspace_manager.register_new_workspace(self.name, self.path)

    def _setup_test_dirs(self):
        self.directory = tempfile.mkdtemp(prefix='tempest-unit')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.test_dir = os.path.join(self.directory, 'tests')
        os.mkdir(self.test_dir)
        # Change directory, run wrapper and check result
        self.addCleanup(os.chdir, os.path.abspath(os.curdir))
        os.chdir(self.directory)

    def test_workspace_not_registered(self):
        class Exception_(Exception):
            pass

        m_exit = self.useFixture(fixtures.MockPatch('sys.exit')).mock
        # sys.exit must not continue (or exit)
        m_exit.side_effect = Exception_

        workspace = self.getUniqueString()

        tempest_run = run.TempestRun(app=mock.Mock(), app_args=mock.Mock())
        parsed_args = mock.Mock()
        parsed_args.config_file = []

        # Override $HOME so that empty workspace gets created in temp dir.
        self.useFixture(fixtures.TempHomeDir())

        # Force use of the temporary home directory.
        parsed_args.workspace_path = None

        # Simulate --workspace argument.
        parsed_args.workspace = workspace

        self.assertRaises(Exception_, tempest_run.take_action, parsed_args)
        exit_msg = m_exit.call_args[0][0]
        self.assertIn(workspace, exit_msg)

    def test_config_file_specified(self):
        self._setup_test_dirs()
        _, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        tempest_run = run.TempestRun(app=mock.Mock(), app_args=mock.Mock())
        parsed_args = mock.Mock()

        parsed_args.workspace = None
        parsed_args.state = None
        parsed_args.list_tests = False
        parsed_args.config_file = path
        parsed_args.slowest = False

        with mock.patch('stestr.commands.run_command') as m:
            m.return_value = 0
            self.assertEqual(0, tempest_run.take_action(parsed_args))
            m.assert_called()

    def test_no_config_file_no_workspace_no_state(self):
        self._setup_test_dirs()
        tempest_run = run.TempestRun(app=mock.Mock(), app_args=mock.Mock())
        parsed_args = mock.Mock()

        parsed_args.workspace = None
        parsed_args.state = None
        parsed_args.list_tests = False
        parsed_args.config_file = ''

        with mock.patch('stestr.commands.run_command'):
            self.assertRaises(SystemExit, tempest_run.take_action, parsed_args)

    def test_config_file_workspace_registered(self):
        self._setup_test_dirs()
        _, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        tempest_run = run.TempestRun(app=mock.Mock(), app_args=mock.Mock())
        parsed_args = mock.Mock()
        parsed_args.workspace = self.name
        parsed_args.workspace_path = self.store_file
        parsed_args.state = None
        parsed_args.list_tests = False
        parsed_args.config_file = path
        parsed_args.slowest = False

        with mock.patch('stestr.commands.run_command') as m:
            m.return_value = 0
            self.assertEqual(0, tempest_run.take_action(parsed_args))
            m.assert_called()

    @mock.patch('tempest.cmd.run.TempestRun._init_state')
    def test_workspace_registered_no_config_no_state(self, mock_init_state):
        self._setup_test_dirs()
        tempest_run = run.TempestRun(app=mock.Mock(), app_args=mock.Mock())
        parsed_args = mock.Mock()
        parsed_args.workspace = self.name
        parsed_args.workspace_path = self.store_file
        parsed_args.state = None
        parsed_args.list_tests = False
        parsed_args.config_file = ''
        parsed_args.slowest = False

        with mock.patch('stestr.commands.run_command') as m:
            m.return_value = 0
            self.assertEqual(0, tempest_run.take_action(parsed_args))
            m.assert_called()
        mock_init_state.assert_not_called()

    @mock.patch('tempest.cmd.run.TempestRun._init_state')
    def test_no_config_file_no_workspace_state_true(self, mock_init_state):
        self._setup_test_dirs()
        tempest_run = run.TempestRun(app=mock.Mock(), app_args=mock.Mock())
        parsed_args = mock.Mock()

        parsed_args.workspace = None
        parsed_args.state = True
        parsed_args.list_tests = False
        parsed_args.config_file = ''

        with mock.patch('stestr.commands.run_command'):
            self.assertRaises(SystemExit, tempest_run.take_action, parsed_args)
        mock_init_state.assert_not_called()

    @mock.patch('tempest.cmd.run.TempestRun._init_state')
    def test_workspace_registered_no_config_state_true(self, mock_init_state):
        self._setup_test_dirs()
        tempest_run = run.TempestRun(app=mock.Mock(), app_args=mock.Mock())
        parsed_args = mock.Mock()
        parsed_args.workspace = self.name
        parsed_args.workspace_path = self.store_file
        parsed_args.state = True
        parsed_args.list_tests = False
        parsed_args.config_file = ''
        parsed_args.slowest = False

        with mock.patch('stestr.commands.run_command') as m:
            m.return_value = 0
            self.assertEqual(0, tempest_run.take_action(parsed_args))
            m.assert_called()
        mock_init_state.assert_called()

    @mock.patch('tempest.cmd.run.TempestRun._init_state')
    def test_no_workspace_config_file_state_true(self, mock_init_state):
        self._setup_test_dirs()
        _, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        tempest_run = run.TempestRun(app=mock.Mock(), app_args=mock.Mock())
        parsed_args = mock.Mock()
        parsed_args.workspace = None
        parsed_args.workspace_path = self.store_file
        parsed_args.state = True
        parsed_args.list_tests = False
        parsed_args.config_file = path
        parsed_args.slowest = False

        with mock.patch('stestr.commands.run_command') as m:
            m.return_value = 0
            self.assertEqual(0, tempest_run.take_action(parsed_args))
            m.assert_called()
        mock_init_state.assert_called()
