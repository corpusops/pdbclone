# vi:set ts=8 sts=4 sw=4 et tw=80:
"""Debugger basics"""

# Python 2-3 compatibility.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
try:
    import reprlib   # Python 3
except ImportError:
    import repr as reprlib   # Python 2

import fnmatch
import sys
import os
import linecache
import ast
import itertools
import types
import tempfile
import shutil
from bisect import bisect
from operator import attrgetter
from inspect import CO_GENERATOR

from . import PY3, PY34, eval_
try:
    from . import _bdb
except ImportError:
    _bdb = None

__all__ = ["BdbQuit", "Bdb", "Breakpoint"]

class ModuleFinder(list):
    """A list of the parent module names imported by the debuggee."""

    PATH_ENTRY = 'pdb_module_finder'

    def __init__(self):
        self.hooked = False

    def __call__(self, path_entry):
        if path_entry != self.PATH_ENTRY:
            raise ImportError()
        return self

    def find_module(self, fullname, path=None):
        # PEP 302
        self.append(fullname)
        return None

    def find_spec(self, fullname, target=None):
        # PEP 451
        return self.find_module(fullname)

    def reset(self):
        """Remove from sys.modules the modules imported by the debuggee."""
        if not self.hooked:
            self.hooked = True
            sys.path_hooks.append(self)
            sys.path.insert(0, self.PATH_ENTRY)
            return

        for modname in self:
            if modname in sys.modules:
                del sys.modules[modname]
                submods = []
                for subm in sys.modules:
                    if subm.startswith(modname + '.'):
                        submods.append(subm)
                # All submodules of modname may not have been imported by the
                # debuggee, but they are still removed from sys.modules as
                # there is no way to distinguish them.
                for subm in submods:
                    del sys.modules[subm]
        self[:] = []

    def close(self):
        self.hooked = False
        if self in sys.path_hooks:
            sys.path_hooks.remove(self)
        if self.PATH_ENTRY in sys.path:
            sys.path.remove(self.PATH_ENTRY)
        if self.PATH_ENTRY in sys.path_importer_cache:
            del sys.path_importer_cache[self.PATH_ENTRY]

def case_sensitive_file_system():
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp()
        one = os.path.join(tmpdir, 'one')
        ONE = os.path.join(tmpdir, 'ONE')
        with open(one, 'w') as f:
            f.write('one')
        with open(ONE, 'w') as f:
            f.write('ONE')
        with open(one) as f:
            if f.read() == 'ONE':
                return False
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir)
    return True

# A dictionary mapping a filename to a BdbModule instance.
_modules = {}
_module_finder = ModuleFinder()
_casesensitive_fs = case_sensitive_file_system()

def all_pathnames(abspath):
    yield abspath
    cwd = os.getcwd()
    if not _casesensitive_fs:
        cwd = cwd.lower()
    if abspath.startswith(cwd):
        relpath = abspath[len(cwd):]
        if relpath.startswith(os.sep):
            relpath = relpath[len(os.sep):]
        if os.path.isfile(relpath):
            yield relpath
        relpath = os.path.join('.', relpath)
        if os.path.isfile(relpath):
            yield relpath

def canonic(filename):
    if filename[:1] + filename[-1:] == '<>':
        return filename
    pathname = os.path.normcase(os.path.abspath(filename))
    # On Mac OS X, normcase does not convert the path to lower case.
    if not _casesensitive_fs:
        pathname = pathname.lower()
    return pathname

def code_line_numbers(code):
    # Source code line numbers generator (see Objects/lnotab_notes.txt).
    valid_lno = lno = code.co_firstlineno
    yield valid_lno
    # The iterator yields (line_incr[i], byte_incr[i+1]) from lnotab.
    for line_incr, byte_incr in itertools.islice(zip(code.co_lnotab,
                itertools.chain(code.co_lnotab[1:], [b'\x01'])), 1, None, 2):
        if PY3:
            lno += line_incr
            if byte_incr == 0:
                continue
        else:
            lno += ord(line_incr)
            if ord(byte_incr) == 0:
                continue
        if lno != valid_lno:
            valid_lno = lno
            yield valid_lno

def safe_repr(obj):
    try:
        return reprlib.repr(obj)
    except Exception:
        return object.__repr__(obj)

class BdbException(Exception):
    """A bdb exception."""

class BdbError(BdbException):
    """A bdb error."""

class BdbSourceError(BdbError):
    """An error related to the debuggee source code."""

class BdbSyntaxError(BdbError):
    """A syntax error in the debuggee source code."""

class BdbQuit(BdbException):
    """Exception to give up completely."""

class IntegersCache:
    """Cache integers in a list.

    The list is used by the _bdb.BdbTracer instance to avoid allocating a
    PyLongObject for each line when trying to find if the line number matches a
    breakpoint line number.

    """
    def __init__(self, cache):
        self.cache = cache
        self.refs = []
        self.len = 0

    def add(self, i):
        if i >= self.len:
            inc = i - self.len + 1
            self.cache.extend(itertools.repeat(None, inc))
            self.refs.extend(itertools.repeat(0, inc))
            self.len = i + 1
        self.cache[i] = i
        self.refs[i] += 1
        return i

    def delete(self, i):
        if i < self.len and self.cache[i] is not None:
            self.refs[i] -= 1
            if not self.refs[i]:
                self.cache[i] = None
                # Pack the end of the list.
                if i == self.len - 1:
                    for j in range(i, -1, -1):
                        if self.cache[j] is not None:
                            break
                    else:
                        j = -1
                    self.cache[:] = self.cache[:j-i]
                    self.refs[:] = self.refs[:j-i]
                    self.len -= i - j
            return i
        return None

    def __repr__(self):
        return '\n'.join((str(self.cache), str(self.refs), str(self.len)))

class BdbModule:
    """A module.

    Instance attributes:
        functions_firstlno: a dictionary mapping function names and fully
        qualified method names to their first line number.
    """

    def __init__(self, filename):
        self.filename = filename
        self.linecache = None
        self.reset()

    def reset(self):
        if (self.filename not in linecache.cache or
                id(linecache.cache[self.filename]) != id(self.linecache)):
            self.functions_firstlno = None
            self.code = None
            lines = ''.join(linecache.getlines(self.filename))
            if not lines:
                raise BdbSourceError('No lines in {}.'.format(self.filename))
            try:
                self.code = compile(lines, self.filename, 'exec', 0, True)
                self.node = compile(lines, self.filename, 'exec',
                                                    ast.PyCF_ONLY_AST, True)
            except (SyntaxError, TypeError) as err:
                raise BdbSyntaxError('{}: {}.'.format(self.filename, err))
            # At this point we still need to test for self.filename in
            # linecache.cache because of doctest scripts, as doctest installs a
            # hook at linecache.getlines to allow <doctest name> to be
            # linecache readable. But the condition is always true for real
            # filenames.
            if self.filename in linecache.cache:
                self.linecache = linecache.cache[self.filename]
            return True
        return False

    def get_func_lno(self, funcname):
        """The first line number of the last defined 'funcname' function."""

        class FuncLineno(ast.NodeVisitor):
            def __init__(self):
                self.clss = []

            def generic_visit(self, node):
                for child in ast.iter_child_nodes(node):
                    for item in self.visit(child):
                        yield item

            def visit_ClassDef(self, node):
                self.clss.append(node.name)
                for item in self.generic_visit(node):
                    yield item
                self.clss.pop()

            def visit_FunctionDef(self, node):
                # Only allow non nested function definitions.
                name = '.'.join(itertools.chain(self.clss, [node.name]))
                yield name, node.lineno

        if self.functions_firstlno is None:
            self.functions_firstlno = {}
            for name, lineno in FuncLineno().visit(self.node):
                if (name not in self.functions_firstlno or
                        self.functions_firstlno[name] < lineno):
                    self.functions_firstlno[name] = lineno
        try:
            return self.functions_firstlno[funcname]
        except KeyError:
            raise BdbSourceError('{}: function "{}" not found.'.format(
                self.filename, funcname))

    def get_actual_bp(self, lineno):
        """Get the actual breakpoint line number.

        When an exact match cannot be found in the lnotab expansion of the
        module code object or one of its subcodes, pick up the next valid
        statement line number.

        Return the statement line defined by the tuple (code firstlineno,
        statement line number) which is at the shortest distance to line
        'lineno' and greater or equal to 'lineno'. When 'lineno' is the first
        line number of a subcode, use its first statement line instead.
        """

        def _distance(code, module_level=False):
            """The shortest distance to the next valid statement."""
            subcodes = dict((c.co_firstlineno, c) for c in code.co_consts
                                if isinstance(c, types.CodeType) and not
                                    c.co_name.startswith('<'))
            # Get the shortest distance to the subcode whose first line number
            # is the last to be less or equal to lineno. That is, find the
            # index of the first subcode whose first_lno is the first to be
            # strictly greater than lineno.
            subcode_dist = None
            subcodes_flnos = sorted(subcodes)
            idx = bisect(subcodes_flnos, lineno)
            if idx != 0:
                flno = subcodes_flnos[idx-1]
                subcode_dist = _distance(subcodes[flno])

            # Check if lineno is a valid statement line number in the current
            # code, excluding function or method definition lines.
            code_lnos = sorted(code_line_numbers(code))
            # Do not stop at execution of function definitions.
            if not module_level and len(code_lnos) > 1:
                code_lnos = code_lnos[1:]
            if lineno in code_lnos and lineno not in subcodes_flnos:
                return 0, (code.co_firstlineno, lineno)

            # Compute the distance to the next valid statement in this code.
            idx = bisect(code_lnos, lineno)
            if idx == len(code_lnos):
                # lineno is greater that all 'code' line numbers.
                return subcode_dist
            actual_lno = code_lnos[idx]
            dist = actual_lno - lineno
            if subcode_dist and subcode_dist[0] < dist:
                return subcode_dist
            if actual_lno not in subcodes_flnos:
                return dist, (code.co_firstlineno, actual_lno)
            else:
                # The actual line number is the line number of the first
                # statement of the subcode following lineno (recursively).
                return _distance(subcodes[actual_lno])

        if self.code:
            code_dist = _distance(self.code, module_level=True)
        if not self.code or not code_dist:
            raise BdbSourceError('{}: line {} is after the last '
                'valid statement.'.format(self.filename, lineno))
        return code_dist[1]

class ModuleBreakpoints(dict):
    """The breakpoints of a module.

    A dictionary that maps a code firstlineno to a 'code_bps' dictionary that
    maps each line number of the code, where one or more breakpoints are set,
    to the list of corresponding Breakpoint instances.

    Note:
    A line in 'code_bps' is the actual line of the breakpoint (the line where the
    debugger stops), this line may differ from the line attribute of the
    Breakpoint instance as set by the user.
    """

    def __init__(self, filename, lineno_cache):
        if filename not in _modules:
            _modules[filename] = BdbModule(filename)
        self.bdb_module = _modules[filename]
        self.lineno_cache = lineno_cache

    def reset(self):
        try:
            do_reset = self.bdb_module.reset()
        except BdbSourceError:
            do_reset = True
        if do_reset:
            bplist = self.all_breakpoints()
            self.clear()
            for bp in bplist:
                try:
                    bp.actual_bp = self.add_breakpoint(bp)
                except BdbSourceError:
                    bp.deleteMe()

    def add_breakpoint(self, bp):
        firstlineno, actual_lno = self.bdb_module.get_actual_bp(bp.line)
        if firstlineno not in self:
            self[firstlineno] = {}
            self.lineno_cache.add(firstlineno)
        code_bps = self[firstlineno]
        if actual_lno not in code_bps:
            code_bps[actual_lno] = []
            self.lineno_cache.add(actual_lno)
        code_bps[actual_lno].append(bp)
        return firstlineno, actual_lno

    def delete_breakpoint(self, bp):
        firstlineno, actual_lno = bp.actual_bp
        try:
            code_bps = self[firstlineno]
            bplist = code_bps[actual_lno]
            bplist.remove(bp)
        except (KeyError, ValueError):
            # This may occur after a reset and the breakpoint could not be
            # added anymore.
            return
        if not bplist:
            del code_bps[actual_lno]
            self.lineno_cache.delete(actual_lno)
        if not code_bps:
            # DO NOT delete the code_bps dictionary even though it is empty.
            # The _bdb extension module may be holding a reference to this
            # dictionary while tracing this function and in this case, a new
            # breakpoint added to the function must be refered to by this same
            # code_bps.
            pass

    def get_breakpoints(self, lineno):
        """Return the list of breakpoints set at lineno."""
        try:
            firstlineno, actual_lno = self.bdb_module.get_actual_bp(lineno)
        except BdbSourceError:
            return []
        if firstlineno not in self:
            return []
        code_bps = self[firstlineno]
        if actual_lno not in code_bps:
            return []
        return [bp for bp in sorted(code_bps[actual_lno],
                    key=attrgetter('number')) if bp.line == lineno]

    def all_breakpoints(self):
        bpts = []
        for code_bps in self.values():
            for bplist in code_bps.values():
                bpts.extend(bplist)
        return [bp for bp in sorted(bpts, key=attrgetter('number'))]

class Tracer:
    """Python implementation of _bdb.BdbTracer type.

    Attributes:
        stopframe: The frame where the debugger must stop. When None, the
        debugger may stop at any frame depending on the value of stop_lineno.
        When not None, the debugger stops at the 'return' debug event in that
        frame, whatever the value of stop_lineno.

        stop_lineno: The debugger stops when the current line number in the
        stopframe frame is greater or equal to stop_lineno. A value of -1 for
        stop_lineno means the infinite line number, i.e. don't stop.

        Therefore the following values of (self.stopframe, self.stop_lineno)
        mean:
            (None, 0):   always stop
            (None, -1):  never stop
            (frame, 0):  stop on next statement in that frame
            (frame, -1): stop when returning from frame
    """

    def __init__(self, to_lowercase, skip_modules=(), skip_calls=()):
        self.to_lowercase = to_lowercase
        self.skip_modules = skip_modules
        self.skip_calls = skip_calls
        # A dictionary mapping filenames to a ModuleBreakpoints instances.
        self.breakpoints = {}
        # The list of line numbers used to improve _bdb performance.
        self.linenumbers = []
        self.reset()

    def reset(self, ignore_first_call_event=True, botframe=None):
        self.ignore_first_call_event = ignore_first_call_event
        self.botframe = botframe
        self.quitting = False
        self.topframe = None
        self.topframe_locals = None
        self.stopframe = None
        self.stop_lineno = 0

    def trace_dispatch(self, frame, event, arg):
        if event == 'line':
            if self.stop_here(frame):
                return self.user_method(frame, self.user_line)
            module_bps = self.bkpt_at_line(frame)
            if module_bps:
                return self.user_method(frame, self.bkpt_user_line, module_bps)
            return self.trace_dispatch

        elif event == 'call':
            if self.ignore_first_call_event:
                self.ignore_first_call_event = False
                return self.trace_dispatch
            if frame.f_code in self.skip_calls:
                return # None
            stop_here = self.stop_here(frame)
            if not (stop_here or self.bkpt_in_code(frame)):
                # When frame is stopframe, we are re-entering a generator
                # frame where the {next, until, return} command had been
                # previously issued, so we need to enable tracing in this
                # function.
                if (PY34 and self.stopframe is frame and
                        frame.f_code.co_flags & CO_GENERATOR):
                    return self.trace_dispatch
                # No need to trace this function.
                return # None
            # Ignore call events in generator except when stepping.
            if (PY34 and frame.f_code.co_flags & CO_GENERATOR and
                    (self.stopframe is not None or self.stop_lineno != 0)):
                return self.trace_dispatch
            if stop_here:
                return self.user_method(frame, self.user_call, arg)
            # A breakpoint is set in this function.
            return self.trace_dispatch

        elif event == 'return':
            if self.stop_here(frame) or frame is self.stopframe:
                # Ignore return events in generator except when stepping.
                if PY34:
                    ignore = (frame.f_code.co_flags & CO_GENERATOR and
                        (self.stopframe is not None or self.stop_lineno != 0))
                else:
                    ignore = False
                if (not ignore and
                        not self.user_method(frame, self.user_return, arg)):
                    return None
                # Set the trace function in the caller when returning from the
                # current frame after step, next, until, return commands.
                if (frame is not self.botframe and
                        ((self.stopframe is None and self.stop_lineno == 0) or
                                        frame is self.stopframe)):
                    if frame.f_back and not frame.f_back.f_trace:
                        frame.f_back.f_trace = self.trace_dispatch
                    if not ignore:
                        self.stopframe = None
                        self.stop_lineno = 0
            if frame is self.botframe:
                self.stop_tracing(frame)
                return None
            return self.trace_dispatch

        elif event == 'exception':
            if not PY34:
                if self.stop_here(frame):
                    return self.user_method(frame, self.user_exception, arg)
            elif self.stop_here(frame):
                # When stepping with next/until/return in a generator frame,
                # skip the internal StopIteration exception (with no
                # traceback) triggered by a subiterator run with the 'yield
                # from' statement.
                if not (frame.f_code.co_flags & CO_GENERATOR
                        and arg[0] is StopIteration and arg[2] is None):
                    return self.user_method(frame, self.user_exception, arg)
            # Stop at the StopIteration or GeneratorExit exception when the
            # user has set stopframe in a generator by issuing a return
            # command, or a next/until command at the last statement in the
            # generator before the exception.
            elif (self.stopframe and frame is not self.stopframe
                    and self.stopframe.f_code.co_flags & CO_GENERATOR
                    and arg[0] in (StopIteration, GeneratorExit)):
                return self.user_method(frame, self.user_exception, arg)
            return self.trace_dispatch

    def stop_here(self, frame):
        if self.skip_modules and self.is_skipped_module(frame):
            return False
        if frame is self.stopframe or self.stopframe is None:
            if self.stop_lineno == -1:
                return False
            return frame.f_lineno >= self.stop_lineno

    def bkpt_at_line(self, frame):
        filename = (frame.f_code.co_filename if not self.to_lowercase
                    else frame.f_code.co_filename.lower())
        if filename not in self.breakpoints:
            return # None
        module_bps = self.breakpoints[filename]
        firstlineno = frame.f_code.co_firstlineno
        if (firstlineno in module_bps and
                frame.f_lineno in module_bps[firstlineno]):
            return module_bps

    def bkpt_in_code(self, frame):
        filename = (frame.f_code.co_filename if not self.to_lowercase
                    else frame.f_code.co_filename.lower())
        if (filename in self.breakpoints and
                frame.f_code.co_firstlineno in self.breakpoints[filename]):
            return True

    def settrace(self, do_set):
        """Set or remove the trace function."""
        if do_set:
            sys.settrace(self.trace_dispatch)
        else:
            sys.settrace(None)

    def gettrace(self):
        """Return the trace object."""
        return sys.gettrace()

    # The following methods are not on the fast path.

    def user_method(self, frame, method, *args, **kwds):
        if not self.botframe:
            self.botframe = frame
        self.topframe = frame
        self.topframe_locals = None
        method(frame, *args, **kwds)
        self.topframe = None
        self.topframe_locals = None
        return self.get_traceobj()

BdbTracer = _bdb.BdbTracer if _bdb else Tracer

class Bdb(BdbTracer):
    """Generic Python debugger base class.

    This class takes care of details of the trace facility;
    a derived class should implement user interaction.
    The standard debugger class (pdb.Pdb) is an example.
    """

    def __init__(self, skip=None):
        skip_modules = tuple(skip) if skip else ()
        skip_calls = (ModuleFinder.__call__.__code__,
                      ModuleFinder.find_module.__code__)
        BdbTracer.__init__(self, not _casesensitive_fs, skip_modules, skip_calls)
        self.lineno_cache = IntegersCache(self.linenumbers)

    # Backward compatibility.
    def canonic(self, filename):
        return canonic(filename)

    def restart(self):
        """Restart the debugger after source code changes."""
        _module_finder.reset()
        linecache.checkcache()
        for module_bpts in self.breakpoints.values():
            module_bpts.reset()

    def get_locals(self, frame):
        # The f_locals dictionary of the top level frame is cached to avoid
        # being overwritten by invocation of its getter frame_getlocals (see
        # frameobject.c).
        if frame is self.topframe:
            if not self.topframe_locals:
                self.topframe_locals = self.topframe.f_locals
            return self.topframe_locals
        # Get the f_locals dictionary and thus explicitly overwrite the
        # previous changes made by the user to locals in this frame (see issue
        # 9633).
        return frame.f_locals

    # Normally derived classes don't override the following
    # methods, but they may if they want to redefine the
    # definition of stopping and breakpoints.

    def is_skipped_module(self, frame):
        module_name = frame.f_globals.get('__name__')
        for pattern in self.skip_modules:
            if fnmatch.fnmatch(module_name, pattern):
                return True
        return False

    def _set_stopinfo(self, stopframe, stop_lineno):
        # Ensure that stopframe belongs to the stack frame in the interval
        # [self.botframe, self.topframe] and that it gets a trace function.
        frame = self.topframe
        while stopframe and frame and frame is not stopframe:
            if frame is self.botframe:
                stopframe = self.botframe
                break
            frame = frame.f_back
        if stopframe and not stopframe.f_trace:
            stopframe.f_trace = self.trace_dispatch
        self.stopframe = stopframe
        self.stop_lineno = stop_lineno

    def bkpt_user_line(self, frame, module_bps):
        # Handle multiple breakpoints on the same line (issue 14789)
        firstlineno = frame.f_code.co_firstlineno
        effective_bp_list = []
        temporaries = []
        for bp in module_bps[firstlineno][frame.f_lineno]:
            stop, delete = bp.process_hit_event(frame)
            if stop:
                effective_bp_list.append(bp.number)
                if bp.temporary and delete:
                    temporaries.append(bp.number)
        if effective_bp_list:
            self.user_line(frame,
                           (sorted(effective_bp_list), sorted(temporaries)))

    def stop_tracing(self, frame=None):
        # Stop tracing, the thread trace function 'c_tracefunc' is NULL and
        # thus, call_trampoline() is not called anymore for all debug events:
        # PyTrace_CALL, PyTrace_RETURN, PyTrace_EXCEPTION and PyTrace_LINE.
        self.settrace(False)

        # See PyFrame_GetLineNumber() in Objects/frameobject.c for why the
        # local trace functions must be deleted.
        # This is also required by pdbhandler: to terminate the
        # subinterpreter where lives the pdb instance, there must be no
        # references to the pdb instance.
        if not frame:
            frame = self.topframe
        while frame:
            del frame.f_trace
            if frame is self.botframe:
                break
            frame = frame.f_back

    # Derived classes and clients can call the following methods
    # to affect the stepping state.

    def set_until(self, frame, lineno=None):
        """Stop when the current line number in frame is greater than lineno or
        when returning from frame."""
        if lineno is None:
            lineno = frame.f_lineno + 1
        self._set_stopinfo(frame, lineno)

    def set_step(self):
        """Stop after one line of code."""
        self._set_stopinfo(None, 0)

    def set_next(self, frame):
        """Stop on the next line in or below the given frame."""
        self._set_stopinfo(frame, 0)

    def set_return(self, frame):
        """Stop when returning from the given frame."""
        self._set_stopinfo(frame, -1)

    def set_trace(self, frame=None):
        """Start debugging from `frame`.

        If frame is not specified, debugging starts from caller's frame.
        """
        # First disable tracing temporarily as set_trace() may be called while
        # tracing is in use. For example when called from a signal handler and
        # within a debugging session started with runcall().
        self.settrace(False)

        if not frame:
            frame = sys._getframe().f_back
        frame.f_trace = self.trace_dispatch

        # Do not change botframe when the debuggee has been started from an
        # instance of Pdb with one of the family of run methods.
        self.reset(ignore_first_call_event=False, botframe=self.botframe)
        self.topframe = frame
        while frame:
            if frame is self.botframe:
                break
            botframe = frame
            frame = frame.f_back
        else:
            self.botframe = botframe

        # Must trace the bottom frame to disable tracing on termination,
        # see issue 13044.
        if not self.botframe.f_trace:
            self.botframe.f_trace = self.trace_dispatch

        self.settrace(True)

    def get_traceobj(self):
        # Do not raise BdbQuit when debugging is started with set_trace.
        if self.quitting and self.botframe.f_back:
            raise BdbQuit
        # Do not re-install the local trace when we are finished debugging, see
        # issues 16482 and 7238.
        if not self.gettrace():
            return None
        if _bdb:
            return self
        else:
            return self.trace_dispatch

    def set_continue(self):
        # Don't stop except at breakpoints or when finished.
        self._set_stopinfo(None, -1)
        if not self.has_breaks():
            # No breakpoints; run without debugger overhead.
            self.stop_tracing()

    def set_quit(self):
        self.quitting = True
        self.stop_tracing()

    # Derived classes should override the user_* methods
    # to gain control.

    def user_call(self, frame, argument_list):
        """This method is called when there is the remote possibility
        that we ever need to stop in this function."""
        pass

    def user_line(self, frame, breakpoint_hits=None):
        """This method is called when we stop or break at this line.

        'breakpoint_hits' is a tuple of the list of breakpoint numbers that
        have been hit at this line, and of the list of temporaries that must be
        deleted.
        """
        pass

    def user_return(self, frame, return_value):
        """This method is called when a return trap is set here."""
        pass

    def user_exception(self, frame, exc_info):
        """This method is called if an exception occurs,
        but only if we are to stop at or just below this level."""
        pass

    # Derived classes and clients can call the following methods
    # to manipulate breakpoints.  These methods return an
    # error message is something went wrong, None if all is well.
    # Call self.get_*break*() to see the breakpoints or better
    # for bp in Breakpoint.bpbynumber: if bp: bp.bpprint().

    def set_break(self, fname, lineno, temporary=False, cond=None,
                  funcname=None):
        filename = canonic(fname)
        if filename not in self.breakpoints:
            module_bps = ModuleBreakpoints(filename, self.lineno_cache)
        else:
            module_bps = self.breakpoints[filename]
        if funcname:
            lineno = module_bps.bdb_module.get_func_lno(funcname)
        bp = Breakpoint(filename, lineno, module_bps, temporary, cond)
        filename_paths = list(all_pathnames(filename))
        if filename not in self.breakpoints:
            # self.breakpoints dictionary maps also the relative path names to
            # the common ModuleBreakpoints instance (co_filename may be a
            # relative path name).
            for pathname in filename_paths:
                self.breakpoints[pathname] = module_bps

        # Set the trace function when the breakpoint is set in one of the
        # frames of the frame stack.
        firstlineno, actual_lno = bp.actual_bp
        frame = self.topframe
        while frame:
            if (frame.f_code.co_filename in filename_paths and
                        firstlineno == frame.f_code.co_firstlineno):
                if not frame.f_trace:
                    frame.f_trace = self.trace_dispatch
            if frame is self.botframe:
                break
            frame = frame.f_back

        return bp

    def clear_break(self, filename, lineno):
        bplist = self.get_breaks(filename, lineno)
        if not bplist:
            return 'There is no breakpoint at %s:%d' % (filename, lineno)
        for bp in bplist:
            bp.deleteMe()

    def clear_bpbynumber(self, arg):
        try:
            bp = self.get_bpbynumber(arg)
        except ValueError as err:
            return str(err)
        bp.deleteMe()

    def clear_all_breaks(self):
        if not self.has_breaks():
            return 'There are no breakpoints'
        for bp in Breakpoint.bpbynumber:
            if bp:
                bp.deleteMe()

    def get_bpbynumber(self, arg):
        if not arg:
            raise ValueError('Breakpoint number expected')
        try:
            number = int(arg)
        except ValueError:
            raise ValueError('Non-numeric breakpoint number %s' % arg)
        try:
            bp = Breakpoint.bpbynumber[number]
        except IndexError:
            raise ValueError('Breakpoint number %d out of range' % number)
        if bp is None:
            raise ValueError('Breakpoint %d already deleted' % number)
        return bp

    def get_breaks(self, filename, lineno):
        filename = canonic(filename)
        if filename in self.breakpoints:
            return self.breakpoints[filename].get_breakpoints(lineno)
        return []

    def get_file_breaks(self, filename):
        filename = canonic(filename)
        if filename not in self.breakpoints:
            return []
        return [bp.line for bp in self.breakpoints[filename].all_breakpoints()]

    def has_breaks(self):
        # A code_bps dictionary b[f][l] may be empty and does not have empty
        # bplist values, see ModuleBreakpoints.delete_breakpoint().
        b = self.breakpoints
        return any(list(b[f][l].values()) for f in b for l in b[f])

    # Derived classes and clients can call the following method
    # to get a data structure representing a stack trace.

    def get_stack(self, f, t):
        stack = []
        if t and t.tb_frame is f:
            t = t.tb_next
        elif self.botframe:
            while t and not t.tb_frame is self.botframe:
                t = t.tb_next
        while f is not None:
            stack.append((f, f.f_lineno))
            if f is self.botframe:
                break
            f = f.f_back
        stack.reverse()
        i = max(0, len(stack) - 1)
        while t is not None:
            stack.append((t.tb_frame, t.tb_lineno))
            t = t.tb_next
        if f is None:
            i = max(0, len(stack) - 1)
        return stack, i

    def format_stack_entry(self, frame_lineno, lprefix=': '):
        frame, lineno = frame_lineno
        filename = canonic(frame.f_code.co_filename)
        s = '%s(%r)' % (filename, lineno)
        if frame.f_code.co_name:
            s += frame.f_code.co_name
        else:
            s += "<lambda>"
        locals = self.get_locals(frame)
        if '__args__' in locals:
            args = locals['__args__']
        else:
            args = None
        if args:
            s += safe_repr(args)
        else:
            s += '()'
        if '__return__' in locals:
            rv = locals['__return__']
            s += '->'
            s += safe_repr(rv)
        line = linecache.getline(filename, lineno, frame.f_globals)
        if line:
            s += lprefix + line.strip()
        return s

    # The following methods can be called by clients to use
    # a debugger to debug a statement or an expression.
    # Both can be given as a string, or a code object.

    def run(self, cmd, globals=None, locals=None):
        if globals is None:
            import __main__
            globals = __main__.__dict__
        if locals is None:
            locals = globals
        self.reset()
        if isinstance(cmd, str):
            cmd = compile(cmd, "<string>", "exec", 0, True)
        self.settrace(True)
        try:
            exec(cmd, globals, locals)
        except BdbQuit:
            pass
        finally:
            self.settrace(False)
            self.reset()

    def runeval(self, expr, globals=None, locals=None):
        if globals is None:
            import __main__
            globals = __main__.__dict__
        if locals is None:
            locals = globals
        self.reset()
        if isinstance(expr, str):
            expr = compile(expr, '<string>', 'eval', 0, True)
        self.settrace(True)
        try:
            return eval(expr, globals, locals)
        except BdbQuit:
            pass
        finally:
            self.settrace(False)
            self.reset()

    def runctx(self, cmd, globals, locals):
        # B/W compatibility
        self.run(cmd, globals, locals)

    # This method is more useful to debug a single function call.

    def runcall(self, func, *args, **kwds):
        self.reset(ignore_first_call_event=False)
        self.settrace(True)
        res = None
        try:
            res = func(*args, **kwds)
        except BdbQuit:
            pass
        finally:
            self.settrace(False)
            self.reset()
        return res


def set_trace():
    Bdb().set_trace()


class Breakpoint:
    """Breakpoint class.

    Implements temporary breakpoints, ignore counts, disabling and
    (re)-enabling, and conditionals.

    Breakpoints are indexed by number through bpbynumber.

    """

    next = 1        # Next bp to be assigned
    bpbynumber = [None] # Each entry is None or an instance of Bpt

    def __init__(self, file, line, module, temporary=False,
                cond=None):
        self.file = file    # This better be in canonical form!
        self.line = line
        self.module = module
        self.actual_bp = module.add_breakpoint(self)
        self.temporary = temporary
        self.cond = cond
        self.enabled = True
        self.ignore = 0
        self.hits = 0
        self.number = Breakpoint.next
        Breakpoint.next += 1
        self.bpbynumber.append(self)

    def deleteMe(self):
        if self.bpbynumber[self.number]:
            self.bpbynumber[self.number] = None   # No longer in list
            self.module.delete_breakpoint(self)

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def process_hit_event(self, frame):
        """Return (stop_state, delete_temporary) at a breakpoint hit event."""
        if not self.enabled:
            return False, False
        # Count every hit when breakpoint is enabled.
        self.hits += 1
        # A conditional breakpoint.
        if self.cond:
            try:
                if not eval_(self.cond, frame.f_globals, frame.f_locals):
                    return False, False
            except Exception:
                # If the breakpoint condition evaluation fails, the most
                # conservative thing is to stop on the breakpoint.  Don't
                # delete temporary, as another hint to the user.
                return True, False
        if self.ignore > 0:
            self.ignore -= 1
            return False, False
        return True, True

    def bpprint(self, out=None):
        if out is None:
            out = sys.stdout
        print(self.bpformat(), file=out)

    def bpformat(self):
        if self.temporary:
            disp = 'del  '
        else:
            disp = 'keep '
        if self.enabled:
            disp = disp + 'yes  '
        else:
            disp = disp + 'no   '
        ret = '%-4dbreakpoint   %s at %s:%d' % (self.number, disp,
                                                self.file, self.line)
        if self.cond:
            ret += '\n\tstop only if %s' % (self.cond,)
        if self.ignore:
            ret += '\n\tignore next %d hits' % (self.ignore,)
        if self.hits:
            if self.hits > 1:
                ss = 's'
            else:
                ss = ''
            ret += '\n\tbreakpoint already hit %d time%s' % (self.hits, ss)
        return ret

    def __str__(self):
        return 'breakpoint %s at %s:%s' % (self.number, self.file, self.line)


# -------------------- testing --------------------

class Tdb(Bdb):
    def user_call(self, frame, args):
        name = frame.f_code.co_name
        if not name: name = '???'
        print('+++ call', name, args)
    def user_line(self, frame):
        name = frame.f_code.co_name
        if not name: name = '???'
        fn = canonic(frame.f_code.co_filename)
        line = linecache.getline(fn, frame.f_lineno, frame.f_globals)
        print('+++', fn, frame.f_lineno, name, ':', line.strip())
    def user_return(self, frame, retval):
        print('+++ return', retval)
    def user_exception(self, frame, exc_stuff):
        print('+++ exception', exc_stuff)
        self.set_continue()

def foo(n):
    print('foo(', n, ')')
    x = bar(n*10)
    print('bar returned', x)

def bar(a):
    print('bar(', a, ')')
    return a/2

def test():
    t = Tdb()
    t.run('from pdb_clone import bdb; bdb.foo(10)')
