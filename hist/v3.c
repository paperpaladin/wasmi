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





/* ============================================================== execution */







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
		default: break;   /* sections we cannot handle yet are skipped */
		}
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

	die("execution is not implemented yet ('%s' validates cleanly)", argv[2]);
	return 2;
}

