# Memory usage

Two programs load the local models Nexus already uses and record the process memory.
Each folder has the program (`measure.py`) and the record from the last run
(`measurement.md`).

Measured on an Apple M4 (`Mac16,10`), 16 GiB RAM, Python 3.14.3, on 2026-10-20.
Physical footprint is the number Activity Monitor shows as Memory. A second run
moves these figures by a few mebibytes.

| | Speech to text | Text to speech |
| --- | --- | --- |
| What Nexus uses it for | Dictation (`/voice`) | Speaking an answer (`/speak`) |
| Model | Parakeet Redux, Kestrel, Metal | Paradee-8M int8, onnxruntime, CPU |
| Process once the model is loaded | 751.2 MiB | 398.8 MiB |
| Added by the load, after the libraries are imported | 560.9 MiB | 176.9 MiB |
| Weights on disk | 170.7 MiB checkpoint | 8.6 MiB ONNX file |

Speech to text is the heavier of the two. Torch and Kestrel account for about
180 MiB before the checkpoint is loaded; the checkpoint then adds about 561 MiB,
including the Metal copy of the weights. Text to speech stays under 400 MiB.
Most of that is misaki and spaCy, not the 8.6 MiB ONNX file.

These processes do only the load. They do not include the Nexus daemon, a
recording, or generated audio. Details, the stage-by-stage table, and how to
run the program again are in each folder.

```sh
.venv/bin/python "memory usage/speech-to-text/measure.py"
.venv/bin/python "memory usage/text-to-speech/measure.py"
```
