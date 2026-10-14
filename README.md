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
mkwasm: 24 fixtures, 11833 cases
t/fix: 11833 cases in 24 modules, matching the pinned count
11833 passed, 0 failed
```

And the sanitizer build over the same suite (ASan + UBSan,
`-fno-sanitize-recover=all`, so any diagnostic aborts the run):

```
$ make test-asan
cc -std=c11 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all -Wall -Wextra -Wpedantic -o wasmi-asan wasmi.c
python3 t/mkwasm.py
mkwasm: 24 fixtures, 11833 cases
t/fix: 11833 cases in 24 modules, matching the pinned count
11833 passed, 0 failed
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

The official WebAssembly spec testsuite is not run. `wasmi` has never been
scored against `spec/test/core`. There is no pass rate to quote, because none
was measured. Everything below is the author testing their own work, a
much weaker claim than being scored by someone else's suite, and the gap matters:
an interpreter that passes its author's fixtures has been tested against the
author's understanding of the spec, including wherever that understanding is
wrong in both halves at once.

What *is* done:

Expectations are computed independently in Python. `t/mkwasm.py` does two
unrelated jobs. It assembles `.wasm` binaries from a flat instruction listing
(there is no `wat2wasm` here, so fixtures have to be built by hand), and it
contains a second implementation of the arithmetic in plain Python, explicit
32/64-bit masking, explicit truncating division, explicit shift-count modulo. For
every one of the 11,833 cases, the expected answer comes out of the Python, and
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
- that the assembler in `t/mkwasm.py` emits valid binaries. If its LEB128 or its
  section framing is wrong, both halves of the differential are being fed the
  same wrong input and will agree happily. Nothing outside this repository has
  ever validated a byte that `mkwasm.py` produced.
- anything about operand values outside the matrix.
- anything about type validation, which `wasmi` does not do (see Status).
- anything about the modules real toolchains emit. Every fixture was written by
  the same person who wrote the interpreter, so the fixtures test the features
  that person remembered to implement.

Supporting evidence, in descending order of worth:

- *Mutation testing.* Thirteen single-line bugs were injected into `wasmi.c` and
  the suite re-run. All thirteen were caught. Unmasked shift counts produced 112
  failures, `lt_s` implemented as unsigned 110, a flooring `rem_s` 76, a
  `load8_s` that does not sign-extend 14, an ignored static memarg offset 10, a
  loop branch that exits instead of iterating 12, a memory bound off by one 6, a
  skipped `call_indirect` type check 4, a `br_table` default never taken 4,
  `clz(0)` returning 0 and a missing `INT32_MIN / -1` trap 1 each. This measures
  how sensitive the suite is. It says nothing about the cases nobody wrote.
- *ASan + UBSan over the entire suite, clean.* Including the deliberately
  malformed modules.
- *The compiler.* `-Wsequence-point` found a bug the 11,833 cases did not:
  `PUSH(clz32(P32()))` modifies the stack pointer twice with no sequence point
  between. This gcc happened to pick an evaluation order that worked, so the
  tests were green while the code was undefined. Worth stating plainly: a green
  suite is not evidence of defined behaviour.
- *A randomized differential sweep.* 6,960 additional random and corner-biased
  operand pairs against the Python reference, zero divergences. Run by hand and
  not committed, so take it as a note, not as a result you can reproduce.

The one real bug the fixtures caught, for the record, was not an arithmetic
disagreement: it was a one-element out-of-bounds read in the validation pass
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
- Traps, all of which unwind cleanly via `setjmp`/`longjmp` and exit 1 rather
  than taking the host down: `div0`, `int-overflow`, `oob`, `unreachable`,
  `undefined-element`, `uninitialized-element`,
  `indirect-call-type-mismatch`, `call-depth`, `stack-overflow`.

Does not work: the honest list:

- f32 and f64 are not implemented at all. This was a deliberate cut. Any
  module with an f32/f64 value type, opcode or constant is rejected at load time
  with a clear error, instead of being run with the floats quietly treated as
  integers. Roughly a third of the MVP opcode space is therefore missing.
- The spec testsuite is not run. No external pass rate exists. This is the
  largest honesty gap in the project and the first item of `PLAN.md`'s Milestone
  1. It is blocked on a download, not on work, and that was checked, not
     assumed: there is no `.wast` file anywhere on this machine, and
  neither `wat2wasm` nor `wasm-tools` is installed, so the corpus cannot be
  produced locally either. Every other open item here is blocked on effort;
  this one needs `WebAssembly/testsuite` fetched and committed under `t/spec/`
  first, so that the corpus is pinned and the suite does not depend on a
  network at test time.
- Operand *types* are not validated, only operand-stack depth. A module that
  is structurally valid but type-confused: `i64.add` applied to two i32 values,
  a block whose arms leave different types, will load and run and produce
  nonsense instead of being rejected. Memory safety does not depend on this
  (indices and addresses are checked independently), but conformance does, and
  most of the spec suite's `assert_invalid` cases would fail.
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
*structured*, so a label stack resolves every target statically. It tracks the
operand-stack height symbolically (not the types) and records the high-water
mark. This has to happen before execution because the interpreter loop pushes
without checking, and `maxstack` is what makes one check per call sufficient
instead of one per push. Without it, a body of nothing but `i32.const` is a heap
overflow. And it bounds-checks every local, global, function, type and label
index, so the hot loop can index raw arrays with no checks at all. Code after
`br`/`return`/`unreachable` is unreachable; the spec validates it
polymorphically, and `prepare` approximates that with a `dead` flag that stops
height tracking while still matching `end`s.

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
