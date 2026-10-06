CC      ?= gcc
CFLAGS  ?= -std=c11 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion
LDFLAGS ?=

# The sanitizer build is not optional nicety here: wasmi parses untrusted binary
# input and then indexes raw arrays from it.  UBSan catches the shift/overflow
# mistakes that a bytecode interpreter attracts; ASan catches the off-by-one in
# a bounds check that a normal build would simply get away with.
SANFLAGS = -std=c11 -O1 -g -fsanitize=address,undefined \
           -fno-omit-frame-pointer -fno-sanitize-recover=all \
           -Wall -Wextra -Wpedantic

.PHONY: all test test-asan fixtures clean

all: wasmi

wasmi: wasmi.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

wasmi-asan: wasmi.c
	$(CC) $(SANFLAGS) -o $@ $< $(LDFLAGS)

fixtures:
	python3 t/mkwasm.py

test: wasmi fixtures
	@sh t/run.sh

test-asan: wasmi-asan fixtures
	@WASMI=./wasmi-asan sh t/run.sh

clean:
	rm -f wasmi wasmi-asan
	rm -rf t/fix
