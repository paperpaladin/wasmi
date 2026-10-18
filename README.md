# wasmi

A WebAssembly 1.0 (MVP) interpreter for the integer subset, in one file of C11,
with no dependencies.

```
$ make
cc -std=c11 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion -o wasmi wasmi.c

$ ./wasmi t/fix/recursion.wasm fib 25
75025
$ ./wasmi t/fix/i32ops.wasm i32.shr_s -16 2
-4
$ ./wasmi t/fix/i32ops.wasm i32.div_s -2147483648 -1
trap:int-overflow
$ echo $?
1
```

## make test

A real transcript, not a reconstruction:

```
$ make clean && make test
rm -f wasmi wasmi-asan
rm -rf t/fix
cc -std=c11 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion -o wasmi wasmi.c
python3 t/mkwasm.py
mkwasm: 67 fixtures, 11904 cases
t/fix: 11904 cases in 67 modules, matching the pinned count
v8check: 67 modules through V8 v22.23.2
  accept direction: 9 modules V8 calls valid and wasmi runs
  reject direction: 56 modules V8 calls invalid and wasmi refuses
  out of scope:     2 modules V8 calls valid and wasmi refuses on purpose
11904 passed, 0 failed
```

And the sanitizer build over the same suite (ASan + UBSan,
`-fno-sanitize-recover=all`, so any diagnostic aborts the run):

```
$ make test-asan
cc -std=c11 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all -Wall -Wextra -Wpedantic -o wasmi-asan wasmi.c
python3 t/mkwasm.py
mkwasm: 67 fixtures, 11904 cases
t/fix: 11904 cases in 67 modules, matching the pinned count
v8check: 67 modules through V8 v22.23.2
  accept direction: 9 modules V8 calls valid and wasmi runs
  reject direction: 56 modules V8 calls invalid and wasmi refuses
  out of scope:     2 modules V8 calls valid and wasmi refuses on purpose
11904 passed, 0 failed
```

## Why this exists

Reading the WebAssembly spec is not the same as having implemented it. The
binary format is small enough to decode in an afternoon and subtle enough that
the afternoon's work will be wrong in ways you cannot see without an oracle:
shift counts are taken modulo the bit width, `rem_s` truncates toward zero,
not flooring, `div_s` has one input pair that traps, the `br_table`
default is a branch and not an error, `clz(0)` is 32 and not 0, `memory.grow`
returns the *old* size, and a `br` out of a `loop` means "iterate" while a `br`
out of a `block` means "leave". Each of those is a place where code that looks
right is not.

So the point of the project is not the interpreter. It is building something
that can tell you the interpreter is wrong, when there is no reference
implementation on the machine to compare against.

## The oracle

Read this section before believing anything else in this file.

The official WebAssembly spec testsuite is still not run. `wasmi` has never
been scored against `spec/test/core`, so there is no pass rate over modules
somebody else chose.

What there now is, for one half of the problem, is a genuine outside
authority. **node is installed, so V8 is on this machine**, and V8 is a
production WebAssembly implementation written by people who have never seen
this code. `WebAssembly.validate` is its spec validator. `t/v8check.js` puts
every fixture through it (and then through compile and instantiate, because
an out-of-bounds data segment is a valid module that cannot be instantiated,
and `wasmi` collapses both into one error) and requires V8's verdict to match
what the fixture declares. `t/run.sh` requires `wasmi` to match the same
declaration. Between them, `wasmi` is compared against V8 on every module in
the corpus. This runs as part of `make test`; if node is absent it prints a
loud SKIP saying the validity of every fixture is then this repository's own
claim and nothing else's.

The two directions are counted separately and never summed, because they are
different claims and a validator that rejects everything scores perfectly on
one of them:

| direction | count | claim |
|---|---|---|
| accept | 9 modules | V8 calls them valid, and `wasmi` loads and runs them |
| reject | 56 modules | V8 calls them invalid, and `wasmi` refuses them |
| out of scope | 2 modules | V8 calls them **valid**, and `wasmi` refuses them on purpose |

The third row is the f32/f64 pair, and it is a check and not an exemption:
V8 has to *accept* the module before a refusal means "this feature is out of
scope" rather than "this module is broken and so are we". Both node v22.23.2
and the system node v20.19.2 give identical verdicts.

What V8 is not asked: anything about a module this repository did not write,
anything about *which* error is reported (only that there is one), and
anything about computed values. The arithmetic oracle is still Python.

What *is* done beyond that:

Expectations are computed independently in Python. `t/mkwasm.py` does two
unrelated jobs. It assembles `.wasm` binaries from a flat instruction listing
(there is no `wat2wasm` here, so fixtures have to be built by hand), and it
contains a second implementation of the arithmetic in plain Python, explicit
32/64-bit masking, explicit truncating division, explicit shift-count modulo. For
every one of the 11,904 cases, the expected answer comes out of the Python, and
`wasmi` then has to agree with it. No expected value was ever captured from a run
of `wasmi`. That is what makes it a differential test, not a snapshot of
whatever the C happens to do.

What that proves: on the operand values in the matrix, two independent
implementations of i32/i64 arithmetic agree; the full
cross-product of 25 binary operators over 16 chosen i32 values and 14 chosen i64
values agrees; the signed/unsigned distinctions, the shift masking, the
truncating remainder, the `clz`/`ctz` zero cases, the trap conditions and the
sign extension on every load width agree.

What that does not prove:

- that either implementation matches the specification. Both could be wrong the
  same way. The Python reference was written from the same reading of the spec as
  the C.
- that either implementation computes the right *value*. V8 is asked whether
  a module is valid, never what it returns. Pointing an engine at the
  arithmetic suite would be a much stronger oracle than the Python reference
  and is not done.
- anything about operand values outside the matrix.
- anything about the modules real toolchains emit. Every fixture was written by
  the same person who wrote the interpreter, so the fixtures test the features
  that person remembered to implement.

Supporting evidence, in descending order of worth:

- *Mutation testing of the type validator.* Eighteen single-line bugs were
  injected into the new validation pass and the suite re-run. All eighteen were
  caught: deleting the type comparison in `vpopt` (30 failures), always
  treating a load or store as i32 (243 each), always treating a local as i32
  (5,278), the loop label following the block rule (27), dropping the
  polymorphic stack in unreachable code (27), not clearing the stack when
  unreachable code starts (29), the `else` arm not being checked against the
  `if` arm (125), globals always i32 (34), `br` carrying nothing (2), `return`
  not taking the function's result (2), `br_table` targets not having to agree
  with the default (2), `select` arms not having to match (2), an `if` with a
  result type not needing an `else` (1), the block-end leftover check (2),
  untyped call arguments (1), untyped `call_indirect` and `br_table` indices
  (1 each).

  Four of those SURVIVED the first time and were vacuous in exactly the sense
  METHOD.md warns about: the module was rejected by the underflow check or by
  the function's return type before the rule under test could run. The
  `br_table` fixture had two labels of different arity and an empty stack, so
  the underflow check fired first; the `select` fixture put an i64 on the
  stack of a function returning i32, so the return-type check fired first.
  Both were rewritten to supply exactly enough operands that the rule under
  test is the only thing left that can reject them, and the `select` one was
  split into two fixtures with the arms in both orders, because a rule dropped
  from one operand is invisible when the interesting type is on the other.

- *Mutation testing of the arithmetic.* Thirteen single-line bugs were injected
  into `wasmi.c` and the suite re-run. All thirteen were caught. Unmasked shift counts produced 112
  failures, `lt_s` implemented as unsigned 110, a flooring `rem_s` 76, a
  `load8_s` that does not sign-extend 14, an ignored static memarg offset 10, a
  loop branch that exits instead of iterating 12, a memory bound off by one 6, a
  skipped `call_indirect` type check 4, a `br_table` default never taken 4,
  `clz(0)` returning 0 and a missing `INT32_MIN / -1` trap 1 each. This measures
  how sensitive the suite is. It says nothing about the cases nobody wrote.
- *ASan + UBSan over the entire suite, clean.* Including the deliberately
  malformed modules.
- *The compiler.* `-Wsequence-point` found a bug the fixtures did not:
  `PUSH(clz32(P32()))` modifies the stack pointer twice with no sequence point
  between. This gcc happened to pick an evaluation order that worked, so the
  tests were green while the code was undefined. Worth stating plainly: a green
  suite is not evidence of defined behaviour.
- *A randomized differential sweep.* 6,960 additional random and corner-biased
  operand pairs against the Python reference, zero divergences. Run by hand and
  not committed, so take it as a note, not as a result you can reproduce.

### The harness bug

Worth its own heading, because it is the largest defect this project has had
and it was in the test harness, not in the interpreter.

Every fixture that had to be *rejected* declared itself as the case line
`!error _`, where `_` is the function to invoke. No fixture exports anything
called `_`. So `wasmi` loaded the module, validated it, found no export named
`_`, printed `error: no exported function named '_'` and exited 2 -- and the
harness, which asked only for exit 2 and a line starting `error:`, recorded a
successful rejection. **The entire reject direction of this suite, fifteen
fixtures, had been passing without rejecting anything since the day it was
written.** It was found by planting a mutation that disables the type check,
watching the suite stay green, and asking why.

Fixed in both places. The fixtures name a function their module really
exports, so a module that loads produces a value instead of an error; and
`t/run.sh` now refuses to count "no exported function named" as a rejection
at all, so naming a missing export can never be mistaken for refusing a bad
module again.

A second, smaller one in the same area: `t/mkwasm.py` only ever wrote into
`t/fix` and never cleaned it, so a fixture that was renamed stayed behind,
was counted by the pinned case count and by `t/v8check.js`, and would have
been committed. It now clears the directory first.

### The oversized buffer, case six

This collection has found five previous cases where a rounded-up allocation
made ASan blind to an overrun. This is the sixth. The per-function array of
local variable types grew by doubling, so a function with one local had a
64-byte allocation, and the bounds check on the local index in `prepare()`
was guarding something the sanitizer could not see. With the bounds check
deliberately broken and a module reading exactly one local past the end:
untrimmed, ASan said nothing and the module *validated*, because the garbage
byte read as 0, which is the validator's code for the polymorphic unknown
type; trimmed to `nlocals` with one `realloc`, ASan names the line. The array
is trimmed, and `t/fix/ty_local_past.wasm` is a permanent fixture reading one
past the end so that the trim stays load-bearing.

The one real bug the fixtures caught before any of this, for the record, was
not an arithmetic disagreement: it was a one-element out-of-bounds read in the validation pass
(`cs[csp-1]` evaluated after the function's own terminating `end` had popped the
label stack to empty). It segfaulted on every module with a function body, which
is to say the very first time a fixture was ever run. ASan named the line. After
that one line, the arithmetic suites went green on the first attempt and stayed
green, a less exciting story than "the differential caught a subtle
wrapping bug", and is what actually happened.

## Status

Works:

- Sections: type, function, table, memory, global, export, start, element, code,
  data, and custom sections (skipped). Import sections are parsed and rejected.
- The complete i32 and i64 instruction set: add, sub, mul, div_s, div_u, rem_s,
  rem_u, and, or, xor, shl, shr_s, shr_u, rotl, rotr, clz, ctz, popcnt, eqz, and
  all ten comparisons at both widths; `i32.wrap_i64`, `i64.extend_i32_s`,
  `i64.extend_i32_u`.
- Control flow: block, loop, if/else, br, br_if, br_table, return, call,
  call_indirect, unreachable, drop, select, nop.
- Locals, globals (mutable and immutable, with constant init expressions), the
  start function.
- One linear memory: every load and store width with correct sign extension,
  unaligned access, the static memarg offset, `memory.size`, `memory.grow`.
- One table with element segments, and `call_indirect` with a runtime signature
  check.
- Full operand *type* validation, ahead of execution: the spec's validation
  algorithm over a stack of valtypes, including the polymorphic stack in
  unreachable code, untyped `select`, block and loop result types, the rule
  that a branch to a `loop` label takes the loop's parameters and not its
  results, and `br_table`'s requirement that every target agree with the
  default. V8 agrees with the verdict on all 67 fixtures.
- Traps, all of which unwind cleanly via `setjmp`/`longjmp` and exit 1 rather
  than taking the host down: `div0`, `int-overflow`, `oob`, `unreachable`,
  `undefined-element`, `uninitialized-element`,
  `indirect-call-type-mismatch`, `call-depth`, `stack-overflow`.

Does not work: the honest list:

- f32 and f64 are not implemented at all. This was a deliberate cut. Any
  module with an f32/f64 value type, opcode or constant is rejected at load time
  with a clear error, instead of being run with the floats quietly treated as
  integers. Roughly a third of the MVP opcode space is therefore missing.
- The spec testsuite is not run. No pass rate over a corpus somebody else
  wrote exists. This is the largest remaining honesty gap and the first item of
  `PLAN.md`'s Milestone 1. It is blocked on a download, not on work: there is
  no `.wast` file anywhere on this machine and neither `wat2wasm` nor
  `wasm-tools` is installed, so `WebAssembly/testsuite` has to be fetched and
  committed under `t/spec/` before a driver can be written. Note what this
  README used to conclude from that same check, which was wrong: that no
  external WebAssembly implementation was available at all. `node` was
  installed the whole time, and V8 inside it answers the validity question
  for any bytes you hand it. The lesson is to enumerate what *is* on the
  machine rather than to check for the two tools you had in mind.
- Validation reports *that* a module is bad, not reliably *which* kind of bad.
  The spec distinguishes a malformed module (a decode error) from an invalid
  one (a type error); `wasmi` prints `error: ...` and exits 2 for both, so the
  spec suite's `assert_malformed` and `assert_invalid` cannot be told apart
  yet. V8 is only asked for a yes or a no here for the same reason.
- Imports are not supported. A module with any import is rejected, so nothing
  a real toolchain emits for a host environment will load.
- No multi-value results, and the block type immediate is read as a single
  byte, which is MVP-only.
- Fixed ceilings: 2^20 value-stack slots, 2^14 label frames, 2,048 nested
  calls. Exceeding one is a trap, which is safe but is not a trap the spec
  sanctions.
- Globals' init expressions may reference a later global. The bound check
  uses the total count, not the index, so a forward reference reads zero
  instead of being rejected. Marked `shortcut:` in the source.
- `memory.grow` can report failure for a reason the spec does not have: if
  `realloc` fails it returns -1, which is allowed, but the ceiling is the host
  allocator, not the declared maximum.
- No `.wat` parser, no disassembler, no module linking, no debugging output.
- Performance was not measured and not worked on.

## Design

One file, `wasmi.c`, about 1,070 lines. Three phases.

Decode. `load_module` walks the section framing with a bounds-checked reader.
Every LEB128 has a length cap, because an unbounded continuation-bit chain is
both a read off the end of the buffer and an undefined shift. Section payloads
are sub-readers over the original mapped bytes: nothing is copied, including
export names and function bodies.

Validate, ahead of execution. `prepare` makes one forward pass per function
body and does three things at once. It matches `block`/`loop`/`if` against
`else`/`end`, which `resolve_jumps` then turns into a pc-indexed table so that a
branch is O(1) instead of a forward scan, WebAssembly control flow is
*structured*, so a label stack resolves every target statically. It keeps a stack of operand
*types*, pushed and popped exactly as the spec's validation algorithm says, so
`i64.add` on two i32 operands is refused rather than run. Its high-water mark
is `maxstack`, and that has to be known before execution because the
interpreter loop pushes without checking: one reservation per call replaces
one check per push, and without it a body of nothing but `i32.const` is a heap
overflow. And it bounds-checks every local, global, function, type and label
index, so the hot loop can index raw arrays with no checks at all.

Code after `br`/`return`/`unreachable` is unreachable, and the spec validates
it *polymorphically* rather than skipping it: the stack there is treated as
holding arbitrarily many values of unknown type underneath, so the code must
still be well formed but may consume operands that are not present. That is
`VT_ANY` in the source, and it is the whole difference between this pass and
one that simply stops looking after a `br`. It is also where the
counter-intuitive cases live. `unreachable i32.add` is valid in a function
returning i32; `unreachable i64.const 1` in the same function is not, because
the i64 it pushes is real and the function's `end` wants an i32. Entering
unreachable code also *discards* whatever the current block had on the stack,
so `block i64.const 1 br 0 end` is valid. All three are fixtures, and V8
agrees with all three.

Execute. A switch-dispatch loop over the original bytes, one C recursion per
wasm call. Values live in a single flat `uint64_t` stack; an i32 occupies the low
32 bits. Labels live in a separate flat `RCtrl` stack shared by all frames, which
is what makes the structured-control-flow unwind cheap: `br N` pops N+1 label
frames, copies the label's result values down to the label's saved stack height,
and jumps, except for a `loop` label, where branching means "iterate", so the
frame stays and the pc goes back to the body. Traps `longjmp` to `main`.

Shift counts are masked (`b & 31`, `b & 63`) because the spec defines shifts
modulo the width and because shifting by the width or more is undefined in C, so
the mask is simultaneously the conformance rule and the safety rule. `INT_MIN /
-1` traps, and `INT_MIN % -1` is special-cased to 0, because both are undefined
in C and the spec has an answer for each. Memory is accessed through `memcpy`, so
unaligned loads and stores are correct for free and the alignment immediate can
be ignored as the hint it is. Address arithmetic is done in 64 bits so that
`addr + offset` cannot wrap into a plausible-looking pointer.

Deliberate shortcuts are marked `shortcut:` in the source, each naming the
ceiling it accepts and the upgrade path out of it.

## License

GPL-3.0. See `LICENSE`.
