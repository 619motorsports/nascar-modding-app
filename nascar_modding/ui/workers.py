from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class FunctionWorker(QRunnable):
    def __init__(self, function, *args, **kwargs):
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            result = self.function(*self.args, **self.kwargs)
        except Exception:
            try:
                self.signals.failed.emit(traceback.format_exc())
            except RuntimeError:
                # The owning page can be closed while a background task is
                # finishing.  Qt deletes its signal object in that case.
                pass
        else:
            try:
                self.signals.finished.emit(result)
            except RuntimeError:
                pass
