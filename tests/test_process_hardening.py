"""Process ownership, cancellation and actual Windows job cleanup."""

import asyncio
import ctypes
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agy_provider.errors import AgyError
from agy_provider.process import capture, spawn, stop_process


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == "nt", "Windows host crash")
    async def test_host_abrupt_exit_closes_job_and_kills_owned_child(self):
        script = """
import asyncio, os, sys
from agy_provider.process import spawn
async def main():
    proc = await spawn([sys.executable, '-c', 'import time; time.sleep(60)'])
    print(proc.pid if proc._agy_job is not None else 0, flush=True)
    sys.stdin.buffer.read(1)
    os._exit(0)
asyncio.run(main())
"""
        helper = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000,
        )
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes, api.OpenProcess.restype = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong], ctypes.c_void_p
        api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = [ctypes.c_void_p, ctypes.c_ulong], ctypes.c_ulong
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = None
        try:
            pid = int(await asyncio.wait_for(helper.stdout.readline(), 3))
            self.assertGreater(pid, 0)
            handle = api.OpenProcess(0x100000, False, pid)
            self.assertTrue(handle)
            helper.stdin.write(b"x")
            await helper.stdin.drain()
            await asyncio.wait_for(helper.wait(), 3)
            self.assertEqual(helper.returncode, 0)
            self.assertEqual(api.WaitForSingleObject(handle, 2000), 0)
        finally:
            if helper.returncode is None:
                helper.kill()
                await helper.wait()
            helper.stdin.close()
            if handle:
                api.CloseHandle(handle)

    async def test_capture_limit_and_timeout_reap_children(self):
        for script, expected in (("print('x'*70000)", "OUTPUT_LIMIT"),
                                 ("import time; time.sleep(30)", "TIMEOUT")):
            with self.assertRaises(AgyError) as caught:
                await capture([sys.executable, "-c", script], 0.2 if expected == "TIMEOUT" else 3)
            self.assertEqual(caught.exception.code, expected)

    async def test_repeated_close_shares_one_cleanup(self):
        proc = SimpleNamespace()
        with patch("agy_provider.process._stop_process", AsyncMock()) as cleanup:
            await asyncio.gather(stop_process(proc), stop_process(proc), stop_process(proc))
        cleanup.assert_awaited_once_with(proc)

    async def test_repeated_spawn_cancel_does_not_abandon_child(self):
        started, finish = asyncio.Event(), asyncio.Event()
        child = SimpleNamespace(pid=999999)
        async def create(*args, **kwargs):
            started.set()
            await finish.wait()
            return child
        with patch("agy_provider.process.asyncio.create_subprocess_exec", create), \
             patch("agy_provider.process.WindowsJob.attach", return_value=None), \
             patch("agy_provider.process.stop_process", AsyncMock()) as stop:
            task = asyncio.create_task(spawn(["fake"]))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            stop.assert_awaited_once_with(child)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    async def test_job_binding_failure_falls_back_without_breaking_stdio(self):
        with patch("agy_provider.process.WindowsJob.attach", return_value=None):
            proc = await spawn([sys.executable, "-c", "print('hello')"])
        try:
            self.assertIsNone(proc._agy_job)
            self.assertEqual((await proc.communicate())[0].strip(), b"hello")
        finally:
            await stop_process(proc)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    async def test_job_kills_descendant_after_front_process_exits(self):
        script = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "print(p.pid,flush=True); time.sleep(0.5)"
        )
        proc = await spawn([sys.executable, "-c", script])
        self.addAsyncCleanup(stop_process, proc)
        self.assertIsNotNone(proc._agy_job, "Job binding failed on this environment")
        pid = int(await asyncio.wait_for(proc.stdout.readline(), 3))
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes, api.OpenProcess.restype = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong], ctypes.c_void_p
        api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = [ctypes.c_void_p, ctypes.c_ulong], ctypes.c_ulong
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = api.OpenProcess(0x100000, False, pid)
        self.assertTrue(handle)
        try:
            await asyncio.wait_for(proc.wait(), 3)
            self.assertEqual(api.WaitForSingleObject(handle, 0), 258)  # STILL RUNNING
            await stop_process(proc)
            self.assertEqual(api.WaitForSingleObject(handle, 2000), 0)
        finally:
            api.CloseHandle(handle)
