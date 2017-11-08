# vi:set ts=8 sts=4 sw=4 et tw=80:
"""Attach to a remote Pdb instance.

Command line history, command completion and completion on the help topic are
available.

On unix, the remote process can be interrupted with Ctrl-C when pdb-attach has
the right privilege to send a signal to the remote process.

Use the 'detach' pdb command to release the remote process from pdb control
and have it continue its execution without tracing overhead.
"""

# Python 2-3 compatibility.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
try:
    import StringIO
except ImportError:
    pass

import sys
import os
import io
import cmd
import argparse
import signal
import time
import random
import socket
import asyncore
import asynchat
import errno
from itertools import takewhile
from collections import deque
from subprocess import Popen, STDOUT, PIPE

from . import PY3, DFLT_ADDRESS, pdb

prompts = ('(Pdb) ', '(com) ', '((Pdb)) ', '>>> ', '... ')
line_prmpts = tuple('\n%s' % p for p in prompts)

class AttachSocket(asynchat.async_chat, cmd.Cmd):
    """A socket connected to a remote Pdb instance."""

    def __init__(self, connections, completekey='tab', stdin=None, stdout=None):
        asynchat.async_chat.__init__(self, map=connections)
        cmd.Cmd.__init__(self, completekey, stdin, stdout)
        if stdout:
            self.use_rawinput = 0
        self.set_terminator(None)
        self.allow_kbdint = False
        self.data = b''
        self.remote = ''
        self.pid = 0
        self._previous_sigint_handler = None

    def message(self, *objs, **kwds):
        kwds['file'] = self.stdout
        if 'flush' not in kwds:
            kwds['flush'] = False
        printflush(*objs, **kwds)

    def sigint_handler(self, signum, frame):
        if self.allow_kbdint:
            raise KeyboardInterrupt
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGINT)
            except OSError as err:
                self.message(err)

    def handle_connect(self):
        pass

    def handle_error(self):
        self.close()
        err = sys.exc_info()[1]
        if isinstance(err, IOError) and err.errno == errno.ECONNREFUSED:
            self.message(err)
            self.message('[if using gdb to attach to the process,'
                                    ' did you forget to quit gdb ?]')
            sys.exit(1)
        raise

    def handle_close (self):
        if self.connected:
            if self._previous_sigint_handler:
                signal.signal(signal.SIGINT, self._previous_sigint_handler)
            content = _decode(self.data, encoding='utf-8')
            if content:
                self.message(content.rstrip('\n'))
            self.message('Closed by remote.')
            self.close()

    def collect_incoming_data(self, data):
        self.data += data
        while self.data and not self.remote:
            idx = self.data.find(b'\n')
            if idx != -1:
                if idx > 0:
                    self.get_header(_decode(self.data[:idx], encoding='utf-8'))
                self.data = self.data[idx + 1:]
                continue
            return
        if self.data:
            self.interaction()

    def connect_retry(self, address, verbose):
        skip_connect = 5
        count = 0
        while not self.connected:
            try:
                self.connect(address)
            except IOError as err:
                if err.errno != errno.ECONNREFUSED:
                    raise
                # Skip printing the first connection failures.
                if count >= skip_connect and verbose:
                    if count == skip_connect:
                        printflush('Connecting to remote pdb' +
                                skip_connect * '.', end='')
                    else:
                        printflush('.', end='')
                count += 1
                if count >= 20:
                    if verbose:
                        printflush('failed')
                    self.close()
                    raise
                yield count
                time.sleep(0.200)
        if verbose and count > skip_connect:
            printflush('ok')

    def get_header(self, line):
        if line.startswith('PROCESS_PID:'):
            self.pid = int(line.split(':')[1])
            self._previous_sigint_handler = signal.signal(signal.SIGINT,
                                                        self.sigint_handler)
        elif line.startswith('PROCESS_NAME:'):
            self.remote = line.split(':', 1)[1]
            msg = ('Connected to %s at %s' %
                    (os.path.basename(self.remote), str(self.addr)))
            end = ', pid: %d.' % self.pid if self.pid else '.'
            msg += end
            self.message(msg)
        else:
            self.message('Invalid header line: %s' % line)

    def interaction(self):
        content = _decode(self.data, encoding='utf-8')
        plen = 0
        if content.endswith(line_prmpts) or content in prompts:
            for i in range(len(prompts)):
                if content.endswith(line_prmpts[i]) or content == prompts[i]:
                    plen = len(prompts[i])
                    break
        if plen:
            self.data = b''
            self.message(content[:-plen], end='')
            self.prompt = content[-plen:]
            while True:
                try:
                    # Here cmdloop is not much of a loop. All commands except
                    # the 'help' command (without any topic), stop the loop
                    # and give back control to the event loop.
                    self.allow_kbdint = True
                    self.cmdloop()
                    self.allow_kbdint = False
                    break
                except KeyboardInterrupt:
                    self.message()

    def precmd(self, line):
        # Use 'curline' to preserve indentation when looping in the 'interact'
        # command.
        self.curline = line + '\n'
        return line

    def default(self, line, cmd=''):
        if cmd == 'interact':
            self.lastcmd = ''
        # Explicitly set the encoding to utf-8.
        self.push(_encode(self.curline, encoding='utf-8'))
        return True

    # Add the Pdb 'do_' and 'help_' methods as commands controlled by the
    # cmd.Cmd completion machinery. They all call the default method, as well
    # as any unrecognized command.
    for name in dir(pdb.Pdb):
        if name.startswith('do_'):
            exec('def %s(self, l): return self.default(l, cmd="%s")'
                                                    % (name, name[3:]))
        elif name.startswith('help_'):
            exec('def %s(self, l): return self.default(l, cmd="help %s")'
                                                    % (name, name[5:]))

class AttachSocketWithDetach(AttachSocket):
    """A socket connected to a remote Pdb instance.

    The 'detach' command is issued at the first pdb prompt.
    """

    def interaction(self):
        content = _decode(self.data, encoding='utf-8')
        if content.endswith(line_prmpts) or content in prompts:
            self.data = b''
            self.push(_encode('detach\n', encoding='utf-8'))

class Result:
    def __init__(self):
        self.attach_cnt = 0
        self.retries = {}

    def add(self, rtype):
        if not rtype in self.retries:
            self.retries[rtype] = 1
        else:
            self.retries[rtype] += 1

    def __str__(self):
        lines = [
            'Results:',
            '  successful attach: %d' % self.attach_cnt]
        if self.retries:
            lines.append('  retries:')
            for rtype in sorted(self.retries):
                lines.append('    %s: %d' % (rtype, self.retries[rtype]))
        return '\n'.join(lines)

class StatementLine:
    def __init__(self):
        self.line = ''
        self.skipping = False
        self.lines = deque()

    def set_line(self, line):
        self.line = line

    def skip(self):
        """Skip this py-pdb command to avoid attaching within the same loop."""

        line = self.line
        self.line = ''
        # 'line' is the statement line of the previous py-pdb command.
        if line in self.lines:
            if not self.skipping:
                self.skipping = True
                printflush('Skipping lines', end='')
            printflush('.', end='')
            return True
        elif line:
            self.lines.append(line)
            if len(self.lines) > 30:
                self.lines.popleft()

        return False

    def print(self):
        if self.line and self.line not in self.lines:
            if self.skipping:
                self.skipping = False
                print('')
            printflush(self.line)

class Context:
    """The execution context shared by all the GdbSocket instances."""
    def __init__(self):
        self.result = Result()
        self.stmt= StatementLine()

class GdbSocket(asynchat.async_chat):
    """The gdb/mi socket connection."""

    ST_INIT, ST_PDB, ST_EXIT, ST_TERMINATED = tuple(range(4))

    def __init__(self, ctx, address, proc, proc_iut, sock,
                 verbose, connections):
        asynchat.async_chat.__init__(self, sock, connections)
        self.ctx = ctx
        self.address = address
        self.proc = proc
        self.proc_iut = proc_iut
        self.verbose = verbose
        self.connections = connections
        self.error = None
        self.state = self.ST_INIT
        self.gdb_version = None
        self.set_terminator(b'\n')
        self.ibuff = io.StringIO() if PY3 else StringIO.StringIO()

        # Setup gdb to not stop the inferior on the following signals.
        self.cli_command('handle SIGPIPE noprint')
        self.cli_command('handle SIGUSR1 noprint')
        self.cli_command('handle SIGUSR2 noprint')
        self.cli_command('handle SIGALRM noprint')
        self.cli_command('handle SIGCHLD noprint')
        self.cli_command('handle SIGABRT noprint')
        self.cli_command('handle SIGKILL noprint')
        self.cli_command('handle SIGTERM noprint')
        self.cli_command('handle SIGXFSZ noprint')
        self.cli_command('handle SIGINT noprint')

    def handle_error(self):
        self.close()
        raise

    def handle_close (self):
        if self.connected:
            if not self.ctx:
                printflush('Socket closed by gdb.')
            self.close()

            # Handle anbormal gdb termination.
            rc = self.proc.wait()
            if rc is not None and rc < 0:
                self.state = self.ST_TERMINATED
                signals = dict((getattr(signal, n), n) for n in dir(signal)
                                                    if n.startswith('SIG'))
                printflush('Gdb terminated, got signal %s.'
                                        % signals.get(-rc, -rc))

    def mi_command(self, line):
        if not line.endswith('\n'):
            line += '\n'
        if self.verbose:
            printflush('+++', line, end='')
        self.push(_encode(line))

    def cli_command(self, cmd):
        self.mi_command('-interpreter-exec console "%s"' % cmd)

    def exit(self, msg=None, where=False):
        self.state = self.ST_EXIT
        self.error = msg
        if where:
            self.verbose = True
            self.cli_command('where')
        self.mi_command('-gdb-exit')

    def attach(self):
        if self.ctx:
            dev_null = io.StringIO() if PY3 else StringIO.StringIO()
            asock = AttachSocketWithDetach(self.connections, stdout=dev_null)
        else:
            asock = AttachSocket(self.connections)
        asock.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        connect_process(asock, self.ctx, self.proc_iut, address=self.address)

    def collect_incoming_data(self, data):
        self.ibuff.write(_decode(data))

    def found_terminator(self):
        line = self.ibuff.getvalue()
        self.ibuff = io.StringIO() if PY3 else StringIO.StringIO()
        if self.verbose:
            printflush(line)
        elif line.startswith('~"->'):
            line = line[1:].strip('"')
            line = line[:-2] if line.endswith(r'\n') else line
            if self.ctx:
                self.ctx.stmt.set_line(line)
            else:
                print(line)

        error_prefix = '^error,msg='
        if line == '^exit':
            self.state = self.ST_TERMINATED
            self.close()

        elif line.startswith('~"') and self.gdb_version is None:
            self.gdb_version = parse_gdb_version(line)
            if self.gdb_version:
                if not self.ctx:
                    print('Starting gdb %s' % self.gdb_version)
            else:
                self.exit('Invalid gdb version line: "%s".' % line)

        elif line.startswith(error_prefix):
            # Do not overwrite the first error message.
            if self.state != self.ST_EXIT:
                err = line[len(error_prefix):].strip('"').replace(r'\n', '\n')
                self.exit(err)

        elif line.startswith('*stopped,'):
            if line.startswith('*stopped,reason="exited'):
                self.exit()
            elif line.startswith('*stopped,frame='):
                # We skip running the 'py-pdb' command based on the statement
                # line at the previous invocation of 'py-pdb', since the
                # statement line is printed by gdb only after issuing that
                # command. Skipping 'py-pdb' commands is useful to stop
                # spending quite a lot of time attaching repeatedly within the
                # same loop.
                if not self.ctx or not self.ctx.stmt.skip():
                    self.state = self.ST_PDB
                    self.cli_command('py-pdb %s %s' %
                                        (self.address[0], self.address[1]))
                else:
                    self.exit()

        elif line.startswith('&"') and not line.startswith('&"warning:'):
            line = line[1:].strip('"').replace(r'\n', '')
            if line:
                print(line)

        elif self.state == self.ST_PDB and line.startswith('~'):
            lines = line[1:].strip('"').replace(r'\n', '\n')
            ok = self.process_result(lines)
            if ok:
                self.exit()
                self.attach()

    def process_result(self, lines):
        if 'Unable to setup pdb' in lines:
            self.exit(lines, True)
        elif 'Cannot setup pdb' in lines:
            if self.ctx:
                rtype = lines.split('\n')[1]
                rtype = rtype.strip('.')
                self.ctx.result.add(rtype)
                # A previous run of the 'py-pdb' command failed to attach to
                # the process under test. Must attach now.
                if 'Address already in use' in rtype:
                    return True
                self.exit()
                return False
            else:
                self.exit(lines)
        elif 'Pdb has been setup' in lines:
            if self.ctx:
                self.ctx.stmt.print()
            return True

def printflush(*args, **kwds):
    if 'flush' in kwds:
        flush = kwds['flush']
        del kwds['flush']
    else:
        flush = True
    print(*args, **kwds)
    if flush:
        file = kwds.get('file', sys.stdout)
        file.flush()

def _decode(data, encoding=None):
    if PY3:
        if encoding:
            return data.decode(encoding=encoding)
        else:
            return data.decode()
    else:
        return data

def _encode(data, encoding=None):
    if PY3:
        if encoding:
            return data.encode(encoding=encoding)
        else:
            return data.encode()
    else:
        return data

def parse_gdb_version(line):
    r"""Parse the gdb version from the gdb header.

    From GNU coding standards: the version starts after the last space of the
    first line.

    >>> DOCTEST_GDB_VERSIONS = [
    ... r'~"GNU gdb (GDB) 7.5.1\n"',
    ... r'~"GNU gdb (Sourcery CodeBench Lite 2011.09-69) 7.2.50.20100908-cvs\n"',
    ... r'~"GNU gdb (GDB) SUSE (7.5.1-2.5.1)\n"',
    ... r'~"GNU gdb (GDB) Fedora (7.6-32.fc19)\n"',
    ... r'~"GNU gdb (GDB) 7.6.1.dummy\n"',
    ... ]
    >>> for header in DOCTEST_GDB_VERSIONS:
    ...     print(parse_gdb_version(header))
    7.5.1
    7.2.50.20100908
    7.5.1
    7.6
    7.6.1

    """
    if line.startswith('~"') and line.endswith(r'\n"'):
        version = line[2:-3].rsplit(' ', 1)
        if len(version) == 2:
            # Strip after first non digit or '.' character. Allow for linux
            # Suse non conformant implementation that encloses the version in
            # brackets.
            version = ''.join(takewhile(lambda x: x.isdigit() or x == '.',
                                                    version[1].lstrip('(')))
            return version.strip('.')
    return ''

def gdb_terminated(msg):
    clist = ('program exited normally', 'zombie', 'ptrace: No such process')
    return any(x in msg for x in clist)

def spawn_gdb(pid, address=DFLT_ADDRESS, gdb='gdb', verbose=False,
              ctx=None, proc_iut=None):
    """Spawn gdb and attach to a process."""

    parent, child = socket.socketpair()
    proc = Popen([gdb, '--interpreter=mi', '-nx'],
                    bufsize=0, stdin=child, stdout=child, stderr=STDOUT)
    child.close()

    connections = {}
    gdb = GdbSocket(ctx, address, proc, proc_iut, parent, verbose,
                    connections)
    gdb.mi_command('-target-attach %d' % pid)
    gdb.cli_command('python import pdb_clone.bootstrappdb_gdb')
    asyncore.loop(map=connections)
    proc.wait()
    return gdb.error

def attach_loop(argv):
    """Spawn the process, then repeatedly attach to the process."""

    # Check if the pdbhandler module is built into python.
    p = Popen((sys.executable, '-X', 'pdbhandler', '-c',
                'import pdbhandler; pdbhandler.get_handler().host'),
               stdout=PIPE, stderr=STDOUT)
    p.wait()
    use_xoption = True if p.returncode == 0 else False

    # Spawn the process.
    args = [sys.executable]
    if use_xoption:
        # Use SIGUSR2 as faulthandler is set on python test suite with
        # SIGUSR1.
        args.extend(['-X', 'pdbhandler=localhost 7935 %d' % signal.SIGUSR2])
        args.extend(argv)
        proc = Popen(args)
    else:
        args.extend(argv)
        proc = Popen(args)

    # Repeatedly attach to the process using the '-X' python option or gdb.
    ctx = Context()
    error = None
    time.sleep(.5 + random.random())
    while not error and proc.poll() is None:
        if use_xoption:
            os.kill(proc.pid, signal.SIGUSR2)
            connections = {}
            dev_null = io.StringIO() if PY3 else StringIO.StringIO()
            asock = AttachSocketWithDetach(connections, stdout=dev_null)
            asock.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            connect_process(asock, ctx, proc)
            asyncore.loop(map=connections)
        else:
            error = spawn_gdb(proc.pid, ctx=ctx, proc_iut=proc)
        time.sleep(random.random())

    if error and gdb_terminated(error):
        error = None
    if proc.poll() is None:
        proc.terminate()
    else:
        print('pdb-attach: program under test return code:', proc.wait())

    result = str(ctx.result)
    if result:
        print(result)
    return error

def connect_process(asock, ctx, proc, address=DFLT_ADDRESS):
    try:
        for count in asock.connect_retry(address, True):
            # Stop attempting to connect when an os.kill() statement has
            # terminated the process after atexit has unregistered the signal
            # handler.
            if proc and proc.poll() is not None:
                asock.close()
                return
    except IOError as err:
        if err.errno != errno.ECONNREFUSED:
            raise
        if ctx:
            ctx.result.add('Failed to connect to remote pdb')
        else:
            print('\nFailed to connect to the remote pdb.')
    else:
        if ctx:
            ctx.result.attach_cnt += 1

def attach(address=DFLT_ADDRESS, stdin=None, stdout=None, verbose=True):
    connections = {}
    asock = AttachSocket(connections, stdin=stdin, stdout=stdout)
    asock.create_socket(socket.AF_INET, socket.SOCK_STREAM)
    for count in asock.connect_retry(address, verbose):
        pass
    asyncore.loop(map=connections)

epilog = """
When the first argument is '-t' or '--test', spawn a process as python started
with the remainder of the command line arguments and repeatedly attach
(followed by detach) to this process until the process exits.
"""

def main():
    GDB = 'gdb'

    if len(sys.argv) > 2 and sys.argv[1] in ('-t', '--test'):
        sys.exit(attach_loop(sys.argv[2:]))

    parser = argparse.ArgumentParser(description=__doc__.strip(),
            epilog=epilog)
    parser.add_argument('-v', '--verbose', action='store_true',
            help='print gdb/mi output')
    parser.add_argument('-p', '--pid', type=int,
            help='attach to the Python process whose pid is PID;'
            ' use gdb to inject the handling of a fake signal when the'
            ' \'--kill\' optional argument is not used')
    parser.add_argument('-g', '--gdb',
            nargs='?', default=None, const=GDB,
            help='use GDB to invoke gdb, the default is \'%(const)s\'')
    parser.add_argument('-k', '--kill', type=int,
            nargs='?', default=None, const=signal.SIGUSR1, metavar='SIGNUM',
            help='send the SIGNUM signal to process PID '
            'that has registered a handler for this signal with the '
            'pdbhandler.register() function and attach to the process;'
            ' SIGNUM default value is \'%(const)s\'')
    parser.add_argument('host', nargs='?', default=DFLT_ADDRESS[0],
            help='default: %(default)s')
    parser.add_argument('port', nargs='?', default=DFLT_ADDRESS[1], type=int,
            help='default: %(default)s')
    args = parser.parse_args()

    address = (args.host, args.port)
    if args.pid is not None:
        if args.kill is not None:
            if sys.platform.startswith("win"):
                parser.error("the '--kill' option"
                             " is not supported on Windows.")
            if args.gdb is not None:
                parser.error("the '--kill' and '--gdb' options conflict.")
            if args.kill <= 0 or args.kill >= signal.NSIG:
                parser.error('signal number out of range.')
            os.kill(args.pid, args.kill)
            attach(address)
        else:
            args.gdb = GDB if args.gdb is None else args.gdb
            error = spawn_gdb(args.pid, address,
                              args.gdb, args.verbose)
            if error:
                print(error)
    else:
        attach(address)

if __name__ == '__main__':
    import doctest
    doctest.testmod()

