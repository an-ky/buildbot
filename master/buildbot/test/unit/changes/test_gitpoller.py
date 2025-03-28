# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

import os
import re
from unittest import mock

from twisted.internet import defer
from twisted.trial import unittest

from buildbot.changes import gitpoller
from buildbot.test.fake.private_tempdir import MockPrivateTemporaryDirectory
from buildbot.test.reactor import TestReactorMixin
from buildbot.test.runprocess import ExpectMasterShell
from buildbot.test.runprocess import MasterRunProcessMixin
from buildbot.test.util import changesource
from buildbot.test.util import config
from buildbot.test.util import logging
from buildbot.util import bytes2unicode
from buildbot.util import unicode2bytes

# Test that environment variables get propagated to subprocesses (See #2116)
os.environ['TEST_THAT_ENVIRONMENT_GETS_PASSED_TO_SUBPROCESSES'] = 'TRUE'


class TestGitPollerBase(
    MasterRunProcessMixin,
    changesource.ChangeSourceMixin,
    logging.LoggingMixin,
    TestReactorMixin,
    unittest.TestCase,
):
    REPOURL = 'git@example.com:~foo/baz.git'
    REPOURL_QUOTED = 'git%40example.com%3A%7Efoo%2Fbaz.git'

    POLLER_WORKDIR = os.path.join('basedir', 'gitpoller-work')

    def createPoller(self):
        # this is overridden in TestGitPollerWithSshPrivateKey
        return gitpoller.GitPoller(self.REPOURL)

    @defer.inlineCallbacks
    def setUp(self):
        self.setup_test_reactor()
        self.setup_master_run_process()
        yield self.setUpChangeSource()
        yield self.master.startService()

        self.poller = yield self.attachChangeSource(self.createPoller())

    @defer.inlineCallbacks
    def tearDown(self):
        yield self.master.stopService()
        yield self.tearDownChangeSource()


class TestGitPoller(TestGitPollerBase):
    dummyRevStr = '12345abcde'

    @defer.inlineCallbacks
    def _perform_git_output_test(
        self, methodToTest, args, desiredGoodOutput, desiredGoodResult, emptyRaisesException=True
    ):
        self.expect_commands(
            ExpectMasterShell(['git'] + args).workdir(self.POLLER_WORKDIR),
        )

        # we should get an Exception with empty output from git
        try:
            yield methodToTest(self.dummyRevStr)
            if emptyRaisesException:
                self.fail("run_process should have failed on empty output")
        except Exception as e:
            if not emptyRaisesException:
                import traceback

                traceback.print_exc()
                self.fail("run_process should NOT have failed on empty output: " + repr(e))

        self.assert_all_commands_ran()

        # and the method shouldn't suppress any exceptions
        self.expect_commands(
            ExpectMasterShell(['git'] + args).workdir(self.POLLER_WORKDIR).exit(1),
        )

        try:
            yield methodToTest(self.dummyRevStr)
            self.fail("run_process should have failed on stderr output")
        except Exception:
            pass

        self.assert_all_commands_ran()

        # finally we should get what's expected from good output
        self.expect_commands(
            ExpectMasterShell(['git'] + args).workdir(self.POLLER_WORKDIR).stdout(desiredGoodOutput)
        )

        r = yield methodToTest(self.dummyRevStr)

        self.assertEqual(r, desiredGoodResult)
        # check types
        if isinstance(r, str):
            self.assertIsInstance(r, str)
        elif isinstance(r, list):
            for e in r:
                self.assertIsInstance(e, str)

        self.assert_all_commands_ran()

    def test_get_commit_author(self):
        authorStr = 'Sammy Jankis <email@example.com>'
        authorBytes = unicode2bytes(authorStr)
        return self._perform_git_output_test(
            self.poller._get_commit_author,
            ['log', '--no-walk', '--format=%aN <%aE>', self.dummyRevStr, '--'],
            authorBytes,
            authorStr,
        )

    def test_get_commit_committer(self):
        committerStr = 'Sammy Jankis <email@example.com>'
        committerBytes = unicode2bytes(committerStr)
        return self._perform_git_output_test(
            self.poller._get_commit_committer,
            ['log', '--no-walk', '--format=%cN <%cE>', self.dummyRevStr, '--'],
            committerBytes,
            committerStr,
        )

    def _test_get_commit_comments(self, commentStr):
        commentBytes = unicode2bytes(commentStr)
        return self._perform_git_output_test(
            self.poller._get_commit_comments,
            ['log', '--no-walk', '--format=%s%n%b', self.dummyRevStr, '--'],
            commentBytes,
            commentStr,
            emptyRaisesException=False,
        )

    def test_get_commit_comments(self):
        comments = ['this is a commit message\n\nthat is multiline', 'single line message', '']
        return defer.DeferredList([
            self._test_get_commit_comments(commentStr) for commentStr in comments
        ])

    def test_get_commit_files(self):
        filesBytes = b'\n\nfile1\nfile2\n"\146ile_octal"\nfile space'
        filesRes = ['file1', 'file2', 'file_octal', 'file space']
        return self._perform_git_output_test(
            self.poller._get_commit_files,
            ['log', '--name-only', '--no-walk', '--format=%n', self.dummyRevStr, '--'],
            filesBytes,
            filesRes,
            emptyRaisesException=False,
        )

    def test_get_commit_files_with_space_in_changed_files(self):
        filesBytes = b'normal_directory/file1\ndirectory with space/file2'
        filesStr = bytes2unicode(filesBytes)
        return self._perform_git_output_test(
            self.poller._get_commit_files,
            ['log', '--name-only', '--no-walk', '--format=%n', self.dummyRevStr, '--'],
            filesBytes,
            [l for l in filesStr.splitlines() if l.strip()],
            emptyRaisesException=False,
        )

    def test_get_commit_timestamp(self):
        stampBytes = b'1273258009'
        stampStr = bytes2unicode(stampBytes)
        return self._perform_git_output_test(
            self.poller._get_commit_timestamp,
            ['log', '--no-walk', '--format=%ct', self.dummyRevStr, '--'],
            stampBytes,
            float(stampStr),
        )

    def test_describe(self):
        self.assertSubstring("GitPoller", self.poller.describe())

    def test_name(self):
        self.assertEqual(bytes2unicode(self.REPOURL), bytes2unicode(self.poller.name))

        # and one with explicit name...
        other = gitpoller.GitPoller(self.REPOURL, name="MyName")
        self.assertEqual("MyName", other.name)

    @defer.inlineCallbacks
    def test_checkGitFeatures_git_not_installed(self):
        self.setUpLogging()
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'Command not found'),
        )

        yield self.assertFailure(self.poller._checkGitFeatures(), EnvironmentError)
        self.assert_all_commands_ran()

    @defer.inlineCallbacks
    def test_checkGitFeatures_git_bad_version(self):
        self.setUpLogging()
        self.expect_commands(ExpectMasterShell(['git', '--version']).stdout(b'git '))

        with self.assertRaises(EnvironmentError):
            yield self.poller._checkGitFeatures()

        self.assert_all_commands_ran()

    @defer.inlineCallbacks
    def test_poll_initial(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5\n'),
        )

        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'}
        )
        self.master.db.state.assertStateByClass(
            name=bytes2unicode(self.REPOURL),
            class_name='GitPoller',
            lastRev={'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'},
        )

    @defer.inlineCallbacks
    def test_poll_initial_poller_not_running(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
        )

        self.poller.doPoll.running = False
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(self.poller.lastRev, {})

    def test_poll_failInit(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]).exit(1),
        )

        self.poller.doPoll.running = True
        d = self.assertFailure(self.poller.poll(), EnvironmentError)

        d.addCallback(lambda _: self.assert_all_commands_ran())
        return d

    def test_poll_failFetch(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .exit(1),
        )

        self.poller.doPoll.running = True
        d = self.assertFailure(self.poller.poll(), EnvironmentError)
        d.addCallback(lambda _: self.assert_all_commands_ran())
        return d

    @defer.inlineCallbacks
    def test_poll_failRevParse(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .exit(1),
        )

        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(len(self.flushLoggedErrors()), 1)
        self.assertEqual(self.poller.lastRev, {})

    @defer.inlineCallbacks
    def test_poll_failLog(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^fa3ae8ed68e664d4db24798611b352e3c6509930',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .exit(1),
        )

        # do the poll
        self.poller.lastRev = {'master': 'fa3ae8ed68e664d4db24798611b352e3c6509930'}

        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(len(self.flushLoggedErrors()), 1)
        self.assertEqual(
            self.poller.lastRev, {'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}
        )

    @defer.inlineCallbacks
    def test_poll_GitError(self):
        # Raised when git exits with status code 128. See issue 2468
        self.expect_commands(
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]).exit(128),
        )

        with self.assertRaises(gitpoller.GitError):
            yield self.poller._dovccmd('init', ['--bare', self.POLLER_WORKDIR])

        self.assert_all_commands_ran()

    @defer.inlineCallbacks
    def test_poll_GitError_log(self):
        self.setUpLogging()
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]).exit(128),
        )

        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertLogged("command.*on repourl.*failed.*exit code 128.*")

    @defer.inlineCallbacks
    def test_poll_nothingNew(self):
        # Test that environment variables get propagated to subprocesses
        # (See #2116)
        self.patch(os, 'environ', {'ENVVAR': 'TRUE'})
        self.add_run_process_expect_env({'ENVVAR': 'TRUE'})

        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'no interesting output'),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b''),
        )

        self.poller.lastRev = {'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}

        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.master.db.state.assertStateByClass(
            name=bytes2unicode(self.REPOURL),
            class_name='GitPoller',
            lastRev={'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241'},
        )

    @defer.inlineCallbacks
    def test_poll_multipleBranches_initial(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2\t'
                b'refs/heads/release\n'
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\t'
                b'refs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
                '+release:refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2'),
        )

        # do the poll
        self.poller.branches = ['master', 'release', 'not_on_remote']
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev,
            {
                'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                'release': '9118f4ab71963d23d02d4bdc54876ac8bf05acf2',
            },
        )

    @defer.inlineCallbacks
    def test_poll_multipleBranches(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2\t'
                b'refs/heads/release\n'
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\t'
                b'refs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
                '+release:refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
                '^fa3ae8ed68e664d4db24798611b352e3c6509930',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(
                b'\n'.join([
                    b'64a5dc2a4bd4f558b5dd193d47c83c7d7abc9a1a',
                    b'4423cdbcbb89c14e50dd5f4152415afd686c5241',
                ])
            ),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '9118f4ab71963d23d02d4bdc54876ac8bf05acf2',
                '^4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'\n'.join([b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2'])),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        # do the poll
        self.poller.branches = ['master', 'release']
        self.poller.lastRev = {
            'master': 'fa3ae8ed68e664d4db24798611b352e3c6509930',
            'release': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
        }
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev,
            {
                'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                'release': '9118f4ab71963d23d02d4bdc54876ac8bf05acf2',
            },
        )

        self.assertEqual(
            self.master.data.updates.changesAdded,
            [
                {
                    'author': 'by:4423cdbc',
                    'committer': 'by:4423cdbc',
                    'branch': 'master',
                    'category': None,
                    'codebase': None,
                    'comments': 'hello!',
                    'files': ['/etc/442'],
                    'project': '',
                    'properties': {},
                    'repository': 'git@example.com:~foo/baz.git',
                    'revision': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                    'revlink': '',
                    'src': 'git',
                    'when_timestamp': 1273258009,
                },
                {
                    'author': 'by:64a5dc2a',
                    'committer': 'by:64a5dc2a',
                    'branch': 'master',
                    'category': None,
                    'codebase': None,
                    'comments': 'hello!',
                    'files': ['/etc/64a'],
                    'project': '',
                    'properties': {},
                    'repository': 'git@example.com:~foo/baz.git',
                    'revision': '64a5dc2a4bd4f558b5dd193d47c83c7d7abc9a1a',
                    'revlink': '',
                    'src': 'git',
                    'when_timestamp': 1273258009,
                },
                {
                    'author': 'by:9118f4ab',
                    'committer': 'by:9118f4ab',
                    'branch': 'release',
                    'category': None,
                    'codebase': None,
                    'comments': 'hello!',
                    'files': ['/etc/911'],
                    'project': '',
                    'properties': {},
                    'repository': 'git@example.com:~foo/baz.git',
                    'revision': '9118f4ab71963d23d02d4bdc54876ac8bf05acf2',
                    'revlink': '',
                    'src': 'git',
                    'when_timestamp': 1273258009,
                },
            ],
        )

    @defer.inlineCallbacks
    def test_poll_multipleBranches_buildPushesWithNoCommits_default(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/release\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+release:refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b''),
        )

        # do the poll
        self.poller.branches = ['release']
        self.poller.lastRev = {
            'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
        }
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'release': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}
        )
        self.assertEqual(len(self.master.data.updates.changesAdded), 0)

    @defer.inlineCallbacks
    def test_poll_multipleBranches_buildPushesWithNoCommits_true(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/release\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+release:refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b''),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        # do the poll
        self.poller.branches = ['release']
        self.poller.lastRev = {
            'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
        }

        self.poller.buildPushesWithNoCommits = True
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'release': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}
        )
        self.assertEqual(
            self.master.data.updates.changesAdded,
            [
                {
                    'author': 'by:4423cdbc',
                    'committer': 'by:4423cdbc',
                    'branch': 'release',
                    'category': None,
                    'codebase': None,
                    'comments': 'hello!',
                    'files': ['/etc/442'],
                    'project': '',
                    'properties': {},
                    'repository': 'git@example.com:~foo/baz.git',
                    'revision': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                    'revlink': '',
                    'src': 'git',
                    'when_timestamp': 1273258009,
                }
            ],
        )

    @defer.inlineCallbacks
    def test_poll_multipleBranches_buildPushesWithNoCommits_true_fast_forward(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/release\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+release:refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^0ba9d553b7217ab4bbad89ad56dc0332c7d57a8c',
                '^4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b''),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        # do the poll
        self.poller.branches = ['release']
        self.poller.lastRev = {
            'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
            'release': '0ba9d553b7217ab4bbad89ad56dc0332c7d57a8c',
        }

        self.poller.buildPushesWithNoCommits = True
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'release': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}
        )
        self.assertEqual(
            self.master.data.updates.changesAdded,
            [
                {
                    'author': 'by:4423cdbc',
                    'committer': 'by:4423cdbc',
                    'branch': 'release',
                    'category': None,
                    'codebase': None,
                    'comments': 'hello!',
                    'files': ['/etc/442'],
                    'project': '',
                    'properties': {},
                    'repository': 'git@example.com:~foo/baz.git',
                    'revision': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                    'revlink': '',
                    'src': 'git',
                    'when_timestamp': 1273258009,
                }
            ],
        )

    @defer.inlineCallbacks
    def test_poll_multipleBranches_buildPushesWithNoCommits_true_not_tip(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/release\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+release:refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^0ba9d553b7217ab4bbad89ad56dc0332c7d57a8c',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b''),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        # do the poll
        self.poller.branches = ['release']
        self.poller.lastRev = {
            'master': '0ba9d553b7217ab4bbad89ad56dc0332c7d57a8c',
        }

        self.poller.buildPushesWithNoCommits = True
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'release': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}
        )
        self.assertEqual(
            self.master.data.updates.changesAdded,
            [
                {
                    'author': 'by:4423cdbc',
                    'committer': 'by:4423cdbc',
                    'branch': 'release',
                    'category': None,
                    'codebase': None,
                    'comments': 'hello!',
                    'files': ['/etc/442'],
                    'project': '',
                    'properties': {},
                    'repository': 'git@example.com:~foo/baz.git',
                    'revision': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                    'revlink': '',
                    'src': 'git',
                    'when_timestamp': 1273258009,
                }
            ],
        )

    @defer.inlineCallbacks
    def test_poll_allBranches_single(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^fa3ae8ed68e664d4db24798611b352e3c6509930',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(
                b'\n'.join([
                    b'64a5dc2a4bd4f558b5dd193d47c83c7d7abc9a1a',
                    b'4423cdbcbb89c14e50dd5f4152415afd686c5241',
                ])
            ),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        # do the poll
        self.poller.branches = True
        self.poller.lastRev = {
            'refs/heads/master': 'fa3ae8ed68e664d4db24798611b352e3c6509930',
        }
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev,
            {
                'refs/heads/master': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
            },
        )

        added = self.master.data.updates.changesAdded
        self.assertEqual(len(added), 2)

        self.assertEqual(added[0]['author'], 'by:4423cdbc')
        self.assertEqual(added[0]['committer'], 'by:4423cdbc')
        self.assertEqual(added[0]['when_timestamp'], 1273258009)
        self.assertEqual(added[0]['comments'], 'hello!')
        self.assertEqual(added[0]['branch'], 'master')
        self.assertEqual(added[0]['files'], ['/etc/442'])
        self.assertEqual(added[0]['src'], 'git')

        self.assertEqual(added[1]['author'], 'by:64a5dc2a')
        self.assertEqual(added[1]['committer'], 'by:64a5dc2a')
        self.assertEqual(added[1]['when_timestamp'], 1273258009)
        self.assertEqual(added[1]['comments'], 'hello!')
        self.assertEqual(added[1]['files'], ['/etc/64a'])
        self.assertEqual(added[1]['src'], 'git')

    @defer.inlineCallbacks
    def test_poll_noChanges(self):
        # Test that environment variables get propagated to subprocesses
        # (See #2116)
        self.patch(os, 'environ', {'ENVVAR': 'TRUE'})
        self.add_run_process_expect_env({'ENVVAR': 'TRUE'})

        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'no interesting output'),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b''),
        )

        self.poller.lastRev = {'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}
        )

    @defer.inlineCallbacks
    def test_poll_allBranches_multiple(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'\n'.join([
                    b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master',
                    b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2\trefs/heads/release',
                ])
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
                '+release:refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
                '^fa3ae8ed68e664d4db24798611b352e3c6509930',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(
                b'\n'.join([
                    b'64a5dc2a4bd4f558b5dd193d47c83c7d7abc9a1a',
                    b'4423cdbcbb89c14e50dd5f4152415afd686c5241',
                ])
            ),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/release',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '9118f4ab71963d23d02d4bdc54876ac8bf05acf2',
                '^4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'\n'.join([b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2'])),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        # do the poll
        self.poller.branches = True
        self.poller.lastRev = {
            'refs/heads/master': 'fa3ae8ed68e664d4db24798611b352e3c6509930',
            'refs/heads/release': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
        }
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev,
            {
                'refs/heads/master': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                'refs/heads/release': '9118f4ab71963d23d02d4bdc54876ac8bf05acf2',
            },
        )

        added = self.master.data.updates.changesAdded
        self.assertEqual(len(added), 3)

        self.assertEqual(added[0]['author'], 'by:4423cdbc')
        self.assertEqual(added[0]['committer'], 'by:4423cdbc')
        self.assertEqual(added[0]['when_timestamp'], 1273258009)
        self.assertEqual(added[0]['comments'], 'hello!')
        self.assertEqual(added[0]['branch'], 'master')
        self.assertEqual(added[0]['files'], ['/etc/442'])
        self.assertEqual(added[0]['src'], 'git')

        self.assertEqual(added[1]['author'], 'by:64a5dc2a')
        self.assertEqual(added[1]['committer'], 'by:64a5dc2a')
        self.assertEqual(added[1]['when_timestamp'], 1273258009)
        self.assertEqual(added[1]['comments'], 'hello!')
        self.assertEqual(added[1]['files'], ['/etc/64a'])
        self.assertEqual(added[1]['src'], 'git')

        self.assertEqual(added[2]['author'], 'by:9118f4ab')
        self.assertEqual(added[2]['committer'], 'by:9118f4ab')
        self.assertEqual(added[2]['when_timestamp'], 1273258009)
        self.assertEqual(added[2]['comments'], 'hello!')
        self.assertEqual(added[2]['files'], ['/etc/911'])
        self.assertEqual(added[2]['src'], 'git')

    @defer.inlineCallbacks
    def test_poll_callableFilteredBranches(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'\n'.join([
                    b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master',
                    b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2\trefs/heads/release',
                ])
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
                '^fa3ae8ed68e664d4db24798611b352e3c6509930',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(
                b'\n'.join([
                    b'64a5dc2a4bd4f558b5dd193d47c83c7d7abc9a1a',
                    b'4423cdbcbb89c14e50dd5f4152415afd686c5241',
                ])
            ),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        # do the poll
        class TestCallable:
            def __call__(self, branch):
                return branch == "refs/heads/master"

        self.poller.branches = TestCallable()
        self.poller.lastRev = {
            'refs/heads/master': 'fa3ae8ed68e664d4db24798611b352e3c6509930',
            'refs/heads/release': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
        }
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()

        # The release branch id should remain unchanged,
        # because it was ignored.
        self.assertEqual(
            self.poller.lastRev, {'refs/heads/master': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}
        )

        added = self.master.data.updates.changesAdded
        self.assertEqual(len(added), 2)

        self.assertEqual(added[0]['author'], 'by:4423cdbc')
        self.assertEqual(added[0]['committer'], 'by:4423cdbc')
        self.assertEqual(added[0]['when_timestamp'], 1273258009)
        self.assertEqual(added[0]['comments'], 'hello!')
        self.assertEqual(added[0]['branch'], 'master')
        self.assertEqual(added[0]['files'], ['/etc/442'])
        self.assertEqual(added[0]['src'], 'git')

        self.assertEqual(added[1]['author'], 'by:64a5dc2a')
        self.assertEqual(added[1]['committer'], 'by:64a5dc2a')
        self.assertEqual(added[1]['when_timestamp'], 1273258009)
        self.assertEqual(added[1]['comments'], 'hello!')
        self.assertEqual(added[1]['files'], ['/etc/64a'])
        self.assertEqual(added[1]['src'], 'git')

    @defer.inlineCallbacks
    def test_poll_branchFilter(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'\n'.join([
                    b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/pull/410/merge',
                    b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2\trefs/pull/410/head',
                ])
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+refs/pull/410/head:refs/buildbot/' + self.REPOURL_QUOTED + '/refs/pull/410/head',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/refs/pull/410/head',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '9118f4ab71963d23d02d4bdc54876ac8bf05acf2',
                '^bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
                '^fa3ae8ed68e664d4db24798611b352e3c6509930',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'\n'.join([b'9118f4ab71963d23d02d4bdc54876ac8bf05acf2'])),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        def pullFilter(branch):
            """
            Note that this isn't useful in practice, because it will only
            pick up *changes* to pull requests, not the original request.
            """
            return re.match('^refs/pull/[0-9]*/head$', branch)

        # do the poll
        self.poller.branches = pullFilter
        self.poller.lastRev = {
            'master': 'fa3ae8ed68e664d4db24798611b352e3c6509930',
            'refs/pull/410/head': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5',
        }
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'refs/pull/410/head': '9118f4ab71963d23d02d4bdc54876ac8bf05acf2'}
        )

        added = self.master.data.updates.changesAdded
        self.assertEqual(len(added), 1)

        self.assertEqual(added[0]['author'], 'by:9118f4ab')
        self.assertEqual(added[0]['committer'], 'by:9118f4ab')
        self.assertEqual(added[0]['when_timestamp'], 1273258009)
        self.assertEqual(added[0]['comments'], 'hello!')
        self.assertEqual(added[0]['files'], ['/etc/911'])
        self.assertEqual(added[0]['src'], 'git')

    @defer.inlineCallbacks
    def test_poll_old(self):
        # Test that environment variables get propagated to subprocesses
        # (See #2116)
        self.patch(os, 'environ', {'ENVVAR': 'TRUE'})
        self.add_run_process_expect_env({'ENVVAR': 'TRUE'})

        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'no interesting output'),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^fa3ae8ed68e664d4db24798611b352e3c6509930',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(
                b'\n'.join([
                    b'64a5dc2a4bd4f558b5dd193d47c83c7d7abc9a1a',
                    b'4423cdbcbb89c14e50dd5f4152415afd686c5241',
                ])
            ),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        # do the poll
        self.poller.lastRev = {'master': 'fa3ae8ed68e664d4db24798611b352e3c6509930'}
        self.poller.doPoll.running = True
        yield self.poller.poll()

        # check the results
        self.assertEqual(
            self.poller.lastRev, {'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241'}
        )
        self.assertEqual(
            self.master.data.updates.changesAdded,
            [
                {
                    'author': 'by:4423cdbc',
                    'committer': 'by:4423cdbc',
                    'branch': 'master',
                    'category': None,
                    'codebase': None,
                    'comments': 'hello!',
                    'files': ['/etc/442'],
                    'project': '',
                    'properties': {},
                    'repository': 'git@example.com:~foo/baz.git',
                    'revision': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                    'revlink': '',
                    'src': 'git',
                    'when_timestamp': 1273258009,
                },
                {
                    'author': 'by:64a5dc2a',
                    'committer': 'by:64a5dc2a',
                    'branch': 'master',
                    'category': None,
                    'codebase': None,
                    'comments': 'hello!',
                    'files': ['/etc/64a'],
                    'project': '',
                    'properties': {},
                    'repository': 'git@example.com:~foo/baz.git',
                    'revision': '64a5dc2a4bd4f558b5dd193d47c83c7d7abc9a1a',
                    'revlink': '',
                    'src': 'git',
                    'when_timestamp': 1273258009,
                },
            ],
        )
        self.assert_all_commands_ran()

        self.master.db.state.assertStateByClass(
            name=bytes2unicode(self.REPOURL),
            class_name='GitPoller',
            lastRev={'master': '4423cdbcbb89c14e50dd5f4152415afd686c5241'},
        )

    @defer.inlineCallbacks
    def test_poll_callableCategory(self):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'4423cdbcbb89c14e50dd5f4152415afd686c5241\n'),
            ExpectMasterShell([
                'git',
                'log',
                '--ignore-missing',
                '--format=%H',
                '4423cdbcbb89c14e50dd5f4152415afd686c5241',
                '^fa3ae8ed68e664d4db24798611b352e3c6509930',
                '--',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(
                b'\n'.join([
                    b'64a5dc2a4bd4f558b5dd193d47c83c7d7abc9a1a',
                    b'4423cdbcbb89c14e50dd5f4152415afd686c5241',
                ])
            ),
        )

        # and patch out the _get_commit_foo methods which were already tested
        # above
        def timestamp(rev):
            return defer.succeed(1273258009)

        self.patch(self.poller, '_get_commit_timestamp', timestamp)

        def author(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_author', author)

        def committer(rev):
            return defer.succeed('by:' + rev[:8])

        self.patch(self.poller, '_get_commit_committer', committer)

        def files(rev):
            return defer.succeed(['/etc/' + rev[:3]])

        self.patch(self.poller, '_get_commit_files', files)

        def comments(rev):
            return defer.succeed('hello!')

        self.patch(self.poller, '_get_commit_comments', comments)

        # do the poll
        self.poller.branches = True

        def callableCategory(chdict):
            return chdict['revision'][:6]

        self.poller.category = callableCategory

        self.poller.lastRev = {
            'refs/heads/master': 'fa3ae8ed68e664d4db24798611b352e3c6509930',
        }
        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev,
            {
                'refs/heads/master': '4423cdbcbb89c14e50dd5f4152415afd686c5241',
            },
        )

        added = self.master.data.updates.changesAdded
        self.assertEqual(len(added), 2)

        self.assertEqual(added[0]['author'], 'by:4423cdbc')
        self.assertEqual(added[0]['committer'], 'by:4423cdbc')
        self.assertEqual(added[0]['when_timestamp'], 1273258009)
        self.assertEqual(added[0]['comments'], 'hello!')
        self.assertEqual(added[0]['branch'], 'master')
        self.assertEqual(added[0]['files'], ['/etc/442'])
        self.assertEqual(added[0]['src'], 'git')
        self.assertEqual(added[0]['category'], '4423cd')

        self.assertEqual(added[1]['author'], 'by:64a5dc2a')
        self.assertEqual(added[1]['committer'], 'by:64a5dc2a')
        self.assertEqual(added[1]['when_timestamp'], 1273258009)
        self.assertEqual(added[1]['comments'], 'hello!')
        self.assertEqual(added[1]['files'], ['/etc/64a'])
        self.assertEqual(added[1]['src'], 'git')
        self.assertEqual(added[1]['category'], '64a5dc')

    def test_startService(self):
        self.assertEqual(self.poller.workdir, self.POLLER_WORKDIR)
        self.assertEqual(self.poller.lastRev, {})

    @defer.inlineCallbacks
    def test_startService_loadLastRev(self):
        yield self.poller.stopService()

        self.master.db.state.set_fake_state(
            self.poller, 'lastRev', {"master": "fa3ae8ed68e664d4db24798611b352e3c6509930"}
        )

        yield self.poller.startService()

        self.assertEqual(
            self.poller.lastRev, {"master": "fa3ae8ed68e664d4db24798611b352e3c6509930"}
        )


class TestGitPollerWithSshPrivateKey(TestGitPollerBase):
    def createPoller(self):
        return gitpoller.GitPoller(self.REPOURL, sshPrivateKey='ssh-key')

    @mock.patch(
        'buildbot.util.private_tempdir.PrivateTemporaryDirectory',
        new_callable=MockPrivateTemporaryDirectory,
    )
    @mock.patch('buildbot.changes.gitpoller.writeLocalFile')
    @defer.inlineCallbacks
    def test_check_git_features_ssh_1_7(self, write_local_file_mock, temp_dir_mock):
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 1.7.5\n'),
        )

        yield self.assertFailure(self.poller._checkGitFeatures(), EnvironmentError)

        self.assert_all_commands_ran()

        self.assertEqual(len(temp_dir_mock.dirs), 0)
        write_local_file_mock.assert_not_called()

    @mock.patch(
        'buildbot.util.private_tempdir.PrivateTemporaryDirectory',
        new_callable=MockPrivateTemporaryDirectory,
    )
    @mock.patch('buildbot.changes.gitpoller.writeLocalFile')
    @defer.inlineCallbacks
    def test_poll_initial_2_10(self, write_local_file_mock, temp_dir_mock):
        key_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@', 'ssh-key')

        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 2.10.0\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell([
                'git',
                '-c',
                f'core.sshCommand=ssh -o "BatchMode=yes" -i "{key_path}"',
                'ls-remote',
                '--refs',
                self.REPOURL,
            ]),
            ExpectMasterShell([
                'git',
                '-c',
                f'core.sshCommand=ssh -o "BatchMode=yes" -i "{key_path}"',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5\n'),
        )

        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'}
        )
        self.master.db.state.assertStateByClass(
            name=bytes2unicode(self.REPOURL),
            class_name='GitPoller',
            lastRev={'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'},
        )

        temp_dir_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@')
        self.assertEqual(temp_dir_mock.dirs, [(temp_dir_path, 0o700), (temp_dir_path, 0o700)])
        write_local_file_mock.assert_called_with(key_path, 'ssh-key\n', mode=0o400)

    @mock.patch(
        'buildbot.util.private_tempdir.PrivateTemporaryDirectory',
        new_callable=MockPrivateTemporaryDirectory,
    )
    @mock.patch('buildbot.changes.gitpoller.writeLocalFile')
    @defer.inlineCallbacks
    def test_poll_initial_2_3(self, write_local_file_mock, temp_dir_mock):
        key_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@', 'ssh-key')

        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 2.3.0\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell(['git', 'ls-remote', '--refs', self.REPOURL]).stdout(
                b'4423cdbcbb89c14e50dd5f4152415afd686c5241\trefs/heads/master\n'
            ),
            ExpectMasterShell([
                'git',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .env({'GIT_SSH_COMMAND': f'ssh -o "BatchMode=yes" -i "{key_path}"'}),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5\n'),
        )

        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'}
        )
        self.master.db.state.assertStateByClass(
            name=bytes2unicode(self.REPOURL),
            class_name='GitPoller',
            lastRev={'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'},
        )

        temp_dir_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@')
        self.assertEqual(temp_dir_mock.dirs, [(temp_dir_path, 0o700), (temp_dir_path, 0o700)])
        write_local_file_mock.assert_called_with(key_path, 'ssh-key\n', mode=0o400)

    @mock.patch(
        'buildbot.util.private_tempdir.PrivateTemporaryDirectory',
        new_callable=MockPrivateTemporaryDirectory,
    )
    @mock.patch('buildbot.changes.gitpoller.writeLocalFile')
    @defer.inlineCallbacks
    def test_poll_failFetch_git_2_10(self, write_local_file_mock, temp_dir_mock):
        key_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@', 'ssh-key')

        # make sure we cleanup the private key when fetch fails
        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 2.10.0\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell([
                'git',
                '-c',
                f'core.sshCommand=ssh -o "BatchMode=yes" -i "{key_path}"',
                'ls-remote',
                '--refs',
                self.REPOURL,
            ]),
            ExpectMasterShell([
                'git',
                '-c',
                f'core.sshCommand=ssh -o "BatchMode=yes" -i "{key_path}"',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .exit(1),
        )

        self.poller.doPoll.running = True
        yield self.assertFailure(self.poller.poll(), EnvironmentError)

        self.assert_all_commands_ran()

        temp_dir_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@')
        self.assertEqual(temp_dir_mock.dirs, [(temp_dir_path, 0o700), (temp_dir_path, 0o700)])
        write_local_file_mock.assert_called_with(key_path, 'ssh-key\n', mode=0o400)


class TestGitPollerWithSshHostKey(TestGitPollerBase):
    def createPoller(self):
        return gitpoller.GitPoller(self.REPOURL, sshPrivateKey='ssh-key', sshHostKey='ssh-host-key')

    @mock.patch(
        'buildbot.util.private_tempdir.PrivateTemporaryDirectory',
        new_callable=MockPrivateTemporaryDirectory,
    )
    @mock.patch('buildbot.changes.gitpoller.writeLocalFile')
    @defer.inlineCallbacks
    def test_poll_initial_2_10(self, write_local_file_mock, temp_dir_mock):
        key_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@', 'ssh-key')
        known_hosts_path = os.path.join(
            'basedir', 'gitpoller-work', '.buildbot-ssh@@@', 'ssh-known-hosts'
        )

        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 2.10.0\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell([
                'git',
                '-c',
                f'core.sshCommand=ssh -o "BatchMode=yes" -i "{key_path}" '
                f'-o "UserKnownHostsFile={known_hosts_path}"',
                'ls-remote',
                '--refs',
                self.REPOURL,
            ]),
            ExpectMasterShell([
                'git',
                '-c',
                f'core.sshCommand=ssh -o "BatchMode=yes" -i "{key_path}" '
                f'-o "UserKnownHostsFile={known_hosts_path}"',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5\n'),
        )

        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'}
        )
        self.master.db.state.assertStateByClass(
            name=bytes2unicode(self.REPOURL),
            class_name='GitPoller',
            lastRev={'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'},
        )

        temp_dir_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@')
        self.assertEqual(temp_dir_mock.dirs, [(temp_dir_path, 0o700), (temp_dir_path, 0o700)])

        expected_file_writes = [
            mock.call(key_path, 'ssh-key\n', mode=0o400),
            mock.call(known_hosts_path, '* ssh-host-key'),
            mock.call(key_path, 'ssh-key\n', mode=0o400),
            mock.call(known_hosts_path, '* ssh-host-key'),
        ]

        self.assertEqual(expected_file_writes, write_local_file_mock.call_args_list)


class TestGitPollerWithSshKnownHosts(TestGitPollerBase):
    def createPoller(self):
        return gitpoller.GitPoller(
            self.REPOURL, sshPrivateKey='ssh-key\n', sshKnownHosts='ssh-known-hosts'
        )

    @mock.patch(
        'buildbot.util.private_tempdir.PrivateTemporaryDirectory',
        new_callable=MockPrivateTemporaryDirectory,
    )
    @mock.patch('buildbot.changes.gitpoller.writeLocalFile')
    @defer.inlineCallbacks
    def test_poll_initial_2_10(self, write_local_file_mock, temp_dir_mock):
        key_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@', 'ssh-key')
        known_hosts_path = os.path.join(
            'basedir', 'gitpoller-work', '.buildbot-ssh@@@', 'ssh-known-hosts'
        )

        self.expect_commands(
            ExpectMasterShell(['git', '--version']).stdout(b'git version 2.10.0\n'),
            ExpectMasterShell(['git', 'init', '--bare', self.POLLER_WORKDIR]),
            ExpectMasterShell([
                'git',
                '-c',
                f'core.sshCommand=ssh -o "BatchMode=yes" -i "{key_path}" '
                f'-o "UserKnownHostsFile={known_hosts_path}"',
                'ls-remote',
                '--refs',
                self.REPOURL,
            ]),
            ExpectMasterShell([
                'git',
                '-c',
                f'core.sshCommand=ssh -o "BatchMode=yes" -i "{key_path}" '
                f'-o "UserKnownHostsFile={known_hosts_path}"',
                'fetch',
                '--progress',
                self.REPOURL,
                '+master:refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ]).workdir(self.POLLER_WORKDIR),
            ExpectMasterShell([
                'git',
                'rev-parse',
                'refs/buildbot/' + self.REPOURL_QUOTED + '/master',
            ])
            .workdir(self.POLLER_WORKDIR)
            .stdout(b'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5\n'),
        )

        self.poller.doPoll.running = True
        yield self.poller.poll()

        self.assert_all_commands_ran()
        self.assertEqual(
            self.poller.lastRev, {'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'}
        )
        self.master.db.state.assertStateByClass(
            name=bytes2unicode(self.REPOURL),
            class_name='GitPoller',
            lastRev={'master': 'bf0b01df6d00ae8d1ffa0b2e2acbe642a6cd35d5'},
        )

        temp_dir_path = os.path.join('basedir', 'gitpoller-work', '.buildbot-ssh@@@')
        self.assertEqual(temp_dir_mock.dirs, [(temp_dir_path, 0o700), (temp_dir_path, 0o700)])

        expected_file_writes = [
            mock.call(key_path, 'ssh-key\n', mode=0o400),
            mock.call(known_hosts_path, 'ssh-known-hosts'),
            mock.call(key_path, 'ssh-key\n', mode=0o400),
            mock.call(known_hosts_path, 'ssh-known-hosts'),
        ]

        self.assertEqual(expected_file_writes, write_local_file_mock.call_args_list)


class TestGitPollerConstructor(
    unittest.TestCase, TestReactorMixin, changesource.ChangeSourceMixin, config.ConfigErrorsMixin
):
    @defer.inlineCallbacks
    def setUp(self):
        self.setup_test_reactor()
        yield self.setUpChangeSource()
        yield self.master.startService()

    @defer.inlineCallbacks
    def tearDown(self):
        yield self.master.stopService()
        yield self.tearDownChangeSource()

    @defer.inlineCallbacks
    def test_deprecatedFetchRefspec(self):
        with self.assertRaisesConfigError("fetch_refspec is no longer supported"):
            yield self.attachChangeSource(
                gitpoller.GitPoller("/tmp/git.git", fetch_refspec='not-supported')
            )

    @defer.inlineCallbacks
    def test_oldPollInterval(self):
        poller = yield self.attachChangeSource(gitpoller.GitPoller("/tmp/git.git", pollinterval=10))
        self.assertEqual(poller.pollInterval, 10)

    @defer.inlineCallbacks
    def test_branches_default(self):
        poller = yield self.attachChangeSource(gitpoller.GitPoller("/tmp/git.git"))
        self.assertEqual(poller.branches, ["master"])

    @defer.inlineCallbacks
    def test_branches_oldBranch(self):
        poller = yield self.attachChangeSource(gitpoller.GitPoller("/tmp/git.git", branch='magic'))
        self.assertEqual(poller.branches, ["magic"])

    @defer.inlineCallbacks
    def test_branches(self):
        poller = yield self.attachChangeSource(
            gitpoller.GitPoller("/tmp/git.git", branches=['magic', 'marker'])
        )
        self.assertEqual(poller.branches, ["magic", "marker"])

    @defer.inlineCallbacks
    def test_branches_True(self):
        poller = yield self.attachChangeSource(gitpoller.GitPoller("/tmp/git.git", branches=True))
        self.assertEqual(poller.branches, True)

    @defer.inlineCallbacks
    def test_only_tags_True(self):
        poller = yield self.attachChangeSource(gitpoller.GitPoller("/tmp/git.git", only_tags=True))
        self.assertIsNotNone(poller.branches)

    @defer.inlineCallbacks
    def test_branches_andBranch(self):
        with self.assertRaisesConfigError("can't specify both branch and branches"):
            yield self.attachChangeSource(
                gitpoller.GitPoller("/tmp/git.git", branch='bad', branches=['listy'])
            )

    @defer.inlineCallbacks
    def test_branches_and_only_tags(self):
        with self.assertRaisesConfigError("can't specify only_tags and branch/branches"):
            yield self.attachChangeSource(
                gitpoller.GitPoller("/tmp/git.git", only_tags=True, branches=['listy'])
            )

    @defer.inlineCallbacks
    def test_branch_and_only_tags(self):
        with self.assertRaisesConfigError("can't specify only_tags and branch/branches"):
            yield self.attachChangeSource(
                gitpoller.GitPoller("/tmp/git.git", only_tags=True, branch='bad')
            )

    @defer.inlineCallbacks
    def test_gitbin_default(self):
        poller = yield self.attachChangeSource(gitpoller.GitPoller("/tmp/git.git"))
        self.assertEqual(poller.gitbin, "git")
