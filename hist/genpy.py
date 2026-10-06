#!/usr/bin/env python3
"""Stage t/mkwasm.py for the git history.  Stage N = the final file minus the
suites that arrive after N."""
import sys

FINAL = open("/tmp/hist/mkwasm_final.py").read()

SPLIT_REF = "# =================================================================== Python ref"
SPLIT_MAIN = "# ========================================================================= main"

STAGE1_TAIL = '''# The expectation half of the oracle -- an independent Python implementation of
# the arithmetic -- and the fixtures themselves come next.  Right now this file
# can only turn text into .wasm; it has no opinion about what the result means.

if __name__ == "__main__":
    print("mkwasm: assembler only, no fixtures generated yet")
'''

# suite blocks, in file order, with the stage they first appear in
SUITE_STAGE = {
    "s_i32": 2, "s_i64": 2, "s_conv": 2,
    "s_control": 3, "s_recursion": 3,
    "s_memory": 4,
    "s_indirect": 5, "s_globals": 5, "s_reject": 5,
}


def cut_suite(txt, fname):
    start = txt.index("@suite\ndef %s():" % fname)
    nxt = txt.find("@suite\ndef ", start + 10)
    end = nxt if nxt >= 0 else txt.index(SPLIT_MAIN)
    return txt[:start] + txt[end:]


def version(stage):
    if stage == 1:
        return FINAL[: FINAL.index(SPLIT_REF)] + STAGE1_TAIL
    txt = FINAL
    for fn, st in SUITE_STAGE.items():
        if st > stage:
            txt = cut_suite(txt, fn)
    if stage < 4:
        # int_suite's helper is only needed once the arithmetic suites exist
        pass
    return txt


if __name__ == "__main__":
    sys.stdout.write(version(int(sys.argv[1])))
