# vi:set ts=8 sts=4 sw=4 et tw=80:
#!/usr/bin/env python
"A clone of pdb, fast and with the remote debugging and attach features."

# Python 2-3 compatibility.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
try:
    from test import support    # Python 3
except ImportError:
    from test import test_support as support    # Python 2

import sys
import os
import doctest
import importlib
import shutil
from unittest import defaultTestLoader

from lib.pdb_clone import __version__, PY3

try:
    from setuptools import setup, Extension, Command
    if not PY3:
        from setuptools.command.build_ext import build_ext as _build_ext
except ImportError:
    from distutils.core import setup, Extension, Command
    if not PY3:
        from distutils.command.build_ext import build_ext as _build_ext

if not PY3:
    class build_ext(_build_ext):
        """Subclass 'build_ext' to build extensions as 'optional'.

        'optional' is not available in Python 2.
        """

        def run(self):
            try:
                _build_ext.run(self)
            except (CCompilerError, DistutilsError, CompileError):
                self.warn('\n\n*** Building the extension failed. ***')

class Test(Command):
    description = 'run the test suite'

    user_options = [
        ('tests=', 't',
            'run a comma separated list of tests, for example             '
            '"--tests=pdb,bdb"; all the tests are run when this option'
            ' is not present'),
        ('prefix=', 'p', 'run only unittest methods whose name starts'
            ' with this prefix'),
        ('stop', 's', 'stop at the first test failure or error'),
        ('detail', 'd', 'detailed test output, each test case is printed'),
    ]

    def initialize_options(self):
        self.testdir = 'testsuite'
        self.tests = ''
        self.prefix = 'test'
        self.stop = False
        self.detail = False

    def finalize_options(self):
        self.tests = (['test_' + t for t in self.tests.split(',') if t] or
            [t[:-3] for t in os.listdir(self.testdir) if
                t.startswith('test_') and t.endswith('.py')])
        defaultTestLoader.testMethodPrefix = self.prefix
        support.failfast = self.stop
        support.verbose = self.detail

    def run (self):
        """Run the test suite."""
        result_tmplt = '{} ... {:d} tests with zero failures'
        optionflags = doctest.REPORT_ONLY_FIRST_FAILURE if self.stop else 0
        cnt = ok = 0
        # Make sure we are testing the installed version of pdb_clone.
        if 'pdb_clone' in sys.modules:
            del sys.modules['pdb_clone']
        sys.path.pop(0)
        import pdb_clone

        for test in self.tests:
            cnt += 1
            with support.temp_cwd() as cwd:
                sys.path.insert(0, os.getcwd())
                try:
                    savedcwd = support.SAVEDCWD
                    shutil.copytree(os.path.join(savedcwd, 'testsuite'),
                                             os.path.join(cwd, 'testsuite'))
                    # Some unittest tests spawn pdb-clone.
                    shutil.copyfile(os.path.join(savedcwd, 'pdb-clone'),
                                            os.path.join(cwd, 'pdb-clone'))
                    abstest = self.testdir + '.' + test
                    module = importlib.import_module(abstest)
                    suite = defaultTestLoader.loadTestsFromModule(module)
                    unittest_count = suite.countTestCases()
                    # Change the module name to allow correct doctest checks.
                    module.__name__ = 'test.' + test
                    print('{}:'.format(abstest))
                    f, t = doctest.testmod(module, verbose=self.detail,
                                           optionflags=optionflags)
                    if f:
                        print('{:d} of {:d} doctests failed'.format(f, t))
                    elif t:
                        print(result_tmplt.format('doctest', t))

                    try:
                        support.run_unittest(suite)
                    except support.TestFailed as msg:
                        print('test', test, 'failed --', msg)
                    else:
                        print(result_tmplt.format('unittest', unittest_count))
                        if not f:
                            ok += 1
                finally:
                    sys.path.pop(0)
        failed = cnt - ok
        cnt = failed if failed else ok
        plural = 's' if cnt > 1 else ''
        result = 'failed' if failed else 'ok'
        print('{:d} test{} {}.'.format(cnt, plural, result))

with open('README.rst') as f:
    long_description = f.read()

if PY3:
    cmdclass = {'test': Test}
    ext_modules = [Extension('pdb_clone._bdb',
                    sources=['lib/pdb_clone/_bdbmodule-py3.c'], optional=True),
                   Extension('pdb_clone._pdbhandler',
                    sources=['lib/pdb_clone/_pdbhandler-py3.c'], optional=True)]
else:
    cmdclass={'build_ext': build_ext, 'test': Test}
    ext_modules = [Extension('pdb_clone._bdb',
                    sources=['lib/pdb_clone/_bdbmodule-py27.c']),
                   Extension('pdb_clone._pdbhandler',
                    sources=['lib/pdb_clone/_pdbhandler-py27.c'])]

setup(
    cmdclass = cmdclass,
    scripts = ['pdb-clone', 'pdb-attach'],
    ext_modules = ext_modules,
    packages = ['pdb_clone'],
    package_dir = {'': 'lib'},

    # meta-data
    name = 'pdb-clone',
    version = __version__,
    description = __doc__,
    long_description = long_description,
    platforms = 'all',
    license = 'GNU GENERAL PUBLIC LICENSE Version 2',
    author = 'Xavier de Gaye',
    author_email = 'xdegaye@users.sourceforge.net',
    url = 'https://github.com/corpusops/pdbclone.git',
    classifiers = [
        'Development Status :: 5 - Production/Stable',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU General Public License v2 (GPLv2)',
        'Operating System :: Unix',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 3',
        'Topic :: Software Development :: Debuggers',
    ],
)

