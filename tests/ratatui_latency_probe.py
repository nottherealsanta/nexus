"""Controlling-PTY latency harness for responsiveness tests and benchmarks."""
from __future__ import annotations
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import threading
import time


class NativeProbe:
    def __init__(self, binary, trace_path=None, command=None):
        self.master, self.slave = pty.openpty()
        termios.tcsetwinsize(self.slave, (40,140))
        self.output = bytearray()
        self.actions = bytearray()
        self.stop = threading.Event()
        self.peak_rss_kib = 0
        command = command or [str(binary)]
        setup = "import os,fcntl,termios; os.setsid(); fcntl.ioctl(2,termios.TIOCSCTTY,0); os.execv("+repr(command[0])+","+repr(command)+")"
        env=dict(os.environ)
        if trace_path:
            env.update(NEXUS_TUI_TRACE="1",NEXUS_TUI_TRACE_FILE=str(trace_path))
        self.process=subprocess.Popen([sys.executable,"-c",setup],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.slave,env=env)
        def drain():
            while not self.stop.is_set():
                if select.select([self.master],[],[],.05)[0]:
                    try: self.output.extend(os.read(self.master,65536))
                    except OSError: return
        self.thread=threading.Thread(target=drain,daemon=True);self.thread.start()

    def send(self, snapshot):
        self.process.stdin.write((json.dumps(snapshot,ensure_ascii=False,separators=(",", ":"))+"\n").encode())
        self.process.stdin.flush()

    def sample_memory(self):
        try:
            value = subprocess.check_output(["ps", "-o", "rss=", "-p", str(self.process.pid)],text=True).strip()
            self.peak_rss_kib = max(self.peak_rss_kib,int(value))
        except (ValueError,subprocess.CalledProcessError):
            pass

    def keys(self, value):
        os.write(self.master,value)

    def read_actions(self, wait=.05):
        if select.select([self.process.stdout],[],[],wait)[0]:
            self.actions.extend(os.read(self.process.stdout.fileno(),65536))
        rows=[]
        while b"\n" in self.actions:
            line,_,rest=self.actions.partition(b"\n");self.actions[:]=rest
            rows.append(json.loads(line))
        return rows

    def close(self):
        if self.process.poll() is None:
            self.keys(b"\x11")
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate();self.process.wait(timeout=3)
        self.stop.set();self.thread.join(timeout=1)
        self.process.stdin.close();self.process.stdout.close()
        os.close(self.master);os.close(self.slave)


def benchmark(binary, wire_encoder, controller, shell, count, output, project, mirror=None):
    trace=Path(output)/f"native-{count}.log"
    probe=NativeProbe(binary,trace)
    snap=project(controller,1,shell=shell,literal=False)
    probe.send(wire_encoder.encode(snap))
    deadline=time.monotonic()+4
    while b"\x1b" not in probe.output and probe.process.poll() is None and time.monotonic()<deadline:
        time.sleep(.02)
    time.sleep(.1)
    probe.sample_memory()
    from dataclasses import replace
    from nexus.ui.ratatui.prototype import _project_turn
    if mirror:
        mirror.remember(snap,controller,shell)
    for n in range(60):
        turn=controller.view.turns[-1];message=turn.messages[-1]
        controller.view.turns[-1]=replace(turn,messages=[*turn.messages[:-1],replace(message,blocks=[*message.blocks[:-1],replace(message.blocks[-1],text=message.blocks[-1].text+" token")])])
        at=time.time()
        snap=mirror.project(controller,shell,n+2,turn.id,_project_turn) if mirror else project(controller,n+2,shell=shell,literal=False)
        snap["event_sent_at"]=at
        wire=wire_encoder.encode(snap)
        if wire: probe.send(wire)
        if n%15==0: probe.sample_memory()
        if n%3==0: probe.keys(b"x\x7f")
        if n%4==0: probe.keys(b"\x1b[<64;20;10M")  # wheel up
        probe.read_actions(0)
        time.sleep(max(0,.02-(time.time()-at)))
    probe.sample_memory()
    probe.close()
    return (trace.read_text() + f"\npeak sampled RSS: {probe.peak_rss_kib} KiB") if trace.exists() else "No trace generated"


def disclosure_benchmark(binary, count, output, large=False):
    trace=Path(output)/f"disclosure-{count}{'-1MB' if large else ''}.log"
    probe=NativeProbe(binary,trace)
    group={"id":"g","kind":"tool_group","text":"LOCAL DISCLOSURE","local_ui":True,"rev":"1",
           "operation":{"kind":"block_toggle","id":"g"},"members":[
               {"id":"c","kind":"tool","heading":"Bash ls","local_ui":True,"rev":"1",
                "operation":{"kind":"block_toggle","id":"c:detail"},
                "output_operation":{"kind":"block_toggle","id":"c:output"},"fold_lines":13,
                "local_detail":"Parameters:\n  command: ls\nResult:\n"+("0123456789abcdef\n"*61681 if large else "local result\n"*30)}]}
    history=[{"id":str(i),"rev":"1","kind":"markdown","text":"Historical transcript row"} for i in range(count*5)]
    probe.send({"schema":3,"reset":True,"revision":1,"generation":1,"blocks":[*history,group],"sessions_sidebar":False,"details_sidebar":False})
    deadline=time.monotonic()+5
    while b"LOCAL DISCLOSURE" not in probe.output and probe.process.poll() is None and time.monotonic()<deadline: time.sleep(.02)
    assert probe.process.poll() is None
    if large:
        for keys in (b"\t\r",b"\t\r",b"\t\r"):
            probe.keys(keys);time.sleep(.3)
        time.sleep(1)
    else:
        for n in range(60):
            # Forty-row terminal, one header row and nine composer rows.
            row=31 if n%2==0 else 30
            probe.keys(f"\x1b[<0;12;{row}M\x1b[<0;12;{row}m".encode())
            time.sleep(.025)
            actions=probe.read_actions(0)
            assert all(action["type"]=="ui_trace" for action in actions),actions
    probe.sample_memory()
    probe.close()
    return (trace.read_text() + f"\npeak sampled RSS: {probe.peak_rss_kib} KiB") if trace.exists() else "No trace generated"


def live_benchmark(count, output):
    trace=Path(output)/f"live-{count}.log"
    probe=NativeProbe(None,trace,command=[sys.executable,"tests/ratatui_live_latency_fixture.py","--history",str(count)])
    try:
        deadline=time.monotonic()+25
        while b"LIVE STREAM DONE" not in probe.output and probe.process.poll() is None and time.monotonic()<deadline:
            time.sleep(.05)
        assert b"LIVE STREAM DONE" in probe.output, bytes(probe.output[-2000:])
        time.sleep(.15)
    finally:
        probe.close()
    return {"native":trace.read_text() if trace.exists() else "No trace", "python":Path(str(trace)+".python").read_text() if Path(str(trace)+".python").exists() else "No Python trace"}
