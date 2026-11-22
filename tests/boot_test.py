#!/usr/bin/env python3
"""Boot checks of the firmware (system/MyFirmware) on the emulator.

Builds the firmware from this checkout into a temporary directory, puts a
tiny kernel on a sparse disk image where tools/mkfs.py puts the real one, and
boots it with myemu --headless. Pins what the firmware is responsible for:

  - it loads as many 64 KB blocks as the kernel image says it takes (the
    image's second word is the address it ends at), and no more;
  - it loads up to tools/mkfs.py's KERNEL_BLOCKS blocks from KERNEL_BLOCK,
    and refuses an image whose size is out of range (0 blocks, more than
    KERNEL_BLOCKS, or an end word below KERNEL_BASE or past 2 GB) without
    jumping to it;
  - it reports an SSD read error and does not jump when there is no disk;
  - its constants match tools/mkfs.py and jump.masm.

Uses the umbrella's built toolchain (mlc, myas, mllinker) and emulator; the
umbrella root is MYCOMPUTER_ROOT, or the directory three levels up.

Run after `make build`:  python3 system/MyFirmware/tests/boot_test.py
"""

import importlib.util
import os
import re
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(os.environ.get("MYCOMPUTER_ROOT") or Path(__file__).resolve().parents[3])

MLC = REPO_ROOT / "toolchain" / "MyLangCompiler" / "mlc"
MYAS = REPO_ROOT / "toolchain" / "MyAssembler" / "build" / "myas"
MLLINKER = REPO_ROOT / "toolchain" / "MyLinker" / "mllinker"
MYEMU = REPO_ROOT / "runtime" / "MyEmulator" / "target" / "release" / "myemu"
MKFS = REPO_ROOT / "tools" / "mkfs.py"

MAIN_SOURCE = FIRMWARE_ROOT / "src" / "fw" / "main.unsafe.mln"
JUMP_SOURCE = FIRMWARE_ROOT / "src" / "fw" / "jump.masm"
STUB_SOURCE = FIRMWARE_ROOT / "src" / "boot" / "stub.masm"

# Where qa/runners/run_system.py links the kernel (--base) and jump.masm jumps.
KERNEL_BASE = 0x00100000
BLOCK_BYTES = 65536
DISK_BYTES = 1 << 30  # what the emulator's SSD holds; the image stays sparse
TOOL_TIMEOUT = 60
EMU_TIMEOUT = 60

BOOTING = "firmware: booting system..."
JUMPING = "firmware: OS loaded. Jumping to Kernel..."
OUT_OF_RANGE = "firmware: the kernel image's size is out of range!"
READ_ERROR = "firmware: SSD read error!"

# A kernel that only halts. Like system/MyOS/src/boot/stub.masm, its second
# word is where the image ends.
KERNEL_MASM = """\
import _end

__START__:
  jmp kernel_boot
kernel_image_end:
  .word _end
kernel_boot:
  halt
"""
END_WORD_OFFSET = 4


def load_mkfs():
    spec = importlib.util.spec_from_file_location("mkfs", MKFS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def firmware_constant(name: str) -> int:
    match = re.search(rf"^i32 {name} = (0x[0-9A-Fa-f]+|\d+);", MAIN_SOURCE.read_text(), re.M)
    if not match:
        raise AssertionError(f"{name} not found in {MAIN_SOURCE}")
    return int(match.group(1), 0)


def run_tool(cmd, cwd):
    result = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
                            timeout=TOOL_TIMEOUT)
    if result.returncode != 0:
        raise AssertionError(f"{cmd[0]} failed ({result.returncode}):\n{result.stdout}{result.stderr}")


def marker(block: int) -> int:
    """The word written at the start of the kernel image's block `block` (> 0)."""
    return 0xA0000000 + block


class Boot:
    """What one emulator run left: the serial lines, PC and dumped memory."""

    def __init__(self, stdout: str, regs: str):
        self.lines = [line.strip() for line in stdout.splitlines()]
        pc = re.search(r"^PC: ([0-9a-fA-F]+)$", regs, re.M)
        self.pc = int(pc.group(1), 16) if pc else None
        self.mem = {}
        for addr, data in re.findall(r"^0x([0-9A-Fa-f]{8}): ((?:[0-9A-Fa-f]{2} ?)+)$", stdout, re.M):
            for i, byte in enumerate(data.split()):
                self.mem[int(addr, 16) + i] = int(byte, 16)

    def firmware_lines(self):
        return [line for line in self.lines if line.startswith("firmware:")]

    def word(self, addr: int) -> int:
        return struct.unpack(">I", bytes(self.mem[addr + i] for i in range(4)))[0]


def boot_once(cmd, run_dir) -> Boot:
    result = subprocess.run([str(c) for c in cmd], cwd=run_dir, capture_output=True, text=True,
                            timeout=EMU_TIMEOUT)
    if result.returncode != 0:
        raise AssertionError(f"myemu failed ({result.returncode}):\n{result.stdout}{result.stderr}")
    regs_file = run_dir / "regs.txt"
    return Boot(result.stdout, regs_file.read_text() if regs_file.exists() else "")


class FirmwareBootTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [str(p) for p in (MLC, MYAS, MLLINKER, MYEMU, MKFS) if not p.exists()]
        if missing:
            raise AssertionError("run `make build` first; missing: " + ", ".join(missing))
        cls.mkfs = load_mkfs()
        cls.tmp = tempfile.TemporaryDirectory(prefix="myfirmware-boot-")
        cls.work = Path(cls.tmp.name)
        try:
            cls.firmware = cls.build_firmware()
            cls.kernel = cls.build_kernel()
        except BaseException:
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def build_firmware(cls) -> Path:
        out = cls.work / "fw"
        out.mkdir()
        main_masm = out / "main.unsafe.masm"
        run_tool([MLC, MAIN_SOURCE, main_masm], out)
        objs = []
        for name, source in (("stub", STUB_SOURCE), ("main", main_masm), ("jump", JUMP_SOURCE)):
            obj = out / f"{name}.mobj"
            run_tool([MYAS, source, out / f"{name}.mbin", "--obj", obj], out)
            objs.append(obj)
        image = out / "firmware_linked.mbin"
        run_tool([MLLINKER, image, *objs], out)
        return image

    @classmethod
    def build_kernel(cls) -> bytes:
        out = cls.work / "kernel"
        out.mkdir()
        (out / "kernel.masm").write_text(KERNEL_MASM)
        run_tool([MYAS, out / "kernel.masm", out / "kernel.mbin", "--obj", out / "kernel.mobj"], out)
        run_tool([MLLINKER, "--base", hex(KERNEL_BASE), out / "kernel_linked.mbin", out / "kernel.mobj"], out)
        return (out / "kernel_linked.mbin").read_bytes()

    def boot(self, end=None, blocks=1, disk=True, dump=()):
        """Boot a kernel image of `blocks` blocks whose end word is `end`
        (None: the linker's), dumping 8 bytes at each address in `dump`
        (myemu dumps one range a run, so this boots once per address)."""
        run_dir = Path(tempfile.mkdtemp(dir=self.work))
        image = bytearray(self.kernel)
        if end is not None:
            image[END_WORD_OFFSET:END_WORD_OFFSET + 4] = struct.pack(">I", end)
        image = image.ljust(blocks * BLOCK_BYTES, b"\0")
        for block in range(1, blocks):
            image[block * BLOCK_BYTES:block * BLOCK_BYTES + 4] = struct.pack(">I", marker(block))
        cmd = [MYEMU, "-i", self.firmware, "--headless", "-o", run_dir / "regs.txt",
               "--log-dir", run_dir / "logs"]
        disk_img = run_dir / "disk.img"
        if disk:
            with open(disk_img, "wb") as f:
                f.truncate(DISK_BYTES)
                f.seek(self.mkfs.KERNEL_BLOCK * BLOCK_BYTES)
                f.write(image)
            cmd += ["--disk", disk_img]
        try:
            boot = None
            for addr in list(dump) or [None]:
                run = boot_once(cmd + (["--mem", hex(addr), "8"] if addr is not None else []), run_dir)
                self.assertIn(BOOTING, run.lines)
                if boot is None:
                    boot = run
                else:
                    self.assertEqual(run.firmware_lines(), boot.firmware_lines())
                    boot.mem.update(run.mem)
        finally:
            disk_img.unlink(missing_ok=True)
        return boot

    def assert_jumped(self, boot):
        self.assertIn(JUMPING, boot.lines)
        self.assertNotIn(OUT_OF_RANGE, boot.lines)
        self.assertNotIn(READ_ERROR, boot.lines)
        # The kernel halted, so the PC is just past its halt.
        self.assertIsNotNone(boot.pc)
        self.assertTrue(KERNEL_BASE <= boot.pc < KERNEL_BASE + len(self.kernel) + 4, hex(boot.pc))

    def assert_refused(self, boot, message=OUT_OF_RANGE):
        self.assertIn(message, boot.lines)
        self.assertNotIn(JUMPING, boot.lines)
        # The firmware returned to its stub, which halted in ROM.
        self.assertIsNotNone(boot.pc)
        self.assertLess(boot.pc, KERNEL_BASE)

    def block_addr(self, block: int) -> int:
        return KERNEL_BASE + block * BLOCK_BYTES

    # --- constants ---------------------------------------------------------

    def test_constants_match_mkfs_and_jump(self):
        self.assertEqual(firmware_constant("KERNEL_BLOCK"), self.mkfs.KERNEL_BLOCK)
        self.assertEqual(firmware_constant("MAX_KERNEL_BLOCKS"), self.mkfs.KERNEL_BLOCKS)
        self.assertEqual(firmware_constant("BLOCK_BYTES"), self.mkfs.BLOCK_SIZE)
        self.assertEqual(firmware_constant("KERNEL_BASE"), KERNEL_BASE)
        jump = re.search(r"movi r1, (0x[0-9A-Fa-f]+)", JUMP_SOURCE.read_text())
        self.assertIsNotNone(jump)
        self.assertEqual(int(jump.group(1), 16), KERNEL_BASE)

    def test_mkfs_accepts_exactly_what_the_firmware_loads(self):
        most = self.mkfs.KERNEL_BLOCKS * BLOCK_BYTES
        self.assertIsNone(self.mkfs.kernel_size_error(most))
        self.assertIsNotNone(self.mkfs.kernel_size_error(most + 1))

    # --- images it loads ---------------------------------------------------

    def test_one_block_image_as_linked(self):
        boot = self.boot(dump=[self.block_addr(1)])
        self.assert_jumped(boot)
        self.assertEqual(boot.word(self.block_addr(1)), 0)

    def test_loads_only_the_blocks_the_image_says(self):
        boot = self.boot(end=self.block_addr(3), blocks=4,
                         dump=[self.block_addr(1), self.block_addr(2), self.block_addr(3)])
        self.assert_jumped(boot)
        self.assertEqual(boot.word(self.block_addr(1)), marker(1))
        self.assertEqual(boot.word(self.block_addr(2)), marker(2))
        self.assertEqual(boot.word(self.block_addr(3)), 0)

    def test_partial_last_block_is_loaded(self):
        boot = self.boot(end=self.block_addr(2) + 1, blocks=4,
                         dump=[self.block_addr(2), self.block_addr(3)])
        self.assert_jumped(boot)
        self.assertEqual(boot.word(self.block_addr(2)), marker(2))
        self.assertEqual(boot.word(self.block_addr(3)), 0)

    def test_largest_image_mkfs_accepts(self):
        most = self.mkfs.KERNEL_BLOCKS
        boot = self.boot(end=self.block_addr(most), blocks=most + 1,
                         dump=[self.block_addr(most - 1), self.block_addr(most)])
        self.assert_jumped(boot)
        self.assertEqual(boot.word(self.block_addr(most - 1)), marker(most - 1))
        self.assertEqual(boot.word(self.block_addr(most)), 0)

    # --- images it refuses -------------------------------------------------

    def assert_refused_end(self, end):
        boot = self.boot(end=end, blocks=2, dump=[self.block_addr(1)])
        self.assert_refused(boot)
        self.assertEqual(boot.word(self.block_addr(1)), 0)

    def test_refuses_zero_end(self):
        self.assert_refused_end(0)

    def test_refuses_empty_image(self):
        self.assert_refused_end(KERNEL_BASE)

    def test_refuses_one_byte_past_the_largest_image(self):
        self.assert_refused_end(self.block_addr(self.mkfs.KERNEL_BLOCKS) + 1)

    def test_refuses_end_at_2_gb(self):
        self.assert_refused_end(0x80000000)

    def test_refuses_end_at_4_gb(self):
        self.assert_refused_end(0xFFFFFFFF)

    # --- the SSD -------------------------------------------------------------

    def test_reports_read_error_without_a_disk(self):
        self.assert_refused(self.boot(disk=False), READ_ERROR)


if __name__ == "__main__":
    unittest.main()
