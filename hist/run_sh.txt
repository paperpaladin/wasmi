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
			if [ "$rc" -eq 2 ] && grep -q '^error:' "$err"; then
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

printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
