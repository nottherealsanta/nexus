"""Repeatable offline TUI projection/wire benchmark (TUI_LOCAL_INTERACTION_PLAN §0).

PYTHONPATH=.:tests .venv/bin/python tests/tui_responsiveness_bench.py --label baseline
Native trace measurements use the same generated snapshots in the PTY benchmark.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import platform
import statistics
import tempfile
import time
from types import SimpleNamespace

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state
from nexus.view.model import BlockView, MessageView, ToolCallView, TurnView


def workload(count, large=False):
    turns = []
    for i in range(count):
        turns.append(TurnView(id=f"t{i}", index=i, phase="completed", elapsed_ms=100,
            messages=[MessageView(id=f"u{i}", role="user", event_seq=1, blocks=[BlockView(text="Inspect the fixture")]),
                      MessageView(id=f"a{i}", role="assistant", event_seq=4, blocks=[BlockView(kind="thinking", text="Inspect the file.\nCheck the diff."), BlockView(text="The fixture is updated.")])],
            tools=[ToolCallView(call_id=f"c{i}", name="Read", event_seq=2, status="completed", input={"path":"mock-notes.txt"}, display="alpha\nbeta\n"),
                   ToolCallView(call_id=f"e{i}", name="Edit", event_seq=3, status="completed", input={"path":"mock-notes.txt"}, diff={"path":"mock-notes.txt", "hunk":"@@ -1,2 +1,2 @@\n alpha\n-beta\n+nexus\n", "added_lines":1, "removed_lines":1})]))
    if large:
        turns[-1] = replace(turns[-1], tools=[replace(turns[-1].tools[0], display=("0123456789abcdef\n" * 61681))])
    view = initial_state("bench")
    view.turns = turns
    controller = SimpleNamespace(view=view, session="bench")
    shell = ShellActions(controller)
    shell.local_transcript = True
    shell.preferences.values["context_preview"] = False
    shell.history = [f"Historical prompt {i}" for i in range(200)]
    return controller, shell


def stats(values):
    values = sorted(values)
    return {"p50": statistics.median(values), "p95": values[min(len(values)-1, int(len(values)*.95))], "max": max(values)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="current")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--baseline-module")
    parser.add_argument("--binary", default="rust/tui/target/debug/nexus-ratatui")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--output", default="artifacts/tui-responsiveness")
    args = parser.parse_args()
    projection = project
    if args.baseline_module:
        import importlib.util
        spec = importlib.util.spec_from_file_location("nexus.ui.ratatui._latency_baseline", args.baseline_module)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        projection = module.project
    import subprocess
    try:
        cpu = subprocess.check_output(["sysctl","-n","machdep.cpu.brand_string"],text=True).strip()
    except (OSError,subprocess.CalledProcessError):
        cpu = platform.processor()
    report = {"cpu":cpu,"label": args.label, "hardware": platform.platform()+" / "+platform.processor(), "python":platform.python_version(), "samples":50, "workloads":{}}
    with tempfile.TemporaryDirectory() as temp:
        os.environ["XDG_CONFIG_HOME"] = temp
        for count, large in ((10,False), (500,False), (10,True)):
            controller, shell = workload(count,large)
            times = {key:[] for key in ("project_ms","fingerprint_ms","encode_ms","bytes")}
            initial_at=time.perf_counter()
            first = projection(controller, 1, shell=shell, literal=False)
            initial_ms=(time.perf_counter()-initial_at)*1000
            initial_bytes = len(json.dumps(first).encode())
            if not args.baseline:
                from nexus.ui.ratatui.wire import TerminalWire
                wire_encoder = TerminalWire()
                wire_encoder.encode(first)
                from nexus.ui.ratatui.stream_projection import StreamProjection
                from nexus.ui.ratatui.prototype import _project_turn
                mirror = StreamProjection()
                mirror.remember(first, controller, shell)
            previous = [(b["id"], b["rev"]) for b in first["blocks"]]
            for n in range(50):
                turn = controller.view.turns[-1]
                message = turn.messages[-1]
                controller.view.turns[-1] = replace(turn, messages=[*turn.messages[:-1], replace(message, blocks=[*message.blocks[:-1], replace(message.blocks[-1], text=message.blocks[-1].text+" token")])])
                at = time.perf_counter()
                snap = projection(controller,n+2,shell=shell,literal=False) if args.baseline else mirror.project(controller,shell,n+2,controller.view.turns[-1].id,_project_turn)
                end = time.perf_counter()
                times["project_ms"].append((end-at)*1000)
                if args.baseline:
                    json.dumps({**snap,"revision":0,"blocks":[(b["id"], b["rev"]) for b in snap["blocks"]]},ensure_ascii=False)
                    current=[(b["id"],b["rev"]) for b in snap["blocks"]]
                    start=0
                    while start < min(len(current),len(previous)) and current[start]==previous[start]: start+=1
                    wire={**snap,"schema":2,"blocks_from":start,"blocks":snap["blocks"][start:]}
                    previous=current
                else:
                    wire=wire_encoder.encode(snap)
                encoded_at=time.perf_counter()
                times["fingerprint_ms"].append((encoded_at-end)*1000)
                encoded=json.dumps(wire,ensure_ascii=False,separators=(",", ":")).encode()
                times["encode_ms"].append((time.perf_counter()-encoded_at)*1000)
                times["bytes"].append(len(encoded))
            report["workloads"][f"{count}-turn"+("-1MB" if large else "")]={"initial_bytes":initial_bytes,"initial_project_ms":initial_ms, **{k:stats(v) for k,v in times.items()}}
    if args.native:
        from ratatui_latency_probe import benchmark, disclosure_benchmark
        from nexus.ui.ratatui.wire import TerminalWire
        from nexus.ui.ratatui.stream_projection import StreamProjection
        class BaselineWire:
            def __init__(self): self.prior=[]
            def encode(self,snapshot):
                keys=[(b["id"],b["rev"]) for b in snapshot["blocks"]]
                start=0
                while start < min(len(keys),len(self.prior)) and keys[start]==self.prior[start]: start+=1
                self.prior=keys
                return {**snapshot,"schema":2,"blocks_from":start,"blocks":snapshot["blocks"][start:]}
        for count in (10,500):
            controller,shell=workload(count)
            directory=Path(args.output)/args.label
            directory.mkdir(parents=True,exist_ok=True)
            report["workloads"][f"{count}-turn"]["native_trace"]=benchmark(Path(args.binary).resolve(),BaselineWire() if args.baseline else TerminalWire(),controller,shell,count,directory,projection,None if args.baseline else StreamProjection())
        if not args.baseline:
            for count in (10,500):
                report["workloads"][f"{count}-turn"]["disclosure_trace"] = disclosure_benchmark(Path(args.binary).resolve(), count, directory)
            report["workloads"]["10-turn-1MB"]["open_trace"] = disclosure_benchmark(Path(args.binary).resolve(), 10, directory, large=True)
    if args.live:
        from ratatui_latency_probe import live_benchmark
        directory=Path(args.output)/args.label
        directory.mkdir(parents=True,exist_ok=True)
        for count in (10,500):
            report["workloads"][f"{count}-turn"]["live_trace"] = live_benchmark(count,directory)
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    (output/f"{args.label}.json").write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))

if __name__=="__main__": main()
