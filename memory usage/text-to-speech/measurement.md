# Text-to-speech memory

A process that has loaded the speech model uses **398.8 MiB** of physical memory. Loading Paradee and the English phonemizer, after onnxruntime, misaki, and spaCy are already imported, adds **176.9 MiB**.

Measured by `measure.py` on 2026-10-20 11:35 IST (Mac16,10, 16 GiB RAM, Darwin 27.0.0, Python 3.14.3).

The program loads the `/speak` stack Nexus uses: Paradee-8M int8 (`sahilmahendrakar/Paradee-8M-v1.0` at `v1.0`) in an onnxruntime CPU session, plus misaki and spaCy's `en_core_web_sm` phonemizer. It then reads this process's physical footprint. That is the number Activity Monitor shows as Memory. Resident size is the classic RSS figure. The two differ because the footprint counts compressed pages, while RSS counts resident pages, including some file-backed ones.

## Result

| Stage | Physical footprint | Resident size | Added since the previous stage |
| --- | ---: | ---: | ---: |
| Python interpreter | 9.9 MiB | 17.8 MiB | 9.9 MiB |
| onnxruntime imported | 27.2 MiB | 46.3 MiB | 17.3 MiB |
| misaki and spaCy imported | 222.0 MiB | 295.6 MiB | 194.8 MiB |
| Paradee model loaded | 398.8 MiB | 481.2 MiB | 176.9 MiB |

Peak physical footprint during this process: 398.8 MiB.
Peak resident size: 481.2 MiB.
`ru_maxrss`: 481.2 MiB.

Execution providers: CPUExecutionProvider. Session inputs: input_ids, speed.
onnxruntime 1.30.0. Load took 0.8 s.
Model file: `/Users/santa/.cache/huggingface/hub/models--sahilmahendrakar--Paradee-8M-v1.0/snapshots/8f34b01ef8adcb0bec470b89bf2fd62a7b2369e6/onnx/paradee_int8.onnx` (8.6 MiB).
Vocabulary: `/Users/santa/.cache/huggingface/hub/models--sahilmahendrakar--Paradee-8M-v1.0/snapshots/8f34b01ef8adcb0bec470b89bf2fd62a7b2369e6/config.json` (1.7 KiB).
Phonemizer directory: `/Users/santa/.nexus/models/speech/python` (14.5 MiB on disk).
Loaded vocabulary entries: 114.
Graphics ledger: 0.0 MiB. Neural ledger: 0.0 MiB. Those tags sit inside the physical footprint; they are not extra on top of it.

## What the number is

The last row is a process whose only job is this model: the Python interpreter, onnxruntime, misaki, spaCy, the English phonemizer, and the Paradee session. The ONNX file is 8.6 MiB on disk. Importing misaki and spaCy adds more than the ONNX runtime does, and the 176.9 MiB load step is the phonemizer model plus the Paradee session. That is the warm `/speak` worker, without the Nexus daemon around it.

The Nexus daemon keeps that worker in a child process and unloads it after 10 idle minutes. This program does not include the daemon, and it does not synthesize a sentence. Generating audio allocates short-lived buffers beyond the loaded session.

## Run again

```sh
cd /path/to/nexus
.venv/bin/python "memory usage/text-to-speech/measure.py"
```

Requires the speak extra (onnxruntime, misaki, huggingface_hub) and a Paradee cache plus `~/.nexus/models/speech/python` already downloaded by `/speak`.
