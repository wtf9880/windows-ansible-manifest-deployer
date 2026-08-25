PYTHON ?= python3
ZIG ?= zig
ANSIBLE ?= ansible
ANSIBLE_PLAYBOOK ?= ansible-playbook
INVENTORY ?= ansible/inventory.yaml
ANSIBLE_ARGS ?=
ZIG_TARGET ?= x86_64-windows-gnu
CXXFLAGS ?= -O2 -s

SRC_SOURCES := $(wildcard src/*.cpp src/*.c src/*.cs)
SRC_BINARIES := $(patsubst src/%.cpp,Cache/%.exe,$(wildcard src/*.cpp)) $(patsubst src/%.c,Cache/%.exe,$(wildcard src/*.c)) $(patsubst src/%.cs,Cache/%.exe,$(wildcard src/*.cs))

.PHONY: all prepare download compile verify update check-updates deploy test clean

all: prepare

prepare: download compile

download: | Cache
	$(PYTHON) tools/prepare.py

Cache/%.exe: src/%.cpp Makefile | Cache
	$(ZIG) c++ -target $(ZIG_TARGET) $(CXXFLAGS) $< -o $@

Cache/%.exe: src/%.c Makefile | Cache
	$(ZIG) cc -target $(ZIG_TARGET) $(CXXFLAGS) $< -o $@

Cache/%.exe: src/%.cs Makefile | Cache
	csc $< /out:$@

Cache:
	mkdir -p $@

compile: $(SRC_BINARIES)

verify:
	$(PYTHON) tools/prepare.py --verify-only

update:
	$(PYTHON) tools/update_manifests.py

check-updates:
	$(PYTHON) tools/update_manifests.py --check

deploy: prepare
	@test -f "$(INVENTORY)" || (echo "Missing $(INVENTORY); copy ansible/inventory.example.yaml first" >&2; exit 2)
	$(ANSIBLE_PLAYBOOK) -i "$(INVENTORY)" ansible/deploy.yaml $(ANSIBLE_ARGS)
ping:
	@test -f "$(INVENTORY)" || (echo "Missing $(INVENTORY); copy ansible/inventory.example.yaml first" >&2; exit 2)
	$(ANSIBLE) windows -m ansible.windows.win_ping -i "$(INVENTORY)" $(ANSIBLE_ARGS)

test:
	$(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) -m compileall -q tools tests

clean:
	rm -rf Cache/.state
	rm -f $(SRC_BINARIES)

