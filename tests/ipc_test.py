"""Named-pipe IPC: framing, size caps, disconnect handling and name reuse.

The container-side denial checks live in scripts/probe-appcontainer-ipc.py; here the pipe is
exercised against real child processes on the host. Children are started with Popen and served by
the parent, never awaited first: a child that is doing a request/response roundtrip would otherwise
deadlock against the parent waiting for it to exit. Every parent-side read has a deadline.
"""
import json
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))
import ipc

CHILD_PRELUDE = "import sys; sys.path.insert(0, r'%s'); import ipc, json, struct, time; " % ENGINE


class PipeTests(unittest.TestCase):
    def setUp(self):
        self.name = "dsh-knowledge-test-" + uuid.uuid4().hex
        self.servers = []
        self.children = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        for server in self.servers:
            server.close()

    def listen(self, name=None):
        server = ipc.PipeServer(name or self.name).listen()
        self.servers.append(server)
        return server

    def start_child(self, code, *arguments):
        process = subprocess.Popen([sys.executable, "-c", CHILD_PRELUDE + code, *arguments],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        return process

    def finish_child(self, process, timeout=15):
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout, stderr

    def test_cancellation_before_connect_submission_cannot_hang_teardown(self):
        # A subprocess bounds even a regression that blocks inside the native API.
        code = r"""
import threading, uuid
ready = threading.Event(); release = threading.Event(); submitted = threading.Event()
connect = ipc.kernel32.ConnectNamedPipe; cancel = ipc.kernel32.CancelIoEx
attempts = [0]
def delayed_connect(handle, overlapped):
    ready.set(); assert release.wait(5); submitted.set()
    return connect(handle, overlapped)
def cancel_before_submission(handle, overlapped):
    result = cancel(handle, overlapped)
    attempts[0] += 1
    if attempts[0] == 1:
        release.set(); assert submitted.wait(5); time.sleep(.05)
    return result
ipc.kernel32.ConnectNamedPipe = delayed_connect
ipc.kernel32.CancelIoEx = cancel_before_submission
server = ipc.PipeServer('synthetic-close-race-' + uuid.uuid4().hex).listen()
assert ready.wait(5)
server.close()
assert server.finished.is_set() and not server._thread.is_alive()
assert server.handle is None
server.close()
print('teardown-completed')
"""
        run = subprocess.run([sys.executable, "-c", CHILD_PRELUDE + code],
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("teardown-completed", run.stdout)

    def test_roundtrip_with_a_real_child_process(self):
        server = self.listen()
        process = self.start_child("print(json.dumps(ipc.roundtrip(sys.argv[1], json.loads(sys.argv[2]))))",
                                   server.path, json.dumps({"ping": 1, "text": "子进程"}))
        self.assertTrue(server.wait_connected(10))
        self.assertEqual(server.receive(timeout=10), {"ping": 1, "text": "子进程"})
        server.send({"server": "parent"})
        code, stdout, stderr = self.finish_child(process)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout.strip()), {"server": "parent"})

    def test_oversized_frame_is_refused_before_the_body_is_read(self):
        server = self.listen()
        process = self.start_child("import struct; h = ipc.connect(sys.argv[1]);"
                                   "ipc._write_all(h, struct.pack('<I', ipc.MAX_MESSAGE + 1))",
                                   server.path)
        self.assertTrue(server.wait_connected(10))
        with self.assertRaises(ipc.PipeMessageTooLarge) as caught:
            server.receive(timeout=10)
        self.assertIn(str(ipc.MAX_MESSAGE), str(caught.exception))
        self.finish_child(process)

    def test_frame_that_never_completes_hits_the_read_deadline(self):
        server = self.listen()
        process = self.start_child("import struct, time; h = ipc.connect(sys.argv[1]);"
                                   "ipc._write_all(h, struct.pack('<I', 100)); time.sleep(30)", server.path)
        self.assertTrue(server.wait_connected(10))
        with self.assertRaises(ipc.PipeTimeout):
            server.receive(timeout=0.5)
        process.kill()
        self.finish_child(process)

    def test_malformed_body_is_reported_and_the_pipe_stays_usable(self):
        server = self.listen()
        process = self.start_child("import struct; h = ipc.connect(sys.argv[1]);"
                                   "ipc._write_all(h, struct.pack('<I', 5) + b'not-!')", server.path)
        self.assertTrue(server.wait_connected(10))
        with self.assertRaises(json.JSONDecodeError):
            server.receive(timeout=10)
        self.assertIsNotNone(server.handle, "a malformed frame must not tear the pipe down")
        self.finish_child(process)

    def test_clean_disconnect_is_reported_as_eof_and_the_name_can_be_reused(self):
        server = self.listen()
        process = self.start_child("h = ipc.connect(sys.argv[1]); ipc.close(h)", server.path)
        self.assertTrue(server.wait_connected(10))
        self.assertIsNone(server.receive(timeout=10), "peer closed before sending: expect EOF")
        self.assertEqual(self.finish_child(process)[0], 0)
        server.close()

        # Same name again: a leaked name makes CreateNamedPipeW fail with ERROR_ACCESS_DENIED.
        second = self.listen()
        follower = self.start_child("print(json.dumps(ipc.roundtrip(sys.argv[1], {'again': True})))", second.path)
        self.assertTrue(second.wait_connected(10),
                        "second server never accepted a connection: %r" % (second.error,))
        self.assertEqual(second.receive(timeout=10), {"again": True})
        second.send({"second": True})
        code, stdout, stderr = self.finish_child(follower)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout.strip()), {"second": True})

    def test_killed_child_is_detected_and_leaves_no_stale_pipe(self):
        server = self.listen()
        process = self.start_child("h = ipc.connect(sys.argv[1]); time.sleep(30)", server.path)
        self.assertTrue(server.wait_connected(10))
        process.kill()
        self.finish_child(process)
        # A killed peer and a peer that closed cleanly both surface as "no more data"; what matters
        # is that the parent detects it instead of blocking, and that the name is released.
        self.assertIsNone(server.receive(timeout=10), "parent must observe the peer going away")
        server.close()

        replacement = self.listen()
        follower = self.start_child("print(json.dumps(ipc.roundtrip(sys.argv[1], {'fresh': True})))", replacement.path)
        self.assertTrue(replacement.wait_connected(10),
                        "replacement server never accepted a connection: %r" % (replacement.error,))
        self.assertEqual(replacement.receive(timeout=10), {"fresh": True})
        replacement.send({"ok": True})
        self.assertEqual(self.finish_child(follower)[0], 0)

    def test_connections_are_served_in_sequence_after_a_restart(self):
        server = self.listen()
        for index in range(2):
            process = self.start_child("print(ipc.roundtrip(sys.argv[1], {'n': int(sys.argv[2])})['n'])",
                                       server.path, str(index))
            self.assertTrue(server.wait_connected(10))
            self.assertEqual(server.receive(timeout=10), {"n": index})
            server.send({"n": index})
            code, stdout, stderr = self.finish_child(process)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(stdout.strip(), str(index))
            if index == 0:
                server.close()
                server = self.listen()


if __name__ == "__main__":
    unittest.main()
