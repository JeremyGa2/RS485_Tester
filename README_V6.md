# RS485 Tester V6

Simple diagnostic tool for an RS485 link. Typical setup: PC → adapter A → module 1 → fibre → module 2 → adapter B → PC.
Whatever leaves one port is compared, byte by byte, with what arrives on the other.

## Using it (target PC with no Python and no Internet)
Copy the whole `RS485_Tester_V6` folder, then double-click `RS485_Tester.exe`. See `LISEZ-MOI.txt`.

## Pages
| Page | What it does |
|---|---|
| **Diagnostic** | One click: A→B, B→A, all 256 byte values, Modbus frames, continuous load, transfer time. Gives a clear verdict and, if something fails, what to check. HTML report. |
| **Envoi continu** | 3 choices: what to send, which direction, the pause between frames. Shows what you should see on the oscilloscope and counts every frame that arrives identical. |
| **Envoi manuel** | Type a frame (HEX or text, optional Modbus CRC), send A→B or B→A, and get the IDENTICAL / DIFFERENT result. |
| **Tests avancés** | Turnaround, speed sweep, detailed latency, endurance (bit error rate). |
| **Aide** | Wiring, how to read the results, oscilloscope, common problems. |

## Building the Windows folder
- **Automatic**: every push to the private GitHub repo runs `.github/workflows/build-windows.yml`. It runs the
  auto-tests, a full pass over the interface on the simulated link (with screenshots), the build, and a check
  of the built .exe. The "RS485_Tester_V6" artifact contains the ready-to-use folder.
- **On a Windows PC with Python**: `build_exe.bat`.

## Code
```
RS485_COM_tester_V6.py     entry point (--selftest, --smoke)
rs485tester/diagnostic.py  one-click diagnostic
rs485tester/engine.py      tests, continuous send, listening
rs485tester/core.py        settings, timings, CRC, patterns, comparison
rs485tester/simport.py     simulated link SIM-A / SIM-B
rs485tester/gui.py         interface
rs485tester/diagrams.py    diagrams
rs485tester/report.py      HTML / CSV report
```
