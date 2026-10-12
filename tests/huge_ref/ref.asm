; Reference player for tests/huge_reference.py: the real hUGEDriver playing one song.
; Writes $FF90 (an unused HRAM byte) before every hUGE_dosound call, so a capture can group the
; APU register writes by driver tick.
include "include/hardware.inc"

SECTION "Header", ROM0[$100]
    nop
    jp start
    ds $150 - @, 0

SECTION "Main", ROM0
start:
    di
    ld sp, $DFFF
    ld a, $80
    ldh [rAUDENA], a
    ld a, $FF
    ldh [rAUDTERM], a
    ld a, $77
    ldh [rAUDVOL], a
    ld hl, ref_song
    call hUGE_init
.loop:
    ld a, 1
    ldh [$FF90 & $FF], a
    call hUGE_dosound
    jr .loop
