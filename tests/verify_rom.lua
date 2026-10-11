-- Run by tests/verify_rom.py: Mesen.exe --testrunner furtest.gb <filled-in copy of this script>
-- Prints every APU register / wave RAM write, grouped by fur_tick() call:
--   T <cycle>      fur_tick() entered
--   E <cycle>      fur_tick() returned (main loop marker write)
--   W <addr> <val> write to $FF10-$FF3F (hex)
local TICK_ADDR = @TICK_ADDR@
local MARK_ADDR = @MARK_ADDR@
local TICKS = @TICKS@
local MEM = emu.memType.gameboyMemory
local CPU = emu.cpuType.gameboy

local ticks = 0
local out = {}
local function flush()
  if #out > 0 then print(table.concat(out, "\n")); out = {} end
end

emu.addMemoryCallback(function(addr, value)
  ticks = ticks + 1
  if ticks > TICKS then flush(); emu.stop(0) end
  out[#out + 1] = string.format("T %d", emu.getState()["cpu.cycleCount"])
end, emu.callbackType.exec, TICK_ADDR, TICK_ADDR, CPU, MEM)

emu.addMemoryCallback(function(addr, value)
  out[#out + 1] = string.format("E %d", emu.getState()["cpu.cycleCount"])
  if #out > 4000 then flush() end
end, emu.callbackType.write, MARK_ADDR, MARK_ADDR, CPU, MEM)

emu.addMemoryCallback(function(addr, value)
  if ticks > 0 then out[#out + 1] = string.format("W %x %x", addr & 0xff, value) end
end, emu.callbackType.write, 0xFF10, 0xFF3F, CPU, MEM)
