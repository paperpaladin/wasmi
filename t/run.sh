#!/bin/sh
# t/run.sh -- fixture harness.  Reads t/fix/*.cases, one case per line:
#
#     EXPECTED FUNC [args...]
#
# EXPECTED is matched against wasmi's stdout, except for the literal `!error`,
# which means "the module must be rejected at load time": exit status 2 with an
# `error:` line on stderr.  Runtime traps come out on stdout as `trap:<code>`
# with exit status 1, so they compare like any other expected value -- which is
# the point: the harness checks *which* trap fired, not merely that one did.
#
# Every case is a fresh process.  That is slow, but it means no case can
# contaminate another through leftover memory, globals or stack state.
set -u

cd "$(dirname "$0")/.." || exit 1
WASMI=${WASMI:-./wasmi}
[ -x "$WASMI" ] || { echo "run.sh: $WASMI not built" >&2; exit 1; }
[ -d t/fix ] || { echo "run.sh: no fixtures; run t/mkwasm.py first" >&2; exit 1; }

# `[ -d t/fix ]` proves the directory exists, not that anything is in it, and
# the loop below is over `t/fix/*.cases`. An empty t/fix therefore gave
# "0 passed, 0 failed" and exit 0 -- a silent pass, and this repo's headline
# number is the 11,833 cases that would not have run. So count the catalogued
# cases up front, require that the count is the expected order of magnitude,
# and afterwards require that every one of them was actually executed.
#
# The count is pinned exactly rather than floored. A floor of 11,000 let a
# truncated .cases file lose 57 cases and still pass, because the catalogue
# count is read from the same truncated file -- so the floor caught an empty
# corpus and nothing short of that. 11,833 is the number README.md and
# INDEX.md both quote, so pinning it is what makes those claims checked.
# Same convention as cmark's t/sections.expected and xmlc's t/suite.expected:
# when mkwasm.py deliberately grows the corpus, this number moves with it, in
# the same commit.
CASES_EXPECTED=11910
cases=$(cat t/fix/*.cases 2>/dev/null | grep -cvE '^[[:space:]]*(#|$)')
if [ "$cases" -ne "$CASES_EXPECTED" ]; then
	printf 'run.sh: t/fix catalogues %d cases, expected %d.\n' \
		"$cases" "$CASES_EXPECTED" >&2
	printf '  if t/mkwasm.py grew the corpus on purpose, update CASES_EXPECTED;\n' >&2
	printf '  otherwise t/fix is stale or truncated -- re-run t/mkwasm.py\n' >&2
	exit 1
fi
printf 't/fix: %d cases in %d modules, matching the pinned count\n' \
	"$cases" "$(ls t/fix/*.cases 2>/dev/null | wc -l)"

# The external half of the oracle.  node's V8 is an outside WebAssembly
# implementation, so it is the only thing here that can say whether a fixture
# is really valid WebAssembly rather than merely what t/mkwasm.py intended.
# It is one oracle among several (the Python differential below is the other),
# so a missing node is a loud SKIP, not a refusal -- `trace`'s convention for
# strace, not `corex`'s for gdb.
if command -v node >/dev/null 2>&1; then
	node t/v8check.js || exit 1
else
	echo "SKIP v8check: node is not installed; the validity of every fixture"
	echo "     below is this repository's own claim, scored by nothing outside it"
fi

err=$(mktemp) || exit 1
trap 'rm -f "$err"' EXIT INT TERM

pass=0
fail=0

for cf in t/fix/*.cases; do
	wasm="${cf%.cases}.wasm"
	base=$(basename "$wasm")
	# `read -r exp func args` leaves every remaining field in $args; it is then
	# left unquoted on purpose so the shell splits it back into arguments.
	while read -r exp func args; do
		case "$exp" in '' | '#'*) continue ;; esac
		out=$($WASMI "$wasm" "$func" $args 2>"$err")
		rc=$?
		if [ "$exp" = '!error' ]; then
			# "no exported function named ..." is exit 2 with an error: line,
			# and it means the module LOADED.  Counting it as a rejection is
			# how the whole reject direction of this suite passed while
			# validating nothing: every reject case named an export that no
			# fixture had. Refuse it here too, so naming a missing export
			# can never again be mistaken for refusing a bad module.
			if [ "$rc" -eq 2 ] && grep -q '^error:' "$err" &&
			   ! grep -q 'no exported function named' "$err"; then
				got='!error'
			else
				got="rc=$rc out=${out:-<none>} err=$(head -1 "$err")"
			fi
		else
			got="$out"
			# A crash (signal) must never be mistaken for a trap.
			[ "$rc" -gt 2 ] && got="CRASH(rc=$rc)"
		fi
		if [ "$got" = "$exp" ]; then
			pass=$((pass + 1))
		else
			fail=$((fail + 1))
			if [ "$fail" -le 25 ]; then
				printf 'FAIL %-12s %-22s %-24s expected %-22s got %s\n' \
					"$base" "$func" "$args" "$exp" "${got:-<empty>}"
			fi
		fi
	done <"$cf"
done

# Every catalogued case must have been run. A .cases file that cannot be read,
# or a `read` that stops early on a malformed line, would otherwise just lower
# the total silently.
ran=$((pass + fail))
if [ "$ran" -ne "$cases" ]; then
	printf 'run.sh: ran %d cases but t/fix catalogues %d\n' "$ran" "$cases" >&2
	printf '%d passed, %d failed\n' "$pass" "$fail"
	exit 1
fi

printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
