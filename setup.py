# vi:set ts=8 sts=4 sw=4 et tw=80:
#!/usr/bin/env python
"A clone of pdb, fast and with the remote debugging and attach features."

# Python 2-3 compatibility.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

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


with open('README.rst') as f:
    long_description = f.read()

if PY3:
    cmdclass = {}
    ext_modules = [Extension('pdb_clone._bdb',
                    sources=['lib/pdb_clone/_bdbmodule-py3.c'], optional=True),
                   Extension('pdb_clone._pdbhandler',
                    sources=['lib/pdb_clone/_pdbhandler-py3.c'], optional=True)]
else:
    cmdclass={'build_ext': build_ext}
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

