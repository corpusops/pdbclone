**Features**

  * Implement the most recent Python 3 features of pdb, as defined in the Python 3 `pdb documentation`_. The pdb command line interface remains unchanged except for the new ``detach`` and ``thread`` pdb commands.

  * Improve significantly pdb performance. With breakpoints, pdb-clone runs just below the speed of the interpreter while pdb runs 10 to 100 times slower than the interpreter, see `Performances <http://code.google.com/p/pdb-clone/wiki/Performances>`_.

  * Extend pdb with remote debugging. A remote debugging session may be started when the program stops at a ``pdb.set_trace_remote()`` hard-coded breakpoint, or at any time and multiple times by attaching to the process main thread. See `RemoteDebugging <http://code.google.com/p/pdb-clone/wiki/RemoteDebugging>`_

  * Fix pdb long standing bugs entered in the Python issue tracker, see the `News <http://code.google.com/p/pdb-clone/wiki/News>`_.

  * Add a bdb comprehensive test suite (more than 70 tests) and run both pdb and bdb test suites.

pdb-clone runs the same source code on all the supported versions of Python, which are:

    * Python 3: from version 3.2 onward.

    * Python 2: version 2.7.

See also the `README <http://code.google.com/p/pdb-clone/wiki/ReadMe>`_ and the project `home page <http://code.google.com/p/pdb-clone/>`_.

Report bugs to the `issue tracker <http://code.google.com/p/pdb-clone/issues/list>`_.

**Usage**

Invoke pdb-clone as a script to debug other scripts. For example::

    $ pdb-clone myscript.py

Or use one of the different ways of running pdb described in the `pdb documentation`_ and replace::

    import pdb

with::

    from pdb_clone import pdb

.. _pdb documentation: http://docs.python.org/3/library/pdb.html

