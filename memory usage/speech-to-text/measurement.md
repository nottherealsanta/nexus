# Speech-to-text memory

A process that has loaded the dictation model uses **751.2 MiB** of physical memory. Loading the checkpoint, after Torch and Kestrel are already imported, adds **560.9 MiB**.

Measured by `measure.py` on 2026-10-20 11:35 IST (Mac16,10, 16 GiB RAM, Darwin 27.0.0, Python 3.14.3).

The program loads the dictation checkpoint Nexus uses (`moondream/parakeet-redux`, revision `2bf128600aac4b16946f7ed8372e56117fe5e23b`) through Kestrel's `ParakeetTdtRuntime`, then reads this process's physical footprint. That is the number Activity Monitor shows as Memory. Resident size is the classic RSS figure. The two differ because the footprint counts compressed pages and Metal allocations, while RSS counts resident pages, including some file-backed ones.

## Result

| Stage | Physical footprint | Resident size | Added since the previous stage |
| --- | ---: | ---: | ---: |
| Python interpreter | 9.8 MiB | 17.7 MiB | 9.8 MiB |
| Torch and Kestrel imported | 190.3 MiB | 243.8 MiB | 180.5 MiB |
| Parakeet model loaded | 751.2 MiB | 646.7 MiB | 560.9 MiB |

Peak physical footprint during this process: 751.3 MiB.
Peak resident size: 762.7 MiB.
`ru_maxrss`: 762.7 MiB.

Resolved device: `mps`. Load took 1.9 s.
Checkpoint on disk: `/Users/santa/.nexus/models/voice/parakeet-redux/2bf128600aac4b16946f7ed8372e56117fe5e23b` (170.7 MiB).
Parameter and buffer tensors report 65.9 MiB. That is the packed ternary storage Torch can see. It is smaller than the 560.9 MiB the process grew by, because the load also builds the Metal copy, the Kestrel decode graph, and allocator scratch.
Metal allocator: 210.7 MiB allocated, 216.5 MiB reserved by the driver.
Graphics ledger: 256.8 MiB. Neural ledger: 0.0 MiB. Those tags sit inside the physical footprint; they are not extra on top of it.

## What the number is

The last row is a process whose only job is this model: the Python interpreter, Torch, Kestrel, and the loaded Parakeet weights. The whole dictation stack is the last row. The model itself is the jump from "Torch and Kestrel imported" to "Parakeet model loaded".

Inside the Nexus daemon those same bytes sit on top of the daemon itself. This program does not include the daemon, a recording, or a transcription. A live dictation pass allocates temporary activation memory beyond the loaded weights.

## Run again

```sh
cd /path/to/nexus
.venv/bin/python "memory usage/speech-to-text/measure.py"
```

Requires the voice extra (`kestrel`, Torch) and a checkpoint already downloaded by `nexus voice download`.
