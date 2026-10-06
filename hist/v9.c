/* wasmi -- a WebAssembly 1.0 (MVP) interpreter for the integer subset, in one file.
 *
 * Scope on purpose: i32/i64 arithmetic, structured control flow, locals, globals,
 * tables (call_indirect) and linear memory.  f32/f64 are NOT implemented; any
 * module that so much as mentions them is rejected at load time rather than
 * silently miscompiled.  See README "Status".
 *
 * Error model, two clearly separated kinds:
 *   - decode/validate failure  -> stderr "error: ...", exit 2.  The module never runs.
 *   - runtime trap             -> stdout "trap:<code>",  exit 1.  Host stays intact.
 * The split matters because the test oracle asserts on *which* trap fired, and
 * because a fuzzer/attacker handing us garbage must land in the first bucket.
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdarg.h>
#include <setjmp.h>
#include <errno.h>
#include <inttypes.h>

/* ------------------------------------------------------------------ limits */
/* Fixed ceilings instead of growable stacks.  Every push is still bounds
 * checked, so exceeding one is a clean trap, never a buffer overrun.
 * shortcut: ceiling is "no module may need more than this"; upgrade path is
 * realloc-on-demand in push_frame(), which only matters once a real-world
 * module trips it. */
#define VSTACK_MAX   (1u << 20)   /* operand+locals slots, 8 MiB */
#define CSTACK_MAX   (1u << 14)   /* label (control) frames, shared by all calls */
#define CALLDEPTH_MAX 2048        /* exec() recurses once per wasm call */
#define PAGE_SIZE    65536u
#define MAX_PAGES    65536u

/* ------------------------------------------------------------- diagnostics */
static jmp_buf g_trap;
static const char *g_trapcode;

static void trap(const char *code) { g_trapcode = code; longjmp(g_trap, 1); }

static void die(const char *fmt, ...) {
	va_list ap; va_start(ap, fmt);
	fputs("error: ", stderr); vfprintf(stderr, fmt, ap); fputc('\n', stderr);
	va_end(ap); exit(2);
}

static void *xmalloc(size_t n) {
	void *p = malloc(n ? n : 1);
	if (!p) die("out of memory");
	return p;
}
static void *xcalloc(size_t n, size_t s) {
	void *p = calloc(n ? n : 1, s ? s : 1);
	if (!p) die("out of memory");
	return p;
}

/* ------------------------------------------------------------------ reader */
typedef struct { const uint8_t *p; size_t n, i; } Rd;

static uint8_t rd_u8(Rd *r) {
	if (r->i >= r->n) die("unexpected end of section");
	return r->p[r->i++];
}
static void rd_bytes(Rd *r, void *dst, size_t k) {
	if (r->i + k > r->n) die("unexpected end of section");
	if (dst) memcpy(dst, r->p + r->i, k);
	r->i += k;
}

/* LEB128: each byte carries 7 payload bits in its low bits; bit 7 is the
 * *continuation* bit, set on every byte except the last.  So we shift in 7 bits
 * at a time and stop on the first byte with the high bit clear.  The length cap
 * is not cosmetic: without it a hostile module can encode 0 in a thousand
 * 0x80 bytes and walk us off the end, or overflow the shift (which is UB). */
static uint32_t rd_u32v(Rd *r) {
	uint32_t v = 0; int shift = 0;
	for (;;) {
		uint8_t b = rd_u8(r);
		if (shift == 28 && (b & 0xf0)) die("u32 LEB128 out of range");
		v |= (uint32_t)(b & 0x7f) << shift;
		if (!(b & 0x80)) return v;
		shift += 7;
		if (shift > 28) die("u32 LEB128 too long");
	}
}
/* Signed LEB128 is the same framing, but the last byte's bit 6 is the sign bit
 * and the value must be sign-extended from wherever we stopped. */
static int64_t rd_sv(Rd *r, int bits) {
	/* Accumulate in unsigned: shifting into (or past) the sign bit of a signed
	 * type is undefined behaviour in C, and a hostile module controls the shift. */
	uint64_t v = 0; int shift = 0; uint8_t b;
	do {
		b = rd_u8(r);
		if (shift >= bits) {
			/* Trailing bytes are only legal if they are pure sign extension. */
			if (!(b == 0x00 || b == 0x7f)) die("signed LEB128 out of range");
		} else {
			v |= (uint64_t)(b & 0x7f) << shift;
		}
		shift += 7;
		if (shift > 70) die("signed LEB128 too long");
	} while (b & 0x80);
	if (shift < 64 && (b & 0x40)) v |= ~(uint64_t)0 << shift;
	return (int64_t)v;
}
static int32_t rd_s32v(Rd *r) { return (int32_t)rd_sv(r, 32); }
static int64_t rd_s64v(Rd *r) { return rd_sv(r, 64); }

/* ------------------------------------------------------------------- types */
#define VT_I32 0x7f
#define VT_I64 0x7e
#define VT_F32 0x7d
#define VT_F64 0x7c

static void check_valtype(uint8_t t) {
	if (t == VT_F32 || t == VT_F64)
		die("f32/f64 are not implemented by this interpreter (module rejected)");
	if (t != VT_I32 && t != VT_I64) die("unknown value type 0x%02x", t);
}

typedef struct {
	uint32_t np, nr;      /* nr is 0 or 1 in MVP */
	uint8_t *pt;          /* np parameter types */
	uint8_t rt;           /* result type, valid when nr==1 */
} FType;

typedef struct {
	uint32_t type;
	uint32_t nlocals;     /* params + declared locals */
	uint8_t *lt;          /* nlocals types (only used to validate/print) */
	const uint8_t *code;  /* body, ends with the function's own 0x0b */
	uint32_t clen;
	uint32_t *endpc;      /* for each block/loop/if pc: pc of its matching end */
	uint32_t *elsepc;     /* for each if pc: pc of its else, or 0 if none */
	uint32_t maxstack;    /* validated operand-stack high-water mark */
	uint32_t maxctrl;     /* validated label-stack depth */
} Func;

typedef struct { uint8_t type, mut; uint64_t val; } Global;
typedef struct { const char *name; uint32_t nlen; uint8_t kind; uint32_t idx; } Export;

typedef struct {
	FType *types; uint32_t ntypes;
	Func *funcs;  uint32_t nfuncs;
	Global *globals; uint32_t nglobals;
	Export *exports; uint32_t nexports;
	uint32_t *table; uint32_t tsize; int has_table;
	uint8_t *mem; uint32_t mpages, mmaxpages; int has_mem;
	int32_t start;
} Module;

static Module M;
#define NO_FUNC 0xffffffffu

/* --------------------------------------------------------------- opcodes */
enum {
	OP_UNREACHABLE=0x00, OP_NOP=0x01, OP_BLOCK=0x02, OP_LOOP=0x03, OP_IF=0x04,
	OP_ELSE=0x05, OP_END=0x0b, OP_BR=0x0c, OP_BR_IF=0x0d, OP_BR_TABLE=0x0e,
	OP_RETURN=0x0f, OP_CALL=0x10, OP_CALL_INDIRECT=0x11,
	OP_DROP=0x1a, OP_SELECT=0x1b,
	OP_LOCAL_GET=0x20, OP_LOCAL_SET=0x21, OP_LOCAL_TEE=0x22,
	OP_GLOBAL_GET=0x23, OP_GLOBAL_SET=0x24,
	OP_LOAD_FIRST=0x28, OP_LOAD_LAST=0x35,
	OP_STORE_FIRST=0x36, OP_STORE_LAST=0x3e,
	OP_MEM_SIZE=0x3f, OP_MEM_GROW=0x40,
	OP_I32_CONST=0x41, OP_I64_CONST=0x42, OP_F32_CONST=0x43, OP_F64_CONST=0x44,
	OP_I32_WRAP=0xa7, OP_I64_EXT_S=0xac, OP_I64_EXT_U=0xad
};

/* Opcodes we deliberately refuse: everything float.  Keeping this as an explicit
 * predicate (rather than "default: die") means a genuinely unknown byte and a
 * known-but-unimplemented one give different, honest messages. */
static int is_float_op(uint8_t op) {
	return (op >= 0x43 && op <= 0x44) || (op >= 0x5b && op <= 0x66) ||
	       (op >= 0x8b && op <= 0xa6) || (op >= 0xa8 && op <= 0xab) ||
	       (op >= 0xae && op <= 0xbf) ||
	       op == 0x2a || op == 0x2b || op == 0x38 || op == 0x39;
}

/* ------------------------------------------------- blocktype -> label arity */
static uint32_t blocktype(Rd *r) {
	uint8_t b = rd_u8(r);
	if (b == 0x40) return 0;             /* empty result */
	check_valtype(b);
	return 1;
}

/* ====================================================================
 * Validation + jump resolution.
 *
 * One forward pass per function body.  It does three jobs:
 *
 *  1. Structural: match block/loop/if against else/end, and record for each
 *     block-start pc where its `end` (and `else`) live.  WebAssembly control
 *     flow is *structured* -- there are no arbitrary jumps, only `br N` to the
 *     N-th enclosing label -- so a label stack is enough to resolve every branch
 *     target statically, once, instead of scanning the body at every branch.
 *
 *  2. Operand-stack depth.  We track the height symbolically (not the types --
 *     that is the honest gap, see README).  This has to happen *before*
 *     execution because the interpreter's hot loop pushes without checking:
 *     knowing maxstack up front lets us reserve the space once per call and
 *     makes "stack overflow" impossible rather than merely unlikely.  Without
 *     it a crafted body of nothing but `i32.const` is a heap overflow.
 *
 *  3. Index bounds: every local/global/func/type/label index is checked here,
 *     so the interpreter can index raw arrays with no per-instruction checks.
 *
 * Code after `br`/`return`/`unreachable` is unreachable; the spec validates it
 * polymorphically.  We approximate that with a `dead` flag: while dead we stop
 * tracking height (and stop complaining about underflow) but keep tracking
 * block nesting, because the `end`s still have to match.
 * ==================================================================== */
typedef struct { uint32_t h; uint8_t arity, labar, is_if, dead; } VCtrl;

static void prepare(Func *f) {
	FType *ft = &M.types[f->type];
	Rd r = { f->code, f->clen, 0 };
	VCtrl *cs = xmalloc(sizeof(VCtrl) * (CSTACK_MAX + 1));
	uint32_t csp = 0, cur = 0, maxh = 0, maxc = 1;
	uint8_t dead = 0;

	f->endpc  = xcalloc(f->clen + 1, sizeof(uint32_t));
	f->elsepc = xcalloc(f->clen + 1, sizeof(uint32_t));

	/* Implicit outermost label == the function itself; `br` to it returns. */
	cs[csp++] = (VCtrl){ 0, (uint8_t)ft->nr, (uint8_t)ft->nr, 0, 0 };

	while (r.i < r.n) {
		uint32_t pc = (uint32_t)r.i;
		uint8_t op = rd_u8(&r);
		uint32_t pops = 0, pushes = 0;

		if (is_float_op(op))
			die("opcode 0x%02x is a floating-point instruction; not implemented", op);

		switch (op) {
		case OP_UNREACHABLE: dead = 1; break;
		case OP_NOP: break;

		case OP_BLOCK: case OP_LOOP: case OP_IF: {
			uint32_t ar = blocktype(&r);
			if (op == OP_IF) pops = 1;
			if (!dead) {
				if (cur < pops || cur - pops < cs[csp-1].h) die("stack underflow");
				cur -= pops;
			}
			if (csp >= CSTACK_MAX) die("control stack too deep");
			cs[csp++] = (VCtrl){ cur, (uint8_t)ar,
			                     (uint8_t)(op == OP_LOOP ? 0 : ar),
			                     (uint8_t)(op == OP_IF), dead };
			if (csp > maxc) maxc = csp;
			continue;
		}
		case OP_ELSE:
			if (csp < 2 || cs[csp-1].is_if != 1) die("else without a matching if");
			cs[csp-1].is_if = 2;             /* so a second else is rejected */
			/* The else-arm starts from the same stack height the if-arm did, and
			 * inherits the enclosing reachability. */
			dead = cs[csp-1].dead;
			cur = cs[csp-1].h;
			continue;
		case OP_END: {
			VCtrl fr = cs[--csp];
			cur = fr.h + fr.arity;
			dead = fr.dead;
			if (cur > maxh) maxh = cur;
			/* csp == 0 means that was the function's own terminating `end`;
			 * cs[csp-1] below would be out of bounds, so leave now. */
			if (csp == 0 && r.i != r.n) die("instructions after function end");
			continue;
		}

		case OP_BR: case OP_BR_IF: {
			uint32_t d = rd_u32v(&r);
			if (d + 1 > csp) die("branch depth %u out of range", d);
			uint32_t need = cs[csp-1-d].labar + (op == OP_BR_IF ? 1u : 0u);
			if (!dead && cur < need) die("stack underflow at branch");
			if (op == OP_BR_IF) { if (!dead) cur -= 1; }
			else dead = 1;
			continue;
		}
		case OP_BR_TABLE: {
			uint32_t n = rd_u32v(&r);
			for (uint32_t k = 0; k < n; k++) {
				uint32_t d = rd_u32v(&r);
				if (d + 1 > csp) die("br_table depth out of range");
			}
			uint32_t d = rd_u32v(&r);
			if (d + 1 > csp) die("br_table default depth out of range");
			if (!dead && cur < 1u + cs[csp-1-d].labar) die("stack underflow at br_table");
			dead = 1;
			continue;
		}
		case OP_RETURN: dead = 1; continue;

		case OP_CALL: {
			uint32_t fi = rd_u32v(&r);
			if (fi >= M.nfuncs) die("call: function index %u out of range", fi);
			FType *t = &M.types[M.funcs[fi].type];
			pops = t->np; pushes = t->nr;
			break;
		}
		case OP_CALL_INDIRECT: {
			uint32_t ti = rd_u32v(&r);
			uint8_t res = rd_u8(&r);
			if (res != 0) die("call_indirect reserved byte must be 0");
			if (ti >= M.ntypes) die("call_indirect: type index out of range");
			if (!M.has_table) die("call_indirect without a table");
			pops = M.types[ti].np + 1; pushes = M.types[ti].nr;
			break;
		}

		case OP_DROP: pops = 1; break;
		case OP_SELECT: pops = 3; pushes = 1; break;

		case OP_LOCAL_GET: case OP_LOCAL_SET: case OP_LOCAL_TEE: {
			uint32_t li = rd_u32v(&r);
			if (li >= f->nlocals) die("local index %u out of range", li);
			if (op == OP_LOCAL_GET) pushes = 1;
			else if (op == OP_LOCAL_SET) pops = 1;
			else { pops = 1; pushes = 1; }
			break;
		}
		case OP_GLOBAL_GET: case OP_GLOBAL_SET: {
			uint32_t gi = rd_u32v(&r);
			if (gi >= M.nglobals) die("global index %u out of range", gi);
			if (op == OP_GLOBAL_SET && !M.globals[gi].mut) die("global.set on immutable global");
			if (op == OP_GLOBAL_GET) pushes = 1; else pops = 1;
			break;
		}

		case OP_MEM_SIZE: case OP_MEM_GROW: {
			if (rd_u8(&r) != 0) die("memory op reserved byte must be 0");
			if (!M.has_mem) die("memory instruction without a memory");
			if (op == OP_MEM_SIZE) pushes = 1; else { pops = 1; pushes = 1; }
			break;
		}
		case OP_I32_CONST: rd_s32v(&r); pushes = 1; break;
		case OP_I64_CONST: rd_s64v(&r); pushes = 1; break;

		default:
			if (op >= OP_LOAD_FIRST && op <= OP_LOAD_LAST) {
				rd_u32v(&r); rd_u32v(&r);
				if (!M.has_mem) die("load without a memory");
				pops = 1; pushes = 1;
			} else if (op >= OP_STORE_FIRST && op <= OP_STORE_LAST) {
				rd_u32v(&r); rd_u32v(&r);
				if (!M.has_mem) die("store without a memory");
				pops = 2;
			} else if (op == 0x45 || op == 0x50 ||                 /* eqz */
			           (op >= 0x67 && op <= 0x69) ||               /* i32 clz/ctz/popcnt */
			           (op >= 0x79 && op <= 0x7b) ||               /* i64 clz/ctz/popcnt */
			           op == OP_I32_WRAP || op == OP_I64_EXT_S || op == OP_I64_EXT_U) {
				pops = 1; pushes = 1;
			} else if ((op >= 0x46 && op <= 0x4f) || (op >= 0x51 && op <= 0x5a) ||
			           (op >= 0x6a && op <= 0x78) || (op >= 0x7c && op <= 0x8a)) {
				pops = 2; pushes = 1;
			} else {
				die("unknown opcode 0x%02x at pc %u", op, pc);
			}
			break;
		}

		if (!dead) {
			if (cur < pops || cur - pops < cs[csp-1].h) die("stack underflow at opcode 0x%02x", op);
			cur -= pops; cur += pushes;
			if (cur > maxh) maxh = cur;
		}
	}
	if (csp != 0) die("unterminated block (missing end)");
	free(cs);
	f->maxstack = maxh;
	f->maxctrl = maxc;
}

/* Second structural pass: fill endpc/elsepc.  Done separately because the
 * matching `end` is only known after we have walked past the body, and keeping
 * it out of prepare() keeps that function readable. */
static void resolve_jumps(Func *f) {
	Rd r = { f->code, f->clen, 0 };
	uint32_t stack[CSTACK_MAX]; uint32_t sp = 0;
	memset(f->elsepc, 0, (f->clen + 1) * sizeof(uint32_t));
	while (r.i < r.n) {
		uint32_t pc = (uint32_t)r.i;
		uint8_t op = rd_u8(&r);
		switch (op) {
		case OP_BLOCK: case OP_LOOP: case OP_IF:
			rd_u8(&r);
			if (sp >= CSTACK_MAX) die("control stack too deep");
			stack[sp++] = pc;
			break;
		case OP_ELSE:
			f->elsepc[stack[sp-1]] = pc;
			break;
		case OP_END:
			if (sp) f->endpc[stack[--sp]] = pc;
			break;
		case OP_BR: case OP_BR_IF: case OP_CALL:
		case OP_LOCAL_GET: case OP_LOCAL_SET: case OP_LOCAL_TEE:
		case OP_GLOBAL_GET: case OP_GLOBAL_SET:
			rd_u32v(&r); break;
		case OP_BR_TABLE: {
			uint32_t n = rd_u32v(&r);
			for (uint32_t k = 0; k <= n; k++) rd_u32v(&r);
			break;
		}
		case OP_CALL_INDIRECT: rd_u32v(&r); rd_u8(&r); break;
		case OP_MEM_SIZE: case OP_MEM_GROW: rd_u8(&r); break;
		case OP_I32_CONST: rd_s32v(&r); break;
		case OP_I64_CONST: rd_s64v(&r); break;
		default:
			if (op >= OP_LOAD_FIRST && op <= OP_STORE_LAST) { rd_u32v(&r); rd_u32v(&r); }
			break;
		}
	}
}

/* ============================================================== execution */
static uint64_t *vstack;
static uint32_t vsp;

typedef struct { uint32_t pc_end, pc_target, vsp; uint8_t arity, is_loop; } RCtrl;
static RCtrl *cstack;
static uint32_t csp;
static uint32_t calldepth;


/* Shift counts are masked, not saturated: the spec defines i32 shifts modulo 32
 * and i64 shifts modulo 64.  In C, shifting by >= the width is undefined
 * behaviour, so the mask is both a correctness and a safety requirement.  This
 * is the single most common place a hand-written interpreter disagrees with the
 * spec, which is why the fixtures hammer it. */
static uint32_t rotl32(uint32_t a, uint32_t k) { k &= 31; return k ? (a << k) | (a >> (32 - k)) : a; }
static uint32_t rotr32(uint32_t a, uint32_t k) { k &= 31; return k ? (a >> k) | (a << (32 - k)) : a; }

static uint32_t clz32(uint32_t v) { return v ? (uint32_t)__builtin_clz(v) : 32; }
static uint32_t ctz32(uint32_t v) { return v ? (uint32_t)__builtin_ctz(v) : 32; }

/* Immediate decoders for the hot loop.  No bounds checks here on purpose: the
 * body was fully walked by prepare()/resolve_jumps() before we ever run it, so
 * every immediate is known to be well-formed and in range.  That is the whole
 * payoff of validating ahead of execution. */
static inline uint32_t uleb(const uint8_t *c, uint32_t *pc) {
	uint32_t v = 0; int s = 0; uint8_t b;
	do { b = c[(*pc)++]; if (s < 32) v |= (uint32_t)(b & 0x7f) << s; s += 7; } while (b & 0x80);
	return v;
}
static inline int64_t sleb(const uint8_t *c, uint32_t *pc) {
	uint64_t v = 0; int s = 0; uint8_t b;
	do { b = c[(*pc)++]; if (s < 64) v |= (uint64_t)(b & 0x7f) << s; s += 7; } while (b & 0x80);
	if (s < 64 && (b & 0x40)) v |= ~(uint64_t)0 << s;
	return (int64_t)v;
}

static void exec(uint32_t fidx, uint32_t base);

static void do_call(uint32_t fidx) {
	Func *g = &M.funcs[fidx];
	FType *gt = &M.types[g->type];
	uint32_t gbase = vsp - gt->np;           /* args are already on top */
	/* maxstack came from the validation pass, so one check per call replaces a
	 * check on every push in the hot loop. */
	if ((uint64_t)gbase + g->nlocals + g->maxstack > VSTACK_MAX) trap("stack-overflow");
	/* Declared (non-parameter) locals are zero-initialised by the spec. */
	memset(&vstack[vsp], 0, (size_t)(g->nlocals - gt->np) * sizeof(uint64_t));
	vsp = gbase + g->nlocals;
	exec(fidx, gbase);
	/* exec leaves nr results at vstack[gbase..]; restore vsp accordingly. */
	vsp = gbase + gt->nr;
}


static void exec(uint32_t fidx, uint32_t base) {
	Func *f = &M.funcs[fidx];
	FType *ft = &M.types[f->type];
	const uint8_t *c = f->code;
	uint32_t pc = 0;
	uint32_t cbase = csp;

	calldepth++;
	if (csp + f->maxctrl > CSTACK_MAX) trap("stack-overflow");

	/* Outermost label: branching to it is a return. */
	cstack[csp++] = (RCtrl){ f->clen - 1, f->clen, base + f->nlocals, (uint8_t)ft->nr, 0 };

#define PUSH(v) (vstack[vsp++] = (uint64_t)(v))
#define POP()   (vstack[--vsp])
#define P32()   ((uint32_t)vstack[--vsp])
#define P64()   (vstack[--vsp])
#define RET()   do { uint32_t nr_ = ft->nr; \
		if (nr_) vstack[base] = vstack[vsp - 1]; \
		vsp = base + nr_; csp = cbase; calldepth--; return; } while (0)

	for (;;) {
		uint8_t op = c[pc++];
		switch (op) {
		case OP_NOP: break;

		case OP_END:
			/* No blocks yet, so the only `end` is the function's own. */
			RET();

		case OP_RETURN: RET();


		case OP_DROP: vsp--; break;
		case OP_SELECT: { uint32_t cond = P32(); uint64_t b2 = POP(), a2 = POP(); PUSH(cond ? a2 : b2); break; }

		case OP_LOCAL_GET:  { uint32_t i = uleb(c, &pc); PUSH(vstack[base + i]); break; }
		case OP_LOCAL_SET:  { uint32_t i = uleb(c, &pc); vstack[base + i] = POP(); break; }
		case OP_LOCAL_TEE:  { uint32_t i = uleb(c, &pc); vstack[base + i] = vstack[vsp - 1]; break; }

		case OP_I32_CONST: { int64_t v = sleb(c, &pc); PUSH((uint32_t)(int32_t)v); break; }
		case OP_I64_CONST: { int64_t v = sleb(c, &pc); PUSH((uint64_t)v); break; }

		default:
			/* Opcodes with no dedicated case above are dispatched numerically. */
			switch (op) {



			/* ------------------------------------------------ i32 arith */
			case 0x67: { uint32_t a = P32(); PUSH(clz32(a)); break; }
			case 0x68: { uint32_t a = P32(); PUSH(ctz32(a)); break; }
			case 0x69: { uint32_t a = P32(); PUSH((uint32_t)__builtin_popcount(a)); break; }
			case 0x6a: { uint32_t b2 = P32(), a = P32(); PUSH((uint32_t)(a + b2)); break; }
			case 0x6b: { uint32_t b2 = P32(), a = P32(); PUSH((uint32_t)(a - b2)); break; }
			case 0x6c: { uint32_t b2 = P32(), a = P32(); PUSH((uint32_t)(a * b2)); break; }
			case 0x6d: { int32_t b2 = (int32_t)P32(), a = (int32_t)P32();
				if (b2 == 0) trap("div0");
				/* INT32_MIN / -1 is not representable: the spec traps, and in C it
				 * would be undefined behaviour. */
				if (a == INT32_MIN && b2 == -1) trap("int-overflow");
				PUSH((uint32_t)(a / b2)); break; }
			case 0x6e: { uint32_t b2 = P32(), a = P32(); if (!b2) trap("div0"); PUSH(a / b2); break; }
			case 0x6f: { int32_t b2 = (int32_t)P32(), a = (int32_t)P32();
				if (b2 == 0) trap("div0");
				/* INT32_MIN % -1 is 0 by the spec, but UB in C; special-case it. */
				PUSH(b2 == -1 ? 0u : (uint32_t)(a % b2)); break; }
			case 0x70: { uint32_t b2 = P32(), a = P32(); if (!b2) trap("div0"); PUSH(a % b2); break; }
			case 0x71: { uint32_t b2 = P32(), a = P32(); PUSH(a & b2); break; }
			case 0x72: { uint32_t b2 = P32(), a = P32(); PUSH(a | b2); break; }
			case 0x73: { uint32_t b2 = P32(), a = P32(); PUSH(a ^ b2); break; }
			case 0x74: { uint32_t b2 = P32(), a = P32(); PUSH((uint32_t)(a << (b2 & 31))); break; }
			case 0x75: { uint32_t b2 = P32(); int32_t a = (int32_t)P32(); PUSH((uint32_t)(a >> (b2 & 31))); break; }
			case 0x76: { uint32_t b2 = P32(), a = P32(); PUSH(a >> (b2 & 31)); break; }
			case 0x77: { uint32_t b2 = P32(), a = P32(); PUSH(rotl32(a, b2)); break; }
			case 0x78: { uint32_t b2 = P32(), a = P32(); PUSH(rotr32(a, b2)); break; }



			default: die("internal: unhandled opcode 0x%02x", op);
			}
			break;
		}
	}
#undef PUSH
#undef POP
#undef P32
#undef P64
#undef RET
}

/* ============================================================== decoding */

static void limits(Rd *r, uint32_t *min, uint32_t *max, uint32_t cap) {
	uint8_t fl = rd_u8(r);
	if (fl > 1) die("bad limits flag 0x%02x", fl);
	*min = rd_u32v(r);
	*max = fl ? rd_u32v(r) : cap;
	if (*min > cap || *max > cap || *max < *min) die("limits out of range");
}

static void load_module(const uint8_t *buf, size_t len) {
	Rd r = { buf, len, 0 };
	uint8_t magic[4]; uint32_t ver;
	rd_bytes(&r, magic, 4);
	if (memcmp(magic, "\0asm", 4)) die("not a wasm module (bad magic)");
	rd_bytes(&r, &ver, 4);
	if (ver != 1) die("unsupported wasm version %u", ver);

	uint32_t *fdecl = NULL, nfdecl = 0;
	const uint8_t *codesec = NULL; size_t codelen = 0;
	int last_id = 0;

	while (r.i < r.n) {
		uint8_t id = rd_u8(&r);
		uint32_t sz = rd_u32v(&r);
		if (r.i + sz > r.n) die("section %u overruns file", id);
		Rd s = { r.p + r.i, sz, 0 };
		r.i += sz;
		/* Custom sections (id 0) may appear anywhere; the rest must be ordered. */
		if (id != 0) {
			if (id <= last_id) die("section %u out of order", id);
			last_id = id;
		}
		switch (id) {
		case 0: break; /* custom: ignored */
		case 1: { /* type */
			M.ntypes = rd_u32v(&s);
			M.types = xcalloc(M.ntypes, sizeof(FType));
			for (uint32_t i = 0; i < M.ntypes; i++) {
				if (rd_u8(&s) != 0x60) die("expected func type (0x60)");
				FType *t = &M.types[i];
				t->np = rd_u32v(&s);
				t->pt = xmalloc(t->np ? t->np : 1);
				for (uint32_t k = 0; k < t->np; k++) { t->pt[k] = rd_u8(&s); check_valtype(t->pt[k]); }
				t->nr = rd_u32v(&s);
				if (t->nr > 1) die("multi-value results are post-MVP");
				if (t->nr) { t->rt = rd_u8(&s); check_valtype(t->rt); }
			}
			break;
		}
		case 2: { /* import */
			uint32_t n = rd_u32v(&s);
			if (n) die("imports are not supported (%u found); wasmi runs self-contained modules only", n);
			break;
		}
		case 3: { /* function */
			nfdecl = rd_u32v(&s);
			fdecl = xmalloc(sizeof(uint32_t) * (nfdecl ? nfdecl : 1));
			for (uint32_t i = 0; i < nfdecl; i++) {
				fdecl[i] = rd_u32v(&s);
				if (fdecl[i] >= M.ntypes) die("function %u: type index out of range", i);
			}
			break;
		}
		case 7: { /* export */
			M.nexports = rd_u32v(&s);
			M.exports = xcalloc(M.nexports, sizeof(Export));
			for (uint32_t i = 0; i < M.nexports; i++) {
				uint32_t nl = rd_u32v(&s);
				if (s.i + nl > s.n) die("export name overruns section");
				M.exports[i].name = (const char *)(s.p + s.i);
				M.exports[i].nlen = nl;
				s.i += nl;
				M.exports[i].kind = rd_u8(&s);
				M.exports[i].idx = rd_u32v(&s);
			}
			break;
		}
		case 10: /* code: deferred until we know the function types */
			codesec = s.p; codelen = s.n;
			break;
		default: die("unknown section id %u", id);
		}
		if (id != 0 && id != 10 && s.i != s.n) die("section %u has %zu trailing bytes", id, s.n - s.i);
	}

	/* ------- code section ------- */
	if (nfdecl && !codesec) die("function section without code section");
	M.nfuncs = nfdecl;
	M.funcs = xcalloc(M.nfuncs ? M.nfuncs : 1, sizeof(Func));
	for (uint32_t i = 0; i < M.nfuncs; i++) M.funcs[i].type = fdecl[i];

	if (codesec) {
		Rd s = { codesec, codelen, 0 };
		uint32_t n = rd_u32v(&s);
		if (n != M.nfuncs) die("code section has %u bodies but %u functions declared", n, M.nfuncs);
		for (uint32_t i = 0; i < n; i++) {
			uint32_t bsz = rd_u32v(&s);
			if (s.i + bsz > s.n) die("function body %u overruns section", i);
			Rd b = { s.p + s.i, bsz, 0 };
			s.i += bsz;
			Func *f = &M.funcs[i];
			FType *ft = &M.types[f->type];
			uint32_t ngroups = rd_u32v(&b);
			uint32_t nloc = ft->np;
			uint8_t *lt = xmalloc(64);
			size_t lcap = 64;
			for (uint32_t k = 0; k < ft->np; k++) lt[k] = ft->pt[k];
			for (uint32_t g = 0; g < ngroups; g++) {
				uint32_t cnt = rd_u32v(&b);
				uint8_t t = rd_u8(&b); check_valtype(t);
				if ((uint64_t)nloc + cnt > 100000) die("too many locals");
				while (nloc + cnt > lcap) { lcap *= 2; lt = realloc(lt, lcap); if (!lt) die("oom"); }
				for (uint32_t k = 0; k < cnt; k++) lt[nloc + k] = t;
				nloc += cnt;
			}
			f->nlocals = nloc; f->lt = lt;
			f->code = b.p + b.i; f->clen = (uint32_t)(b.n - b.i);
			if (f->clen == 0 || f->code[f->clen - 1] != OP_END)
				die("function body %u does not end with 0x0b", i);
		}
		if (s.i != s.n) die("code section has trailing bytes");
	}

	for (uint32_t i = 0; i < M.nfuncs; i++) { prepare(&M.funcs[i]); resolve_jumps(&M.funcs[i]); }

	for (uint32_t i = 0; i < M.nexports; i++) {
		Export *e = &M.exports[i];
		if (e->kind == 0 && e->idx >= M.nfuncs) die("export: function index out of range");
	}
	free(fdecl);
}

/* ================================================================== main */
static int find_export(const char *name) {
	size_t l = strlen(name);
	for (uint32_t i = 0; i < M.nexports; i++)
		if (M.exports[i].kind == 0 && M.exports[i].nlen == l &&
		    !memcmp(M.exports[i].name, name, l)) return (int)M.exports[i].idx;
	/* Also accept a bare function index, which is handy without an export table. */
	char *end; long v = strtol(name, &end, 10);
	if (*name && !*end && v >= 0 && v < (long)M.nfuncs) return (int)v;
	return -1;
}

static uint64_t parse_arg(const char *s, uint8_t t) {
	errno = 0;
	char *end;
	long long sv = strtoll(s, &end, 0);
	if (errno == ERANGE || *end) {
		errno = 0;
		unsigned long long uv = strtoull(s, &end, 0);
		if (errno || *end) die("cannot parse argument '%s'", s);
		return t == VT_I32 ? (uint64_t)(uint32_t)uv : (uint64_t)uv;
	}
	return t == VT_I32 ? (uint64_t)(uint32_t)(int32_t)sv : (uint64_t)sv;
}

int main(int argc, char **argv) {
	if (argc < 3) {
		fprintf(stderr, "usage: %s MODULE.wasm FUNC [args...]\n", argv[0]);
		return 2;
	}
	FILE *fp = fopen(argv[1], "rb");
	if (!fp) die("cannot open %s", argv[1]);
	if (fseek(fp, 0, SEEK_END)) die("cannot seek %s", argv[1]);
	long sz = ftell(fp);
	if (sz < 0) die("cannot size %s", argv[1]);
	rewind(fp);
	uint8_t *buf = xmalloc((size_t)sz + 1);
	if (fread(buf, 1, (size_t)sz, fp) != (size_t)sz) die("short read on %s", argv[1]);
	fclose(fp);

	load_module(buf, (size_t)sz);

	int fi = find_export(argv[2]);
	if (fi < 0) die("no exported function named '%s'", argv[2]);
	FType *ft = &M.types[M.funcs[fi].type];
	if ((uint32_t)(argc - 3) != ft->np)
		die("'%s' takes %u argument(s), %d given", argv[2], ft->np, argc - 3);

	vstack = xcalloc(VSTACK_MAX, sizeof(uint64_t));
	cstack = xcalloc(CSTACK_MAX, sizeof(RCtrl));

	if (setjmp(g_trap)) {
		printf("trap:%s\n", g_trapcode);
		return 1;
	}

	vsp = 0; calldepth = 0; csp = 0;
	for (int i = 3; i < argc; i++) vstack[vsp++] = parse_arg(argv[i], ft->pt[i - 3]);
	do_call((uint32_t)fi);

	if (ft->nr) {
		if (ft->rt == VT_I32) printf("%" PRId32 "\n", (int32_t)(uint32_t)vstack[0]);
		else printf("%" PRId64 "\n", (int64_t)vstack[0]);
	}
	return 0;
}
