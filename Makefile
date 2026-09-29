CXX ?= c++
CXXFLAGS ?= -O3 -DNDEBUG
WARNINGS = -Wall -Wextra -Wpedantic -Werror
SOURCES = solver/main.cpp solver/solver.cpp
HEADERS = solver/solver.hpp solver/third_party/json.hpp

.PHONY: solver sanitize test
solver: build/dispatch-solver build/dispatch-transit.so

build/dispatch-transit.so: solver/transit_search.cpp
	mkdir -p build
	$(CXX) -std=c++20 $(CXXFLAGS) $(WARNINGS) -shared -fPIC $< -o $@

build/dispatch-solver: $(SOURCES) $(HEADERS)
	mkdir -p build
	$(CXX) -std=c++20 $(CXXFLAGS) $(WARNINGS) -Isolver $(SOURCES) -o $@

sanitize: build/dispatch-solver-sanitize build/transit-search-sanitize
	./build/transit-search-sanitize

build/transit-search-sanitize: solver/transit_search.cpp tests/transit_search_sanitize.cpp
	mkdir -p build
	$(CXX) -std=c++20 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer $(WARNINGS) -Isolver tests/transit_search_sanitize.cpp -o $@

build/dispatch-solver-sanitize: $(SOURCES) $(HEADERS)
	mkdir -p build
	$(CXX) -std=c++20 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer $(WARNINGS) -Isolver $(SOURCES) -o $@

test: solver
	.venv/bin/python -m pytest -q
