Patched Furnace for fur2gb's converter (Windows, 64 bit)

furnace.exe is Furnace v0.6.8.2 with tools/furnace_trace.patch from this repository applied, built
with tools/build_furnace.sh. The patch makes it log every register write of its Game Boy backend,
which is what fur2gb compares its own output against. It needs nothing but Windows.

Furnace is free software under the GNU General Public License, version 2 or later: see LICENSE.

Corresponding source:
  Furnace v0.6.8.2      https://github.com/tildearrow/furnace/tree/v0.6.8.2
  The patch and script  tools/furnace_trace.patch and tools/build_furnace.sh in this repository

The zip is what Pocket Dash's build downloads when a song has changed.
