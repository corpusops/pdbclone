#include "Python.h"
#include "frameobject.h"

#if PY_MAJOR_VERSION >= 3
#ifndef MS_WINDOWS
#include <signal.h>

/* the following macros come from Python: Modules/signalmodule.c */
#ifndef NSIG
# if defined(_NSIG)
#  define NSIG _NSIG            /* For BSD/SysV */
# elif defined(_SIGMAX)
#  define NSIG (_SIGMAX + 1)    /* For QNX */
# elif defined(SIGMAX)
#  define NSIG (SIGMAX + 1)     /* For djgpp */
# else
#  define NSIG 64               /* Use a reasonable default value */
# endif
#endif

#ifdef HAVE_SIGACTION
typedef struct sigaction _Py_sighandler_t;
#else
typedef PyOS_sighandler_t _Py_sighandler_t;
#endif

typedef struct {
    int signum;
    PyObject *address;
    _Py_sighandler_t previous;
} pdbhandler_signal_t;

static pdbhandler_signal_t pdbhandler_signal;
#endif

/* A dummy object that ends the pdb's subinterpreter when deallocated. */
typedef struct {
    PyObject_HEAD
    PyThreadState *substate;
} pdbtracerctxobject;

/* Only one instance of pdbtracerctxobject at any given time.
 * Note that we do not own a reference to this object. The 'stdin' pdb
 * attribute owns a reference to this object, 'stdin' being an instance of
 * pdb.RemoteSocket. */
static pdbtracerctxobject *current_pdbctx = NULL;

/* Forward declarations. */
static void pdbtracerctx_dealloc(pdbtracerctxobject *);

static PyTypeObject pdbtracerctxtype = {
    PyVarObject_HEAD_INIT(NULL, 0)
    "pdbhandler.context",               /* tp_name        */
    sizeof(pdbtracerctxobject),         /* tp_basicsize   */
    0,                                  /* tp_itemsize    */
    (destructor)pdbtracerctx_dealloc,   /* tp_dealloc     */
    0,                                  /* tp_print       */
    0,                                  /* tp_getattr     */
    0,                                  /* tp_setattr     */
    0,                                  /* tp_reserved    */
    0,                                  /* tp_repr        */
    0,                                  /* tp_as_number   */
    0,                                  /* tp_as_sequence */
    0,                                  /* tp_as_mapping  */
    0,                                  /* tp_hash        */
    0,                                  /* tp_call        */
    0,                                  /* tp_str         */
    0,                                  /* tp_getattro    */
    0,                                  /* tp_setattro    */
    0,                                  /* tp_as_buffer   */
    Py_TPFLAGS_DEFAULT,                 /* tp_flags       */
    "Pdb tracer context",               /* tp_doc         */
};

/* Set up pdb in a sub-interpreter to handle the cases where we are stopped in
 * a loop iterating over sys.modules, or within the import system, or while
 * sys.modules or builtins are empty (such as in some test cases), and to
 * avoid circular imports. */
static int
bootstrappdb(void *args)
{
    PyThreadState *substate;
    Py_tracefunc tracefunc;
    PyObject *tracemalloc;
    PyObject *traceobj;
    PyObject *type, *value, *traceback;
    PyThreadState *mainstate = PyThreadState_GET();
    PyObject *pdb = NULL;
    PyObject *rsock = NULL;
    int rc = -1;

    /* When bootstrappdb() is a signal handler, 'args' is the address field of
     * a pdbhandler_signal_t structure.
     * 'kwds', a copy of the 'args' dictionary, is passed as argument to
     * set_trace_remote(). */
    if (!PyDict_Check((PyObject *)args)) {
        PyErr_SetString(PyExc_TypeError, "'args' must be a dict");
        return -1;
    }

    if (!Py_IsInitialized())
        return 0;

#ifdef WITH_THREAD
    /* Do not instantiate pdb when stopped in a subinterpreter. */
# if PY_MINOR_VERSION >= 4
    if (!PyGILState_Check())
# else
    if (!mainstate || mainstate != PyGILState_GetThisThreadState())
# endif
        return 0;
#endif

    /* The tracemalloc module calls the PyGILState_ API during the
     * subinterpreter creation and instantiation of pdb. */
    tracemalloc = PyImport_ImportModule("tracemalloc");
    if (tracemalloc != NULL ) {
        int istrue;
        PyObject *rv = PyObject_CallMethod(tracemalloc, "is_tracing", NULL);
        Py_DECREF(tracemalloc);
        if (rv == NULL)
            return -1;
        istrue = (rv == Py_True) ? 1 : 0;
        Py_DECREF(rv);
        if (istrue)
            Py_FatalError("cannot run pdbhandler while"
                          " tracemalloc is tracing");
    }
    else
        PyErr_Clear();

    /* See python issue 21033. */
    if (mainstate->tracing || current_pdbctx)
        return 0;

    pdbtracerctxtype.tp_new = PyType_GenericNew;
    if (PyType_Ready(&pdbtracerctxtype) < 0)
        return -1;

    PyThreadState_Swap(NULL);
    if ((substate=Py_NewInterpreter()) == NULL) {
        PyThreadState_Swap(mainstate);
        PyErr_SetString(PyExc_RuntimeError,
                        "pdb subinterpreter creation failed");
        return -1;
    }

    pdb = PyImport_ImportModule("pdb_clone.pdb");
    if (pdb != NULL ) {
        PyObject *func = PyObject_GetAttrString(pdb, "set_trace_remote");
        if (func != NULL) {
            PyObject *kwds = PyDict_Copy((PyObject *)args);
            if (kwds && PyDict_SetItemString(kwds, "frame",
                                    (PyObject *)mainstate->frame) == 0) {
                PyObject *empty_tuple = PyTuple_New(0);
                rsock = PyObject_Call(func, empty_tuple, kwds);
                Py_DECREF(empty_tuple);
            }
            Py_XDECREF(kwds);
        }
        Py_XDECREF(func);
    }

    tracefunc = substate->c_tracefunc;
    traceobj = substate->c_traceobj;
    Py_XINCREF(traceobj);
    if (rsock == NULL)
        goto err;
    if (tracefunc == NULL) {
        PyErr_SetString(PyExc_RuntimeError,
                        "Internal error - trace function not set");
        goto err;
    }

    /* The sub-interpreter remains alive until the pdb socket is closed. */
    current_pdbctx = (pdbtracerctxobject *) pdbtracerctxtype.tp_alloc(
                                                    &pdbtracerctxtype, 0);
    if (current_pdbctx == NULL)
        goto err;
    if (PyObject_SetAttrString(rsock, "_pdbtracerctxobject",
                                      (PyObject *)current_pdbctx) != 0)
        goto err;
    current_pdbctx->substate = substate;

    /* Swap the trace function between both tread states. */
    PyEval_SetTrace(NULL, NULL);
    PyThreadState_Swap(mainstate);
    PyEval_SetTrace(tracefunc, traceobj);
    Py_DECREF(traceobj);
    rc = 0;
    goto fin;

err:
    Py_XDECREF(traceobj);
    PyErr_Fetch(&type, &value, &traceback);
    Py_EndInterpreter(substate);
    PyThreadState_Swap(mainstate);
    if (type)
        PyErr_Restore(type, value, traceback);
fin:
    Py_XDECREF(pdb);
    Py_XDECREF(rsock);
    Py_XDECREF(current_pdbctx);
    return rc;
}

int
bootstrappdb_string(char *arg)
{
    PyObject *addresslist;
    PyObject *addressdict = NULL;
    PyObject *host = NULL;
    PyObject *port = NULL;
#if PY_MINOR_VERSION >= 3
    PyObject *address = PyUnicode_DecodeLocale(arg, NULL);
#else
    PyObject *address = PyUnicode_DecodeFSDefault(arg);
#endif
    int rc = -1;

    if (address == NULL)
        return -1;
    if ((addresslist=PyUnicode_Split(address, NULL, -1)) == NULL)
        goto err;
    if ((addressdict=PyDict_New()) == NULL)
        goto err;

    if (Py_SIZE(addresslist) >= 1) {
#if PY_MINOR_VERSION >= 3
        host = PyUnicode_EncodeLocale(PyList_GET_ITEM(addresslist, 0), NULL);
#else
        host = PyUnicode_EncodeFSDefault(PyList_GET_ITEM(addresslist, 0));
#endif
        if (host == NULL)
            goto err;
        if (PyDict_SetItemString(addressdict, "host", host) != 0)
            goto err;
    }
    if (Py_SIZE(addresslist) >= 2) {
#if PY_MINOR_VERSION >= 3
        port = PyLong_FromUnicodeObject(PyList_GET_ITEM(addresslist, 1), 10);
#else
        Py_ssize_t length;
        PyObject *item = PyList_GET_ITEM(addresslist, 1);
        length = PyUnicode_GET_SIZE(item);
        port = PyLong_FromUnicode(PyUnicode_AS_UNICODE(item), length, 10);
#endif
        if (port == NULL)
            goto err;
        if (PyDict_SetItemString(addressdict, "port", port) != 0)
            goto err;
    }
    rc = bootstrappdb(addressdict);

err:
    Py_DECREF(address);
    Py_XDECREF(addresslist);
    Py_XDECREF(addressdict);
    Py_XDECREF(host);
    Py_XDECREF(port);
    return rc;
}

static void
pdbtracerctx_dealloc(pdbtracerctxobject *self)
{
    if (self->substate != NULL) {
        PyThreadState *substate = PyThreadState_GET();
        PyThreadState_Swap(self->substate);
        Py_EndInterpreter(self->substate);
        PyThreadState_Swap(substate);
        self->substate = NULL;
    }
    Py_TYPE(self)->tp_free((PyObject*)self);
    current_pdbctx = NULL;
}

/* Windows does not have signal processing. */
#ifndef MS_WINDOWS
static void
_pdbhandler(int signum)
{
    pdbhandler_signal_t *psignal = &pdbhandler_signal;

    if (psignal->signum != signum)
        return;

    /* Silently ignore a full queue condition or a lock race condition. */
    Py_AddPendingCall(bootstrappdb, (void *)psignal->address);
}

static int
check_signum(int *psignum)
{
    if (*psignum == 0)
        *psignum = SIGUSR1;
    if (*psignum < 0 || *psignum >= NSIG) {
        PyErr_SetString(PyExc_ValueError, "signal number out of range");
        return 0;
    }
    return 1;
}

static int atexit_register(void)
{
    PyObject *pdbhandler;
    PyObject *unregister;
    PyObject *atexit;
    PyObject *rv;
    int rc;
    static int registered = 0;

    if (registered)
        return 0;
    registered = 1;

    pdbhandler = PyImport_ImportModule("pdb_clone.pdbhandler");
    if (pdbhandler == NULL)
        return -1;
    unregister = PyObject_GetAttrString(pdbhandler, "unregister");
    Py_DECREF(pdbhandler);
    if (unregister == NULL)
        return -1;
    atexit = PyImport_ImportModule("atexit");
    if (atexit == NULL) {
        Py_DECREF(unregister);
        return -1;
    }
    rv = PyObject_CallMethod(atexit, "register", "O", unregister);
    rc = (rv != NULL ? 0 : -1);

    Py_DECREF(atexit);
    Py_DECREF(unregister);
    Py_XDECREF(rv);
    return rc;
}

static void
_unregister(pdbhandler_signal_t *psignal)
{
    if (psignal->signum == 0)
        return;
#ifdef HAVE_SIGACTION
    (void)sigaction(psignal->signum, &psignal->previous, NULL);
#else
    (void)signal(psignal->signum, psignal->previous);
#endif
    psignal->signum = 0;
    Py_CLEAR(psignal->address);
}

static int
_register(pdbhandler_signal_t *psignal, PyObject *host, int port, int signum)
{
    PyObject *address;
    PyObject *host_bytes = NULL;
    PyObject *port_obj = NULL;
    int rc = -1;

    if (!check_signum(&signum))
        return -1;

    /* Build the address dict. */
    if ((address=PyDict_New()) == NULL)
        return -1;
    if (host != NULL) {
#if PY_MINOR_VERSION >= 3
        host_bytes = PyUnicode_EncodeLocale(host, NULL);
#else
        host_bytes = PyUnicode_EncodeFSDefault(host);
#endif
        if (host_bytes == NULL)
            goto err;
        if (PyDict_SetItemString(address, "host", host_bytes) != 0)
            goto err;
    }
    if (port != 0) {
        port_obj = PyLong_FromLong(port);
        if (port_obj == NULL)
            goto err;
        if (PyDict_SetItemString(address, "port", port_obj) != 0)
            goto err;
    }

    if (psignal->signum != 0 && psignal->signum != signum)
        _unregister(psignal);

    if (psignal->signum == 0) {
        int err;
        _Py_sighandler_t previous;

#ifdef HAVE_SIGACTION
        struct sigaction action;
        action.sa_handler = _pdbhandler;
        sigemptyset(&action.sa_mask);
        action.sa_flags = SA_RESTART;
        err = sigaction(signum, &action, &previous);
#else
        previous = signal(signum, _pdbhandler);
        err = (previous == SIG_ERR);
#endif
        if (err) {
            PyErr_SetFromErrno(PyExc_OSError);
            goto err;
        }
        psignal->signum = signum;
        psignal->previous = previous;
    }

    Py_XDECREF(psignal->address);
    Py_INCREF(address);
    psignal->address = address;
    rc = atexit_register();

err:
    Py_DECREF(address);
    Py_XDECREF(host_bytes);
    Py_XDECREF(port_obj);
    return rc;
}

static PyObject*
_pdbhandler_register(PyObject *self, PyObject *args, PyObject *kwargs)
{
    static char *kwlist[] = {"host", "port", "signum", NULL};
    int signum = 0;
    PyObject *host = NULL;
    int port = 0;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs,
            "|O!ii:register", kwlist, &PyUnicode_Type, &host, &port, &signum))
        return NULL;
    if (_register(&pdbhandler_signal, host, port, signum) == -1)
        return NULL;
    Py_RETURN_NONE;
}

static PyObject*
_pdbhandler_unregister(PyObject *self)
{
    _unregister(&pdbhandler_signal);
    Py_RETURN_NONE;
}

static PyObject*
_pdbhandler_registered(PyObject *self)
{
    PyObject *rv;
    pdbhandler_signal_t *psignal = &pdbhandler_signal;
    PyObject *host = NULL;
    PyObject *port = NULL;
    int port_0 = 0;

    if (psignal->address) {
        host = PyDict_GetItemString(psignal->address, "host");
        port = PyDict_GetItemString(psignal->address, "port");
    }
    if (port == NULL) {
        port_0 = 1;
        port = PyLong_FromLong(0);
    }
    rv = Py_BuildValue("(OOi)", host == NULL ? Py_None: host, port,
                       psignal->signum);
    if (port_0)
        Py_DECREF(port);
    return rv;
}

static PyMethodDef _pdbhandler_methods[] = {
    {"_register",
     (PyCFunction)_pdbhandler_register, METH_VARARGS|METH_KEYWORDS, NULL},
    {"_unregister", (PyCFunction)_pdbhandler_unregister, METH_NOARGS, NULL},
    {"_registered", (PyCFunction)_pdbhandler_registered, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}        /* Sentinel */
};
#endif

PyDoc_STRVAR(_pdbhandler_doc, "The _pdbhandler module.");

static struct PyModuleDef _pdbhandler_def = {
    PyModuleDef_HEAD_INIT,
    "_pdbhandler",
    _pdbhandler_doc,
    -1,
#ifndef MS_WINDOWS
    _pdbhandler_methods,
#else
    NULL,
#endif
    NULL,
    NULL,
    NULL,
    NULL
};

PyMODINIT_FUNC
PyInit__pdbhandler(void)
{
    return PyModule_Create(&_pdbhandler_def);
}
#endif  /* PY_MAJOR_VERSION >= 3 */

