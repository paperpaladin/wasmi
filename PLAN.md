# PLAN

## Contract

`wasmi MODULE.wasm FUNC [args...]` loads a self-contained WebAssembly 1.0 (MVP)
binary, runs the exported function `FUNC` with the given integer arguments, and
prints the single result as a signed decimal number.

Three outcomes, each with a distinct exit status, so a harness can tell them
apart without parsing prose:

| outcome | stdout | stderr | exit |
|---|---|---|---|
| success | the result, signed decimal | (nothing) | 0 |
| runtime trap | `trap:<code>` | (nothing) | 1 |
| malformed / unsupported module | (nothing) | `error: <reason>` | 2 |

Trap codes are a closed set: `div0`, `int-overflow`, `oob`, `unreachable`,
`undefined-element`, `uninitialized-element`, `indirect-call-type-mismatch`,
`call-depth`, `stack-overflow`.

In scope: the complete i32 and i64 instruction set, structured control flow,
locals, globals, one table, one linear memory, the start function, and the type,
function, table, memory, global, export, start, element, code and data sections.

Out of scope and *rejected, not approximated*: f32/f64 (any valtype, any
opcode, any constant), imports, and everything post-MVP.

## Hard invariant

1. A malformed or malicious module is rejected or traps; it never corrupts the
   host. Every byte of a `.wasm` file is attacker-controlled. Decoding
   therefore bounds-checks every read, caps every LEB128 length, and rejects
   every out-of-range index *before* execution starts. Execution bounds-checks
   every memory access and every table access, and every stack has a validated
   ceiling. The sanitizer build (`make test-asan`, ASan + UBSan with
   `-fno-sanitize-recover=all`) runs the entire fixture suite, including the
   malformed-module fixtures, and must be clean.

2. Every arithmetic result matches a value computed independently in Python.
   No expected value in the test suite was ever produced by running `wasmi`.
   They are all computed in `t/mkwasm.py` from Python integer arithmetic with
   32/64-bit wrapping and truncating division written out by hand. If the C and
   the Python disagree, the test fails and one of them is wrong.

Neither invariant is a claim of spec conformance. See `## Oracle`.

## Oracle

Ranked by how much an outside party should believe it. Entry 1, the one that
would make every claim here external, is still **not reached**. Entry 0 was
added when it turned out that an outside WebAssembly implementation had been
installed on this machine the whole time.

0. V8, through node. REACHED, for validity only. V8 is a production
   WebAssembly implementation written by people who have never seen this code,
   and `WebAssembly.validate` is its spec validator. `t/v8check.js` asks it
   about all 68 fixtures and requires its accept/reject verdict to match the
   fixture's declaration; `t/run.sh` requires wasmi to match the same
   declaration, so the two together compare wasmi against V8. What it proves:
   every module this suite calls well formed really is valid WebAssembly, and
   every module it calls malformed really is not. What it does not prove:
   anything about a module this repository did not write, anything about
   *which* error is reported, and anything about computed values, which V8 is
   not asked for.

1. The official WebAssembly spec testsuite: NOT YET REACHED. `wasmi` has
   never been scored by `spec/test/core`. That suite is the only authority
   independent of this repository's author AND of the modules written here,
   and until a real pass rate is published, nothing above covers a module
   somebody else chose. This is the biggest remaining gap in the project and
   the first item of Milestone 1. Running it still needs a `.wast` driver and
   a `.wat` assembler, neither of which exists here, though V8 can now stand
   in for the latter's validity check.

2. A differential fixture suite against Python (11,910 cases). For every
   case, `t/mkwasm.py` assembles a `.wasm` binary *and* computes the expected
   answer in Python, from Python semantics, with the wrapping and the truncating
   division spelled out explicitly. This is a genuine differential test. Two
   independent implementations of the same arithmetic must agree. What it proves:
   `wasmi` agrees with a second implementation on the operand values chosen.
   What it does not prove: that either implementation matches the spec, that
   the hand-written assembler in `t/mkwasm.py` emits correct binaries (if it is
   wrong, both halves are tested against the same wrong input), or that any case
   outside the matrix works.

3. Mutation testing of the suite itself. Thirteen deliberate single-line
   bugs were injected into `wasmi.c` and the suite was re-run. Every one was
   caught, most by double-digit numbers of cases. Unmasked shift counts (112
   failures), `lt_s` as unsigned (110), flooring instead of truncating `rem_s`
   (76), `load8_s` without sign extension (14), an ignored static memarg offset
   (10), a loop branch that exits instead of iterating (12), a `br_table`
   default never taken (4), an off-by-one memory bound (6), a skipped
   `call_indirect` type check (4), `clz(0)` returning 0 (1), a missing
   `INT32_MIN / -1` trap (1). This measures the suite's sensitivity, not the
   interpreter's correctness.

4. ASan + UBSan over the whole suite. Catches the memory and
   undefined-behaviour errors a bytecode interpreter attracts, shifts at the
   bit width, signed overflow, off-by-one bounds checks, that a plain `-O2`
   build silently gets away with.

5. The compiler. `-Wall -Wextra -Wpedantic -Wshadow -Wconversion`.
   `-Wsequence-point` found a real bug the fixtures missed (see the commit log):
   `PUSH(clz32(P32()))` modifies the stack pointer twice without an intervening
   sequence point. This gcc happened to pick a working evaluation order, so the
   tests passed anyway. A test suite that passes is not evidence of defined
   behaviour.

6. A randomized differential sweep. 6,960 extra random and corner-biased
   operand pairs compared against the Python reference, 0 divergences. Run ad
   hoc, not committed, because it is not reproducible from the repo as it stands.

## Non-goals

- Floating point. Not partially, not approximately. Any module mentioning
  f32 or f64 is refused at load time. Getting NaN propagation, signed zero and
  rounding right is a project of its own, and a wrong answer is worse than a
  clear refusal.
- Imports, WASI, host functions. `wasmi` runs self-contained modules.
- Multi-value blocks. Operand types are validated now, but the block type
  immediate is one byte, so a block declares 0 or 1 results and never
  parameters. That is where the MVP limit sits.
- Speed. A switch-dispatch loop over the original bytes. No threading of the
  interpreter, no superinstructions, no JIT.
- Post-MVP proposals. No multi-value, no reference types, no bulk memory, no
  SIMD, no tail calls, no exceptions, no threads, no 64-bit memories.
- A `.wat` parser. `t/mkwasm.py` assembles a flat instruction listing, which
  is enough for fixtures and nothing more.

## Commit sequence

- [x] Skeleton: file loading, magic and version, section framing, LEB128.
      why: every later bug report starts with "which byte", so the reader and
      the bounds checks come before anything that could trust them.
- [x] Type and export sections; resolve a function by exported name.
      why: the smallest pair that makes the CLI's second argument mean something.
- [x] Function and code sections, including run-length-encoded locals.
      why: bodies have to exist before there is anything to validate or run.
- [x] Ahead-of-time validation: block structure, branch targets, operand-stack
      depth, every index bound.
      why: the interpreter loop pushes without checking, so the ceiling must be
      known before execution; this is also where malformed modules die.
- [x] Fix: `prepare()` read `cs[csp-1]` after the function's own `end`.
      why: one-element out-of-bounds read that segfaulted on every module; kept
      as its own commit so the diff that fixed it is one line.
- [x] Explicit refusal of imports and of every floating-point construct.
      why: an honest error beats a plausible wrong answer.
- [x] Interpreter core: frames, operand stack, locals, constants, drop, select.
      why: the smallest thing that can return a value.
- [x] i32 arithmetic, with the divide-by-zero and `INT_MIN / -1` traps.
- [x] i32 bitwise and shifts, with shift counts masked to the bit width.
      why: masking is both a spec rule and the thing that keeps the C defined.
- [x] clz, ctz, popcnt.
- [x] Fix: sequence-point UB in the unary opcodes.
      why: `PUSH(f(P32()))` touches the stack pointer twice; gcc caught what the
      tests could not.
- [x] Signed and unsigned comparisons for i32 and i64.
- [x] i64 arithmetic and the width conversions.
- [x] Structured control flow: block, loop, if/else, br, br_if.
- [x] br_table and unreachable.
- [x] call, with a recursion-depth trap instead of a smashed C stack.
- [x] Global and start sections.
- [x] Memory and data sections, every load/store width, the out-of-bounds trap.
- [x] Table and element sections, call_indirect and its three traps.
- [x] `t/mkwasm.py`: the assembler half of the oracle.
      why: there is no wat2wasm here, so fixtures have to be built by hand.
- [x] `t/mkwasm.py`: the independent Python reference and the arithmetic suites.
      why: expectations computed in Python are what make this differential.
- [x] Control-flow, recursion, memory, indirect-call, globals and
      malformed-module fixtures.
- [x] `t/run.sh` and `make test`.
- [x] `make test-asan`.
- [x] README and PLAN.

### Milestone 1: make the oracle external

- [ ] Vendor `spec/test/core/*.wast`, write a `.wast` driver, and publish the
      real pass/fail count per file in the README.
      why: this is the only entry on the oracle list that an outsider has any
      reason to trust; everything this repo currently claims is self-scored.
      BLOCKED, and checked, not assumed: there is no `.wast` file
      anywhere on this machine, and neither `wat2wasm` nor `wasm-tools` is
      installed, so the corpus cannot be produced locally either. It has to be
      fetched: `testsuite/` from the WebAssembly/spec repository, and this
      environment has no network. Nothing in the item can start until the files
      are here.
      This is a different kind of blocker from the rest of the queue and worth
      saying so: every other item is blocked on work, and this one is blocked
      on a download. If you are reading this with a network, the sequence is
      `git clone https://github.com/WebAssembly/testsuite`, commit the `.wast`
      files under `t/spec/` so the corpus is pinned and the suite stays
      reproducible, and only then write the driver. Vendoring,
      not cloning at test time matters for the same reason `width_table.h` is
      committed in vtparse: a suite whose corpus arrives over the network is
      not a suite.
- [x] Full operand *type* validation, not just depth.
      why: the spec suite's `assert_invalid` cases are mostly type errors, so
      the pass rate above cannot move much without this.
      done: `prepare()` now runs the spec's validation algorithm over a stack
      of valtypes, with VT_ANY as the polymorphic bottom in unreachable code.
      And the oracle for it IS external, which this file assumed was
      impossible: node is installed, so V8 is on this machine. `t/v8check.js`
      puts every fixture through `WebAssembly.validate` plus compile and
      instantiate and requires V8's verdict to match what the fixture
      declares, while `t/run.sh` requires wasmi to match the same
      declaration. 68 modules: 10 V8 calls valid and wasmi runs, 56 V8 calls
      invalid and wasmi refuses, 2 V8 calls valid and wasmi refuses on purpose
      (the f32/f64 modules, which is the unimpl() shape from METHOD.md: the
      outside authority has to ACCEPT the module before our refusal means
      anything). Both node v22.23.2 and the system node v20.19.2 agree.
      Two harness bugs found, the first serious. Every reject fixture named
      the export `_`, which no module has, so a module that LOADED still
      exited 2 with "no exported function named" and scored as a rejection:
      the whole reject direction of this suite, 15 fixtures, had been passing
      without validating anything since it was written. Fixed in both places:
      the fixtures name a real export, and `t/run.sh` refuses to read a
      missing export as a refusal. The second: `t/mkwasm.py` only ever wrote
      into `t/fix` and never cleaned it, so a renamed fixture stayed behind
      and was counted by both harnesses.
      18 mutations of the new validator, all caught. Four needed sharper
      fixtures first, and all four were vacuous in METHOD.md's sense: the
      underflow check or the return-type check rejected the module before the
      rule under test could see it. The sixth and seventh buffer-larger-than-its-data
      cases in this collection were also here: the locals type array, and
      then, found by scanning for the shape rather than for the function just
      fixed, linear memory itself, allocated as pages*65536+1.
- [ ] Reject the malformed-module corpus the spec suite ships
      (`assert_malformed`) with the right distinction between a decode error and
      a validation error.
      why: "rejected somehow" is not the same claim as "rejected correctly".
- [ ] Implement f32/f64, or document the spec-suite pass rate with the float
      files excluded and say exactly how many cases that skips.
      why: right now "we reject floats" quietly removes a large fraction of the
      suite from the denominator, which flatters the number.
- [ ] Imports and a minimal host-function ABI.
      why: `assert_return` on linked modules is a large part of the suite, and
      no real-world module is import-free.
- [ ] A coverage-guided fuzzer over the decoder (`afl++` or libFuzzer harness)
      with the sanitizer build, and a committed crash corpus.
      why: the malformed-module fixtures are the ones the author thought of,
      which is exactly the wrong sampling distribution for a parser.
- [ ] Commit the randomized differential sweep as a seeded, reproducible target
      (`make fuzz-arith`).
      why: a one-off run that is not in the repo proves nothing to a reader.
- [ ] Multi-value blocks and the post-MVP block type encoding.
      why: blocktype is read as a single byte today, a latent
      incompatibility, not a deliberate limit.
- [ ] Replace the fixed `VSTACK_MAX` / `CSTACK_MAX` / `CALLDEPTH_MAX` ceilings
      with growth on demand, keeping the trap as the last resort.
      why: the ceilings are guesses; a real module that trips one gets a trap
      that the spec does not sanction.
- [ ] Measure and publish an interpreter throughput number against a named
      benchmark.
      why: "no performance work was done" is currently an assertion with no
      number behind it.
