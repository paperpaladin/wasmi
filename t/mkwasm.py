#!/usr/bin/env python3
"""mkwasm.py -- assemble .wasm fixtures from a terse textual description, and
compute every expected result *independently* in Python.

Two jobs, deliberately in one file:

  1. A miniature assembler (text -> .wasm bytes).  There is no wat2wasm here, so
     this is how fixtures come into existence.  It emits LEB128 and section
     framing by hand; if it is wrong, the fixtures are wrong, and that is the
     honest weak point of the whole arrangement (see README "The oracle").

  2. An independent reference implementation of the arithmetic, written in
     Python with explicit 32/64-bit wrapping and explicit truncating division.
     The expected value of every case is computed *here*, from the Python
     semantics, and never by running wasmi.  That is what makes this a
     differential test rather than a snapshot of whatever wasmi happens to do.

Output: t/fix/NAME.wasm plus t/fix/NAME.cases, one case per line:

    EXPECTED FUNC [args...]

EXPECTED is a signed decimal number, a `trap:<code>` token, or `!error`
meaning "the module must be rejected at load time with exit status 2".
"""

import os
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix")

# ===================================================================== LEB128
def uleb(n):
    """Unsigned LEB128: 7 payload bits per byte, bit 7 = continuation."""
    assert n >= 0
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def sleb(n):
    """Signed LEB128: same framing; stop once the remaining bits are all sign."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7                      # Python's >> is arithmetic, which is what we want
        done = (n == 0 and not (b & 0x40)) or (n == -1 and (b & 0x40))
        out.append(b if done else b | 0x80)
        if done:
            return bytes(out)


def vec(items):
    return uleb(len(items)) + b"".join(items)


def name(s):
    b = s.encode()
    return uleb(len(b)) + b


VT = {"i32": 0x7F, "i64": 0x7E, "f32": 0x7D, "f64": 0x7C}

# =================================================================== assembler
NONE_OPS = {
    "unreachable": 0x00, "nop": 0x01, "else": 0x05, "end": 0x0B,
    "return": 0x0F, "drop": 0x1A, "select": 0x1B,
    "i32.eqz": 0x45, "i32.eq": 0x46, "i32.ne": 0x47,
    "i32.lt_s": 0x48, "i32.lt_u": 0x49, "i32.gt_s": 0x4A, "i32.gt_u": 0x4B,
    "i32.le_s": 0x4C, "i32.le_u": 0x4D, "i32.ge_s": 0x4E, "i32.ge_u": 0x4F,
    "i64.eqz": 0x50, "i64.eq": 0x51, "i64.ne": 0x52,
    "i64.lt_s": 0x53, "i64.lt_u": 0x54, "i64.gt_s": 0x55, "i64.gt_u": 0x56,
    "i64.le_s": 0x57, "i64.le_u": 0x58, "i64.ge_s": 0x59, "i64.ge_u": 0x5A,
    "i32.clz": 0x67, "i32.ctz": 0x68, "i32.popcnt": 0x69,
    "i32.add": 0x6A, "i32.sub": 0x6B, "i32.mul": 0x6C,
    "i32.div_s": 0x6D, "i32.div_u": 0x6E, "i32.rem_s": 0x6F, "i32.rem_u": 0x70,
    "i32.and": 0x71, "i32.or": 0x72, "i32.xor": 0x73,
    "i32.shl": 0x74, "i32.shr_s": 0x75, "i32.shr_u": 0x76,
    "i32.rotl": 0x77, "i32.rotr": 0x78,
    "i64.clz": 0x79, "i64.ctz": 0x7A, "i64.popcnt": 0x7B,
    "i64.add": 0x7C, "i64.sub": 0x7D, "i64.mul": 0x7E,
    "i64.div_s": 0x7F, "i64.div_u": 0x80, "i64.rem_s": 0x81, "i64.rem_u": 0x82,
    "i64.and": 0x83, "i64.or": 0x84, "i64.xor": 0x85,
    "i64.shl": 0x86, "i64.shr_s": 0x87, "i64.shr_u": 0x88,
    "i64.rotl": 0x89, "i64.rotr": 0x8A,
    "i32.wrap_i64": 0xA7, "i64.extend_i32_s": 0xAC, "i64.extend_i32_u": 0xAD,
    # Present only so the "must be rejected" fixtures can emit them.
    "f32.add": 0x92, "f64.add": 0xA0,
}

# mnemonic -> (opcode, natural alignment exponent)
MEM_OPS = {
    "i32.load": (0x28, 2), "i64.load": (0x29, 3),
    "i32.load8_s": (0x2C, 0), "i32.load8_u": (0x2D, 0),
    "i32.load16_s": (0x2E, 1), "i32.load16_u": (0x2F, 1),
    "i64.load8_s": (0x30, 0), "i64.load8_u": (0x31, 0),
    "i64.load16_s": (0x32, 1), "i64.load16_u": (0x33, 1),
    "i64.load32_s": (0x34, 2), "i64.load32_u": (0x35, 2),
    "i32.store": (0x36, 2), "i64.store": (0x37, 3),
    "i32.store8": (0x3A, 0), "i32.store16": (0x3B, 1),
    "i64.store8": (0x3C, 0), "i64.store16": (0x3D, 1), "i64.store32": (0x3E, 2),
}

IDX_OPS = {"br": 0x0C, "br_if": 0x0D, "call": 0x10,
           "local.get": 0x20, "local.set": 0x21, "local.tee": 0x22,
           "global.get": 0x23, "global.set": 0x24}


def blocktype(tok):
    if tok is None:
        return bytes([0x40])          # empty result type
    return bytes([VT[tok]])


def asm(text):
    """Assemble a newline-separated instruction listing into a code body.

    Deliberately dumb: one instruction per line, `;;` starts a comment,
    immediates are whitespace-separated.  No folded s-expressions, no implicit
    `end` -- you write every `end` yourself, which keeps the assembler honest
    about block structure instead of inventing it.
    """
    out = bytearray()
    for raw in text.strip().splitlines():
        line = raw.split(";;")[0].strip()
        if not line:
            continue
        parts = line.split()
        op, args = parts[0], parts[1:]
        if op in ("block", "loop", "if"):
            out.append({"block": 0x02, "loop": 0x03, "if": 0x04}[op])
            out += blocktype(args[0] if args else None)
        elif op in IDX_OPS:
            out.append(IDX_OPS[op])
            out += uleb(int(args[0]))
        elif op == "br_table":
            ns = [int(a) for a in args]        # last entry is the default label
            out.append(0x0E)
            out += uleb(len(ns) - 1)
            for n in ns:
                out += uleb(n)
        elif op == "call_indirect":
            out.append(0x11)
            out += uleb(int(args[0]))
            out.append(0x00)                  # reserved; must be zero in MVP
        elif op in ("memory.size", "memory.grow"):
            out.append(0x3F if op == "memory.size" else 0x40)
            out.append(0x00)                  # reserved
        elif op == "i32.const":
            out.append(0x41)
            out += sleb(int(args[0], 0))
        elif op == "i64.const":
            out.append(0x42)
            out += sleb(int(args[0], 0))
        elif op in MEM_OPS:
            code, natural = MEM_OPS[op]
            align, off = natural, 0
            for a in args:
                k, v = a.split("=")
                if k == "align":
                    align = int(v)
                elif k == "offset":
                    off = int(v)
                else:
                    raise SystemExit("bad memarg %r" % a)
            out.append(code)
            out += uleb(align) + uleb(off)
        elif op in NONE_OPS:
            out.append(NONE_OPS[op])
        else:
            raise SystemExit("mkwasm: unknown instruction %r" % line)
    return bytes(out)


# ============================================================== module builder
class Mod:
    def __init__(self):
        self.types = []       # list of (params tuple, results tuple)
        self.funcs = []       # list of dict(type=, locals=[], body=bytes, export=)
        self.globals = []     # list of (type, mut, init_bytes)
        self.mem = None       # (min, max|None)
        self.data = []        # list of (offset, bytes)
        self.table = None     # size
        self.elems = []       # list of (offset, [funcidx])
        self.start = None

    def ftype(self, params, results):
        key = (tuple(params), tuple(results))
        if key not in self.types:
            self.types.append(key)
        return self.types.index(key)

    def func(self, fname, params, results, body, locals=(), export=True):
        t = self.ftype(params, results)
        self.funcs.append({
            "type": t, "locals": list(locals), "body": asm(body),
            "export": fname if export else None,
        })
        return len(self.funcs) - 1

    # ---- section encoders -------------------------------------------------
    def _sec(self, sid, payload):
        return bytes([sid]) + uleb(len(payload)) + payload

    def build(self):
        b = bytearray(b"\0asm" + (1).to_bytes(4, "little"))

        tsec = vec([bytes([0x60]) + vec([bytes([VT[p]]) for p in ps])
                    + vec([bytes([VT[r]]) for r in rs]) for ps, rs in self.types])
        b += self._sec(1, tsec)

        if self.funcs:
            b += self._sec(3, vec([uleb(f["type"]) for f in self.funcs]))

        if self.table is not None:
            # 0x70 = funcref (anyfunc), flags 0 = min only
            b += self._sec(4, vec([bytes([0x70, 0x00]) + uleb(self.table)]))

        if self.mem is not None:
            mn, mx = self.mem
            lim = bytes([0x00]) + uleb(mn) if mx is None else bytes([0x01]) + uleb(mn) + uleb(mx)
            b += self._sec(5, vec([lim]))

        if self.globals:
            b += self._sec(6, vec([bytes([VT[t], 1 if mut else 0]) + init
                                   for t, mut, init in self.globals]))

        exports = [(f["export"], i) for i, f in enumerate(self.funcs) if f["export"]]
        if exports:
            b += self._sec(7, vec([name(n) + bytes([0x00]) + uleb(i) for n, i in exports]))

        if self.start is not None:
            b += self._sec(8, uleb(self.start))

        if self.elems:
            b += self._sec(9, vec([uleb(0) + asm("i32.const %d" % off) + bytes([0x0B])
                                   + vec([uleb(i) for i in idxs])
                                   for off, idxs in self.elems]))

        if self.funcs:
            bodies = []
            for f in self.funcs:
                # Locals are run-length encoded: (count, type) groups.
                groups = []
                for t in f["locals"]:
                    if groups and groups[-1][1] == t:
                        groups[-1][0] += 1
                    else:
                        groups.append([1, t])
                loc = vec([uleb(c) + bytes([VT[t]]) for c, t in groups])
                body = loc + f["body"]
                bodies.append(uleb(len(body)) + body)
            b += self._sec(10, vec(bodies))

        if self.data:
            b += self._sec(11, vec([uleb(0) + asm("i32.const %d" % off) + bytes([0x0B])
                                    + uleb(len(d)) + d for off, d in self.data]))
        return bytes(b)


# The expectation half of the oracle -- an independent Python implementation of
# the arithmetic -- and the fixtures themselves come next.  Right now this file
# can only turn text into .wasm; it has no opinion about what the result means.

if __name__ == "__main__":
    print("mkwasm: assembler only, no fixtures generated yet")
