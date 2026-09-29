#include "transit_search.cpp"

#include <cassert>

int main() {
    // A fast walk uses the whole budget. A slower transit path must survive
    // dominance and can then reach a destination that needs another walk.
    const Edge edges[] = {{1, 1, 5000, 0}, {1, 10, 0, 1}, {2, 1, 100, 0}};
    const Int offsets[] = {0, 2, 3, 3};
    const Seed seeds[] = {{0, 0, 0}};
    for (int constrained : {0, 1}) {
        void* result = dispatch_transit_search(edges, 3, offsets, 3, seeds, 1,
                                               21600, 5000, constrained);
        assert(result);
        assert(dispatch_transit_label_count(result) >= 4);
        Int count = 0;
        const auto* front = dispatch_transit_front(result, 2 * 2 + 1, &count);
        assert(count == 1);
        const auto* labels = dispatch_transit_labels(result);
        assert(labels[front[0]].time == 11 && labels[front[0]].walked == 100);
        dispatch_transit_front(result, 2 * 2, &count);
        assert(count == (constrained ? 0 : 1));
        dispatch_transit_front(result, -1, &count);
        assert(count == 0);
        dispatch_transit_free(result);
    }
    assert(!dispatch_transit_search(edges, 2, offsets, 3, seeds, 1, 21600, 5000, 1));
    const Seed invalid[] = {{3, 0, 0}};
    assert(!dispatch_transit_search(edges, 3, offsets, 3, invalid, 1, 21600, 5000, 1));
    const Int empty_offsets[] = {0};
    void* empty = dispatch_transit_search(nullptr, 0, empty_offsets, 0, nullptr, 0,
                                         21600, 5000, 1);
    assert(empty && dispatch_transit_label_count(empty) == 0);
    dispatch_transit_free(empty);
}
