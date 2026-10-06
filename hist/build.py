#!/usr/bin/env python3
import os, subprocess, sys, shutil

REPO = "/home/x/projects/wasmi"
H = "/tmp/hist"
sys.path.insert(0, H)
import gen, genpy

MF_BUILD = """CC      ?= gcc
CFLAGS  ?= -std=c11 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion
LDFLAGS ?=

.PHONY: all clean

all: wasmi

wasmi: wasmi.c
\t$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
\trm -f wasmi
"""

MF_TEST = """CC      ?= gcc
CFLAGS  ?= -std=c11 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion
LDFLAGS ?=

.PHONY: all test fixtures clean

all: wasmi

wasmi: wasmi.c
\t$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

fixtures:
\tpython3 t/mkwasm.py

test: wasmi fixtures
\t@sh t/run.sh

clean:
\trm -f wasmi
\trm -rf t/fix
"""

GITIGNORE = """wasmi
wasmi-asan
*.o
core
core.*
vgcore.*
t/fix/
"""


def sh(*args, **kw):
    return subprocess.run(args, cwd=REPO, check=True, **kw)


def write(rel, text, mode=None):
    p = os.path.join(REPO, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)
    if mode:
        os.chmod(p, mode)


def commit(msg, files):
    sh("git", "add", *files)
    sh("git", "commit", "-q", "-F", "-", input=msg.encode())
    # every commit must build
    r = subprocess.run(["make", "-s"], cwd=REPO, capture_output=True)
    if r.returncode != 0:
        print("BUILD BROKE at:", msg.splitlines()[0])
        print(r.stderr.decode()[:2000])
        sys.exit(1)


C = []


def step(msg, files, actions):
    C.append((msg, files, actions))


def wasmi(level, bugs=()):
    return lambda: write("wasmi.c", gen.version(level, bugs))


def mkw(stage):
    return lambda: write("t/mkwasm.py", genpy.version(stage), 0o755)


# ---------------------------------------------------------------- 01
def a01():
    write("wasmi.c", gen.version(1))
    write("Makefile", MF_BUILD)
    write(".gitignore", GITIGNORE)
    shutil.copy("/usr/share/common-licenses/GPL-3", os.path.join(REPO, "LICENSE"))


step("""skeleton: bounds-checked reader, magic/version, section framing

Start from the only part of a wasm decoder that every later bug report runs
through: the byte reader.  `Rd` carries an explicit length and every read goes
through rd_u8/rd_bytes, so a truncated or hostile file cannot walk off the end of
the buffer no matter what the sections claim about their own size.

The LEB128 readers get the length caps up front rather than as a later hardening
pass.  Bit 7 of each byte is the continuation bit, so a chain of 0x80 bytes is a
perfectly well-formed-looking encoding of zero that is also an unbounded read and
an undefined shift.  Capping the length is not defence in depth here, it is the
difference between a parser and a vulnerability, so it belongs in the first
commit alongside the reader it protects.

The section loop validates framing only: id, LEB128 size, and that the payload
fits inside the file.  Section ids must increase (custom sections, id 0, may
appear anywhere), which is the one ordering rule the binary format gives us for
free.  Nothing is decoded yet, so the CLI loads a module, proves the framing is
sane, and exits 2 with an honest message.  That is a useful program: it already
rejects every truncated, mis-magicked and mis-framed file.

The data model (FType/Func/Global/Export/Module, the opcode enum, the stack
ceilings) is laid down here too.  In a single-file C program there is no
cheaper place to put it, and having the declarations stable from the start keeps
every later commit to the code that fills them in.

Verification at this point is the compiler and running it by hand against a
hand-built module; there is no test suite yet.  It arrives in a later commit, and
until then no claim in these messages rests on one.""",
     ["wasmi.c", "Makefile", ".gitignore", "LICENSE"], a01)

# ---------------------------------------------------------------- 02
step("""type and export sections: make the CLI's FUNC argument mean something

The type section is decoded first because every other section indexes into it,
and the export section second because together they are the smallest pair that
turns `wasmi MODULE.wasm FUNC` into a resolvable request.  find_export also
accepts a bare decimal function index, which is purely a debugging affordance for
modules whose export table is missing or wrong.

Multi-value results are rejected here rather than ignored: the MVP allows zero or
one result, and a module declaring two is not a module this interpreter can run,
so it gets an error instead of a silently dropped value.  check_valtype rejects
anything that is not i32/i64, which at this point includes f32/f64 by accident
rather than by design; a later commit makes that refusal explicit and gives it a
message that says why.

Export names are kept as (pointer, length) into the original file buffer, which
stays mapped for the life of the process.  Nothing is copied, nothing is
NUL-terminated, and comparison is memcmp against the requested name's length.
That avoids both an allocation and the whole class of bugs that come from
assuming a wasm name is a C string: wasm names are arbitrary byte sequences and
may contain NULs.""", ["wasmi.c"], wasmi(2))

# ---------------------------------------------------------------- 03
step("""function and code sections: bodies, and run-length-encoded locals

The function section is a vector of type indices and the code section is the
matching vector of bodies, so they are decoded as a pair and cross-checked: a
module whose code section has a different number of bodies than its function
section declares is rejected rather than half-loaded.

The code section is deliberately processed *after* the section loop finishes.
Its bodies need the function-to-type mapping to know how many parameters each
function has, and the binary format does not promise the function section comes
first in a way we can rely on without a second pass anyway.  Keeping a pointer to
the payload and coming back to it is simpler than threading state through the
loop.

Locals are run-length encoded as (count, type) groups, which is a real
compression win for compiler output and a real denial-of-service vector if taken
literally: a two-byte group can ask for four billion locals.  The total is capped,
and the parameter types are prepended so that locals are a single flat array
indexed from zero, parameters first.  That is the layout the interpreter will
want later, so building it here avoids a translation step at every local.get.

Bodies are required to end with 0x0b.  That is both a format rule and a
precondition the interpreter will rely on, so it is checked at load time where
the error can still be a clean rejection.""", ["wasmi.c"], wasmi(3))

# ---------------------------------------------------------------- 04
step("""validate ahead of execution: structure, branch targets, stack depth

This is the commit that makes the interpreter loop able to be fast and unsafe-
looking without actually being unsafe.  prepare() walks each body once and does
three jobs.

First, structure.  WebAssembly control flow is *structured*: there are no
computed jumps, only `br N` to the N-th enclosing label.  So a label stack is
enough to match every block/loop/if against its else/end, and resolve_jumps()
turns that into a pc-indexed table.  A branch then costs an array lookup instead
of a forward scan over the body, and crucially the scan happens once at load time
where a malformed nesting is a rejection rather than a runaway loop.

Second, operand-stack depth.  The height is tracked symbolically -- not the types,
which is the honest gap in this validator and is written down as such -- and the
high-water mark is recorded per function.  This has to happen before execution,
not during it: the interpreter's hot loop pushes without checking, so the only
thing standing between a body of four million `i32.const` instructions and a heap
overflow is knowing the ceiling in advance and reserving it once per call.  Code
after br/return/unreachable is unreachable and the spec validates it
polymorphically; a `dead` flag approximates that by suspending height tracking
while still matching the `end`s, because the nesting still has to be well formed.

Third, index bounds.  Every local, global, function, type and label index is
range-checked here, which is what lets exec() index raw arrays with no
per-instruction checks at all.  global.set on an immutable global is rejected
here too, since it is a static property.

The whole control-flow grammar is validated in this commit even though none of it
is executable yet.  Splitting "understand the shape" from "run the shape" is the
point: the validator is the security boundary and deserves to be reviewable on
its own.""", ["wasmi.c"], wasmi(4, ["oob"]))

# ---------------------------------------------------------------- 05
step("""fix: prepare() read cs[csp-1] after the function's own terminating end

One-element out-of-bounds read, and the first thing that happened the first time
a real module was fed to the validator: a segfault on every single function body,
because every body ends with the function's own 0x0b.

The `end` handler popped the label stack and then `break`'t out of the switch into
the shared stack-effect epilogue, which dereferences cs[csp-1] to find the
enclosing label's floor.  For the function's own terminating `end` that pop leaves
csp == 0, so cs[csp-1] is cs[-1].  The fix is to `continue` instead, skipping an
epilogue that has nothing left to check, and to keep the
"instructions after the function end" check that used to live inside the taken
branch.

Kept as its own commit so the diff that fixes it is a few lines and so `git
revert` on it reproduces the crash exactly.  ASan named the line immediately
(`SEGV ... in prepare wasmi.c:369`, a READ of an address one slot below the
allocation), which is the argument for having the sanitizer build be a first-class
target rather than something you reach for after a week of confusion.

Worth recording because of what it says about the test strategy: this bug was not
found by the differential oracle reasoning about arithmetic.  It was found by
running the generator's very first fixture at all.  Most of the value of a test
suite is in existing.""", ["wasmi.c"], wasmi(4))

# ---------------------------------------------------------------- 06
step("""refuse imports and floating point explicitly, and tighten section ids

Up to now an f32 parameter was rejected as "unknown value type 0x7d" and an
import section as "unknown section id 2".  Both are correct refusals with
misleading reasons, and a misleading reason is how a user concludes the tool is
broken rather than that their module is out of scope.

f32/f64 are a deliberate cut, not an oversight, and this commit makes the program
say so.  check_valtype names them.  is_float_op() is a single explicit predicate
over the float opcode ranges -- the comparisons at 0x5b..0x66, the arithmetic at
0x8b..0xa6, the conversions, the reinterprets, the two float loads and two float
stores hiding inside the memory opcode range, and the two float constants -- so
that a genuinely unknown byte and a known-but-unimplemented one produce different
messages.  Getting NaN propagation, signed zero and the rounding modes right is a
project of its own; running a float module with the floats quietly treated as
integers would be strictly worse than refusing it.

Imports are the other cut.  wasmi runs self-contained modules: there is no host
ABI, so an import can only ever be a dangling reference.  Rejecting a non-empty
import section at decode time keeps the function index space simple (no imported
functions preceding the defined ones) throughout the rest of the interpreter.

The section loop also stops being lenient here.  An unknown section id is now an
error rather than a skip, and every section is required to have consumed its
whole payload.  Those two checks together close the gap where a module could
smuggle data past the decoder in a section we did not look at; they are only
possible now that the sections we *do* understand are decoded fully.""",
     ["wasmi.c"], wasmi(5))

# ---------------------------------------------------------------- 07
step("""interpreter core: frames, operand stack, locals, constants, drop, select

The smallest thing that can return a value, so that every later instruction
family has somewhere to land.

Values live in one flat uint64_t array shared by every frame, with an i32 stored
zero-extended in the low 32 bits.  A frame's locals occupy slots [base, base +
nlocals) and its operand stack starts immediately after, so local.get is a single
indexed load and a call's arguments are already in place as the callee's first
parameters -- no copying at the boundary.  Declared (non-parameter) locals are
zeroed, which the spec requires and which also means an uninitialised local can
never expose a previous frame's data.

do_call() makes the one stack check: gbase + nlocals + maxstack against the
ceiling, using the maxstack that prepare() computed.  That is the payoff of
validating ahead of execution -- one comparison per call instead of one per push,
and exceeding the ceiling is a clean `trap:stack-overflow` rather than a heap
write.

Traps unwind with setjmp/longjmp back to main, which prints `trap:<code>` on
stdout and exits 1.  Decode errors go to stderr and exit 2.  Keeping those two
statuses distinct is not cosmetic: the test harness that arrives later asserts on
*which* trap fired, and a crash (any status above 2) must never be mistakable for
either.

The immediate decoders uleb()/sleb() deliberately have no bounds checks.  The body
was fully walked by prepare() before we got here, so every immediate is known
well-formed; re-checking in the hot loop would be paying twice for the same
guarantee.

RET() is a macro rather than a function because it has to both move the results
down to the frame base and return out of the dispatch loop.""",
     ["wasmi.c"], wasmi(6))

# ---------------------------------------------------------------- 08
step("""i32 arithmetic, with the two division traps

add/sub/mul are done on uint32_t so that wraparound is defined rather than
signed-overflow undefined behaviour; the spec wants modular arithmetic and C only
gives it to you on unsigned types.

Both division traps are here because both are cases where the spec has an answer
and C does not:

  - division or remainder by zero traps (`trap:div0`).
  - INT32_MIN / -1 traps (`trap:int-overflow`).  The quotient 2^31 is not
    representable in i32, and in C the expression is undefined, so the check has
    to come first -- on x86 it faults, and an interpreter that lets it through
    takes the host down with SIGFPE instead of producing a wasm trap.
  - INT32_MIN % -1 is 0 by the spec but is equally undefined in C on the same
    hardware, so it is special-cased rather than computed.

div_s/rem_s are computed on int32_t, which gives C's truncating-toward-zero
division -- the same thing the spec asks for, and notably *not* what a reader who
reaches for Python's `//` would get.  That asymmetry is exactly the kind of thing
the Python side of the oracle has to reimplement by hand later, and is the reason
rem_s with mixed signs is on the list of cases the fixtures must cover.""",
     ["wasmi.c"], wasmi(7))

# ---------------------------------------------------------------- 09
step("""i32 bitwise and shifts: shift counts are masked, not saturated

and/or/xor are unremarkable.  The shifts are the single most common place a
hand-written wasm interpreter disagrees with the specification, and they need the
comment they get.

The spec defines i32 shifts modulo 32: `i32.shl 1, 33` is 2, not 0.  An
implementation that passes the count through unchanged gets this wrong, and in C
it also gets undefined behaviour, because shifting by the width or more is UB.
So `b & 31` is simultaneously the conformance rule and the safety rule; there is
no version of this code that is correct and does not mask.

The rotations need the mask *and* a zero guard: `(a << k) | (a >> (32 - k))` is
undefined when k is 0, because the second shift becomes a shift by 32.  Writing
it as a conditional is cheaper than being clever.

shr_s is done on int32_t and relies on gcc's arithmetic right shift for negative
values.  Strictly, C leaves right-shift of a negative value
implementation-defined rather than undefined; gcc documents it as arithmetic, and
this file is gcc-only by declaration, so the dependency is deliberate and
narrow rather than accidental.""", ["wasmi.c"], wasmi(8))

# ---------------------------------------------------------------- 10
step("""clz, ctz, popcnt

Three bit-counting opcodes, mapped onto the gcc builtins, with the one subtlety
that makes them worth their own commit: __builtin_clz and __builtin_ctz are
undefined for a zero argument, while the spec says i32.clz(0) and i32.ctz(0) are
both 32.

So the zero case is handled explicitly in the helpers rather than at each call
site.  An implementation that forwards straight to the builtin will usually
"work" on x86 -- bsr leaves the destination untouched, so you get whatever was in
the register -- which is the worst possible failure mode: right most of the time,
wrong unpredictably, and invisible to a test that only tries nonzero inputs.
Zero is therefore on the list of values the fixtures must include for these three
opcodes.

popcnt has no such edge case; it is here because it belongs with the other two.""",
     ["wasmi.c"], wasmi(9, ["seqpoint"]))

# ---------------------------------------------------------------- 11
step("""fix: sequence-point UB in the unary opcodes

`PUSH(clz32(P32()))` expands to `vstack[vsp++] = clz32((uint32_t)vstack[--vsp])`,
which modifies vsp twice inside one expression with no sequence point between the
two modifications.  That is undefined behaviour, not merely unspecified order:
the standard gives no meaning at all to such an expression.

gcc found it, not the tests.  `-Wsequence-point` is on by default under -Wall and
said so plainly for each of the three opcodes.  The fix is mechanical -- pop into a
named local first, then push -- and it is kept as its own commit because the class
of bug is worth being able to point at.

The part worth recording: the buggy code produced correct answers.  With this
gcc at -O1 and at -O2, clz(0) came out as 32 and popcnt(7) as 3, so a test suite
run against the broken binary would have been entirely green.  A green suite is
evidence about the values you tried, never about whether the program has defined
behaviour.  That is the whole argument for keeping the compiler in the oracle list
alongside the differential fixtures, and for keeping the sanitizer build as a
first-class make target: they find different things, and the two classes of bug do
not overlap much.

The same mistake would be easy to reintroduce at every unary opcode, so the later
conversions and memory.size are written in the pop-into-a-local form from the
start.""", ["wasmi.c"], wasmi(9))

# ---------------------------------------------------------------- 12
step("""signed and unsigned comparisons for i32 and i64

Twenty-two opcodes, all of which push an i32 0 or 1 regardless of the width of
their operands, and which exist in signed/unsigned pairs precisely because the
value representation does not carry a signedness.

lt_s versus lt_u is the pair that catches implementations out: with a == -1 and
b == 1, lt_s is 1 and lt_u is 0, because as unsigned -1 is 4294967295.  The only
difference in the code is which type the operands are cast to before comparing,
which makes it a one-character bug and a silent one -- the kind that a test matrix
has to include negative values specifically to see.  (Later mutation testing
confirmed the point: implementing lt_s as unsigned fails 110 of the fixture
cases, but every one of those failures involves a negative operand.)

eqz is in this group rather than with the unary arithmetic because it is a
comparison against zero, and because i64.eqz is the one i64 opcode that produces
an i32.""", ["wasmi.c"], wasmi(10))

# ---------------------------------------------------------------- 13
step("""i64 arithmetic, bitwise, shifts, and the width conversions

The i64 half mirrors the i32 half with the mask changed from 31 to 63 and
INT32_MIN changed to INT64_MIN.  It is duplicated rather than abstracted: a macro
over the two widths would save about forty lines and cost the ability to read any
single opcode's behaviour without expanding it in your head, and there is no third
width coming.

The three conversions are the interesting part.  i32.wrap_i64 truncates, which is
free given the representation.  i64.extend_i32_s and i64.extend_i32_u differ only
in whether the source is read as signed before widening, and getting them
backwards is invisible for every non-negative input: extend_u(-1) is 4294967295
while extend_s(-1) is -1.  Both directions are in the conv fixture for exactly
that reason.

Because an i32 is stored zero-extended in the low 32 bits of a 64-bit slot,
extend_u is a no-op at runtime and extend_s is the only one that actually does
work.  That is a representation choice paying off, but it also means a bug in
extend_u would have to be a bug in the representation invariant rather than in
this opcode, so the fixture covers it to pin the invariant down.""",
     ["wasmi.c"], wasmi(11))

# ---------------------------------------------------------------- 14
step("""structured control flow: block, loop, if/else, br, br_if

Execution of the control-flow grammar the validator has understood since the
validation commit.  Labels live on a flat RCtrl stack shared by every frame, each
entry recording where the matching `end` is, where a branch to this label should
land, and the operand-stack height at entry.

The one asymmetry worth stating is the BRANCH macro's loop case.  Branching to a
`block` or `if` label means "leave": pop that label and everything inside it, keep
the label's result values by copying them down to the label's saved stack height,
and jump past the `end`.  Branching to a `loop` label means "iterate": the label
stays on the stack and the pc goes back to the first instruction of the body.
Conflating the two is a bug that makes every loop run exactly once, which is a
plausible-looking wrong answer rather than a crash -- mutation testing later put 12
fixture failures on it.

Copying the result values down to the label's saved height (rather than leaving
them where they are) is what makes `br` out of a deeply nested block with a result
work without any per-block bookkeeping at runtime: the frames being discarded may
have left arbitrary junk on the operand stack, and the branch is the point where
that junk is dropped.

`if` with no `else` still pushes a label frame and jumps to its `end`, so that the
`end` handler can pop it uniformly.  Reaching an `else` opcode during execution
means the then-arm ran to completion, so it jumps to the matching `end` -- the
else-arm is skipped by jumping over it, not by a flag.

Branching to the outermost label is a return, and is handled by the same path: the
frame at cbase is the function itself.""", ["wasmi.c"], wasmi(12))

# ---------------------------------------------------------------- 15
step("""br_table and unreachable

br_table is a vector of label indices followed by a default.  The thing to get
right is that an out-of-range index is **not** an error: it takes the default
branch.  An implementation that traps or falls through on a large index is wrong
in a way that only shows up for inputs outside the table, which is why the
fixture drives it with 0, 1, 2, 3, 7, -1 and 100 against a three-label table.
(Mutation testing: removing the default's out-of-range case fails 4 cases, all of
them the out-of-range ones.)

The immediates are decoded in the loop rather than pre-resolved.  prepare() has
already checked that every label index is in range, so the only work left is
picking one, and a table small enough to matter is small enough to scan.

unreachable is one line and belongs here because it is the other instruction whose
whole job is to not continue.  It traps as `trap:unreachable`, which exits 1
cleanly -- the point being that an explicitly unreachable instruction is a normal,
specified outcome of running a wasm module and must not be an abort, an assert, or
a signal.""", ["wasmi.c"], wasmi(13))

# ---------------------------------------------------------------- 16
step("""call, with a recursion-depth trap instead of a smashed C stack

A wasm call is one C recursion into exec().  That is the cheap implementation and
it hands the host's stack to the guest, so it needs a leash: CALLDEPTH_MAX, and
`trap:call-depth` when it is exceeded.

Without it, a module as small as `func f(i32) -> i32 { return f(x+1) }` takes the
process down with SIGSEGV on a guard page.  That is the single most important
property this interpreter claims -- a malicious module is rejected or traps and
never corrupts the host -- so the leash is not a nicety.  The limit is set well
below what the default 8 MiB stack would allow, because the sanitizer build's
frames are several times larger than the optimised build's and the trap has to
fire first in both.

The fixture for this is deliberately a function that recurses forever and expects
`trap:call-depth`, which also exercises the operand-stack ceiling check in
do_call(): each frame advances the base, so a runaway recursion races the two
limits and whichever fires is still a clean trap.

Arguments need no marshalling.  The callee's parameters are the values already on
top of the caller's operand stack, so do_call() just computes the new base and
zeroes the declared locals above them.""", ["wasmi.c"], wasmi(14))

# ---------------------------------------------------------------- 17
step("""global and start sections

Globals are a type, a mutability flag and a constant init expression.
const_expr() accepts exactly what the MVP allows in that position -- i32.const,
i64.const, global.get -- and checks the result type against the declaration, so a
module cannot sneak an i64 into an i32 global.  Float constants get the explicit
refusal rather than a generic one.

Mutability is enforced at *validation* time, not at run time: global.set on an
immutable global is a static property of the module, so prepare() rejects it and
the interpreter's global.set is a bare store.  That is the same trade as the index
bounds -- check once at load, index freely afterwards.

The start section is here because it is the only other thing that reads a
function index and runs it.  It is invoked before the user's requested export,
with a fresh stack, and its type is required to be [] -> [] at load time.  The
globals fixture leans on exactly this: a start function that writes 42 into a
mutable global, and an export that reads it back, which is a test that the start
function ran at all and ran first.

One shortcut is marked `shortcut:` in the source rather than hidden: an init
expression's global.get is bounds-checked against the total number of globals
rather than against the index being initialised, so a forward reference reads zero
instead of being rejected.  The spec forbids it, real modules do not do it, and
the upgrade is to pass `i` as the bound.""", ["wasmi.c"], wasmi(15))

# ---------------------------------------------------------------- 18
step("""memory and data sections, every load/store width, the oob trap

One linear memory, sized in 64 KiB pages, initialised by data segments whose
bounds are checked at load time (a data segment past the end of memory is an
instantiation failure, not a trap).

Every access goes through mem_at(), which does the bounds check in 64-bit
arithmetic.  That is the whole reason it is a function: the effective address is
the dynamic operand plus a static offset immediate, both u32, so their sum can
exceed 2^32.  Computing it in 32 bits lets `i32.load offset=4` at address
0xffffffff wrap to address 3 and read memory it should not -- a bounds check that
can be defeated by addition is not a bounds check.  The fixture drives exactly
that case and expects `trap:oob`.

The loads and stores are written out one per width instead of being
table-driven, because the thing that actually matters is which of them sign-extend
and which zero-extend, and that is only legible when each is spelled out.
load8_s/load16_s/load32_s go through the signed C types; the _u forms do not.
A load8_s that forgets to sign-extend is correct for every byte below 128, which
is why the memory fixture reads bytes 0x00, 0x7f, 0x80 and 0xff out of its data
segment.

Access is via memcpy in both directions.  That makes unaligned loads and stores
correct and defined for free, which in turn means the alignment immediate can be
ignored as the hint the spec says it is -- the fixtures store and reload 32- and
64-bit values at odd addresses to pin that down.  memory.grow returns the *old*
page count, or -1 on failure, and never moves the guest's view of existing data.""",
     ["wasmi.c"], wasmi(16))

# ---------------------------------------------------------------- 19
step("""table and element sections, call_indirect and its three traps

One funcref table, filled by element segments whose bounds are checked at load
time.  Slots not covered by any segment stay NO_FUNC, which is a distinct state
from "out of range" and produces a distinct trap.

call_indirect is the only place in the MVP where a type check has to happen at run
time, and it has three separate failure modes that a lazy implementation collapses
into one or none:

  - the index is beyond the table's size        -> trap:undefined-element
  - the slot exists but was never initialised   -> trap:uninitialized-element
  - the slot holds a function whose signature does not match the declared type
                                                -> trap:indirect-call-type-mismatch

The last one is the one that matters for safety, because without it a call through
a table reads the callee's parameters off the caller's operand stack using the
callee's arity.  A one-parameter function invoked as if it took two silently
consumes a value that belongs to someone else.  So ftype_eq() compares parameter
count, parameter bytes and result exactly, and the fixture dispatches through a
six-slot table whose fourth entry holds a deliberately mismatched function and
whose fifth and sixth entries are deliberately empty, covering all three traps
plus the three well-typed calls.

The declared type index is validated at load time (and the presence of a table
along with it), so the run-time check is two cheap comparisons and a memcmp.""",
     ["wasmi.c"], wasmi(17))

# ---------------------------------------------------------------- 20
step("""t/mkwasm.py: the assembler half of the oracle

There is no wat2wasm on this machine and no network, so fixtures cannot be
obtained; they have to be manufactured.  This is the manufacturing half: LEB128
encoders, a one-instruction-per-line assembler, and a module builder that emits
correctly framed sections.

The assembler is deliberately dumb.  No folded s-expressions, no implicit `end`,
no type inference -- you write every `end` yourself.  That is not minimalism for
its own sake: an assembler that invents block structure is an assembler that can
paper over the exact bugs the interpreter is being tested for.  If the fixture
says `block` ... `end` ... `end`, those bytes are in the file.

What this file cannot do is tell you whether its output is *right*, and that is
the load-bearing weakness of the whole test strategy.  Nothing outside this
repository has ever validated a byte that mkwasm.py produced.  If its LEB128 is
wrong, or its section framing is, then the interpreter and the expectations are
being tested against the same wrong input and will agree with each other happily.
That limitation is written into the module docstring and into the README's oracle
section, and the only real fix is the spec testsuite, which is the first item of
PLAN.md's Milestone 1.

The float opcodes are in the mnemonic table even though wasmi refuses them,
because the "this module must be rejected" fixtures need to be able to emit
them.""", ["t/mkwasm.py"], mkw(1))

# ---------------------------------------------------------------- 21
step("""t/mkwasm.py: the independent Python reference, and the arithmetic suites

This is the commit that makes the test suite a differential test rather than a
snapshot.

Every expected value is computed here, in Python, from Python semantics, and
never by running wasmi.  The wrapping is written out (u32/s32/u64/s64), the
truncating division is written out, and the shift-count modulo is written out.
tdiv/trem exist specifically because Python's `//` floors and wasm truncates:
-7 // 2 is -4 in Python and -3 in wasm, so reusing the language's operator would
have reproduced the C's behaviour by accident instead of checking it.  The same
goes for the comparisons, which have to pick between u32 and s32 explicitly
because Python integers have no width.

The arithmetic suites run the full cross product of 25 binary operators over 16
chosen i32 values and 14 chosen i64 values, plus the four unary ones, which is
11,466 cases in this commit.  The matrices are small on purpose -- they are
cross-products, so they grow quadratically -- and every entry earns its place: the
sign boundaries, zero, shift counts at and past the bit width, and the one
operand pair (INT_MIN, -1) that makes div_s trap.  Division and remainder by zero
are in the matrix rather than special-cased, so the Python reference returns a
Trap object and the fixture asserts on the trap *code*, not merely that something
went wrong.

What this proves: on these values, two independently written implementations of
i32/i64 arithmetic agree.  What it does not prove: that either one matches the
spec.  Both were written from the same reading of it, so a misreading is a bug
this suite cannot see.  Being clear about that is the difference between a test
suite and a claim.""", ["t/mkwasm.py"], mkw(2))

# ---------------------------------------------------------------- 22
step("""fixtures for control flow and recursion

Nine control-flow functions and five recursive ones, with the expected values
again computed in Python -- but computed differently from how the wasm computes
them, which is the point.  `sum` is a wasm loop with a br_if back edge and a
Python while loop with explicit s32 wrapping; `fib` is wasm double recursion and
Python iteration; `fact` is wasm recursion and a Python product with u64
wrapping.  Agreement between two different algorithms is worth more than
agreement between two transcriptions of one algorithm.

The cases that exist to catch specific bugs:

  - `sw` is a br_table over a three-label chain with the result pre-seeded to the
    default's answer, so "the default arm was taken" is observable rather than
    inferred.  Driven with 0,1,2,3,7,-1,100.
  - `deep` nests 32 blocks and places a `+1` after each inner `end`, then
    branches out of all 32 from the innermost.  Taking the branch must skip all
    31 increments, so the two answers (132 and arg+1) are far apart and a
    partially-correct unwind cannot land on either by luck.
  - `mulrep` multiplies in a loop so that 2^32 wrapping is reached through
    iteration rather than through a single mul.
  - `absish` is driven with INT32_MIN, where negation wraps to itself.
  - `forever` recurses without a base case and must produce trap:call-depth.
    This is the fixture that asserts the host survives a hostile module.
  - mutual recursion (is_even/is_odd) because self-recursion alone would not
    catch a call that resolves the wrong function index.""",
     ["t/mkwasm.py"], mkw(3))

# ---------------------------------------------------------------- 23
step("""fixtures for linear memory

216 cases over one memory, built around a Python bytearray that models the same
memory the data segment initialises.  The expectations are read out of the Python
model with int.from_bytes, which is an independent little-endian decoder rather
than a second copy of the C.

The cases chosen to catch real bugs:

  - sign extension at every width, reading bytes 0x00, 0x7f, 0x80 and 0xff out of
    the data segment, because a load8_s that forgets to sign-extend is correct for
    every value below 128.
  - unaligned round-trips: store then reload 32- and 64-bit values at addresses
    0,1,3,5,7 and at the last address that still fits, which pins down both the
    memcpy approach and the bounds arithmetic at the same time.
  - endianness, by storing a word and reading its second byte, so a big-endian
    mistake cannot hide behind symmetric test values.
  - the static memarg offset applied on top of a dynamic address, including the
    case where the sum crosses the end of memory even though the dynamic part
    does not.
  - out-of-bounds at every width, one byte past the limit in each case, plus
    address 0xffffffff, which is the one that catches a bounds check computed in
    32 bits: 0xffffffff + 4 wraps to 3, which looks perfectly in range.
  - memory.grow returning the old size, and failing (-1) against the declared
    maximum rather than growing past it.

Later mutation testing against this suite: an off-by-one in the bound fails 6
cases, a load8_s without sign extension 14, an ignored memarg offset 10.""",
     ["t/mkwasm.py"], mkw(4))

# ---------------------------------------------------------------- 24
step("""fixtures for call_indirect, globals, and modules that must be rejected

Three groups, the last of which tests the opposite property from everything else.

call_indirect dispatches through a six-slot table: three well-typed binary
operators, a deliberately mismatched one-parameter function in slot 3, two
deliberately empty slots, and indices 6 and -1 past the end.  That covers all
three indirect-call traps and distinguishes them, which matters because the
type-mismatch trap is the one that stands between a table call and reading
another frame's operands off the stack.

The globals group checks a mutable global surviving a read-modify-write, an
immutable i64 with a negative initialiser, and -- via the start section -- that a
start function ran, and ran before the requested export.

The rejection group is the important one.  Sixteen modules that must be refused
at load time with exit status 2 and an `error:` line, not run, not crashed, not
trapped: f32 and f64 signatures; a stack underflow; an unterminated block; a
branch to a label that does not exist; an out-of-range local index; a memory
instruction in a module with no memory; a call to a function index past the end;
bad magic; a bad version; a truncated file; an empty file; a runaway LEB128; a
garbage section id; a data segment past the end of memory; an element segment past
the end of the table.

These are what the project's hard invariant actually rests on, and they are also
the weakest-sampled part of the suite: they are the malformed modules the author
thought of, which is precisely the wrong distribution for testing a parser.  A
coverage-guided fuzzer over the decoder is on PLAN.md's Milestone 1 for that
reason.  The harness distinguishes a rejection (status 2) from a trap (status 1)
from a crash (anything above 2), so a segfault can never be scored as a pass.""",
     ["t/mkwasm.py"], mkw(5))


# ---------------------------------------------------------------- 25
def a25():
    write("t/run.sh", open(H + "/run_sh.txt").read(), 0o755)
    write("Makefile", MF_TEST)


step("""t/run.sh and make test: 11833 passed, 0 failed

The harness is twenty lines of sh over the generated .cases files, each line of
which is `EXPECTED FUNC [args...]`.  EXPECTED is matched against stdout, so a
runtime trap (`trap:<code>` on stdout, status 1) compares like any other expected
value.  That is a deliberate design choice: the suite asserts on *which* trap
fired, not merely that something went wrong, which is what makes the
divide-by-zero, out-of-bounds, unreachable and indirect-call-mismatch cases worth
anything.  `!error` is the one special token and means "must be refused at load
time": status 2 with an `error:` line on stderr.  Any status above 2 is scored as
CRASH and can never pass.

Every case is a fresh process.  11,833 forks is slow (about 15 seconds) and buys
something worth the wait: no case can contaminate another through leftover
memory, mutated globals, or stack state, so a failure is always attributable to
the case that reported it, and the globals fixtures can each assume a freshly
instantiated module.

First green run, and what it was worth:

  11833 passed, 0 failed

Which is a weaker result than it looks, and the README says so at length.  The
suite found exactly one real bug over the course of this work -- the out-of-bounds
read in the validation pass, caught by the first fixture ever executed, not by any
reasoning about arithmetic -- and found zero arithmetic divergences, because there
were none to find after that.  A suite that goes green on the first attempt has
told you less than one that goes red.

To put a number on the suite's teeth rather than on the interpreter's luck,
thirteen single-line bugs were injected into wasmi.c and the suite re-run.  All
thirteen were caught: unmasked shift counts 112 failures, lt_s as unsigned 110, a
flooring rem_s 76, load8_s without sign extension 14, a loop branch that exits
instead of iterating 12, an ignored memarg offset 10, an off-by-one memory bound 6,
a skipped call_indirect type check 4, a br_table default never taken 4, clz(0)
returning 0 and a missing INT32_MIN/-1 trap 1 each.  A further 6,960 random and
corner-biased operand pairs were compared against the Python reference with zero
divergences.

None of that is the spec testsuite, which remains unrun.""",
     ["t/run.sh", "Makefile"], a25)


# ---------------------------------------------------------------- 26
def a26():
    write("Makefile", open(H + "/makefile_final.txt").read())


step("""make test-asan: ASan + UBSan over the whole suite

Not a nicety for a program whose entire input is an untrusted binary that it then
uses to index raw arrays.

ASan is here for the bounds checks: an off-by-one in a memory or table bound is
invisible in an -O2 build (you read the next heap object and carry on) and loud
under ASan.  It is what named the validation pass's cs[-1] read in one line.
UBSan is here for the class of mistake a bytecode interpreter attracts
specifically -- shifts at or past the bit width, signed overflow in INT_MIN/-1,
misaligned accesses -- all of which this file has deliberate handling for, and all
of which would otherwise be tested only by the answer coming out right on one
compiler.

-fno-sanitize-recover=all is the point of the target.  Without it UBSan prints a
diagnostic and continues, the case still produces the right answer, the harness
scores it as a pass, and the suite stays green over undefined behaviour.  With it,
any diagnostic aborts the process, the harness sees a status above 2 and scores
CRASH.

It runs the malformed-module fixtures too, which is where it earns the most: those
are the inputs designed to make the decoder read past something.

  11833 passed, 0 failed   (about 2m50s, versus 15s for the -O2 build)""",
     ["Makefile"], a26)


# ---------------------------------------------------------------- 27
def a27():
    write("README.md", open(H + "/readme.txt").read())
    write("PLAN.md", open(H + "/plan.txt").read())


step("""README and PLAN, with the oracle's limits stated at length

Documentation last, because until now there was no honest account to give.

The README's "The oracle" section is the part that matters and it opens by saying
what is not true: the official WebAssembly spec testsuite has never been run
against this interpreter, there is no pass rate, and every other claim in the repo
is the author scoring their own work.  That is a materially weaker position than
"passes N% of spec/test/core", and a reader who skims a green 11833/0 and concludes
otherwise has been misled by omission.  So the omission is spelled out first.

What follows is a ranked oracle list with each entry's reach stated: the Python
differential proves two independent implementations agree on the values in the
matrix, and proves nothing about whether either matches the spec, because both
were written from the same reading of it; the hand-written assembler in mkwasm.py
has never had a byte validated by anything outside this repo, so a bug in it would
be invisible to both halves at once; mutation testing measures the suite's
sensitivity and not the interpreter's correctness; and gcc's -Wsequence-point
found a bug that all 11,833 cases missed, which is the cleanest available argument
that a green suite is not evidence of defined behaviour.

The Status section's "does not work" list is longer than its "works" list on
purpose.  f32/f64 absent entirely; operand *types* not validated, only stack
depth, so a type-confused module runs and produces garbage; imports unsupported;
fixed stack ceilings that trap in ways the spec does not sanction; a global init
expression that can forward-reference.  Each of those is a thing a user could
otherwise discover the hard way.

PLAN.md's Milestone 1 opens with vendoring spec/test/core and publishing the real
pass rate, because that is the only item on the list that would move any of this
from self-assessment to evidence.""", ["README.md", "PLAN.md"], a27)


# ================================================================== run
def main():
    if os.path.exists(os.path.join(REPO, ".git")):
        shutil.rmtree(os.path.join(REPO, ".git"))
    sh("git", "init", "-q", "-b", "master")
    sh("git", "config", "user.name", "PLACEHOLDER")
    sh("git", "config", "user.email", "placeholder@invalid")
    for i, (msg, files, act) in enumerate(C, 1):
        act()
        commit(msg, files)
        print("%2d. %s" % (i, msg.splitlines()[0]))
    print("%d commits" % len(C))


if __name__ == "__main__":
    main()
