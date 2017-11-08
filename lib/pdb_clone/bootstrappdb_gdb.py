# vi:set ts=8 sts=4 sw=4 et tw=80:
''' The 'py-pdb' gdb command.

See https://sourceware.org/gdb/current/onlinedocs/gdb/Python.html#Python.

When the subdirectory 'Tools/gdb' of the Python source distribution is in
sys.path, 'py-pdb' prints the Python source line where the process is stopped
at.
'''

# NOTE: some gdbs are linked with Python 3, so this file should be dual-syntax
# compatible (2.7+ and 3+).

import os
import sys
import subprocess
import re
import tempfile
import socket
import traceback
import gdb
try:
    from libpython import Frame
except ImportError:
    Frame = None

# Python 3.0 or newer
PY3 = (sys.version_info >= (3,))

class PdbLocalError(Exception):
    """Local error in the py-pdb command that may be retried."""

class PdbFatalError(Exception):
    """Fatal error in the py-pdb command."""

def already_in_use(addr):
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(addr)
    except Exception:
        raise PdbLocalError('%s: %s' % sys.exc_info()[:2])
    finally:
        if s:
            s.close()

def gdb_execute(command):
    rv = gdb.execute(command, False, True)
    if rv and len(rv.split('=')) == 2:
        return rv.split('=')[1].strip()
    return None

def is_symbol(symbol):
    re_symbol = re.compile(r'^\s*0x[0-9A-Fa-f]+\s*%s\s*$' % symbol)
    txt = gdb.execute('info functions ^%s$' % symbol, False, True)
    for line in txt.split('\n'):
        if re_symbol.match(line):
            return True
    return False

def module_fname(module):
    inferior = gdb.progspaces()[0].filename
    try:
        proc = subprocess.Popen([inferior, '-c',
                        'import %(m)s; print(%(m)s.__file__)' % {'m':module}],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        raise PdbFatalError('module_fname: %s: %s' %
                                            (sys.exc_info()[1], inferior))
    else:
        out, err = proc.communicate()
        if proc.returncode == 0:
            if not isinstance(out, str):
                out = out.decode()
            return out.strip()
        else:
            raise PdbFatalError('module_fname:\n%s' % err.strip())

def get_curline():
    """Return the current python source line."""
    if Frame:
        frame = Frame.get_selected_python_frame()
        if frame:
            line = ''
            f = frame.get_pyop()
            if f and not f.is_optimized_out():
                cwd = os.path.join(os.getcwd(), '')
                fname = f.filename()
                if cwd in fname:
                    fname = fname[len(cwd):]
                try:
                    line = f.current_line()
                except IOError:
                    pass
                if line:
                    # Use repr(line) to avoid UnicodeDecodeError on the
                    # following print invocation.
                    line = repr(line).strip("'")
                    line = line[:-2] if line.endswith(r'\n') else line
                    return ('-> %s(%s): %s' % (fname,
                                        f.current_line_num(), line))
    return ''

SOURCE = """
#include <stdio.h>
#include <dlfcn.h>
int main() {
    printf("%%d", %s);
    return 0;
}
"""

def dlopen_flag(flag):
    if os.name != 'posix':
        return

    # An os attribute in Python 3.
    if hasattr(os, flag):
        return int(getattr(os, flag))

    f = tempfile.NamedTemporaryFile()
    a_out = f.name
    f.close()
    # Silently ignore failures to compile 'SOURCE'.
    try:
        proc = subprocess.Popen(['cc', '-o', a_out, '-x', 'c', '-'],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return None
    else:
        proc.communicate(SOURCE % flag)
        if proc.returncode != 0:
            return None

    try:
        try:
            proc = subprocess.Popen([a_out],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise PdbFatalError('%s: %s' % (sys.exc_info()[1], 'dlfcn'))
        else:
            value, err = proc.communicate()
            if proc.returncode != 0:
                raise PdbFatalError(err.strip())
            try:
                return int(value)
            except ValueError:
                raise PdbFatalError('%s: %s' % (sys.exc_info()[1], value))
    finally:
        try:
            os.unlink(a_out)
        except OSError:
            pass

if PY3:
    LOAD_DYN = ("call PyRun_SimpleString(\"from _imp import load_dynamic; "
                "load_dynamic('%s', '%s', None)\")")
    LOADDYNAMIC = '_PyImport_LoadDynamicModule'
else:
    LOAD_DYN = ("call PyRun_SimpleString(\"from imp import load_dynamic; "
                "load_dynamic('%s', '%s')\")")
    LOADDYNAMIC = '_PyImport_GetDynLoadFunc'

class PyPdb(gdb.Command):
    """Setup pdb for remote debugging."""
    def __init__(self):
        gdb.Command.__init__ (self, "py-pdb", gdb.COMMAND_RUNNING,
                                                  gdb.COMPLETE_NONE)

    def invoke(self, arg, from_tty):
        try:
            self._invoke(arg)
        except PdbLocalError:
            print('Cannot setup pdb for remote debugging.\n%s'
                                        % sys.exc_info()[1])
        except PdbFatalError:
            print('Unable to setup pdb for remote debugging.\n%s'
                                        % sys.exc_info()[1])
        except Exception:
            traceback.print_exc()
            print('Cannot setup pdb for remote debugging.\n%s'
                                        % sys.exc_info()[1])
        finally:
            self.dont_repeat()

    def _invoke(self, arg):
        tracing_possible = gdb.lookup_symbol('_Py_TracingPossible')[0]
        if not tracing_possible:
            raise PdbFatalError('Please use a Python program built'
                                            ' with debugging symbols.')

        address = arg.split()
        try:
            address[1] = int(address[1])
        except (IndexError, ValueError):
            raise PdbFatalError(
                        'The "host port" arguments are required".')
        already_in_use(tuple(address))

        if int(tracing_possible.value()):
            raise PdbLocalError(
                        'Tracing is already set on one of the threads.')

        alive_pdb_context = gdb.lookup_symbol('alive_pdb_context')[0]
        if alive_pdb_context and int(alive_pdb_context.value()):
            raise PdbLocalError(
                    'Refusing to attach at the previous pdb subinterpreter.')

        in_dlopen = in_load_dynamic = False
        f = gdb.newest_frame()
        while f:
            name = f.name()
            if name == 'Py_Initialize':
                raise PdbLocalError('Interpreter not yet initialized.')
            elif name == 'Py_Finalize':
                raise PdbLocalError('Interpreter is being finalized.')
            elif name == 'Py_NewInterpreter':
                raise PdbLocalError('A subinterpreter is being initialized.')
            elif name == 'Py_MakePendingCalls' or name == 'Py_AddPendingCall':
                raise PdbLocalError('A signal is being processed.')
            elif name == 'dlopen':
                in_dlopen = True
            elif name == LOADDYNAMIC:
                in_load_dynamic = True
            f = f.older()

        loader = ''
        if not gdb.lookup_symbol('bootstrappdb_string')[0]:
            mpath = module_fname('pdb_clone._pdbhandler')
            if not os.path.isfile(mpath):
                raise PdbFatalError('%s does not exist.' % mpath)

            # Try first dlopen on unix.
            if os.name == 'posix' and is_symbol('dlopen'):
                if in_dlopen:
                    raise PdbLocalError('Stopped within dlopen.')
                flag = dlopen_flag('RTLD_NOW')
                if (isinstance(flag, int) and
                        gdb_execute('call dlopen("%s", %d)' %
                                            (mpath, flag)) != '0'):
                    loader = 'dlopen'

            # When dlopen fails, use load_dynamic which is safer than directly
            # importing _pdbhandler as the _imp module is a builtin and
            # load_dynamic will succeed even when stopped in the import
            # machinery. Note that mixing multiple interpreters and the
            # PyGILState_*() API is unsupported by Python, and see also issue
            # 20891: PyGILState_Ensure on non-Python thread causes fatal
            # error.
            if not loader:
                if in_load_dynamic:
                    raise PdbLocalError('Stopped within load_dynamic.')
                loader = 'load_dynamic'
                state = gdb_execute('call (int)PyGILState_Ensure()')
                rv = gdb_execute(LOAD_DYN % ('_pdbhandler', mpath))
                gdb_execute('call PyGILState_Release(%s)' % state)
                if rv != '0':
                    raise PdbFatalError(
                            'Could not load the _pdbhandler library.')

        if (gdb_execute(
                'call Py_AddPendingCall(bootstrappdb_string, "%s %s")' %
                                            (address[0], address[1])) != '0'):
            raise PdbFatalError('Failed to setup _pdbhandler.')

        method = ''
        if loader:
            method = ' using %s()' % loader

        if PY3:
            curline = get_curline()
            if curline:
                print(curline)
        else:
            # Some versions of gcc optimize-out or set to null the values of
            # objects visible to the debugger (e.g. gcc version 4.9.0).  See also
            # https://bugzilla.redhat.com/show_bug.cgi?id=556975
            try:
                curline = get_curline()
            except:
                # Using a bare except: when python 2.7 is built without
                # '--with-pydebug' and with gcc 4.9.0, many types of exceptions
                # occur: NullPyObjectPtr, gdb.MemoryError, AttributeError,
                # TypeError...
                pass
            else:
                if curline:
                    print(curline)

        print("\nPdb has been setup for remote debugging%s.\n"
            "Enter now the 'detach' or 'quit' gdb command, and"
            " connect to pdb on port %d with 'pdb_attach.py'." %
            (method, address[1]))

PyPdb()

