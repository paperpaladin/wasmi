#!/usr/bin/env python3
"""Generate progressive versions of wasmi.c for a bisectable history.
Level N = the final file minus every feature whose level is > N."""
import sys

FINAL = open("/tmp/hist/wasmi_final.c").read()
CASE_END = "\t\t\tbreak;\n\t\t}\n"

# ("s", sub)            remove substring
# ("x", old, new)       replace substring
# ("r", start, end)     remove from start through end (end found after start)
CUTS = {
 2: [
   ("r", "\t\tcase 1: { /* type */", CASE_END),
   ("r", "\t\tcase 7: { /* export */", CASE_END),
   ("r", "static int find_export(const char *name) {", "\treturn -1;\n}\n"),
   ("r", "\tfor (uint32_t i = 0; i < M.nexports; i++) {\n\t\tExport *e",
         'die("export: function index out of range");\n\t}\n'),
   ("r", "\tint fi = find_export(argv[2]);", "argv[2], ft->np, argc - 3);\n"),
 ],
 3: [
   ("s", "\tuint32_t *fdecl = NULL, nfdecl = 0;\n\tconst uint8_t *codesec = NULL; size_t codelen = 0;\n"),
   ("r", "\t\tcase 3: { /* function */", CASE_END),
   ("r", "\t\tcase 10: /* code:", "codesec = s.p; codelen = s.n;\n\t\t\tbreak;\n"),
   ("r", "\t/* ------- code section ------- */", '\t\tif (s.i != s.n) die("code section has trailing bytes");\n\t}\n'),
   ("s", "\tfree(fdecl);\n"),
 ],
 4: [
   ("r", "/* ====================================================================\n * Validation", "\tf->maxstack = maxh;\n\tf->maxctrl = maxc;\n}\n"),
   ("r", "/* Second structural pass: fill endpc/elsepc.", "\t\t}\n\t}\n}\n"),
   ("s", "\tfor (uint32_t i = 0; i < M.nfuncs; i++) { prepare(&M.funcs[i]); resolve_jumps(&M.funcs[i]); }\n\n"),
   ("r", "/* ------------------------------------------------- blocktype -> label arity */", "\treturn 1;\n}\n"),
 ],
 5: [
   ("s", "\tif (t == VT_F32 || t == VT_F64)\n\t\tdie(\"f32/f64 are not implemented by this interpreter (module rejected)\");\n"),
   ("r", "/* Opcodes we deliberately refuse: everything float.", "\t       op == 0x2a || op == 0x2b || op == 0x38 || op == 0x39;\n}\n"),
   ("s", "\t\tif (is_float_op(op))\n\t\t\tdie(\"opcode 0x%02x is a floating-point instruction; not implemented\", op);\n\n"),
   ("r", "\t\tcase 2: { /* import */", CASE_END),
   ("s", "\t\tif (op == OP_F32_CONST || op == OP_F64_CONST)\n\t\t\tdie(\"float constant in init expression; f32/f64 not implemented\");\n"),
   ("x", '\t\tdefault: die("unknown section id %u", id);\n',
         "\t\tdefault: break;   /* sections we cannot handle yet are skipped */\n"),
   ("s", '\t\tif (id != 0 && id != 10 && s.i != s.n) die("section %u has %zu trailing bytes", id, s.n - s.i);\n'),
 ],
 6: [
   ("r", "static jmp_buf g_trap;", "static void trap(const char *code) { g_trapcode = code; longjmp(g_trap, 1); }\n\n"),
   ("r", "static uint64_t *vstack;", "static uint32_t calldepth;\n\n"),
   ("r", "/* Immediate decoders for the hot loop.", "\treturn (int64_t)v;\n}\n"),
   ("r", "static void exec(uint32_t fidx, uint32_t base);", "\tvsp = gbase + gt->nr;\n}\n"),
   ("r", "static void exec(uint32_t fidx, uint32_t base) {\n\tFunc *f", "#undef BRANCH\n}\n"),
   ("r", "static uint64_t parse_arg(const char *s, uint8_t t) {", "\treturn t == VT_I32 ? (uint64_t)(uint32_t)(int32_t)sv : (uint64_t)sv;\n}\n"),
   ("r", "\tvstack = xcalloc(VSTACK_MAX, sizeof(uint64_t));", "\t}\n\treturn 0;\n}\n"),
 ],
 7: [
   ("r", "\t\t\tcase 0x6a: { uint32_t b2 = P32(), a = P32(); PUSH((uint32_t)(a + b2)); break; }",
         '\t\t\tcase 0x70: { uint32_t b2 = P32(), a = P32(); if (!b2) trap("div0"); PUSH(a % b2); break; }\n'),
 ],
 8: [
   ("r", "\t\t\tcase 0x71: { uint32_t b2 = P32(), a = P32(); PUSH(a & b2); break; }",
         "\t\t\tcase 0x78: { uint32_t b2 = P32(), a = P32(); PUSH(rotr32(a, b2)); break; }\n"),
   ("r", "/* Shift counts are masked, not saturated:", "static uint32_t rotr32(uint32_t a, uint32_t k) { k &= 31; return k ? (a >> k) | (a << (32 - k)) : a; }\n"),
 ],
 9: [
   ("r", "\t\t\tcase 0x67: { uint32_t a = P32(); PUSH(clz32(a)); break; }",
         "\t\t\tcase 0x69: { uint32_t a = P32(); PUSH((uint32_t)__builtin_popcount(a)); break; }\n"),
   ("s", "static uint32_t clz32(uint32_t v) { return v ? (uint32_t)__builtin_clz(v) : 32; }\nstatic uint32_t ctz32(uint32_t v) { return v ? (uint32_t)__builtin_ctz(v) : 32; }\n"),
 ],
 10: [
   ("r", "\t\t\t/* ------------------------------------------------ i32 compare */",
          "\t\t\tcase 0x4f: { uint32_t b2 = P32(), a = P32(); PUSH((uint32_t)(a >= b2)); break; }\n"),
   ("r", "\t\t\t/* ------------------------------------------------ i64 compare */",
          "\t\t\tcase 0x5a: { uint64_t b2 = P64(), a = P64(); PUSH((uint32_t)(a >= b2)); break; }\n"),
 ],
 11: [
   ("r", "\t\t\t/* ------------------------------------------------ i64 arith */",
          "\t\t\tcase 0x8a: { uint64_t b2 = P64(), a = P64(); PUSH(rotr64(a, b2)); break; }\n"),
   ("r", "\t\t\t/* ------------------------------------------------ conversions */",
          "\t\t\tcase OP_I64_EXT_U: { uint32_t a = P32(); PUSH((uint64_t)a); break; }\n"),
   ("s", "static uint64_t rotl64(uint64_t a, uint64_t k) { k &= 63; return k ? (a << k) | (a >> (64 - k)) : a; }\nstatic uint64_t rotr64(uint64_t a, uint64_t k) { k &= 63; return k ? (a >> k) | (a << (64 - k)) : a; }\n"),
   ("s", "static uint32_t clz64(uint64_t v) { return v ? (uint32_t)__builtin_clzll(v) : 64; }\nstatic uint32_t ctz64(uint64_t v) { return v ? (uint32_t)__builtin_ctzll(v) : 64; }\n"),
 ],
 12: [
   ("r", "\t\tcase OP_BLOCK: case OP_LOOP: {\n\t\t\tuint32_t spc",
         "\t\t\tpc = cstack[csp-1].pc_end;\n\t\t\tbreak;\n"),
   ("x", "\t\tcase OP_END:\n\t\t\tif (csp == cbase + 1) RET();\n\t\t\tcsp--;\n\t\t\tbreak;\n",
         "\t\tcase OP_END:\n\t\t\t/* No blocks yet, so the only `end` is the function's own. */\n\t\t\tRET();\n"),
   ("r", "\t\t/* Taking a branch to label depth d:", "\t\t\tBRANCH(d);\n\t\t\tbreak;\n\t\t}\n"),
   ("s", "#undef BRANCH\n"),
 ],
 13: [
   ("r", "\t\tcase OP_BR_TABLE: {\n\t\t\tuint32_t n = uleb(c, &pc);", "\t\t\tBRANCH(d);\n\t\t\tbreak;\n\t\t}\n"),
   ("s", "\t\tcase OP_UNREACHABLE: trap(\"unreachable\");\n"),
 ],
 14: [
   ("s", "\t\tcase OP_CALL: do_call(uleb(c, &pc)); break;\n"),
   ("x", "\tif (++calldepth > CALLDEPTH_MAX) trap(\"call-depth\");\n", "\tcalldepth++;\n"),
 ],
 15: [
   ("r", "\t\tcase 6: { /* global */", CASE_END),
   ("r", "\t\tcase 8: /* start */", "M.start = (int32_t)rd_u32v(&s);\n\t\t\tbreak;\n"),
   ("r", "static uint64_t const_expr(Rd *r, uint8_t want) {", "\treturn v;\n}\n"),
   ("s", "\t\tcase OP_GLOBAL_GET: { uint32_t i = uleb(c, &pc); PUSH(M.globals[i].val); break; }\n\t\tcase OP_GLOBAL_SET: { uint32_t i = uleb(c, &pc); M.globals[i].val = POP(); break; }\n"),
   ("r", "\tif (M.start >= 0) {\n\t\tif ((uint32_t)M.start >= M.nfuncs)", 'die("start function must have type [] -> []");\n\t}\n'),
   ("s", "\tif (M.start >= 0) { vsp = 0; calldepth = 0; csp = 0; do_call((uint32_t)M.start); }\n\n"),
   ("s", "\tM.start = -1;\n"),
 ],
 16: [
   ("r", "\t\tcase 5: { /* memory */", CASE_END),
   ("r", "\t\tcase 11: { /* data */", CASE_END),
   ("r", "static uint64_t mem_size_bytes(void)", "\treturn M.mem + addr;\n}\n"),
   ("r", "\t\t\tif (op >= OP_LOAD_FIRST && op <= OP_STORE_LAST) {\n\t\t\t\t/* Memory access.",
         "\t\t\t\tbreak;\n\t\t\t}\n"),
   ("r", "\t\t\tcase OP_MEM_SIZE: pc++;", "\t\t\t\tPUSH(old);\n\t\t\t\tbreak;\n\t\t\t}\n"),
 ],
 17: [
   ("r", "\t\tcase 4: { /* table */", CASE_END),
   ("r", "\t\tcase 9: { /* element */", CASE_END),
   ("r", "static int ftype_eq(const FType *a, const FType *b) {", "\treturn memcmp(a->pt, b->pt, a->np) == 0;\n}\n"),
   ("r", "\t\tcase OP_CALL_INDIRECT: {\n\t\t\tuint32_t ti = uleb(c, &pc);", "\t\t\tdo_call(target);\n\t\t\tbreak;\n\t\t}\n"),
 ],
}

REPL = {
 2: [("\tload_module(buf, (size_t)sz);\n",
      "\tload_module(buf, (size_t)sz);\n"
      "\t/* Section framing is all we understand so far; nothing is runnable. */\n"
      "\tdie(\"module parsed, but exports are not decoded yet (wanted '%s')\", argv[2]);\n"
      "\treturn 2;\n}\n")],
 6: [("\t\tdie(\"'%s' takes %u argument(s), %d given\", argv[2], ft->np, argc - 3);\n",
      "\t\tdie(\"'%s' takes %u argument(s), %d given\", argv[2], ft->np, argc - 3);\n\n"
      "\tdie(\"execution is not implemented yet ('%s' validates cleanly)\", argv[2]);\n"
      "\treturn 2;\n}\n")],
}

# The two real bugs, put back where they actually lived so each fix is its own
# revertable commit.  level -> list of (fixed_text, buggy_text).
BUGS = {
 "oob": (4, [("\t\t\t/* csp == 0 means that was the function's own terminating `end`;\n"
              "\t\t\t * cs[csp-1] below would be out of bounds, so leave now. */\n"
              "\t\t\tif (csp == 0 && r.i != r.n) die(\"instructions after function end\");\n"
              "\t\t\tcontinue;",
              "\t\t\tif (csp == 0) {\n"
              "\t\t\t\tif (r.i != r.n) die(\"instructions after function end\");\n"
              "\t\t\t\tbreak;\n"
              "\t\t\t}\n"
              "\t\t\tcontinue;")]),
 "seqpoint": (9, [
   ("\t\t\tcase 0x67: { uint32_t a = P32(); PUSH(clz32(a)); break; }\n"
    "\t\t\tcase 0x68: { uint32_t a = P32(); PUSH(ctz32(a)); break; }\n"
    "\t\t\tcase 0x69: { uint32_t a = P32(); PUSH((uint32_t)__builtin_popcount(a)); break; }",
    "\t\t\tcase 0x67: PUSH(clz32(P32())); break;\n"
    "\t\t\tcase 0x68: PUSH(ctz32(P32())); break;\n"
    "\t\t\tcase 0x69: PUSH((uint32_t)__builtin_popcount(P32())); break;"),
 ]),
}

# Every anchor must be unambiguous in the final file, or a cut could eat the
# wrong region (prepare() and exec() share many `case` labels).
for lv, specs in CUTS.items():
    for sp in specs:
        tgt = sp[1]
        n = FINAL.count(tgt)
        if n != 1:
            raise SystemExit("level %d: anchor %r occurs %d times" % (lv, tgt[:60], n))


def apply_cut(txt, spec):
    if spec[0] == "s":
        return txt.replace(spec[1], "", 1)
    if spec[0] == "x":
        return txt.replace(spec[1], spec[2], 1)
    _, a, b = spec
    i = txt.find(a)
    if i < 0:
        return txt
    j = txt.find(b, i)
    if j < 0:
        raise SystemExit("end anchor %r missing for %r" % (b[:40], a[:40]))
    return txt[:i] + txt[j + len(b):]


def version(level, bugs=()):
    txt = FINAL
    # Level 6 (the whole interpreter) is removed first: it physically contains
    # most of the higher-level regions, and those cuts then become no-ops.
    order = [6] + [l for l in sorted(CUTS, reverse=True) if l != 6]
    for lv in order:
        if lv > level:
            for spec in CUTS[lv]:
                txt = apply_cut(txt, spec)
    for lv, pairs in REPL.items():
        if lv > level:
            for old, new in pairs:
                txt = txt.replace(old, new, 1)
    for b in bugs:
        bl, pairs = BUGS[b]
        if bl <= level:
            for fixed, buggy in pairs:
                txt = txt.replace(fixed, buggy, 1)
    return txt


if __name__ == "__main__":
    lvl = int(sys.argv[1])
    bugs = [b for b in (sys.argv[2].split(",") if len(sys.argv) > 2 else []) if b]
    sys.stdout.write(version(lvl, bugs))
