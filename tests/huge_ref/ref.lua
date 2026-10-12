-- Run by tests/huge_reference.py: prints the APU writes of each hUGE_dosound call.
--   T              a call starts (the player's marker write)
--   W <addr> <val> write to $FF10-$FF3F (hex)
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
  out[#out + 1] = "T"
  if #out > 4000 then flush() end
end, emu.callbackType.write, 0xFF90, 0xFF90, CPU, MEM)
emu.addMemoryCallback(function(addr, value)
  if ticks > 0 then out[#out + 1] = string.format("W %x %x", addr & 0xff, value) end
end, emu.callbackType.write, 0xFF10, 0xFF3F, CPU, MEM)
