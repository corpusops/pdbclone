# vi:set ts=8 sts=4 sw=4 et tw=80:
#! /usr/bin/env python

"""
The Python Debugger Pdb
=======================

To use the debugger in its simplest form:

        >>> import pdb
        >>> pdb.run('<a statement>')

The debugger's prompt is '(Pdb) '.  This will stop in the first
function call in <a statement>.

Alternatively, if a statement terminated with an unhandled exception,
you can use pdb's post-mortem facility to inspect the contents of the
traceback:

        >>> <a statement>
        <exception traceback>
        >>> import pdb
        >>> pdb.pm()

The commands recognized by the debugger are listed in the next
section.  Most can be abbreviated as indicated; e.g., h(elp) means
that 'help' can be typed as 'h' or 'help' (but not as 'he' or 'hel',
nor as 'H' or 'Help' or 'HELP').  Optional arguments are enclosed in
square brackets.  Alternatives in the command syntax are separated
by a vertical bar (|).

A blank line repeats the previous command literally, except for
'list', where it lists the next 11 lines.

Commands that the debugger doesn't recognize are assumed to be Python
statements and are executed in the context of the program being
debugged.  Python statements can also be prefixed with an exclamation
point ('!').  This is a powerful way to inspect the program being
debugged; it is even possible to change variables or call functions.
When an exception occurs in such a statement, the exception name is
printed but the debugger's state is not changed.

The debugger supports aliases, which can save typing.  And aliases can
have parameters (see the alias help entry) which allows one a certain
level of adaptability to the context under examination.

Multiple commands may be entered on a single line, separated by the
pair ';;'.  No intelligence is applied to separating the commands; the
input is split at the first ';;', even if it is in the middle of a
quoted string.

If a file ".pdbrc" exists in your home directory or in the current
directory, it is read in and executed as if it had been typed at the
debugger prompt.  This is particularly useful for aliases.  If both
files exist, the one in the home directory is read first and aliases
defined there can be overriden by the local file.

Aside from aliases, the debugger is not directly programmable; but it
is implemented as a class from which you can derive your own debugger
class, which you can make as fancy as you like.


Debugger commands
=================

"""
# NOTE: the actual command documentation is collected from docstrings of the
# commands and is appended to __doc__ after the class has been defined.

# Python 2-3 compatibility.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import re
import sys
import cmd
import dis
import code
import glob
import pprint
import signal
import errno
import inspect
import importlib
if not hasattr(importlib, 'find_loader'):
    import imp
import traceback
import linecache
import socket
import readline
import shlex
import pydoc
from operator import attrgetter

from . import PY3, PY34, exec_, eval_, bdb

class Restart(Exception):
    """Causes a debugger to be restarted for the debugged python program."""
    pass

__all__ = ["run", "pm", "Pdb", "runeval", "runctx", "runcall", "set_trace",
           "set_trace_remote", "post_mortem", "help"]

def restart_call(func, *args):
    while 1:
        try:
            return func(*args)
        except IOError as e:
            if e.errno == errno.EINTR:
                continue
            raise

def user_method(user_event):
    """Decorator of the Pdb user_* methods that controls the RemoteSocket."""
    def wrapper(self, *args):
        stdin = self.stdin
        is_sock = isinstance(stdin, RemoteSocket)
        try:
            try:
                if is_sock and not stdin.connect():
                    return
                return user_event(self, *args)
            except Exception:
                self.close()
                raise
        finally:
            if is_sock and stdin.closed():
                self.do_detach(None)
    return wrapper

class RemoteSocket:
    """File like class that wraps the remote debugging socket."""

    ST_INIT, ST_CONNECTED, ST_CLOSED = tuple(range(3))

    def __init__(self, addr):
        self.addr = addr
        self.state = self.ST_INIT
        self.server = None
        self.socket = None
        self.madefile = None

    def connect(self):
        if self.state is self.ST_INIT:
            try:
                self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server.setsockopt(
                                    socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                # The default socket timeout setting may have been changed by
                # a call to socket.setdefaulttimeout().
                self.server.setblocking(True)
                self.server.bind(self.addr)
                restart_call(self.server.listen, 0)
                self.socket, _ = restart_call(self.server.accept)
                self.socket.setblocking(True)
                self.server.close()
                self.server = None
                # Do not use the preferred encoding as - a) both ends of the
                # socket may not have the same preferred encoding - b) the
                # debuggee may be playing tricks with the preferred encoding
                # as in test_universal_newlines_communicate_encodings of
                # test_subprocess.py.
                if PY3:
                    self.madefile = self.socket.makefile('rw', encoding='utf-8')
                else:
                    self.madefile = self.socket.makefile('rw')
            except KeyboardInterrupt:
                self.close()
            except IOError as e:
                self.close()
                if e.errno == errno.EADDRINUSE:
                    print('pdb.RemoteSocket:', str(e), file=sys.stderr)
                else:
                    raise
            else:
                self.state = self.ST_CONNECTED
                init = ''
                if hasattr(os, 'getpid'):
                    init += 'PROCESS_PID:%s\n' % os.getpid()
                try:
                    procname = sys.argv[0]
                except AttributeError:
                    # sys.argv is not defined in a sub-interpreter.
                    procname = '<unknown>'
                init += 'PROCESS_NAME:%s\n' % procname
                self.write(init)
        return self.state is self.ST_CONNECTED

    def readline(self):
        if self.madefile:
            try:
                line = restart_call(self.madefile.readline)
                if not line:
                    self.close()
                else:
                    return line
            except IOError:
                self.close()
                raise
        return ''

    def write(self, data):
        if self.madefile:
            try:
                return restart_call(self.madefile.write, data)
            except IOError:
                self.close()
                raise
        return 0

    def flush(self):
        if self.madefile:
            try:
                self.madefile.flush()
            except IOError:
                self.close()
                raise

    def closed(self):
        return self.state is not self.ST_CONNECTED

    def close(self):
        if self.state is self.ST_CONNECTED:
            try:
                self.write('%s socket closed by pdb.\n' % str(self.addr))
            except IOError:
                pass
        self.state = self.ST_CLOSED
        if self.madefile:
            self.madefile.close()
            self.madefile = None
        if self.socket:
            self.socket.close()
            self.socket = None
        if self.server:
            self.server.close()
            self.server = None

def find_function(funcname, filename):
    cre = re.compile(r'def\s+%s\s*[(]' % re.escape(funcname))
    try:
        fp = open(filename)
    except IOError:
        return None
    # consumer of this info expects the first line to be 1
    with fp:
        for lineno, line in enumerate(fp, start=1):
            if cre.match(line):
                return funcname, filename, lineno
    return None

def getsourcelines(obj, locals=None):
    lines, lineno = inspect.findsource(obj)
    if inspect.isframe(obj):
        if not locals:
            locals = obj.f_locals
        if obj.f_globals is locals:
            # Must be a module frame: do not try to cut a block out of it.
            return lines, 1
    elif inspect.ismodule(obj):
        return lines, 1
    return inspect.getblock(lines[lineno:]), lineno+1

def lasti2lineno(code, lasti):
    linestarts = list(dis.findlinestarts(code))
    linestarts.reverse()
    for i, lineno in linestarts:
        if lasti >= i:
            return lineno
    return 0

def get_module_fname(module_name, path=None, inpackage=None):
    if module_name in sys.modules:
        return getattr(sys.modules[module_name], '__file__', None)

    if inpackage is not None:
        fullmodule = '{}.{}'.format(inpackage, module_name)
    else:
        fullmodule = module_name

    i = module_name.rfind('.')
    if i >= 0:
        package = module_name[:i]
        submodule = module_name[i+1:]
        parent = get_module_fname(package, path, inpackage)
        if not parent:
            return None
        if inpackage is not None:
            package = '{}.{}'.format(inpackage, package)
        return get_module_fname(submodule, [os.path.dirname(parent)], package)

    if inpackage is not None:
        search_path = path
    else:
        search_path = sys.path
    if hasattr(importlib, 'find_loader'):
        try:
            loader = importlib.find_loader(fullmodule, search_path)
            if not loader:
                return None
        except (ImportError, ValueError):
            return None
        try:
            return loader.get_filename(fullmodule)
        except AttributeError:
            return None
    else:
        try:
            f, fname, (s, m, t) = imp.find_module(module_name, search_path)
            if f: f.close()
            if t == imp.PKG_DIRECTORY:
                f, fname, desc = imp.find_module('__init__', [fname])
                if f: f.close()
            return fname
        except ImportError:
            return None

def source_filename(filename):
    if filename:
        filename = os.path.abspath(filename)
        if filename[-4:].lower() in ('.pyc', '.pyo'):
            filename = filename[:-1]
        if os.path.exists(filename):
            return filename
    return None

def get_fqn_fname(fqn, frame):
    try:
        func = eval_(fqn, frame.f_globals)
    except Exception:
        # fqn is defined in a module not yet (fully) imported.
        module = inspect.getmodule(frame)
        candidate_tuples = []
        frame_fname = source_filename(get_module_fname(module.__name__))
        # Try first the current module for a function or method.
        if frame_fname:
            candidate_tuples.append((fqn, frame_fname))
        names = fqn.split('.')
        for i in range(len(names) - 1, 0, -1):
            filename = source_filename(get_module_fname('.'.join(names[:i])))
            if filename:
                candidate_tuples.append(('.'.join(names[i:]), filename))
        return candidate_tuples
    else:
        module_name = getattr(func, '__module__', None)
        if module_name is not None:
            try:
                filename = inspect.getfile(func)
            except TypeError:
                pass
            else:
                # Substitute the module name in fqn, for the case where the
                # fully qualified name refers to a name defined in an
                # 'import as' statement.
                names = fqn.split('.')
                if len(names) > 1:
                    names[0] = eval_(names[0], frame.f_globals).__name__
                    fqn = '.'.join(names)
                if fqn.startswith(module_name) and module_name != fqn:
                    fqn = fqn[len(module_name)+1:]
                return [(fqn, filename)]
    return []

class _rstr(str):
    """String that doesn't quote its repr."""
    def __repr__(self):
        return self


# Interaction prompt line will separate file and call info from code
# text using value of line_prefix string.  A newline and arrow may
# be to your liking.  You can set it once pdb is imported using the
# command "pdb.line_prefix = '\n% '".
# line_prefix = ': '    # Use this to get the old situation back
line_prefix = '\n-> '   # Probably a better default

class Pdb(bdb.Bdb, cmd.Cmd):

    _previous_sigint_handler = None

    def __init__(self, completekey='tab', stdin=None, stdout=None, skip=None,
                 nosigint=False, debug=False):
        bdb.Bdb.__init__(self, skip=skip)
        cmd.Cmd.__init__(self, completekey, stdin, stdout)
        if stdout:
            self.use_rawinput = 0
        self.prompt = '(Pdb) '
        self.pdb_thread = None
        self.is_debug_instance = debug
        self.closed = False
        self.aliases = {}
        self.displaying = {}
        self.mainpyfile = ''
        self.tb_lineno = {}
        self.forget()
        # Try to load readline if it exists
        try:
            # remove some common file name delimiters
            readline.set_completer_delims(' \t\n`@#$%^&*()=+[{]}\\|;:\'",<>?')
        except ImportError:
            pass
        self.allow_kbdint = False
        self.nosigint = nosigint

        # Read $HOME/.pdbrc and ./.pdbrc
        self.rcLines = []
        if 'HOME' in os.environ:
            envHome = os.environ['HOME']
            try:
                with open(os.path.join(envHome, ".pdbrc")) as rcFile:
                    self.rcLines.extend(rcFile)
            except IOError:
                pass
        try:
            with open(".pdbrc") as rcFile:
                self.rcLines.extend(rcFile)
        except IOError:
            pass

        self.commands = {} # associates a command list to breakpoint numbers
        self.commands_doprompt = {} # for each bp num, tells if the prompt
                                    # must be disp. after execing the cmd list
        self.commands_silent = {} # for each bp num, tells if the stack trace
                                  # must be disp. after execing the cmd list
        self.commands_defining = False # True while in the process of defining
                                       # a command list
        self.commands_bnum = None # The breakpoint number for which we are
                                  # defining a list

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        if isinstance(self.stdin, RemoteSocket) and not self.is_debug_instance:
            self.stdin.close()
        if self._previous_sigint_handler:
            signal.signal(signal.SIGINT, self._previous_sigint_handler)

    def sigint_handler(self, signum, frame):
        if self.allow_kbdint:
            raise KeyboardInterrupt
        self.message("\nProgram interrupted. (Use 'cont' to resume).")
        self.set_trace(frame)

    def set_sigint_handler(self):
        if not self.nosigint:
            try:
                Pdb._previous_sigint_handler = \
                    signal.signal(signal.SIGINT, self.sigint_handler)
            except ValueError:
                # ValueError happens when do_continue() is invoked from
                # a non-main thread in which case we just continue without
                # SIGINT set. Would printing a message here (once) make
                # sense?
                if not sys.gettrace():
                    self.message('The trace function has been removed and'
                            ' this non-main thread cannot be interrupted.')
                    self.close()

    def forget(self):
        self.lineno = None
        self.stack = []
        self.curindex = 0
        self.curframe = None
        self.tb_lineno.clear()
        self.current_thread = None

    def setup(self, f, tb):
        self.forget()
        self.stack, self.curindex = self.get_stack(f, tb)
        while tb:
            # when setting up post-mortem debugging with a traceback, save all
            # the original line numbers to be displayed along the current line
            # numbers (which can be different, e.g. due to finally clauses)
            lineno = lasti2lineno(tb.tb_frame.f_code, tb.tb_lasti)
            self.tb_lineno[tb.tb_frame] = lineno
            tb = tb.tb_next
        self.curframe = self.stack[self.curindex][0]
        return self.execRcLines()

    # Can be executed earlier than 'setup' if desired
    def execRcLines(self):
        if not self.rcLines:
            return
        # local copy because of recursion
        rcLines = self.rcLines
        rcLines.reverse()
        # execute every line only once
        self.rcLines = []
        while rcLines:
            line = rcLines.pop().strip()
            if line and line[0] != '#':
                if self.onecmd(line):
                    # if onecmd returns True, the command wants to exit
                    # from the interaction, save leftover rc lines
                    # to execute before next interaction
                    self.rcLines += reversed(rcLines)
                    return True

    # Override Bdb methods

    @user_method
    def user_call(self, frame, argument_list):
        """This method is called when there is the remote possibility
        that we ever need to stop in this function."""
        self.message('--Call--')
        self.interaction(frame, None)

    @user_method
    def user_line(self, frame, breakpoint_hits=None):
        """This function is called when we stop or break at this line."""
        if not breakpoint_hits:
            self.interaction(frame, None)
        else:
            commands_result = self.bp_commands(frame, breakpoint_hits)
            if not commands_result:
                self.interaction(frame, None)
            else:
                doprompt, silent = commands_result
                if not silent:
                    self.print_stack_entry(self.stack[self.curindex])
                if doprompt:
                    self._cmdloop()
                self.forget()

    def bp_commands(self, frame, breakpoint_hits):
        """Call every command that was set for the current active breakpoints.

        Returns True if the normal interaction function must be called,
        False otherwise."""
        # Handle multiple breakpoints on the same line (issue 14789)
        effective_bp_list, temporaries = breakpoint_hits
        silent = True
        doprompt = False
        atleast_one_cmd = False
        for bp in effective_bp_list:
            if bp in self.commands:
                if not atleast_one_cmd:
                    atleast_one_cmd = True
                    self.setup(frame, None)
                lastcmd_back = self.lastcmd
                for line in self.commands[bp]:
                    self.onecmd(line)
                self.lastcmd = lastcmd_back
                if not self.commands_silent[bp]:
                    silent = False
                if self.commands_doprompt[bp]:
                    doprompt = True
        # Delete the temporary breakpoints.
        tmp_to_delete = ' '.join(str(bp) for bp in temporaries)
        if tmp_to_delete:
            self.do_clear(tmp_to_delete)

        if atleast_one_cmd:
            return doprompt, silent
        return None

    @user_method
    def user_return(self, frame, return_value):
        """This function is called when a return trap is set here."""
        frame.f_locals['__return__'] = return_value
        self.message('--Return--')
        self.interaction(frame, None)

    @user_method
    def user_exception(self, frame, exc_info):
        """This function is called if an exception occurs,
        but only if we are to stop at or just below this level."""
        exc_type, exc_value, exc_traceback = exc_info
        frame.f_locals['__exception__'] = exc_type, exc_value

        # An 'Internal StopIteration' exception is an exception debug event
        # issued by the interpreter when handling a subgenerator run with
        # 'yield from' or a generator controled by a for loop. No exception has
        # actually occured in this case. The debugger uses this debug event to
        # stop when the debuggee is returning from such generators.
        prefix = 'Internal ' if (PY34 and not exc_traceback
                                    and exc_type is StopIteration) else ''
        self.message('--Exception--\n%s%s' % (prefix,
            traceback.format_exception_only(exc_type, exc_value)[-1].strip()))
        self.interaction(frame, exc_traceback)

    # General interaction function
    def _cmdloop(self):
        while True:
            try:
                # keyboard interrupts allow for an easy way to cancel
                # the current command, so allow them during interactive input
                self.allow_kbdint = True
                self.cmdloop()
                self.allow_kbdint = False
                break
            except KeyboardInterrupt:
                self.message('--KeyboardInterrupt--')

    # Called before loop, handles display expressions
    def preloop(self):
        displaying = self.displaying.get(self.curframe)
        if displaying:
            for expr, oldvalue in displaying.items():
                newvalue = self._getval_except(expr)
                # check for identity first; this prevents custom __eq__ to
                # be called at every loop, and also prevents instances whose
                # fields are changed to be displayed
                if newvalue is not oldvalue and newvalue != oldvalue:
                    displaying[expr] = newvalue
                    self.message('display %s: %s  [old: %s]' %
                         (expr, bdb.safe_repr(newvalue),
                          bdb.safe_repr(oldvalue)))

    def interaction(self, frame, traceback):
        # restore previous signal handler
        if self._previous_sigint_handler:
            signal.signal(signal.SIGINT, self._previous_sigint_handler)
        if self.setup(frame, traceback):
            # no interaction desired at this time (happens if .pdbrc contains
            # a command like "continue")
            self.forget()
            return
        self.print_stack_entry(self.stack[self.curindex])
        try:
            self.pdb_toplevel_frame = frame
            self._cmdloop()
        finally:
            self.pdb_toplevel_frame = None
            self.forget()

    def displayhook(self, obj):
        """Custom displayhook for the exec in default(), which prevents
        assignment of the _ variable in the builtins.
        """
        # reproduce the behavior of the standard displayhook, not printing None
        if obj is not None:
            self.message(repr(obj))

    def redirect(self, func, *args, **kwds):
        # When Pdb has been instantiated in a subinterpreter, the redirection
        # must be done with the sys module of the main interpreter, not the
        # one of the subinterpreter.
        if PY3:
            import sys as _sys
        else:
            # Parent module 'pdb_clone' not found while handling absolute
            # import.
            _sys = __import__('sys', level=0)

        save_stdout = _sys.stdout
        save_stderr = _sys.stderr
        save_stdin = _sys.stdin
        save_displayhook = _sys.displayhook
        _sys.stdin = self.stdin
        _sys.stdout = self.stdout
        _sys.stderr = self.stdout
        _sys.displayhook = self.displayhook
        try:
            func(*args, **kwds)
        finally:
            _sys.stdout = save_stdout
            _sys.stderr = save_stderr
            _sys.stdin = save_stdin
            _sys.displayhook = save_displayhook

    def default(self, line):
        if line[:1] == '!': line = line[1:]
        locals = self.get_locals(self.curframe)
        ns = self.curframe.f_globals.copy()
        ns.update(locals)
        try:
            code = compile(line + '\n', '<stdin>', 'single', 0, True)
            self.redirect(exec_, code, ns, locals)
        except Exception:
            exc_info = sys.exc_info()[:2]
            self.error(traceback.format_exception_only(*exc_info)[-1].strip())

    def precmd(self, line):
        """Handle alias expansion and ';;' separator."""
        if not line.strip():
            return line
        args = line.split()
        while args[0] in self.aliases:
            line = self.aliases[args[0]]
            ii = 1
            for tmpArg in args[1:]:
                line = line.replace("%" + str(ii),
                                      tmpArg)
                ii += 1
            line = line.replace("%*", ' '.join(args[1:]))
            args = line.split()
        # split into ';;' separated commands
        # unless it's an alias command
        if args[0] != 'alias':
            marker = line.find(';;')
            if marker >= 0:
                # queue up everything after marker
                next = line[marker+2:].lstrip()
                self.cmdqueue.append(next)
                line = line[:marker].rstrip()
        return line

    def onecmd(self, line):
        """Interpret the argument as though it had been typed in response
        to the prompt.

        Checks whether this line is typed at the normal prompt or in
        a breakpoint command list definition.
        """
        if not self.commands_defining:
            return cmd.Cmd.onecmd(self, line)
        else:
            return self.handle_command_def(line)

    def handle_command_def(self, line):
        """Handles one command line during command list definition."""
        cmd, arg, line = self.parseline(line)
        if not cmd:
            return
        if cmd == 'silent':
            self.commands_silent[self.commands_bnum] = True
            return # continue to handle other cmd def in the cmd list
        elif cmd == 'end':
            self.cmdqueue = []
            return 1 # end of cmd list
        cmdlist = self.commands[self.commands_bnum]
        if arg:
            cmdlist.append(cmd+' '+arg)
        else:
            cmdlist.append(cmd)
        # Determine if we must stop
        try:
            func = getattr(self, 'do_' + cmd)
        except AttributeError:
            func = self.default
        # one of the resuming commands
        if func.__name__ in self.commands_resuming:
            self.commands_doprompt[self.commands_bnum] = False
            self.cmdqueue = []
            return 1
        return

    # interface abstraction functions

    def message(self, msg):
        print(msg, file=self.stdout)

    def error(self, msg):
        print('***', msg, file=self.stdout)

    # Generic completion functions.  Individual complete_foo methods can be
    # assigned below to one of these functions.

    def _complete_location(self, text, line, begidx, endidx):
        # Complete a file/module/function location for break/tbreak/clear.
        if line.strip().endswith((':', ',')):
            # Here comes a line number or a condition which we can't complete.
            return []
        # First, try to find matching functions (i.e. expressions).
        try:
            ret = self._complete_expression(text, line, begidx, endidx)
        except Exception:
            ret = []
        # Then, try to complete file names as well.
        globs = glob.glob(text + '*')
        for fn in globs:
            if os.path.isdir(fn):
                ret.append(fn + '/')
            elif os.path.isfile(fn) and fn.lower().endswith(('.py', '.pyw')):
                ret.append(fn + ':')
        return ret

    def _complete_bpnumber(self, text, line, begidx, endidx):
        # Complete a breakpoint number.  (This would be more helpful if we could
        # display additional info along with the completions, such as file/line
        # of the breakpoint.)
        return [str(i) for i, bp in enumerate(bdb.Breakpoint.bpbynumber)
                if bp is not None and str(i).startswith(text)]

    def _complete_expression(self, text, line, begidx, endidx):
        # Complete an arbitrary expression.
        if not self.curframe:
            return []
        # Collect globals and locals.  It is usually not really sensible to also
        # complete builtins, and they clutter the namespace quite heavily, so we
        # leave them out.
        ns = self.curframe.f_globals.copy()
        ns.update(self.get_locals(self.curframe))
        if '.' in text:
            # Walk an attribute chain up to the last part, similar to what
            # rlcompleter does.  This will bail if any of the parts are not
            # simple attribute access, which is what we want.
            dotted = text.split('.')
            try:
                obj = ns[dotted[0]]
                for part in dotted[1:-1]:
                    obj = getattr(obj, part)
            except (KeyError, AttributeError):
                return []
            prefix = '.'.join(dotted[:-1]) + '.'
            return [prefix + n for n in dir(obj) if n.startswith(dotted[-1])]
        else:
            # Complete a simple name.
            return [n for n in ns.keys() if n.startswith(text)]

    # Command definitions, called by cmdloop()
    # The argument is the remaining string on the command line
    # Return true to exit from the command loop

    def do_commands(self, arg):
        """commands [bpnumber]
        (com) ...
        (com) end
        (Pdb)

        Specify a list of commands for breakpoint number bpnumber.
        The commands themselves are entered on the following lines.
        Type a line containing just 'end' to terminate the commands.
        The commands are executed when the breakpoint is hit.

        To remove all commands from a breakpoint, type commands and
        follow it immediately with end; that is, give no commands.

        With no bpnumber argument, commands refers to the last
        breakpoint set.

        You can use breakpoint commands to start your program up
        again.  Simply use the continue command, or step, or any other
        command that resumes execution.

        Specifying any command resuming execution (currently continue,
        step, next, return, jump, quit and their abbreviations)
        terminates the command list (as if that command was
        immediately followed by end).  This is because any time you
        resume execution (even with a simple next or step), you may
        encounter another breakpoint -- which could have its own
        command list, leading to ambiguities about which list to
        execute.

        If you use the 'silent' command in the command list, the usual
        message about stopping at a breakpoint is not printed.  This
        may be desirable for breakpoints that are to print a specific
        message and then continue.  If none of the other commands
        print anything, you will see no sign that the breakpoint was
        reached.
        """
        if not arg:
            bnum = len(bdb.Breakpoint.bpbynumber) - 1
        else:
            try:
                bnum = int(arg)
            except Exception:
                self.error("Usage: commands [bnum]\n        ...\n        end")
                return
        self.commands_bnum = bnum
        # Save old definitions for the case of a keyboard interrupt.
        if bnum in self.commands:
            old_command_defs = (self.commands[bnum],
                                self.commands_doprompt[bnum],
                                self.commands_silent[bnum])
        else:
            old_command_defs = None
        self.commands[bnum] = []
        self.commands_doprompt[bnum] = True
        self.commands_silent[bnum] = False

        prompt_back = self.prompt
        self.prompt = '(com) '
        self.commands_defining = True
        try:
            self.cmdloop()
        except KeyboardInterrupt:
            # Restore old definitions.
            if old_command_defs:
                self.commands[bnum] = old_command_defs[0]
                self.commands_doprompt[bnum] = old_command_defs[1]
                self.commands_silent[bnum] = old_command_defs[2]
            else:
                del self.commands[bnum]
                del self.commands_doprompt[bnum]
                del self.commands_silent[bnum]
            self.error('command definition aborted, old commands restored')
        finally:
            self.commands_defining = False
            self.prompt = prompt_back

    complete_commands = _complete_bpnumber

    def do_break(self, arg, temporary = 0):
        """b(reak) [ ([filename:]lineno | function) [, condition] ]
        Without argument, list all breaks.

        With a line number argument, set a break at this line in the
        current file.  With a function name, set a break at the first
        executable line of that function.  If a second argument is
        present, it is a string specifying an expression which must
        evaluate to true before the breakpoint is honored.

        The line number may be prefixed with a filename and a colon,
        to specify a breakpoint in another file (probably one that
        hasn't been loaded yet).  The file is searched for on
        sys.path; the .py suffix may be omitted.
        """
        if not arg:
            all_breaks = '\n'.join(bp.bpformat() for bp in
                                bdb.Breakpoint.bpbynumber if bp)
            if all_breaks:
                self.message("Num Type         Disp Enb   Where")
                self.message(all_breaks)
            return

        # Parse arguments, comma has lowest precedence and cannot occur in
        # filename.
        args = arg.rsplit(',', 1)
        cond =  args[1].strip() if len(args) == 2 else None
        # Parse stuff before comma: [filename:]lineno | function.
        args = args[0].rsplit(':', 1)
        name = args[0].strip()
        lineno =  args[1] if len(args) == 2 else args[0]
        try:
            lineno = int(lineno)
        except ValueError:
            if len(args) == 2:
                self.error('Bad lineno: "{}".'.format(lineno))
            else:
                # Attempt the list of possible function or method fully
                # qualified names and corresponding filenames.
                candidates = get_fqn_fname(name, self.curframe)
                for fqn, fname in candidates:
                    try:
                        bp = self.set_break(fname, None, temporary, cond, fqn)
                        self.message('Breakpoint {:d} at {}:{:d}'.format(
                                                bp.number, bp.file, bp.line))
                        return
                    except bdb.BdbError:
                        pass
                if not candidates:
                    self.error(
                        'Not a function or a built-in: "{}"'.format(name))
                else:
                    self.error('Bad name: "{}".'.format(name))
        else:
            filename = self.curframe.f_code.co_filename
            if len(args) == 2 and name:
                filename = name
            if filename.startswith('<') and filename.endswith('>'):
                # allow <doctest name>: doctest installs a hook at
                # linecache.getlines to allow <doctest name> to be
                # linecached and readable.
                if filename == '<string>' and self.mainpyfile:
                    filename = self.mainpyfile
            else:
                root, ext = os.path.splitext(filename)
                if ext == '':
                    filename = filename + '.py'
                if not os.path.exists(filename):
                    self.error('Bad filename: "{}".'.format(arg))
                    return
            try:
                bp = self.set_break(filename, lineno, temporary, cond)
            except bdb.BdbError as err:
                self.error(err)
            else:
                self.message('Breakpoint {:d} at {}:{:d}'.format(
                                        bp.number, bp.file, bp.line))

    # To be overridden in derived debuggers
    def defaultFile(self):
        """Produce a reasonable default."""
        filename = self.curframe.f_code.co_filename
        if filename == '<string>' and self.mainpyfile:
            filename = self.mainpyfile
        return filename

    do_b = do_break

    complete_break = _complete_location
    complete_b = _complete_location

    def do_tbreak(self, arg):
        """tbreak [ ([filename:]lineno | function) [, condition] ]
        Same arguments as break, but sets a temporary breakpoint: it
        is automatically deleted when first hit.
        """
        self.do_break(arg, 1)

    complete_tbreak = _complete_location

    def done_breakpoint_state(self, bp, state):
        name = 'Enabled' if state else 'Disabled'
        self.message('%s %s' % (name, bp))

    def do_enable(self, arg):
        """enable bpnumber [bpnumber ...]
        Enables the breakpoints given as a space separated list of
        breakpoint numbers.
        """
        args = arg.split()
        for i in args:
            try:
                bp = self.get_bpbynumber(i)
            except ValueError as err:
                self.error(err)
            else:
                bp.enable()
                self.done_breakpoint_state(bp, True)

    complete_enable = _complete_bpnumber

    def do_disable(self, arg):
        """disable bpnumber [bpnumber ...]
        Disables the breakpoints given as a space separated list of
        breakpoint numbers.  Disabling a breakpoint means it cannot
        cause the program to stop execution, but unlike clearing a
        breakpoint, it remains in the list of breakpoints and can be
        (re-)enabled.
        """
        args = arg.split()
        for i in args:
            try:
                bp = self.get_bpbynumber(i)
            except ValueError as err:
                self.error(err)
            else:
                bp.disable()
                self.done_breakpoint_state(bp, False)

    complete_disable = _complete_bpnumber

    def do_condition(self, arg):
        """condition bpnumber [condition]
        Set a new condition for the breakpoint, an expression which
        must evaluate to true before the breakpoint is honored.  If
        condition is absent, any existing condition is removed; i.e.,
        the breakpoint is made unconditional.
        """
        args = arg.split(' ', 1)
        try:
            cond = args[1]
        except IndexError:
            cond = None
        try:
            bp = self.get_bpbynumber(args[0].strip())
        except IndexError:
            self.error('Breakpoint number expected')
        except ValueError as err:
            self.error(err)
        else:
            bp.cond = cond
            if not cond:
                self.message('Breakpoint %d is now unconditional.' % bp.number)
            else:
                self.message('New condition set for breakpoint %d.' % bp.number)

    complete_condition = _complete_bpnumber

    def do_ignore(self, arg):
        """ignore bpnumber [count]
        Set the ignore count for the given breakpoint number.  If
        count is omitted, the ignore count is set to 0.  A breakpoint
        becomes active when the ignore count is zero.  When non-zero,
        the count is decremented each time the breakpoint is reached
        and the breakpoint is not disabled and any associated
        condition evaluates to true.
        """
        args = arg.split(' ', 1)
        try:
            count = int(args[1].strip())
        except Exception:
            count = 0
        try:
            bp = self.get_bpbynumber(args[0].strip())
        except IndexError:
            self.error('Breakpoint number expected')
        except ValueError as err:
            self.error(err)
        else:
            bp.ignore = count
            if count > 0:
                if count > 1:
                    countstr = '%d crossings' % count
                else:
                    countstr = '1 crossing'
                self.message('Will ignore next %s of breakpoint %d.' %
                             (countstr, bp.number))
            else:
                self.message('Will stop next time breakpoint %d is reached.'
                             % bp.number)

    complete_ignore = _complete_bpnumber

    def done_delete_breakpoint(self, bp):
        self.message('Deleted %s' % bp)

    def do_clear(self, arg):
        """cl(ear) filename:lineno\ncl(ear) [bpnumber [bpnumber...]]
        With a space separated list of breakpoint numbers, clear
        those breakpoints.  Without argument, clear all breaks (but
        first ask confirmation).  With a filename:lineno argument,
        clear all breaks at that line in that file.
        """
        if not arg:
            try:
                if PY3:
                    reply = input('Clear all breaks? ')
                else:
                    reply = raw_input('Clear all breaks? ')
            except EOFError:
                reply = 'no'
            reply = reply.strip().lower()
            if reply in ('y', 'yes'):
                bplist = [bp for bp in bdb.Breakpoint.bpbynumber if bp]
                self.clear_all_breaks()
                for bp in bplist:
                    self.done_delete_breakpoint(bp)
            return
        if ':' in arg:
            # Make sure it works for "clear C:\foo\bar.py:12"
            i = arg.rfind(':')
            filename = arg[:i]
            arg = arg[i+1:]
            try:
                lineno = int(arg)
            except ValueError:
                err = "Invalid line number (%s)" % arg
            else:
                bplist = self.get_breaks(filename, lineno)
                err = self.clear_break(filename, lineno)
            if err:
                self.error(err)
            else:
                for bp in bplist:
                    self.done_delete_breakpoint(bp)
            return
        numberlist = arg.split()
        for i in numberlist:
            try:
                bp = self.get_bpbynumber(i)
            except ValueError as err:
                self.error(err)
            else:
                self.clear_bpbynumber(i)
                self.done_delete_breakpoint(bp)
    do_cl = do_clear # 'c' is already an abbreviation for 'continue'

    complete_clear = _complete_location
    complete_cl = _complete_location

    def do_where(self, arg):
        """w(here)
        Print a stack trace, with the most recent frame at the bottom.
        An arrow indicates the "current frame", which determines the
        context of most commands.  'bt' is an alias for this command.
        """
        self.print_stack_trace()
    do_w = do_where
    do_bt = do_where

    def _select_frame(self, number):
        assert 0 <= number < len(self.stack)
        self.curindex = number
        self.curframe = self.stack[self.curindex][0]
        self.print_stack_entry(self.stack[self.curindex])
        self.lineno = None

    def do_up(self, arg):
        """u(p) [count]
        Move the current frame count (default one) levels up in the
        stack trace (to an older frame).
        """
        if self.curindex == 0:
            self.error('Oldest frame')
            return
        try:
            count = int(arg or 1)
        except ValueError:
            self.error('Invalid frame count (%s)' % arg)
            return
        if count < 0:
            newframe = 0
        else:
            newframe = max(0, self.curindex - count)
        self._select_frame(newframe)
    do_u = do_up

    def do_down(self, arg):
        """d(own) [count]
        Move the current frame count (default one) levels down in the
        stack trace (to a newer frame).
        """
        if self.curindex + 1 == len(self.stack):
            self.error('Newest frame')
            return
        try:
            count = int(arg or 1)
        except ValueError:
            self.error('Invalid frame count (%s)' % arg)
            return
        if count < 0:
            newframe = len(self.stack) - 1
        else:
            newframe = min(len(self.stack) - 1, self.curindex + count)
        self._select_frame(newframe)
    do_d = do_down

    def do_until(self, arg):
        """unt(il) [lineno]
        Without argument, continue execution until the line with a
        number greater than the current one is reached.  With a line
        number, continue execution until a line with a number greater
        or equal to that is reached.  In both cases, also stop when
        the current frame returns.
        """
        if arg:
            try:
                lineno = int(arg)
            except ValueError:
                self.error('Error in argument: %r' % arg)
                return
            if lineno <= self.curframe.f_lineno:
                self.error('"until" line number is smaller than current '
                           'line number')
                return
        else:
            lineno = None
        self.set_until(self.curframe, lineno)
        self.set_sigint_handler()
        return 1
    do_unt = do_until

    def do_step(self, arg):
        """s(tep)
        Execute the current line, stop at the first possible occasion
        (either in a function that is called or in the current
        function).
        """
        self.set_step()
        self.set_sigint_handler()
        return 1
    do_s = do_step

    def do_next(self, arg):
        """n(ext)
        Continue execution until the next line in the current function
        is reached or it returns.
        """
        self.set_next(self.curframe)
        self.set_sigint_handler()
        return 1
    do_n = do_next

    def do_run(self, arg):
        """run [args...]
        Restart the debugged python program. If a string is supplied
        it is splitted with "shlex", and the result is used as the new
        sys.argv.  History, breakpoints, actions and debugger options
        are preserved.  "restart" is an alias for "run".
        """
        if arg:
            argv0 = sys.argv[0:1]
            sys.argv = shlex.split(arg)
            sys.argv[:0] = argv0
        # this is caught in the main debugger loop
        raise Restart

    do_restart = do_run

    def do_return(self, arg):
        """r(eturn)
        Continue execution until the current function returns.
        """
        self.set_return(self.curframe)
        self.set_sigint_handler()
        return 1
    do_r = do_return

    def do_continue(self, arg):
        """c(ont(inue))
        Continue execution, only stop when a breakpoint is encountered.
        """
        self.set_continue()
        self.set_sigint_handler()
        return 1
    do_c = do_cont = do_continue

    def do_jump(self, arg):
        """j(ump) lineno
        Set the next line that will be executed.  Only available in
        the bottom-most frame.  This lets you jump back and execute
        code again, or jump forward to skip code that you don't want
        to run.

        It should be noted that not all jumps are allowed -- for
        instance it is not possible to jump into the middle of a
        for loop or out of a finally clause.
        """
        if self.curindex + 1 != len(self.stack):
            self.error('You can only jump within the bottom frame')
            return
        try:
            arg = int(arg)
        except ValueError:
            self.error("The 'jump' command requires a line number")
        else:
            try:
                # Do the jump, fix up our copy of the stack, and display the
                # new position
                self.curframe.f_lineno = arg
                self.stack[self.curindex] = self.stack[self.curindex][0], arg
                self.print_stack_entry(self.stack[self.curindex])
            except ValueError as e:
                self.error('Jump failed: %s' % e)
    do_j = do_jump

    def do_debug(self, arg):
        """debug code
        Enter a recursive debugger that steps through the code
        argument (which is an arbitrary expression or statement to be
        executed in the current environment).
        """
        self.settrace(False)
        globals = self.curframe.f_globals
        locals = self.get_locals(self.curframe)
        p = Pdb(self.completekey, self.stdin, self.stdout, debug=True)
        p.prompt = "(%s) " % self.prompt.strip()
        self.message("ENTERING RECURSIVE DEBUGGER")
        sys.call_tracing(p.run, (arg, globals, locals))
        self.message("LEAVING RECURSIVE DEBUGGER")
        self.settrace(True)
        self.lastcmd = p.lastcmd

    complete_debug = _complete_expression

    def do_detach(self, arg):
        """detach
        Release the process from pdb control. Detaching the process continues
        its execution.
        """
        self.clear_all_breaks()
        self.set_continue()
        self.close()
        return 1

    def do_quit(self, arg):
        """q(uit)\nexit
        Quit from the debugger. The program being executed is aborted.
        """
        if isinstance(self.stdin, RemoteSocket) and not self.is_debug_instance:
            return self.do_detach(arg)
        self._user_requested_quit = True
        self.set_quit()
        return 1

    do_q = do_quit
    do_exit = do_quit

    def do_EOF(self, arg):
        """EOF
        Handles the receipt of EOF as a command.
        """
        self.message('')
        return self.do_quit(arg)

    def do_args(self, arg):
        """a(rgs)
        Print the argument list of the current function.
        """
        co = self.curframe.f_code
        dict = self.get_locals(self.curframe)
        n = co.co_argcount
        if co.co_flags & 4: n = n+1
        if co.co_flags & 8: n = n+1
        for i in range(n):
            name = co.co_varnames[i]
            if name in dict:
                self.message('%s = %s' % (name, bdb.safe_repr(dict[name])))
            else:
                self.message('%s = *** undefined ***' % (name,))
    do_a = do_args

    def do_retval(self, arg):
        """retval
        Print the return value for the last return of a function.
        """
        locals = self.get_locals(self.curframe)
        if '__return__' in locals:
            self.message(bdb.safe_repr(locals['__return__']))
        else:
            self.error('Not yet returned!')
    do_rv = do_retval

    def _getval(self, arg):
        try:
            return eval_(arg, self.curframe.f_globals,
                            self.get_locals(self.curframe))
        except Exception:
            exc_info = sys.exc_info()[:2]
            self.error(traceback.format_exception_only(*exc_info)[-1].strip())
            raise

    def _getval_except(self, arg, frame=None):
        try:
            if frame is None:
                return eval_(arg, self.curframe.f_globals,
                                self.get_locals(self.curframe))
            else:
                return eval_(arg, frame.f_globals, frame.f_locals)
        except Exception:
            exc_info = sys.exc_info()[:2]
            err = traceback.format_exception_only(*exc_info)[-1].strip()
            return _rstr('** raised %s **' % err)

    def do_p(self, arg):
        """p expression
        Print the value of the expression.
        """
        try:
            self.message(bdb.safe_repr(self._getval(arg)))
        except Exception:
            pass

    def do_pp(self, arg):
        """pp expression
        Pretty-print the value of the expression.
        """
        obj = self._getval(arg)
        try:
            repr(obj)
        except Exception:
            self.message(bdb.safe_repr(obj))
        else:
            self.message(pprint.pformat(obj))

    complete_print = _complete_expression
    complete_p = _complete_expression
    complete_pp = _complete_expression

    def do_list(self, arg):
        """l(ist) [first [,last] | .]

        List source code for the current file.  Without arguments,
        list 11 lines around the current line or continue the previous
        listing.  With . as argument, list 11 lines around the current
        line.  With one argument, list 11 lines starting at that line.
        With two arguments, list the given range; if the second
        argument is less than the first, it is a count.

        The current line in the current frame is indicated by "->".
        If an exception is being debugged, the line where the
        exception was originally raised or propagated is indicated by
        ">>", if it differs from the current line.
        """
        self.lastcmd = 'list'
        last = None
        if arg and arg != '.':
            try:
                if ',' in arg:
                    first, last = arg.split(',')
                    first = int(first.strip())
                    last = int(last.strip())
                    if last < first:
                        # assume it's a count
                        last = first + last
                else:
                    first = int(arg.strip())
                    first = max(1, first - 5)
            except ValueError:
                self.error('Error in argument: %r' % arg)
                return
        elif self.lineno is None or arg == '.':
            first = max(1, self.curframe.f_lineno - 5)
        else:
            first = self.lineno + 1
        if last is None:
            last = first + 10
        filename = self.curframe.f_code.co_filename
        breaklist = self.get_file_breaks(filename)
        try:
            lines = linecache.getlines(filename, self.curframe.f_globals)
            self._print_lines(lines[first-1:last], first, breaklist,
                              self.curframe)
            self.lineno = min(last, len(lines))
            if len(lines) < last:
                self.message('[EOF]')
        except KeyboardInterrupt:
            pass
    do_l = do_list

    def do_longlist(self, arg):
        """longlist | ll
        List the whole source code for the current function or frame.
        """
        filename = self.curframe.f_code.co_filename
        breaklist = self.get_file_breaks(filename)
        try:
            lines, lineno = getsourcelines(self.curframe,
                                self.get_locals(self.curframe))
        except IOError as err:
            self.error(err)
            return
        self._print_lines(lines, lineno, breaklist, self.curframe)
    do_ll = do_longlist

    def do_source(self, arg):
        """source expression
        Try to get source code for the given object and display it.
        """
        try:
            obj = self._getval(arg)
        except Exception:
            return
        try:
            lines, lineno = getsourcelines(obj, self.get_locals(self.curframe))
        except (IOError, TypeError) as err:
            self.error(err)
            return
        self._print_lines(lines, lineno)

    complete_source = _complete_expression

    def _print_lines(self, lines, start, breaks=(), frame=None):
        """Print a range of lines."""
        if frame:
            current_lineno = frame.f_lineno
            exc_lineno = self.tb_lineno.get(frame, -1)
        else:
            current_lineno = exc_lineno = -1
        for lineno, line in enumerate(lines, start):
            s = str(lineno).rjust(3)
            if len(s) < 4:
                s += ' '
            if lineno in breaks:
                s += 'B'
            else:
                s += ' '
            if lineno == current_lineno:
                s += '->'
            elif lineno == exc_lineno:
                s += '>>'
            self.message(s + '\t' + line.rstrip())

    def do_whatis(self, arg):
        """whatis arg
        Print the type of the argument.
        """
        try:
            value = self._getval(arg)
        except Exception:
            # _getval() already printed the error
            return
        code = None
        # Is it a function?
        try:
            code = value.__code__
        except Exception:
            pass
        if code:
            self.message('Function %s' % code.co_name)
            return
        # Is it an instance method?
        try:
            code = value.__func__.__code__
        except Exception:
            pass
        if code:
            self.message('Method %s' % code.co_name)
            return
        # Is it a class?
        if value.__class__ is type:
            self.message('Class %s.%s' % (value.__module__, value.__name__))
            return
        # None of the above...
        self.message(type(value))

    complete_whatis = _complete_expression

    def do_display(self, arg):
        """display [expression]

        Display the value of the expression if it changed, each time execution
        stops in the current frame.

        Without expression, list all display expressions for the current frame.
        """
        if not arg:
            self.message('Currently displaying:')
            for item in self.displaying.get(self.curframe, {}).items():
                self.message('%s: %s' % bdb.safe_repr(item))
        else:
            val = self._getval_except(arg)
            self.displaying.setdefault(self.curframe, {})[arg] = val
            self.message('display %s: %s' % (arg, bdb.safe_repr(val)))

    complete_display = _complete_expression

    def do_undisplay(self, arg):
        """undisplay [expression]

        Do not display the expression any more in the current frame.

        Without expression, clear all display expressions for the current frame.
        """
        if arg:
            try:
                del self.displaying.get(self.curframe, {})[arg]
            except KeyError:
                self.error('not displaying %s' % arg)
        else:
            self.displaying.pop(self.curframe, None)

    def complete_undisplay(self, text, line, begidx, endidx):
        return [e for e in self.displaying.get(self.curframe, {})
                if e.startswith(text)]

    def do_interact(self, arg):
        """interact

        Start an interative interpreter whose global namespace
        contains all the (global and local) names found in the current scope.
        """
        def readfunc(prompt):
            self.stdout.write(prompt)
            self.stdout.flush()
            line = self.stdin.readline()
            line = line.rstrip('\r\n')
            if line == 'EOF':
                raise EOFError
            return line

        ns = self.curframe.f_globals.copy()
        ns.update(self.get_locals(self.curframe))
        if isinstance(self.stdin, RemoteSocket):
            # Main interpreter redirection of the code module.
            if PY3:
                import sys as _sys
            else:
                # Parent module 'pdb_clone' not found while handling absolute
                # import.
                _sys = __import__('sys', level=0)
            code.sys = _sys
            self.redirect(code.interact, local=ns, readfunc=readfunc)
        else:
            code.interact("*interactive*", local=ns)

    def do_alias(self, arg):
        """alias [name [command [parameter parameter ...] ]]
        Create an alias called 'name' that executes 'command'.  The
        command must *not* be enclosed in quotes.  Replaceable
        parameters can be indicated by %1, %2, and so on, while %* is
        replaced by all the parameters.  If no command is given, the
        current alias for name is shown. If no name is given, all
        aliases are listed.

        Aliases may be nested and can contain anything that can be
        legally typed at the pdb prompt.  Note!  You *can* override
        internal pdb commands with aliases!  Those internal commands
        are then hidden until the alias is removed.  Aliasing is
        recursively applied to the first word of the command line; all
        other words in the line are left alone.

        As an example, here are two useful aliases (especially when
        placed in the .pdbrc file):

        # Print instance variables (usage "pi classInst")
        alias pi for k in %1.__dict__.keys(): print("%1.",k,"=",%1.__dict__[k])
        # Print instance variables in self
        alias ps pi self
        """
        args = arg.split()
        if len(args) == 0:
            keys = sorted(self.aliases.keys())
            for alias in keys:
                self.message("%s = %s" % (alias, self.aliases[alias]))
            return
        if args[0] in self.aliases and len(args) == 1:
            self.message("%s = %s" % (args[0], self.aliases[args[0]]))
        else:
            self.aliases[args[0]] = ' '.join(args[1:])

    def do_unalias(self, arg):
        """unalias name
        Delete the specified alias.
        """
        args = arg.split()
        if len(args) == 0: return
        if args[0] in self.aliases:
            del self.aliases[args[0]]

    def _do_thread(self, arg, current_frames, tlist):
        if not arg:
            self.message('   {:3} {:18} {:16} {}'.format(
                    'Nb', 'Name', 'Identifier', 'Stack entry'))
            for (nb, t) in enumerate(tlist, start=1):
                prefix = '+' if t is self.pdb_thread else ' '
                prefix += '*' if t is self.current_thread else ' '
                if t is self.pdb_thread:
                    frame = self.pdb_toplevel_frame
                else:
                    frame = current_frames.get(t.ident)
                if frame:
                    stack_entry = self.format_stack_entry(
                            (frame, frame.f_lineno), line_prefix).split('\n')
                    self.message('{} {:3d} {:18} {:16d} {}\n{:43}{}'.format(
                        prefix, nb, t.name, t.ident, stack_entry[0],
                        '', stack_entry[1]))
                else:
                    self.message('{} {:3d} {:18} {:16d} {}'.format(
                        prefix, nb, t.name, t.ident, 'Thread not active.'))
            return

        t = frame = None
        try:
            idx = int(arg)
            if idx > 0:
                t = tlist[idx - 1]
        except (ValueError, IndexError):
            pass
        if t is None:
            self.error(
                'Invalid thread number, must be in a range from 1 to {:d}.'
                .format(len(tlist)))
        elif t is self.pdb_thread:
            frame = self.pdb_toplevel_frame
        else:
            try:
                frame = current_frames[t.ident]
            except IndexError:
                self.error('Internal error, the thread "{}" is not active.'
                .format(t.name))
        if frame:
            self.setup(frame, None)
            self.current_thread = t
            self.print_stack_entry(self.stack[self.curindex])

    def do_thread(self, arg):
        """th(read) [threadnumber]
        Without argument, display a summary of all active threads.
        The summary prints for each thread:
           1. the thread number assigned by pdb
           2. the thread name
           3. the python thread identifier
           4. the current stack frame summary for that thread
        An asterisk '*' to the left of the pdb thread number indicates the
        current thread, a plus sign '+' indicates the thread being traced by
        pdb.

        With a pdb thread number as argument, make this thread the current
        thread. The 'where', 'up' and 'down' commands apply now to the frame
        stack of this thread. The current scope is now the frame currently
        executed by this thread at the time the command is issued and the
        'list', 'll', 'args', 'p', 'pp', 'source' and 'interact' commands are
        run in the context of that frame. Note that this frame may bear no
        relationship (for a non-deadlocked thread) to that thread's current
        activity by the time you are examining the frame.
        This command does not stop the thread.
        """
        # Import the threading module in the main interpreter to get an
        # enumeration of the main interpreter threads.
        if PY3:
            try:
                import threading
            except ImportError:
                import dummy_threading as threading
        else:
            # Do not use relative import detection to avoid the RuntimeWarning:
            # Parent module 'pdb_clone' not found while handling absolute
            # import.
            try:
                threading = __import__('threading', level=0)
            except ImportError:
                threading = __import__('dummy_threading', level=0)


        if not self.pdb_thread:
            self.pdb_thread = threading.current_thread()
        if not self.current_thread:
            self.current_thread = self.pdb_thread
        current_frames = sys._current_frames()
        tlist = sorted(threading.enumerate(), key=attrgetter('name', 'ident'))
        try:
            self._do_thread(arg, current_frames, tlist)
        finally:
            # For some reason this local must be explicitly deleted in order
            # to release the subinterpreter.
            del current_frames

    def complete_unalias(self, text, line, begidx, endidx):
        return [a for a in self.aliases if a.startswith(text)]

    # List of all the commands making the program resume execution.
    commands_resuming = ['do_continue', 'do_step', 'do_next', 'do_return',
                         'do_quit', 'do_jump']

    # Print a traceback starting at the top stack frame.
    # The most recently entered frame is printed last;
    # this is different from dbx and gdb, but consistent with
    # the Python interpreter's stack trace.
    # It is also consistent with the up/down commands (which are
    # compatible with dbx and gdb: up moves towards 'main()'
    # and down moves towards the most recent stack frame).

    def print_stack_trace(self):
        try:
            for frame_lineno in self.stack:
                self.print_stack_entry(frame_lineno)
        except KeyboardInterrupt:
            pass

    def print_stack_entry(self, frame_lineno, prompt_prefix=line_prefix):
        frame, lineno = frame_lineno
        if frame is self.curframe:
            prefix = '> '
        else:
            prefix = '  '
        self.message(prefix +
                     self.format_stack_entry(frame_lineno, prompt_prefix))

    # Provide help

    def do_help(self, arg):
        """h(elp)
        Without argument, print the list of available commands.
        With a command name as argument, print help about that command.
        "help pdb" shows the full pdb documentation.
        "help exec" gives help on the ! command.
        """
        if not arg:
            return cmd.Cmd.do_help(self, arg)
        try:
            try:
                topic = getattr(self, 'help_' + arg)
                return topic()
            except AttributeError:
                command = getattr(self, 'do_' + arg)
        except AttributeError:
            self.error('No help for %r' % arg)
        else:
            if sys.flags.optimize >= 2:
                self.error('No help for %r; please do not run Python with -OO '
                           'if you need command help' % arg)
                return
            self.message(command.__doc__.rstrip())

    do_h = do_help

    def help_exec(self):
        """(!) statement
        Execute the (one-line) statement in the context of the current
        stack frame.  The exclamation point can be omitted unless the
        first word of the statement resembles a debugger command.  To
        assign to a global variable you must always prefix the command
        with a 'global' command, e.g.:
        (Pdb) global list_options; list_options = ['-l']
        (Pdb)
        """
        self.message((self.help_exec.__doc__ or '').strip())

    def help_pdb(self):
        help(self.stdout)

    # other helper functions

    def _runscript(self, filename):
        # The script has to run in __main__ namespace (or imports from
        # __main__ will break).
        #
        # So we clear up the __main__ and set several special variables
        # (this gets rid of pdb's globals and cleans old variables on restarts).
        import __main__
        __main__.__dict__.clear()
        __main__.__dict__.update({"__name__"    : "__main__",
                                  "__file__"    : filename,
                                  "__builtins__": __builtins__,
                                 })

        self.mainpyfile = filename
        self._user_requested_quit = False
        with open(filename, "rb") as fp:
            content = fp.read()
        self.forget()
        self.run(compile(content, self.mainpyfile, 'exec', 0, True))

# Collect all command help into docstring, if not run with -OO

if __doc__ is not None:
    # unfortunately we can't guess this order from the class definition
    _help_order = [
        'help', 'where', 'down', 'up', 'break', 'tbreak', 'clear', 'disable',
        'enable', 'ignore', 'condition', 'commands', 'step', 'next', 'until',
        'jump', 'return', 'retval', 'run', 'continue', 'list', 'longlist',
        'args', 'p', 'pp', 'whatis', 'source', 'display', 'undisplay',
        'thread', 'interact', 'alias', 'unalias', 'debug', 'detach', 'quit',
    ]

    for _command in _help_order:
        __doc__ += getattr(Pdb, 'do_' + _command).__doc__.strip() + '\n\n'
    __doc__ += Pdb.help_exec.__doc__

    del _help_order, _command


# Simplified interface

def run(statement, globals=None, locals=None):
    Pdb().run(statement, globals, locals)

def runeval(expression, globals=None, locals=None):
    return Pdb().runeval(expression, globals, locals)

def runctx(statement, globals, locals):
    # B/W compatibility
    run(statement, globals, locals)

def runcall(*args, **kwds):
    return Pdb().runcall(*args, **kwds)

def set_trace():
    Pdb().set_trace(sys._getframe().f_back)

def set_trace_remote(host=b'127.0.0.1', port=7935, frame=None):
    # When the set_trace_remote() hard-coded breakpoint is set in a loop
    # iterating over sys.modules, allowing 'host' to be an str instance could
    # possibly raise a RuntimeError (dictionary changed size) after the bind()
    # call on the socket causes the import of 'encodings.idna'.
    if not isinstance(host, bytes):
        raise ValueError("'host' must be a bytes object.")

    rsock = RemoteSocket((host, port))
    pdb = Pdb(stdin=rsock, stdout=rsock)
    if not frame:
        frame = sys._getframe().f_back
    pdb.set_trace(frame)
    return rsock

# Post-Mortem interface

def post_mortem(t=None):
    # handling the default
    if t is None:
        # sys.exc_info() returns (type, value, traceback) if an exception is
        # being handled, otherwise it returns None
        t = sys.exc_info()[2]
    if t is None:
        raise ValueError("A valid traceback must be passed if no "
                         "exception is being handled")

    p = Pdb()
    p.interaction(None, t)

def pm():
    post_mortem(sys.last_traceback)


# Main program for testing

TESTCMD = 'import x; x.main()'

def test():
    run(TESTCMD)

# print help
def help(stdout=sys.stdout):
    save_stdout = sys.stdout
    try:
        sys.stdout = stdout
        pydoc.pager(__doc__)
        # Ends the pager output on a newline to enable prompt detection when
        # doing remote debugging.
        if save_stdout != stdout:
            print(file=stdout)
    finally:
        sys.stdout = save_stdout

_usage = """\
usage: pdb-clone [-c command] ... pyfile [arg] ...

Debug the Python program given by pyfile.

Initial commands are read from .pdbrc files in your home directory
and in the current directory, if they exist.  Commands supplied with
-c are executed after commands from .pdbrc files.

To let the script run until an exception occurs, use "-c continue".
To let the script run up to a given line X in the debugged file, use
"-c 'until X'"."""

def main():
    import getopt

    opts, args = getopt.getopt(sys.argv[1:], 'hc:', ['--help', '--command='])

    if not args:
        print(_usage)
        sys.exit(2)

    commands = []
    for opt, optarg in opts:
        if opt in ['-h', '--help']:
            print(_usage)
            sys.exit()
        elif opt in ['-c', '--command']:
            commands.append(optarg)

    mainpyfile = args[0]     # Get script filename
    if not os.path.exists(mainpyfile):
        print('Error:', mainpyfile, 'does not exist')
        sys.exit(1)

    sys.argv[:] = args      # Hide "pdb.py" and pdb options from argument list

    # Replace pdb's dir with script's dir in front of module search path.
    sys.path[0] = os.path.dirname(mainpyfile)

    # Note on saving/restoring sys.argv: it's a good idea when sys.argv was
    # modified by the script being debugged. It's a bad idea when it was
    # changed by the user from the command line. There is a "restart" command
    # which allows explicit specification of command line arguments.
    pdb = Pdb()
    pdb.rcLines.extend(commands)
    while True:
        try:
            pdb.restart()
            pdb._runscript(mainpyfile)
            if pdb._user_requested_quit:
                break
            print("The program finished and will be restarted")
        except Restart:
            print("Restarting", mainpyfile, "with arguments:")
            print("\t" + " ".join(args))
        except SystemExit:
            # In most cases SystemExit does not warrant a post-mortem session.
            print("The program exited via sys.exit(). Exit status:", end=' ')
            print(sys.exc_info()[1])
        except (SyntaxError, bdb.BdbSyntaxError):
            traceback.print_exc()
            break
        except Exception:
            traceback.print_exc()
            print("Uncaught exception. Entering post mortem debugging")
            print("Running 'cont' or 'step' will restart the program")
            t = sys.exc_info()[2]
            pdb.interaction(None, t)
            print("Post mortem debugger finished. The " + mainpyfile +
                  " will be restarted")


# When invoked as main program, invoke the debugger on a script.
if __name__ == '__main__':
    main()
