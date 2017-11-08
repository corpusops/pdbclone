# vi:set ts=8 sts=4 sw=4 et tw=80:
"""The pdbhandler module."""

# Python 2-3 compatibility.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
if sys.platform.startswith('win'):
    raise ImportError('The pdbhandler module is not supported on Windows.')

import signal
from pdb_clone import _pdbhandler, DFLT_ADDRESS
from collections import namedtuple

Handler = namedtuple('Handler', 'host, port, signum')

def register(host=DFLT_ADDRESS[0], port=DFLT_ADDRESS[1],
             signum=signal.SIGUSR1):
    """Register a pdb handler for signal 'signum'.

    The handler sets pdb to listen on the ('host', 'port') internet address
    and to start a remote debugging session on accepting a socket connection.
    """
    _pdbhandler._register(host, port, signum)

def unregister():
    """Unregister the pdb handler.

    Do nothing when no handler has been registered.
    """
    _pdbhandler._unregister()

def get_handler():
    """Return the handler as a named tuple.

    The named tuple attributes are 'host', 'port', 'signum'.
    Return None when no handler has been registered.
    """
    host, port, signum = _pdbhandler._registered()
    if signum:
        return Handler(host if host else DFLT_ADDRESS[0].encode(),
                       port if port else DFLT_ADDRESS[1], signum)

