"""
A multiprocessing context where ``proc.daemon`` is always False,
and setting it to True is ignored.
All processes will still be cleaned up though.
For cleaning up, we use SIGINT (instead of the standard SIGTERM)
such that the processes can do proper cleanup themselves.

See :class:`NonDaemonicSpawnProcess` and :class:`NonDaemonicSpawnContext`.

This is when you don't want to use the default multiprocessing fork start method,
but the spawn start method, but you also want that the started processes are never daemonic.

References:
    https://github.com/rwth-i6/returnn/issues/1494
    https://github.com/rwth-i6/returnn/issues/1495
    https://github.com/pytorch/pytorch/issues/15950
"""

from __future__ import annotations
from typing import Optional, Any, Callable, Tuple
import os
import signal
import atexit

# noinspection PyProtectedMember
from multiprocessing.context import BaseContext, SpawnProcess


class NonDaemonicSpawnProcess(SpawnProcess):
    """
    This process is always non-daemon, even if ``proc.daemon=True`` is executed
    (like https://stackoverflow.com/a/8963618/133374).

    Still, we make sure that the proc is cleaned up at exit.
    Instead of using SIGTERM as Python does for normal daemonic threads,
    we use SIGINT, to allow the subprocess to do proper cleanup,
    e.g. like cleaning up sub-sub procs.
    So the sub proc would not leave its children orphaned.
    Note, if SIGINT does nothing on the subproc, this will hang.
    """

    daemon = property(lambda self: False, lambda self, v: None)  # always False

    pre_init_func: Optional[Callable[[], None]] = None

    _at_exit_cleanup_handler: Optional[_AtExitCleanupProcess] = None

    def start(self):
        """start"""
        super().start()
        self._at_exit_cleanup_handler = _AtExitCleanupProcess(self.ident)
        atexit.register(self._at_exit_cleanup_handler)

    def terminate(self):
        """terminate"""
        super().terminate()
        self._close_cleanup()

    def kill(self):
        """kill"""
        super().kill()
        self._close_cleanup()

    def join(self, timeout=None):
        """join"""
        super().join(timeout=timeout)
        self._close_cleanup()

    def close(self):
        """close"""
        super().close()
        self._close_cleanup()

    def _close_cleanup(self):
        """close"""
        if not self._at_exit_cleanup_handler:
            return
        atexit.unregister(self._at_exit_cleanup_handler)
        self._at_exit_cleanup_handler = None

    def __reduce__(self):
        res_tuple = super().__reduce__()
        if not self.pre_init_func:
            return res_tuple
        reconstruct_func, reconstruct_args, *other = res_tuple
        # Use our own reconstruct function to call the pre_init_func.
        # This is unpickled and executed *before* the other state is unpickled.
        # This is important: This allows to potentially prepare some global state,
        # to make the following unpickling work.
        # E.g. in case the remaining state depends on some dynamic module,
        # which must be imported in a special way before (e.g. __returnn_config__),
        # this is the way to do it.
        # Note that internally, multiprocessing SpawnProcess does sth similar,
        # see multiprocessing.spawn._main, spawn.prepare.
        return self._reconstruct_with_pre_init_func, (reconstruct_func, reconstruct_args, self.pre_init_func), *other

    @staticmethod
    def _reconstruct_with_pre_init_func(
        reconstruct_func: Callable, reconstruct_args: Tuple[Any, ...], pre_init_func: Callable[[], None]
    ):
        pre_init_func()
        return reconstruct_func(*reconstruct_args)


class NonDaemonicSpawnContext(BaseContext):
    """
    Spawn start methods, where all procs are non-daemonic.
    """

    _name = "spawn_non_daemonic"

    def __init__(self, *, process_pre_init_func: Optional[Callable[[], None]] = None):
        super().__init__()
        self.process_pre_init_func = process_pre_init_func

    # noinspection PyPep8Naming
    def Process(self, *args, **kwargs):
        """create a new process"""
        proc = NonDaemonicSpawnProcess(*args, **kwargs)
        if self.process_pre_init_func:
            proc.pre_init_func = self.process_pre_init_func
        return proc


class _AtExitCleanupProcess:
    def __init__(self, proc_pid: int):
        self.cur_pid = os.getpid()
        self.proc_pid = proc_pid

    def __call__(self):
        if os.getpid() != self.cur_pid:  # e.g. in fork
            return  # ignore
        if self.proc_pid is None:  # already cleaned
            return
        # The proc might have been killed by some other code. That's ok.
        try:
            # Send SIGINT, not SIGTERM or SIGKILL. See NonDaemonicSpawnProcess docstring.
            os.kill(self.proc_pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        else:
            try:
                os.waitpid(self.proc_pid, 0)
            except ChildProcessError:
                pass
        self.proc_pid = None
