# image_updater — minimal Makefile.
#
# Build natively on the target (or on the GitHub-hosted ubuntu-24.04-arm
# runner, which is also aarch64). For a cross-compile from x86 you would
# invoke `make CC=aarch64-linux-gnu-gcc` instead, but we don't currently
# support that flow because GitHub Actions ARM runners are free for public
# repos and produce identical output.

CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -pthread
LDFLAGS ?=
LDLIBS  ?= -lpthread

TARGET   := image_updater
SOURCES  := image_updater.c

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SOURCES)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

clean:
	rm -f $(TARGET)
