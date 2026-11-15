"""Best-effort Windows child-tree ownership using stdlib ctypes only.

No global process enumeration or PID cleanup list. The handle is non-inheritable;
closing it (including host termination) kills only descendants assigned to this job.
Asyncio does not expose atomic suspended-create/assign/resume: attachment is best
effort immediately after spawn, and taskkill remains the documented fallback.
"""

import ctypes
import asyncio
import os
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime",
    )] + [(name, wintypes.DWORD) for name in (
        "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses",
    )]


class WindowsJob:
    def __init__(self, api, handle):
        self._api, self._handle = api, handle

    @classmethod
    def attach(cls, pid: int):
        if os.name != "nt":
            return None
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        ):
            function = getattr(api, name)
            function.argtypes, function.restype = args, result
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            return None
        process = None
        assigned = False
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                return None
            process = api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not api.AssignProcessToJobObject(handle, process):
                return None
            assigned = True
            return cls(api, handle)
        finally:
            if process:
                api.CloseHandle(process)
            if not assigned:
                api.CloseHandle(handle)

    def close(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            self._api.CloseHandle(handle)

    async def stop(self) -> None:
        if self._handle is None:
            return
        try:
            # CloseHandle's kill-on-close is asynchronous. Waiting only for the
            # front process can leave a dying MCP child holding the workspace
            # as its cwd, causing WinError 32 during session directory cleanup.
            self._api.TerminateJobObject(self._handle, 1)
            deadline = asyncio.get_running_loop().time() + 3
            while True:
                accounting = _Accounting()
                if not self._api.QueryInformationJobObject(
                    self._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                ) or accounting.ActiveProcesses == 0:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.01)
        finally:
            self.close()
