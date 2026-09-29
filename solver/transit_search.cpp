// Exact counterpart of PublicRouter.labels_from. Queue and dominance ties are
// deliberately identical to the Python reference, including insertion order.
#include <algorithm>
#include <cstdint>
#include <limits>
#include <memory>
#include <queue>
#include <stdexcept>
#include <tuple>
#include <vector>

namespace {
using Int = std::int64_t;
struct Edge { Int target, time, walked, boarded; };
struct Seed { Int node, time, walked; };
struct Label { Int node, time, walked, previous, via; };
struct Result {
    std::vector<Label> labels;
    std::vector<std::vector<Int>> fronts;
    std::vector<bool> active;
};

bool valid_cost(Int value) {
    return value >= 0 && value <= std::numeric_limits<Int>::max() / 4;
}
} // namespace

extern "C" {
// Keep ABI structs fixed-width; callers own input arrays for the duration of a
// call. Results own their data until explicitly released. No static OD cache.
void* dispatch_transit_search(const Edge* edges, Int edge_count, const Int* offsets,
                              Int node_count, const Seed* seeds, Int seed_count,
                              Int max_time, Int max_walk, int constrained) noexcept {
    try {
        if (node_count < 0 || node_count > 10'000'000 || edge_count < 0 ||
            seed_count < 0 || !offsets || (!edges && edge_count) || (!seeds && seed_count) ||
            !valid_cost(max_time) || !valid_cost(max_walk) || offsets[0] != 0 ||
            offsets[node_count] != edge_count)
            throw std::invalid_argument("Invalid transit graph");
        for (Int node = 0; node < node_count; ++node)
            if (offsets[node] < 0 || offsets[node] > offsets[node + 1])
                throw std::invalid_argument("Invalid offsets");
        for (Int i = 0; i < edge_count; ++i)
            if (edges[i].target < 0 || edges[i].target >= node_count ||
                !valid_cost(edges[i].time) || !valid_cost(edges[i].walked) ||
                (edges[i].boarded != 0 && edges[i].boarded != 1))
                throw std::invalid_argument("Invalid edge");
        for (Int i = 0; i < seed_count; ++i)
            if (seeds[i].node < 0 || seeds[i].node >= node_count ||
                !valid_cost(seeds[i].time) || !valid_cost(seeds[i].walked))
                throw std::invalid_argument("Invalid access");

        auto result = std::make_unique<Result>();
        result->fronts.resize(static_cast<std::size_t>(node_count * 2));
        using Entry = std::tuple<Int, Int, Int>;
        std::priority_queue<Entry, std::vector<Entry>, std::greater<Entry>> queue;
        auto add = [&](Int node, Int time, Int walked, Int previous, Int via) {
            if (time > max_time || (constrained && walked > max_walk)) return;
            auto& front = result->fronts[static_cast<std::size_t>(node)];
            if (constrained) {
                for (Int i : front) {
                    const auto& old = result->labels[static_cast<std::size_t>(i)];
                    if (old.time <= time && old.walked <= walked) return;
                }
                front.erase(std::remove_if(front.begin(), front.end(), [&](Int i) {
                    const auto& old = result->labels[static_cast<std::size_t>(i)];
                    const bool dominated = old.time >= time && old.walked >= walked;
                    if (dominated) result->active[static_cast<std::size_t>(i)] = false;
                    return dominated;
                }), front.end());
            } else if (!front.empty()) {
                const auto& old = result->labels[static_cast<std::size_t>(front.front())];
                if (std::tie(old.time, old.walked) <= std::tie(time, walked)) return;
                result->active[static_cast<std::size_t>(front.front())] = false;
                front.clear();
            }
            const Int i = static_cast<Int>(result->labels.size());
            result->labels.push_back({node, time, walked, previous, via});
            result->active.push_back(true);
            front.push_back(i);
            queue.emplace(time, walked, i);
        };
        for (Int i = 0; i < seed_count; ++i)
            add(seeds[i].node * 2, seeds[i].time, seeds[i].walked, -1, -i - 1);
        while (!queue.empty()) {
            const auto [time, walked, index] = queue.top();
            queue.pop();
            if (!result->active[static_cast<std::size_t>(index)]) continue;
            // Copy before add(): appending may reallocate the label vector.
            const auto label = result->labels[static_cast<std::size_t>(index)];
            const Int node = label.node / 2;
            for (Int i = offsets[node]; i < offsets[node + 1]; ++i) {
                const auto& edge = edges[i];
                if (walked > std::numeric_limits<Int>::max() - edge.walked)
                    throw std::overflow_error("Walking distance overflow");
                add(edge.target * 2 + ((label.node % 2) || edge.boarded),
                    time + edge.time, walked + edge.walked, index, i);
            }
        }
        return result.release();
    } catch (...) {
        return nullptr; // Allocation/validation failures are errors, never unreachable routes.
    }
}

Int dispatch_transit_label_count(const void* handle) noexcept {
    return static_cast<Int>(static_cast<const Result*>(handle)->labels.size());
}

const Label* dispatch_transit_labels(const void* handle) noexcept {
    return static_cast<const Result*>(handle)->labels.data();
}

const Int* dispatch_transit_front(const void* handle, Int node, Int* size) noexcept {
    const auto& fronts = static_cast<const Result*>(handle)->fronts;
    if (node < 0 || static_cast<std::size_t>(node) >= fronts.size()) {
        *size = 0;
        return nullptr;
    }
    const auto& front = fronts[static_cast<std::size_t>(node)];
    *size = static_cast<Int>(front.size());
    return front.data();
}

void dispatch_transit_free(void* handle) noexcept { delete static_cast<Result*>(handle); }
} // extern "C"
