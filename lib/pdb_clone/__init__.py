# vi:set ts=8 sts=4 sw=4 et tw=80:
" The pdb_clone package."

# Python 2-3 compatibility.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys

__version__ =  '1.10.2'
DFLT_ADDRESS = ('127.0.0.1', 7935)

# Python 2.6 or older
PY26 = (sys.version_info < (2, 7))

# Python 3.0 or newer
PY3 = (sys.version_info >= (3,))

# Python 3.2 or newer
PY32 = (sys.version_info >= (3, 2))

# Python 3.3 or newer
PY33 = (sys.version_info >= (3, 3))

# Python 3.4 or newer
PY34 = (sys.version_info >= (3, 4))

if PY26 or (PY3 and not PY32):
    raise NotImplementedError('Python 2.7 or Python 3.2 or newer is required.')

# Derived from 'six' written by Benjamin Peterson.
if PY3:
    import builtins
    exec_ = getattr(builtins, 'exec')
    eval_ = getattr(builtins, 'eval')

    exec_("""if 1:
            def raise_from(value, from_value):
                raise value from from_value
          """)

else:
    def exec_(code, globs=None, locs=None):
        if globs is None:
            frame = sys._getframe(1)
            globs = frame.f_globals
            if locs is None:
                locs = frame.f_locals
            del frame
        elif locs is None:
            locs = globs
        # 'code' must be compiled without inheriting the future statements.
        exec('exec code in globs, locs')

    def eval_(stmt, globs=None, locs=None):
        """Do not inherit the future statements."""
        if isinstance(stmt, str):
            if not stmt.endswith('\n'):
                stmt += '\n'
            stmt = compile(stmt, '<string>', 'eval', 0, True)
        return eval(stmt, globs, locs)

    def raise_from(value, from_value):
        raise value

