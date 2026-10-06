CC      ?= gcc
CFLAGS  ?= -std=c11 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion
LDFLAGS ?=

.PHONY: all test fixtures clean

all: wasmi

wasmi: wasmi.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

fixtures:
	python3 t/mkwasm.py

test: wasmi fixtures
	@sh t/run.sh

clean:
	rm -f wasmi
	rm -rf t/fix
