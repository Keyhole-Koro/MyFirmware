# MyFirmware

The firmware of the MyComputer machine. The emulator runs it from ROM at
power-on; it loads the kernel image from the SSD into RAM and jumps to it.

1. It loads the first 64 KB block of the kernel image from SSD block
   `KERNEL_BLOCK` (16000) to `KERNEL_BASE` (0x00100000).
2. The image's second word is the address it ends at
   (system/MyOS/src/boot/stub.masm). From it the firmware works out how many
   blocks the image takes and loads the rest, up to `MAX_KERNEL_BLOCKS` (64
   blocks, 4 MB). It refuses an image whose size is out of range.
3. It jumps to `KERNEL_BASE`.

It prints its progress and any error on the serial port.

## Sources

- `src/boot/stub.masm`: the entry point (`__START__`); calls `fw_main` and
  halts if it returns.
- `src/fw/main.unsafe.mln`: serial output, SSD reads, and the kernel loading.
- `src/fw/jump.masm`: `jump_to_kernel`, the jump to `KERNEL_BASE`.

`KERNEL_BLOCK` and `MAX_KERNEL_BLOCKS` must match `KERNEL_BLOCK` and
`KERNEL_BLOCKS` in the umbrella's `tools/mkfs.py`, which puts the kernel on the
disk image and refuses one bigger than the firmware loads. The address in
`jump.masm` must match `KERNEL_BASE`.

## Build

This repository has no build of its own. The umbrella's
`qa/runners/run_system.py` (run by `make build` there) compiles it with
`qa/runners/build_toolchain.py` into `build/firmware_linked.mbin`, linking
`stub`, `main` and `jump` in that order.

## Test

`tests/boot_test.py` builds the firmware from this checkout into a temporary
directory with the umbrella's toolchain, boots small kernel images on the
emulator, and checks which blocks are loaded, the refusals, the SSD read
error, and that the constants above match. Run it after `make build` in the
umbrella:

```bash
python3 system/MyFirmware/tests/boot_test.py
```

From a checkout outside the umbrella, point `MYCOMPUTER_ROOT` at it:

```bash
MYCOMPUTER_ROOT=/path/to/MyComputer python3 tests/boot_test.py
```
