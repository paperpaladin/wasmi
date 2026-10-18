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
import shutil
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


# =================================================================== Python ref
# The independent half of the oracle.  Everything below is plain Python integer
# arithmetic with the wrapping written out explicitly -- no reuse of anything
# from wasmi.c, not even its structure.

MASK32 = (1 << 32) - 1
MASK64 = (1 << 64) - 1


def u32(x): return x & MASK32
def u64(x): return x & MASK64
def s32(x): x = u32(x); return x - (1 << 32) if x >> 31 else x
def s64(x): x = u64(x); return x - (1 << 64) if x >> 63 else x


class Trap:
    def __init__(self, code): self.code = code
    def __str__(self): return "trap:" + self.code


def tdiv(a, b):
    """Truncating division.  Python's // floors, which disagrees with wasm (and
    with C) for mixed signs: -7 // 2 == -4, but i32.div_s(-7,2) == -3."""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def trem(a, b):
    """Remainder with the sign of the dividend, matching truncating division."""
    r = abs(a) % abs(b)
    return -r if a < 0 else r


def make_int_ref(bits):
    M = (1 << bits) - 1
    MIN = -(1 << (bits - 1))
    us = u32 if bits == 32 else u64
    ss = s32 if bits == 32 else s64

    def div_s(a, b):
        a, b = ss(a), ss(b)
        if b == 0: return Trap("div0")
        if a == MIN and b == -1: return Trap("int-overflow")
        return ss(tdiv(a, b))

    def rem_s(a, b):
        a, b = ss(a), ss(b)
        if b == 0: return Trap("div0")
        return ss(trem(a, b))

    def div_u(a, b):
        a, b = us(a), us(b)
        if b == 0: return Trap("div0")
        return ss(a // b)

    def rem_u(a, b):
        a, b = us(a), us(b)
        if b == 0: return Trap("div0")
        return ss(a % b)

    def shl(a, b): return ss(us(a) << (us(b) % bits))
    def shr_u(a, b): return ss(us(a) >> (us(b) % bits))
    def shr_s(a, b): return ss(ss(a) >> (us(b) % bits))   # Python >> is arithmetic

    def rotl(a, b):
        k = us(b) % bits
        v = us(a)
        return ss(((v << k) | (v >> (bits - k))) & M) if k else ss(v)

    def rotr(a, b):
        k = us(b) % bits
        v = us(a)
        return ss(((v >> k) | (v << (bits - k))) & M) if k else ss(v)

    def clz(a):
        v = us(a)
        n = 0
        while n < bits and not (v & (1 << (bits - 1 - n))):
            n += 1
        return n

    def ctz(a):
        v = us(a)
        if v == 0: return bits
        n = 0
        while not (v >> n) & 1:
            n += 1
        return n

    def popcnt(a): return bin(us(a)).count("1")

    return {
        "add": lambda a, b: ss(us(a) + us(b)),
        "sub": lambda a, b: ss(us(a) - us(b)),
        "mul": lambda a, b: ss(us(a) * us(b)),
        "div_s": div_s, "div_u": div_u, "rem_s": rem_s, "rem_u": rem_u,
        "and": lambda a, b: ss(us(a) & us(b)),
        "or": lambda a, b: ss(us(a) | us(b)),
        "xor": lambda a, b: ss(us(a) ^ us(b)),
        "shl": shl, "shr_s": shr_s, "shr_u": shr_u, "rotl": rotl, "rotr": rotr,
        # Comparisons return i32 0/1 regardless of operand width.
        "eq": lambda a, b: int(us(a) == us(b)),
        "ne": lambda a, b: int(us(a) != us(b)),
        "lt_s": lambda a, b: int(ss(a) < ss(b)),
        "lt_u": lambda a, b: int(us(a) < us(b)),
        "gt_s": lambda a, b: int(ss(a) > ss(b)),
        "gt_u": lambda a, b: int(us(a) > us(b)),
        "le_s": lambda a, b: int(ss(a) <= ss(b)),
        "le_u": lambda a, b: int(us(a) <= us(b)),
        "ge_s": lambda a, b: int(ss(a) >= ss(b)),
        "ge_u": lambda a, b: int(us(a) >= us(b)),
        "clz": clz, "ctz": ctz, "popcnt": popcnt,
        "eqz": lambda a: int(us(a) == 0),
    }


R32 = make_int_ref(32)
R64 = make_int_ref(64)

BIN_OPS = ["add", "sub", "mul", "div_s", "div_u", "rem_s", "rem_u",
           "and", "or", "xor", "shl", "shr_s", "shr_u", "rotl", "rotr",
           "eq", "ne", "lt_s", "lt_u", "gt_s", "gt_u", "le_s", "le_u", "ge_s", "ge_u"]
UN_OPS = ["clz", "ctz", "popcnt", "eqz"]

# Operand matrices.  Chosen for the corners that break naive implementations:
# sign boundaries, shift counts at and past the bit width, and 0.
# Every binary op is run over the full cross product, so the matrices are kept
# small enough that `make test` stays in the tens of seconds while still hitting
# every corner: sign boundaries, shift counts at/past the width, and zero.
V32 = [0, 1, -1, 2, -2, 7, -7, 0x7FFFFFFF, -0x80000000, 0x55555555,
       31, 32, 33, -31, 65535, 123456789]
V64 = [0, 1, -1, 2, -2, 7, -7, 0x7FFFFFFFFFFFFFFF, -0x8000000000000000,
       0x0123456789ABCDEF, 63, 64, 65, 0x100000000]

# ===================================================================== fixtures
SUITES = []


def suite(fn):
    SUITES.append(fn)
    return fn


def emit(fname, mod, cases):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, fname + ".wasm"), "wb") as f:
        f.write(mod.build() if isinstance(mod, Mod) else mod)
    lines = []
    for exp, func, args in cases:
        lines.append("%s %s%s" % (exp, func, "".join(" " + str(a) for a in args)))
    with open(os.path.join(OUT, fname + ".cases"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return len(cases)


def expect(v):
    """Render a Python reference result as the exact line wasmi should print."""
    return str(v) if not isinstance(v, Trap) else str(v)


# ------------------------------------------------------------------- integer ops
def int_suite(fname, ty, ref, values):
    m = Mod()
    for op in BIN_OPS:
        res = "i32" if op in ("eq", "ne", "lt_s", "lt_u", "gt_s", "gt_u",
                              "le_s", "le_u", "ge_s", "ge_u") else ty
        m.func("%s.%s" % (ty, op), [ty, ty], [res],
               "local.get 0\nlocal.get 1\n%s.%s\nend" % (ty, op))
    for op in UN_OPS:
        res = "i32" if op == "eqz" else ty
        m.func("%s.%s" % (ty, op), [ty], [res],
               "local.get 0\n%s.%s\nend" % (ty, op))

    cases = []
    for op in BIN_OPS:
        f = ref[op]
        for a in values:
            for b in values:
                cases.append((expect(f(a, b)), "%s.%s" % (ty, op), [a, b]))
    for op in UN_OPS:
        f = ref[op]
        for a in values:
            cases.append((expect(f(a)), "%s.%s" % (ty, op), [a]))
    return emit(fname, m, cases)


@suite
def s_i32():
    return int_suite("i32ops", "i32", R32, V32)


@suite
def s_i64():
    return int_suite("i64ops", "i64", R64, V64)


@suite
def s_conv():
    """Width conversions: wrap, and the two extends.  extend_i32_u must NOT
    sign-extend, which is the bug this catches."""
    m = Mod()
    m.func("wrap", ["i64"], ["i32"], "local.get 0\ni32.wrap_i64\nend")
    m.func("ext_s", ["i32"], ["i64"], "local.get 0\ni64.extend_i32_s\nend")
    m.func("ext_u", ["i32"], ["i64"], "local.get 0\ni64.extend_i32_u\nend")
    cases = []
    for v in V64:
        cases.append((str(s32(v)), "wrap", [v]))
    for v in V32:
        cases.append((str(s64(s32(v))), "ext_s", [v]))
        cases.append((str(s64(u32(v))), "ext_u", [v]))
    return emit("conv", m, cases)


# ------------------------------------------------------------------ control flow
@suite
def s_control():
    m = Mod()

    # if / else
    m.func("absish", ["i32"], ["i32"], """
        local.get 0
        i32.const 0
        i32.lt_s
        if i32
          i32.const 0
          local.get 0
          i32.sub
        else
          local.get 0
        end
        end""")

    # block with a result + br carrying a value
    m.func("clamp10", ["i32"], ["i32"], """
        block i32
          local.get 0
          i32.const 10
          i32.gt_s
          if
            i32.const 10
            br 1
          end
          local.get 0
        end
        end""")

    # loop: sum 1..n with br_if as the back edge
    m.func("sum", ["i32"], ["i32"], locals=["i32", "i32"], body="""
        block
          loop
            local.get 1
            local.get 0
            i32.gt_s
            br_if 1
            local.get 2
            local.get 1
            i32.add
            local.set 2
            local.get 1
            i32.const 1
            i32.add
            local.set 1
            br 0
          end
        end
        local.get 2
        end""")

    # br_table with a default, 3 labels deep.  local 1 is pre-seeded with the
    # default answer, so "the default arm was taken" is observable.
    m.func("sw", ["i32"], ["i32"], locals=["i32"], body="""
        i32.const 40
        local.set 1
        block
          block
            block
              block
                local.get 0
                br_table 0 1 2 3
              end
              i32.const 10
              local.set 1
              br 2
            end
            i32.const 20
            local.set 1
            br 1
          end
          i32.const 30
          local.set 1
          br 0
        end
        local.get 1
        end""")

    # early return from inside two blocks
    m.func("ret", ["i32"], ["i32"], """
        block
          block
            local.get 0
            i32.eqz
            br_if 0
            i32.const 7
            return
          end
        end
        i32.const -7
        end""")

    # select
    m.func("sel", ["i32", "i32", "i32"], ["i32"],
           "local.get 1\nlocal.get 2\nlocal.get 0\nselect\nend")

    # deeply nested blocks: 32 levels, with a br that unwinds all of them
    N = 32
    body = []
    for _ in range(N):
        body.append("block")
    body += ["local.get 0", "i32.const 1", "i32.add", "local.set 1",
             "local.get 0", "br_if %d" % (N - 1),
             "local.get 1", "i32.const 100", "i32.add", "local.set 1"]
    for k in range(N):
        body.append("end")
        if k < N - 1:
            body += ["local.get 1", "i32.const 1", "i32.add", "local.set 1"]
    body += ["local.get 1", "end"]
    m.func("deep", ["i32"], ["i32"], locals=["i32"], body="\n".join(body))

    # loop as a tail-recursive-style countdown producing a value
    m.func("mulrep", ["i32", "i32"], ["i32"], locals=["i32"], body="""
        i32.const 1
        local.set 2
        block
          loop
            local.get 1
            i32.eqz
            br_if 1
            local.get 2
            local.get 0
            i32.mul
            local.set 2
            local.get 1
            i32.const 1
            i32.sub
            local.set 1
            br 0
          end
        end
        local.get 2
        end""")

    m.func("boom", ["i32"], ["i32"], """
        local.get 0
        if
          unreachable
        end
        i32.const 1
        end""")

    # ---- Python reference ------------------------------------------------
    def py_sum(n):
        t, i = 0, 0
        while not (i > s32(n)):
            t = s32(t + i)
            i = s32(i + 1)
        return t

    def py_deep(a):
        # arg != 0 -> br unwinds all 32 blocks, skipping every +1
        if s32(a) != 0:
            return s32(s32(a) + 1)
        return s32(1 + 100 + (N - 1))

    def py_mulrep(x, n):
        acc = 1
        n = s32(n)
        while n != 0:
            acc = s32(acc * s32(x))
            n = s32(n - 1)
        return acc

    cases = []
    for v in [0, 1, -1, 5, -5, 0x7FFFFFFF, -0x80000000, 100, -100]:
        cases.append((str(s32(0 - s32(v)) if s32(v) < 0 else s32(v)), "absish", [v]))
        cases.append((str(10 if s32(v) > 10 else s32(v)), "clamp10", [v]))
        cases.append((str(7 if s32(v) != 0 else -7), "ret", [v]))
        cases.append((str(py_deep(v)), "deep", [v]))
    for n in [0, 1, 2, 3, 10, 100, 1000, -1, -5]:
        cases.append((str(py_sum(n)), "sum", [n]))
    for i in [0, 1, 2, 3, 4, 7, -1, 100]:
        cases.append((str({0: 10, 1: 20, 2: 30}.get(u32(i), 40)), "sw", [i]))
    for c in [0, 1, -1, 2]:
        cases.append((str(11 if c else 22), "sel", [c, 11, 22]))
    for x, n in [(2, 10), (3, 5), (-2, 3), (7, 0), (10, 10), (-3, 7), (2, 31), (2, 32)]:
        cases.append((str(py_mulrep(x, n)), "mulrep", [x, n]))
    cases.append(("trap:unreachable", "boom", [1]))
    cases.append(("1", "boom", [0]))
    return emit("control", m, cases)


# -------------------------------------------------------------------- recursion
@suite
def s_recursion():
    m = Mod()
    fact = m.func("fact", ["i64"], ["i64"], """
        local.get 0
        i64.const 1
        i64.le_s
        if i64
          i64.const 1
        else
          local.get 0
          local.get 0
          i64.const 1
          i64.sub
          call 0
          i64.mul
        end
        end""")
    assert fact == 0
    m.func("fib", ["i32"], ["i32"], """
        local.get 0
        i32.const 2
        i32.lt_s
        if i32
          local.get 0
        else
          local.get 0
          i32.const 1
          i32.sub
          call 1
          local.get 0
          i32.const 2
          i32.sub
          call 1
          i32.add
        end
        end""")
    # mutual recursion: is_even / is_odd
    m.func("is_even", ["i32"], ["i32"], """
        local.get 0
        i32.eqz
        if i32
          i32.const 1
        else
          local.get 0
          i32.const 1
          i32.sub
          call 3
        end
        end""")
    m.func("is_odd", ["i32"], ["i32"], """
        local.get 0
        i32.eqz
        if i32
          i32.const 0
        else
          local.get 0
          i32.const 1
          i32.sub
          call 2
        end
        end""")
    # unbounded recursion must trap, not smash the C stack
    m.func("forever", ["i32"], ["i32"], """
        local.get 0
        i32.const 1
        i32.add
        call 4
        end""")

    def py_fact(n):
        if s64(n) <= 1:
            return 1
        acc = 1
        for k in range(2, s64(n) + 1):
            acc = u64(acc * k)
        return s64(acc)

    def py_fib(n):
        n = s32(n)
        if n < 2:
            return n
        a, b = 0, 1
        for _ in range(n - 1):
            a, b = b, s32(a + b)
        return b

    cases = []
    for n in [0, 1, 2, 3, 5, 10, 20, 25, -1]:
        cases.append((str(py_fact(n)), "fact", [n]))
    for n in [0, 1, 2, 3, 10, 20, 25, -1, -5]:
        cases.append((str(py_fib(n)), "fib", [n]))
    for n in [0, 1, 2, 7, 10, 101]:
        cases.append((str(1 if s32(n) % 2 == 0 else 0), "is_even", [n]))
        cases.append((str(1 if s32(n) % 2 == 1 else 0), "is_odd", [n]))
    cases.append(("trap:call-depth", "forever", [0]))
    return emit("recursion", m, cases)


# ----------------------------------------------------------------- memory
@suite
def s_memory():
    DATA = b"hello, wasm\x00\xff\x80\x7f"
    m = Mod()
    m.mem = (1, 2)
    m.data.append((0, DATA))
    m.func("ld8u", ["i32"], ["i32"], "local.get 0\ni32.load8_u\nend")
    m.func("ld8s", ["i32"], ["i32"], "local.get 0\ni32.load8_s\nend")
    m.func("ld16u", ["i32"], ["i32"], "local.get 0\ni32.load16_u align=0\nend")
    m.func("ld16s", ["i32"], ["i32"], "local.get 0\ni32.load16_s align=0\nend")
    m.func("ld32", ["i32"], ["i32"], "local.get 0\ni32.load align=0\nend")
    m.func("ld64", ["i32"], ["i64"], "local.get 0\ni64.load align=0\nend")
    m.func("ld32s64", ["i32"], ["i64"], "local.get 0\ni64.load32_s align=0\nend")
    m.func("ld32u64", ["i32"], ["i64"], "local.get 0\ni64.load32_u align=0\nend")
    # round-trips: store then load back at the same (possibly unaligned) address
    m.func("rt32", ["i32", "i32"], ["i32"],
           "local.get 0\nlocal.get 1\ni32.store align=0\nlocal.get 0\ni32.load align=0\nend")
    m.func("rt16s", ["i32", "i32"], ["i32"],
           "local.get 0\nlocal.get 1\ni32.store16 align=0\nlocal.get 0\ni32.load16_s align=0\nend")
    m.func("rt8s", ["i32", "i32"], ["i32"],
           "local.get 0\nlocal.get 1\ni32.store8\nlocal.get 0\ni32.load8_s\nend")
    m.func("rt64", ["i32", "i64"], ["i64"],
           "local.get 0\nlocal.get 1\ni64.store align=0\nlocal.get 0\ni64.load align=0\nend")
    # endianness: store a word, read its second byte
    m.func("byte1", ["i32"], ["i32"],
           "i32.const 100\nlocal.get 0\ni32.store\ni32.const 100\ni32.load8_u offset=1\nend")
    # static offset immediate on top of the dynamic address
    m.func("ldoff", ["i32"], ["i32"], "local.get 0\ni32.load8_u offset=4\nend")
    m.func("size", [], ["i32"], "memory.size\nend")
    m.func("grow", ["i32"], ["i32"], "local.get 0\nmemory.grow\nend")
    m.func("grow_size", ["i32"], ["i32"], "local.get 0\nmemory.grow\ndrop\nmemory.size\nend")

    PAGE = 65536
    mem = bytearray(PAGE)
    mem[: len(DATA)] = DATA

    def ld(a, n):
        if a + n > PAGE:
            return Trap("oob")
        return int.from_bytes(mem[a:a + n], "little")

    cases = []
    for a in [0, 1, 4, 11, 12, 13, 14]:
        v = ld(a, 1)
        cases.append((str(v), "ld8u", [a]))
        cases.append((str(s32(v - 256 if v >> 7 else v)), "ld8s", [a]))
        cases.append((str(ld(a + 4, 1)), "ldoff", [a]))
    for a in [0, 1, 3, 11, 13]:
        v = ld(a, 2)
        cases.append((str(v), "ld16u", [a]))
        cases.append((str(s32(v - 65536 if v >> 15 else v)), "ld16s", [a]))
    for a in [0, 1, 3, 7, 11]:
        cases.append((str(s32(ld(a, 4))), "ld32", [a]))
        cases.append((str(s64(ld(a, 8))), "ld64", [a]))
        cases.append((str(s64(s32(ld(a, 4)))), "ld32s64", [a]))
        cases.append((str(s64(ld(a, 4))), "ld32u64", [a]))
    # unaligned round-trips, including right at the page boundary
    for a in [0, 1, 3, 5, 7, 1000, 65532]:
        for v in [0, 1, -1, 0x7FFFFFFF, -0x80000000, 0x12345678, -123456]:
            cases.append((str(s32(v)), "rt32", [a, v]))
    for a in [0, 1, 2, 3, 65534]:
        for v in [0, 1, -1, 0x7FFF, -0x8000, 0x1234, 0xFFFF, 0x12345]:
            x = u32(v) & 0xFFFF
            cases.append((str(s32(x - 65536 if x >> 15 else x)), "rt16s", [a, v]))
    for a in [0, 5, 65535]:
        for v in [0, 1, -1, 127, 128, 255, 256, -128, -129]:
            x = u32(v) & 0xFF
            cases.append((str(s32(x - 256 if x >> 7 else x)), "rt8s", [a, v]))
    for a in [0, 1, 7, 65528]:
        for v in [0, 1, -1, 0x7FFFFFFFFFFFFFFF, -0x8000000000000000, 0x0123456789ABCDEF]:
            cases.append((str(s64(v)), "rt64", [a, v]))
    for v in [0, 0x1234, 0xFF00, -1, 0x12345678]:
        cases.append((str((u32(v) >> 8) & 0xFF), "byte1", [v]))
    # out-of-bounds accesses: each must be a clean trap
    cases += [
        ("trap:oob", "ld32", [65533]),
        ("trap:oob", "ld32", [65534]),
        ("trap:oob", "ld32", [65536]),
        ("trap:oob", "ld32", [-1]),            # 0xffffffff, must not wrap to 0
        ("trap:oob", "ld64", [65529]),
        ("trap:oob", "ld8u", [65536]),
        ("trap:oob", "ldoff", [65533]),        # dynamic 65533 + static 4
        ("trap:oob", "rt32", [65533, 1]),
        ("trap:oob", "rt8s", [65536, 1]),
        ("trap:oob", "rt64", [65529, 1]),
        ("0", "ld8u", [65535]),                # last valid byte, still in range
        ("0", "ld32", [65532]),                # last valid word, still in range
    ]
    cases += [("1", "size", []), ("1", "grow", [0]), ("1", "grow", [1]),
              ("-1", "grow", [2]), ("-1", "grow", [65536]),
              ("1", "grow_size", [0]), ("2", "grow_size", [1]),
              ("1", "grow_size", [5])]
    return emit("memory", m, cases)


# -------------------------------------------------------------- call_indirect
@suite
def s_indirect():
    m = Mod()
    m.func("add", ["i32", "i32"], ["i32"], "local.get 0\nlocal.get 1\ni32.add\nend")
    m.func("sub", ["i32", "i32"], ["i32"], "local.get 0\nlocal.get 1\ni32.sub\nend")
    m.func("mul", ["i32", "i32"], ["i32"], "local.get 0\nlocal.get 1\ni32.mul\nend")
    m.func("neg", ["i32"], ["i32"], "i32.const 0\nlocal.get 0\ni32.sub\nend")
    t_bin = m.ftype(["i32", "i32"], ["i32"])
    # table slots 0..3 filled; 4 and 5 left uninitialised on purpose
    m.table = 6
    m.elems.append((0, [0, 1, 2, 3]))
    m.func("dispatch", ["i32", "i32", "i32"], ["i32"],
           "local.get 1\nlocal.get 2\nlocal.get 0\ncall_indirect %d\nend" % t_bin)

    cases = []
    for a, b in [(3, 4), (-5, 7), (0x7FFFFFFF, 1), (-0x80000000, -1)]:
        cases.append((str(s32(a + b)), "dispatch", [0, a, b]))
        cases.append((str(s32(a - b)), "dispatch", [1, a, b]))
        cases.append((str(s32(a * b)), "dispatch", [2, a, b]))
        # slot 3 holds `neg`, type [i32]->[i32]: signature mismatch must trap
        cases.append(("trap:indirect-call-type-mismatch", "dispatch", [3, a, b]))
        cases.append(("trap:uninitialized-element", "dispatch", [4, a, b]))
        cases.append(("trap:uninitialized-element", "dispatch", [5, a, b]))
        cases.append(("trap:undefined-element", "dispatch", [6, a, b]))
        cases.append(("trap:undefined-element", "dispatch", [-1, a, b]))
    return emit("indirect", m, cases)


# -------------------------------------------------------------------- globals
@suite
def s_globals():
    m = Mod()
    m.globals.append(("i32", True, asm("i32.const 7") + bytes([0x0B])))
    m.globals.append(("i64", False, asm("i64.const -1000000000000") + bytes([0x0B])))
    m.globals.append(("i32", True, asm("i32.const 0") + bytes([0x0B])))
    m.func("get0", [], ["i32"], "global.get 0\nend")
    m.func("get1", [], ["i64"], "global.get 1\nend")
    m.func("bump", ["i32"], ["i32"],
           "global.get 0\nlocal.get 0\ni32.add\nglobal.set 0\nglobal.get 0\nend")
    m.func("started", [], ["i32"], "global.get 2\nend")
    start = m.func("_start", [], [], "i32.const 42\nglobal.set 2\nend", export=False)
    m.start = start
    cases = [("7", "get0", []), ("-1000000000000", "get1", []),
             ("12", "bump", [5]), ("6", "bump", [-1]),
             ("42", "started", [])]
    return emit("globals", m, cases)


# ------------------------------------------------------- zero-page memory
# A memory declared with 0 pages.  This is the case the linear-memory
# allocation used to carry a spare byte for ("+1 so mem_at() of a zero-page
# memory still yields a valid pointer"), and nothing exercised it.  The spare
# byte is gone -- xcalloc already returns a valid pointer for a zero size --
# so this fixture is what keeps the exact-size allocation honest, together
# with the oob cases below, which read at offset 0 of a memory that has no
# offset 0.
@suite
def s_mem0():
    m = Mod()
    m.mem = (0, None)
    m.func("size", [], ["i32"], "memory.size\nend")
    m.func("ld", ["i32"], ["i32"], "local.get 0\ni32.load8_u\nend")
    m.func("st", ["i32"], [], "local.get 0\ni32.const 7\ni32.store8\nend")
    # memory.grow from zero pages: the realloc path whose size is 0 bytes
    # before it grows.  Returns the OLD size, so 0.
    m.func("grow", ["i32"], ["i32"], "local.get 0\nmemory.grow\nend")
    cases = [
        ("0", "size", []),
        ("trap:oob", "ld", [0]), ("trap:oob", "ld", [1]),
        ("trap:oob", "st", [0]),
        ("0", "grow", [0]), ("0", "grow", [1]),
    ]
    return emit("mem0", m, cases)


# ------------------------------------------------------------ type validation
# Both directions of the claim, kept apart on purpose: a validator that
# rejects everything satisfies the "rejects the bad" half perfectly.
#
# s_types() is the ACCEPT direction: well-typed modules that exercise both
# value types, select without an annotation, block/loop result types, the
# loop-label-takes-nothing rule and the spec's polymorphic stack in
# unreachable code.  Every one must load AND return the right value.
#
# s_type_reject() is the REJECT direction: one module per defect.
#
# Neither direction is self-scored.  t/v8check.js asks V8, an outside
# WebAssembly implementation, whether each of these modules is valid, and the
# answers have to line up with what is declared here.
@suite
def s_types():
    m = Mod()
    m.mem = (1, None)
    m.globals.append(("i64", False, asm("i64.const -9000000000") + b"\x0b"))

    # select with no type annotation, at both widths, in the same module.
    m.func("sel64", ["i64", "i64", "i32"], ["i64"],
           "local.get 0\nlocal.get 1\nlocal.get 2\nselect\nend")
    m.func("sel32", ["i32", "i32", "i32"], ["i32"],
           "local.get 0\nlocal.get 1\nlocal.get 2\nselect\nend")

    # block with an i64 result, and a block result consumed by a conversion.
    m.func("blk64", [], ["i64"], "block i64\ni64.const -5000000000\nend\nend")
    m.func("blkwrap", [], ["i32"],
           "block i64\ni64.const 0x123456789\nend\ni32.wrap_i64\nend")

    # loop with a result type.  The br_if leaves the stack empty, which is
    # only legal because a branch to a LOOP label takes the loop's parameters
    # (none here), not its results.  Give the loop a block's rule instead and
    # this stops validating.
    m.func("loopres", ["i32"], ["i32"],
           "loop i32\n"
           "  local.get 0\n  i32.const 2\n  i32.mul\n  local.set 0\n"
           "  local.get 0\n  i32.const 100\n  i32.lt_s\n  br_if 0\n"
           "  local.get 0\n"
           "end\nend")
    m.func("loopzero", [], ["i32"],
           "loop i32\ni32.const 0\nbr_if 0\ni32.const 42\nend\nend")

    # Unreachable code is validated polymorphically, not skipped: the i32.add
    # below has no operands and is still well typed.
    m.func("polyadd", [], ["i32"], "unreachable\ni32.add\nend")
    m.func("brdead", [], ["i32"],
           "block i32\ni32.const 7\nbr 0\ni32.const 1\ni32.add\nend\nend")
    m.func("retdead", [], ["i32"], "i32.const 11\nreturn\ni32.const 1\ndrop\nend")
    # One select arm comes from the polymorphic bottom, so the other decides.
    m.func("selpoly", [], ["i64"],
           "unreachable\ni64.const 1\ni32.const 0\nselect\nend")

    # Entering unreachable code DISCARDS whatever the frame had on the stack:
    # the spec replaces it with the polymorphic bottom.  Both of these leave an
    # i64 behind that nothing ever consumes, and both are valid because of it.
    m.func("deadstack", [], ["i32"],
           "block\ni64.const 1\nbr 0\nend\ni32.const 5\nend")
    m.func("deadtrap", [], ["i32"], "i64.const 1\nunreachable\nend")

    # br_table whose targets all agree with the default.
    m.func("brtbl", ["i32"], ["i32"],
           "block\nblock\nlocal.get 0\nbr_table 0 1 1\nend\n"
           "i32.const 10\nreturn\nend\ni32.const 20\nend")

    # if/else with an i64 result.
    m.func("ifres", ["i32"], ["i64"],
           "local.get 0\nif i64\ni64.const 100\nelse\ni64.const -200\nend\nend")

    # i64 through memory, an i64 global, an i64 local via local.tee.
    m.func("mem64", ["i64"], ["i64"],
           "i32.const 8\nlocal.get 0\ni64.store\ni32.const 8\ni64.load\nend")
    m.func("glob64", [], ["i64"], "global.get 0\nend")
    m.func("teetype", ["i64"], ["i32"],
           "local.get 0\nlocal.tee 1\ni64.eqz\nend", locals=["i64"])

    # call and call_indirect with an i64 parameter.
    h = m.func("h64", ["i64"], ["i64"], "local.get 0\ni64.const 3\ni64.mul\nend")
    m.func("callmix", ["i32"], ["i64"],
           "local.get 0\ni64.extend_i32_s\ncall %d\nend" % h)
    m.table = 1
    m.elems.append((0, [h]))
    m.func("indmix", ["i64"], ["i64"],
           "local.get 0\ni32.const 0\ncall_indirect %d\nend" % m.ftype(["i64"], ["i64"]))

    def py_loopres(n):
        while True:
            n = s32(n * 2)
            if not (n < 100):
                return n

    cases = [
        ("7", "sel64", [7, -9, 1]), ("-9", "sel64", [7, -9, 0]),
        ("0", "sel64", [0, 1, 1]),
        ("7", "sel32", [7, -9, 1]), ("-9", "sel32", [7, -9, 0]),
        ("-5000000000", "blk64", []),
        (str(s32(0x123456789 & MASK32)), "blkwrap", []),
        (str(py_loopres(3)), "loopres", [3]), (str(py_loopres(1)), "loopres", [1]),
        (str(py_loopres(-200)), "loopres", [-200]),
        ("42", "loopzero", []),
        ("trap:unreachable", "polyadd", []),
        ("7", "brdead", []),
        ("5", "deadstack", []), ("trap:unreachable", "deadtrap", []),
        ("11", "retdead", []),
        ("trap:unreachable", "selpoly", []),
        ("10", "brtbl", [0]), ("20", "brtbl", [1]), ("20", "brtbl", [7]),
        ("100", "ifres", [1]), ("-200", "ifres", [0]),
        ("-9000000000", "mem64", [-9000000000]),
        ("9223372036854775807", "mem64", [9223372036854775807]),
        ("-9000000000", "glob64", []),
        ("1", "teetype", [0]), ("0", "teetype", [-1]),
        ("-3000000000", "callmix", [-1000000000]),
        ("21", "indmix", [7]),
    ]
    return emit("types", m, cases)


@suite
def s_type_reject():
    n = 0

    def rej(fname, mod, note):
        nonlocal n
        n += emit("ty_" + fname, mod.build() if isinstance(mod, Mod) else mod,
                  [("!error", "f", [])])

    def one(fname, params, results, body, note, locals=(), setup=None):
        m = Mod()
        if setup:
            setup(m)
        m.func("f", params, results, body, locals=locals)
        rej(fname, m, note)

    # The headline case: an i64 operator applied to i32 operands.  Depth
    # tracking accepts this and the interpreter then reads garbage.
    one("add_mixed", [], ["i64"], "i32.const 1\ni32.const 2\ni64.add\nend",
        "i64.add on two i32s")
    one("add_mixed2", [], ["i32"], "i64.const 1\ni64.const 2\ni32.add\nend",
        "i32.add on two i64s")

    # Function results.
    one("result", [], ["i32"], "i64.const 1\nend", "returns i64 where i32 declared")
    one("result_none", [], [], "i32.const 1\nend", "leaves an operand at function end")
    one("result_missing", [], ["i32"], "nop\nend", "returns nothing where i32 declared")

    # Locals and globals.
    one("local_set", [], [], "i32.const 1\nlocal.set 0\nend",
        "local.set i64 from i32", locals=["i64"])
    one("local_get", [], ["i32"], "local.get 0\nend",
        "local.get i64 as i32", locals=["i64"])
    # Exactly one past the end of the locals array.  This is the case that
    # keeps the `lt` buffer honest: run under make test-asan with the bounds
    # check in prepare() broken, a rounded-up `lt` swallows the read silently
    # (and the garbage byte reads as the polymorphic type, so the module
    # validates), while a `lt` trimmed to nlocals makes ASan name the line.
    one("local_past", [], ["i64"], "local.get 1\nend",
        "local index one past the end", locals=["i64"])

    def g64(m):
        m.globals.append(("i64", True, asm("i64.const 0") + b"\x0b"))
    one("global_set", [], [], "i32.const 1\nglobal.set 0\nend",
        "global.set i64 from i32", setup=g64)
    one("global_get", [], ["i32"], "global.get 0\nend",
        "global.get i64 as i32", setup=g64)

    # Block and loop result types.
    one("block_res", [], ["i64"], "block i64\ni32.const 1\nend\nend",
        "block declares i64, body leaves i32")
    one("block_extra", [], ["i32"], "block i32\ni32.const 1\ni32.const 2\nend\nend",
        "block leaves a spare operand")
    one("loop_res", [], ["i64"], "loop i64\ni32.const 1\nend\nend",
        "loop declares i64, body leaves i32")

    # if/else.
    one("if_arms", [], ["i32"],
        "i32.const 0\nif i32\ni32.const 1\nelse\ni64.const 2\nend\nend",
        "if arms produce different types")
    one("if_noelse", [], ["i32"], "i32.const 0\nif i32\ni32.const 1\nend\nend",
        "if with a result type and no else")
    one("if_cond", [], [], "i64.const 1\nif\nend\nend", "if condition is i64")

    # `return` carries the function's result types.  Nothing else can reject
    # these: the implicit `end` that follows sees an unreachable frame, where
    # a pop of the wrong type succeeds.
    one("return_type", [], ["i64"], "i32.const 1\nreturn\nend",
        "return carries i32 from a function declared i64")
    one("return_empty", [], ["i64"], "return\nend",
        "return carries nothing from a function declared i64")

    # Branches.
    one("br_value", [], ["i32"], "block i32\ni64.const 1\nbr 0\nend\nend",
        "br carries i64 to an i32 label")
    one("br_empty", [], ["i32"], "block i32\nbr 0\nend\nend",
        "br to an i32 label with an empty stack")
    one("brif_cond", [], [], "block\ni64.const 1\nbr_if 0\nend\nend",
        "br_if condition is i64")
    one("brtable_cond", [], [], "block\ni64.const 0\nbr_table 0 0\nend\nend",
        "br_table index is i64")
    # Both of these put enough operands on the stack for the default label, so
    # the only thing that can reject them is the rule that every br_table
    # target must agree with the default.  The obvious version -- two labels of
    # different arity and nothing on the stack -- is rejected by the underflow
    # check instead, and so cannot see the agreement rule at all.
    one("brtable_arity", [], [],
        "block\nblock i32\ni32.const 7\ni32.const 0\nbr_table 0 1\nend\n"
        "drop\nend\nend",
        "br_table target takes i32, default takes nothing")
    one("brtable_type", [], ["i64"],
        "block i64\nblock i32\ni64.const 1\ni32.const 0\nbr_table 0 1\nend\n"
        "drop\ni64.const 5\nend\nend",
        "br_table target takes i32, default takes i64")

    # select.
    # The result type is chosen so that whichever arm a broken select picks,
    # the function signature accepts it: otherwise the return-type check
    # rejects the module and the select rule is never exercised.  Both
    # orderings, because a rule dropped from one operand is invisible when the
    # interesting type is on the other.
    one("select_arms", [], ["i64"],
        "i32.const 1\ni64.const 2\ni32.const 0\nselect\nend",
        "select arms i32/i64, i64 on top")
    one("select_arms2", [], ["i32"],
        "i64.const 1\ni32.const 2\ni32.const 0\nselect\nend",
        "select arms i64/i32, i32 on top")
    one("select_cond", [], ["i32"],
        "i32.const 1\ni32.const 2\ni64.const 0\nselect\nend",
        "select condition is i64")

    # Calls.
    m = Mod()
    m.func("h", ["i64"], ["i64"], "local.get 0\nend")
    m.func("f", [], ["i64"], "i32.const 1\ncall 0\nend")
    rej("call_arg", m, "call passes i32 where i64 declared")

    m = Mod()
    m.table = 1
    ti = m.ftype(["i64"], ["i64"])
    m.func("f", [], ["i64"], "i32.const 1\ni32.const 0\ncall_indirect %d\nend" % ti)
    rej("callind_arg", m, "call_indirect passes i32 where i64 declared")

    m = Mod()
    m.table = 1
    ti = m.ftype(["i64"], ["i64"])
    m.func("f", [], ["i64"], "i64.const 1\ni64.const 0\ncall_indirect %d\nend" % ti)
    rej("callind_index", m, "call_indirect table index is i64")

    # Memory.
    def mem(m_):
        m_.mem = (1, None)
    one("store_val", [], [], "i32.const 0\ni64.const 1\ni32.store\nend",
        "i32.store given an i64 value", setup=mem)
    one("store_addr", [], [], "i64.const 0\ni32.const 1\ni32.store\nend",
        "i32.store given an i64 address", setup=mem)
    one("load_addr", [], ["i32"], "i64.const 0\ni32.load\nend",
        "i32.load given an i64 address", setup=mem)
    one("load_res", [], ["i64"], "i32.const 0\ni32.load\nend",
        "i32.load result used as i64", setup=mem)
    one("memgrow_arg", [], ["i32"], "i64.const 1\nmemory.grow\nend",
        "memory.grow given an i64", setup=mem)

    # Conversions and unary operators.
    one("wrap_arg", [], ["i32"], "i32.const 1\ni32.wrap_i64\nend",
        "i32.wrap_i64 given an i32")
    one("extend_arg", [], ["i64"], "i64.const 1\ni64.extend_i32_s\nend",
        "i64.extend_i32_s given an i64")
    one("eqz_arg", [], ["i32"], "i64.const 1\ni32.eqz\nend", "i32.eqz given an i64")
    one("cmp_mixed", [], ["i32"], "i32.const 1\ni64.const 2\ni64.lt_s\nend",
        "i64.lt_s with one i32 operand")

    # Unreachable code is still type-checked.  These are the cases that
    # separate "polymorphic" from "not looked at".
    one("dead_result", [], ["i32"], "unreachable\ni64.const 1\nend",
        "dead code leaves i64 where i32 is declared")
    one("dead_br", [], ["i32"],
        "block i32\ni32.const 7\nbr 0\ni64.const 1\nend\nend",
        "dead code after br leaves i64 at an i32 block end")
    one("dead_add", [], ["i32"], "unreachable\ni64.const 1\ni32.add\nend",
        "i32.add on an i64 pushed in dead code")

    return n


# ----------------------------------------------------- modules that must be rejected
@suite
def s_reject():
    n = 0

    def rej(fname, blob, note):
        nonlocal n
        # The function name has to be one the module really exports.  It was
        # "_" here, which no fixture exports, so a module that LOADED still
        # exited 2 with "no exported function named '_'" and scored as a
        # rejection: the entire reject direction passed without validating
        # anything.  See README, "the harness bug".
        n += emit(fname, blob, [("!error", "f", [])])

    # f32 in a signature: honest refusal, not a miscompile
    m = Mod()
    m.func("f", ["f32"], ["f32"], "local.get 0\nlocal.get 0\nf32.add\nend")
    rej("rej_f32", m.build(), "float types")

    m = Mod()
    m.func("f", ["f64"], ["f64"], "local.get 0\nlocal.get 0\nf64.add\nend")
    rej("rej_f64", m.build(), "float types")

    # structurally broken bodies
    m = Mod()
    m.func("f", [], ["i32"], "i32.add\nend")                       # stack underflow
    rej("rej_underflow", m.build(), "underflow")

    m = Mod()
    m.func("f", [], ["i32"], "block\ni32.const 1\nend")            # missing end
    rej("rej_noend", m.build(), "unterminated block")

    m = Mod()
    m.func("f", [], ["i32"], "i32.const 1\nbr 5\nend")             # label out of range
    rej("rej_brdepth", m.build(), "branch depth")

    m = Mod()
    m.func("f", [], ["i32"], "local.get 3\nend")                   # bad local index
    rej("rej_local", m.build(), "local index")

    m = Mod()
    m.func("f", [], ["i32"], "i32.const 0\ni32.load\nend")         # memory op, no memory
    rej("rej_nomem", m.build(), "no memory")

    m = Mod()
    m.func("f", [], ["i32"], "call 9\nend")                        # bad func index
    rej("rej_call", m.build(), "call index")

    m = Mod()
    m.func("f", [], ["i32"], "i32.const 0\nend")
    good = m.build()
    rej("rej_magic", b"\x00asx" + good[4:], "bad magic")
    rej("rej_version", good[:4] + b"\x09\x00\x00\x00" + good[8:], "bad version")
    rej("rej_truncated", good[: len(good) - 3], "truncated file")
    rej("rej_empty", b"", "empty file")
    # A LEB128 that never terminates (or is absurdly padded) must be rejected,
    # not walked off the end of the buffer.
    pad = b"\x80" * 12
    rej("rej_leb", b"\0asm" + (1).to_bytes(4, "little")
        + bytes([1]) + uleb(len(pad)) + pad, "runaway LEB128")
    # trailing garbage section id
    rej("rej_badsec", good + bytes([99]) + uleb(1) + b"\x00", "unknown section")

    # data segment past the end of memory
    m = Mod()
    m.mem = (1, None)
    m.data.append((65530, b"0123456789"))
    m.func("f", [], ["i32"], "i32.const 0\nend")
    rej("rej_data_oob", m.build(), "data out of bounds")

    # element segment past the end of the table
    m = Mod()
    m.table = 2
    m.func("f", [], ["i32"], "i32.const 0\nend")
    m.elems.append((1, [0, 0]))
    rej("rej_elem_oob", m.build(), "element out of bounds")

    return n


# ========================================================================= main
def main():
    # Start from an empty t/fix.  mkwasm only ever wrote files, so a renamed or
    # deleted fixture stayed behind, was counted by t/run.sh and by
    # t/v8check.js, and would have been committed.  One did.
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    total = 0
    files = 0
    for fn in SUITES:
        total += fn()
        files += 1
    print("mkwasm: %d fixtures, %d cases" % (len(os.listdir(OUT)) // 2, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
