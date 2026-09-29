#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <optional>
#include <random>
#include <string>
#include <vector>

namespace dispatch {
using Int = std::int64_t;
using Routes = std::vector<std::vector<int>>;
// Versioned morning/day components, minimized lexicographically.
using Score = std::vector<Int>;

struct Request {
    std::string id;
    Int window_start, window_end, service, released_at;
    int skill, profile, source_order;
    bool urgent;
    int priority;
    std::array<Int, 2> equipment{};
};
struct Matrix {
    int size;
    std::vector<Int> duration, distance;
    std::vector<int> request_locations;
};
struct Engineer {
    std::string id;
    int skills, profile, start_location;
    Int shift_start, shift_end;
    bool unavailable = false;
    bool on_shift = true;
    std::array<Int, 2> equipment_issued{};
};
struct Visit {
    int request;
    Int arrival, start, finish, travel, waiting, distance;
    bool operator==(const Visit&) const = default;
};
struct Problem {
    std::string id, input_hash;
    std::vector<Request> requests;
    std::vector<Engineer> engineers;
    std::vector<Matrix> matrices;
    std::vector<std::vector<Visit>> frozen;
    Int at = 0;
    bool has_state = false;
    bool day_policy = false, preserve = false;
    bool work_type_priority = false;
    bool compact_schedule = false;
    bool equipment_active = false;
    Int max_delay = 900;
    std::vector<int> reference_engineer, reference_order;
    std::vector<Int> reference_start;
    std::vector<bool> eligible;
    Routes protected_routes;
};
struct RouteEvaluation {
    std::vector<Visit> visits;
    Int distance = 0, travel = 0, waiting = 0, service = 0, urgent_delay = 0;
    int urgent_assigned = 0;
};
struct Solution {
    Routes routes;
    std::vector<RouteEvaluation> evaluations;
    Score score;
};
struct Options {
    int rounds = 2048;
    Int max_evaluations = 32000000;
    int time_limit_ms = 60000;
    std::uint32_t seed = 20260917;
    bool use_local_search = true, use_eliminate = false, use_destroy = true;
};
struct Statistics {
    Int evaluations = 0, improvements = 0;
    int rounds_completed = 0;
    std::string status = "iteration_limit";
};

bool compatible(const Problem& problem, int engineer, int request);
std::optional<RouteEvaluation> evaluate_route(const Problem& problem, int engineer,
                                               const std::vector<int>& route, std::string* failure = nullptr);
std::optional<Solution> evaluate(const Problem& problem, const Routes& routes);

class Search {
public:
    Search(const Problem& problem, Options options);
    Solution solve(const Routes& initial);
    Statistics statistics;
private:
    const Problem& problem_;
    Options options_;
    std::mt19937 random_;
    std::chrono::steady_clock::time_point started_;
    std::optional<Solution> best_;

    void budget();
    std::optional<Solution> assess(const Routes& routes);
    void remember(const Solution& solution);
    std::vector<int> unassigned(const Solution& solution) const;
    std::optional<Solution> insert(const Solution& solution, int request, int forbidden = -1);
    Solution repair(Solution solution, std::vector<int> order, int forbidden = -1);
    Solution regret_repair(Solution solution);
    Solution local_search(Solution solution);
    Solution augment(Solution solution);
    Solution eliminate(Solution solution);
};
} // namespace dispatch
