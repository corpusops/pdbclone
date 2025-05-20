
DISCLAIMER - ABANDONED/UNMAINTAINED CODE / DO NOT USE
=======================================================
While this repository has been inactive for some time, this formal notice, issued on **December 10, 2024**, serves as the official declaration to clarify the situation. Consequently, this repository and all associated resources (including related projects, code, documentation, and distributed packages such as Docker images, PyPI packages, etc.) are now explicitly declared **unmaintained** and **abandoned**.

I would like to remind everyone that this project’s free license has always been based on the principle that the software is provided "AS-IS", without any warranty or expectation of liability or maintenance from the maintainer.
As such, it is used solely at the user's own risk, with no warranty or liability from the maintainer, including but not limited to any damages arising from its use.

Due to the enactment of the Cyber Resilience Act (EU Regulation 2024/2847), which significantly alters the regulatory framework, including penalties of up to €15M, combined with its demands for **unpaid** and **indefinite** liability, it has become untenable for me to continue maintaining all my Open Source Projects as a natural person.
The new regulations impose personal liability risks and create an unacceptable burden, regardless of my personal situation now or in the future, particularly when the work is done voluntarily and without compensation.

**No further technical support, updates (including security patches), or maintenance, of any kind, will be provided.**

These resources may remain online, but solely for public archiving, documentation, and educational purposes.

Users are strongly advised not to use these resources in any active or production-related projects, and to seek alternative solutions that comply with the new legal requirements (EU CRA).

**Using these resources outside of these contexts is strictly prohibited and is done at your own risk.**

This project has been transfered to Makina Corpus <freesoftware-corpus.com> ( https://makina-corpus.com ). This project and its associated resources, including published resources related to this project (e.g., from PyPI, Docker Hub, GitHub, etc.), may be removed starting **March 15, 2025**, especially if the CRA’s risks remain disproportionate.

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

