# minimal_endpoint

The smallest thing that is still an MRRoIP endpoint: the core, and the profile hooks the core
requires (`mrroip_profile.h` §14 of the spec) implemented as stubs that do nothing. It answers
`/definition`, `/config`, `/control` and `/state`, announces itself, and owns no hardware.

Two jobs:

* **A starting point for a new device.** Copy the folder, fill in the stubs, add a board file. The
  three `profile_object_stream_*` hooks are deliberately *not* implemented here — the component's
  weak defaults answer `not_uploadable`, so a device that takes no binary uploads can ignore them.
* **A compile test for the library.** Every public header is included by its own `h_*.c` and nothing
  else, so a header that quietly depends on another file breaks this build.

```bash
idf.py set-target esp32c3
idf.py build
```

Real endpoints: `../../../esp32/oled` (1-bit SSD1306) and `../../../esp32/colour` (240x280 ST7789V2).
