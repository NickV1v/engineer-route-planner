#include "solver.hpp"

#include <algorithm>
#include <numeric>
#include <stdexcept>
#include <tuple>

namespace dispatch {
bool compatible(const Problem& p, int e, int r) {
    const auto& engineer = p.engineers.at(e);
    const auto& request = p.requests.at(r);
    return engineer.on_shift && !engineer.unavailable &&
           (!p.preserve || (p.eligible[r] && (p.reference_engineer[r] < 0 ||
            p.engineers[p.reference_engineer[r]].unavailable || p.reference_engineer[r] == e))) &&
           (engineer.skills & request.skill) != 0 &&
           (request.profile < 0 || request.profile == engineer.profile);
}

std::optional<RouteEvaluation> evaluate_route(const Problem& p, int e,
                                               const std::vector<int>& route, std::string* failure) {
    const auto& engineer = p.engineers.at(e);
    const auto& matrix = p.matrices.at(engineer.profile);
    auto location = engineer.start_location;
    auto available = engineer.shift_start;
    RouteEvaluation result;
    std::array<Int, 2> equipment{};
    auto reject = [&](const char* reason) -> std::optional<RouteEvaluation> {
        if (failure) *failure = reason;
        return std::nullopt;
    };
    const auto& frozen = p.frozen.at(e);
    if (!engineer.on_shift && !route.empty()) return reject("off_shift");
    if (p.preserve) {
        std::vector<int> retained;
        for (const auto r : route) {
            if (!p.eligible.at(r)) return reject("outside_event");
            const auto owner = p.reference_engineer.at(r);
            if (owner >= 0 && !p.engineers.at(owner).unavailable) retained.push_back(r);
        }
        if (retained != p.protected_routes.at(e)) return reject("fixed_order");
    }
    if (route.size() < frozen.size()) return reject("frozen");
    if (p.equipment_active) {
        // Frozen visits keep their devices committed, even after their scheduled finish.
        for (const auto r : route) {
            equipment[0] += p.requests.at(r).equipment[0];
            equipment[1] += p.requests.at(r).equipment[1];
        }
        if (equipment[0] > engineer.equipment_issued[0]) return reject("equipment_routers");
        if (equipment[1] > engineer.equipment_issued[1]) return reject("equipment_set_top_boxes");
    }
    for (std::size_t pos = 0; pos < route.size(); ++pos) {
        const auto r = route[pos];
        const bool fixed = pos < frozen.size();
        if (fixed && r != frozen[pos].request) return reject("frozen");
        if (!fixed && engineer.unavailable) return reject("unavailable");
        const auto& request = p.requests.at(r);
        if (!(engineer.skills & request.skill)) return reject("skill");
        if (request.profile >= 0 && request.profile != engineer.profile) return reject("transport");
        const auto destination = matrix.request_locations.at(r);
        const auto offset = location * matrix.size + destination;
        const auto travel = matrix.duration.at(offset);
        if (travel < 0) return reject("unreachable");
        auto arrival = fixed ? frozen[pos].arrival : std::max({available, p.at, request.released_at}) + travel;
        if (fixed && arrival - travel < std::max(available, request.released_at)) return reject("frozen");
        const auto earliest = std::max(arrival, request.window_start);
        const auto start = fixed ? frozen[pos].start : std::max(earliest, p.reference_start.at(r));
        if (!fixed && pos == 0 && p.compact_schedule) arrival = start;
        if (start < earliest) return reject("frozen");
        const auto owner = p.reference_engineer.at(r);
        if (!fixed && p.preserve && owner >= 0 && !p.engineers.at(owner).unavailable &&
            start > p.reference_start[r] + p.max_delay) return reject("schedule_change");
        const auto finish = start + request.service;
        if (start > request.window_end) return reject("window");
        if (finish > engineer.shift_end) return reject("shift");
        const auto distance = matrix.distance.at(offset);
        const Visit visit{r, arrival, start, finish, travel, start - arrival, distance};
        if (fixed && !(visit == frozen[pos])) return reject("frozen");
        result.visits.push_back(visit);
        result.distance += distance;
        result.travel += travel;
        result.waiting += start - arrival;
        result.service += request.service;
        if (request.urgent) {
            ++result.urgent_assigned;
            // The planning horizon begins at the earliest configured shift.
            auto horizon = engineer.shift_start;
            for (const auto& other : p.engineers) horizon = std::min(horizon, other.shift_start);
            result.urgent_delay += start - std::max({request.window_start, horizon, request.released_at});
        }
        available = finish;
        location = destination;
    }
    return result;
}

std::optional<Solution> evaluate(const Problem& p, const Routes& routes) {
    if (routes.size() != p.engineers.size()) return std::nullopt;
    std::vector<bool> seen(p.requests.size());
    Int urgent = 0;
    for (const auto& r : p.requests) urgent += r.urgent;
    Solution result{routes, {}, {static_cast<Int>(p.requests.size()), 0, urgent, 0, 0, 0}};
    for (std::size_t e = 0; e < routes.size(); ++e) {
        for (const auto r : routes[e]) {
            if (r < 0 || static_cast<std::size_t>(r) >= seen.size() || seen[r]) return std::nullopt;
            seen[r] = true;
        }
        auto evaluation = evaluate_route(p, static_cast<int>(e), routes[e]);
        if (!evaluation) return std::nullopt;
        result.score[0] -= routes[e].size();
        result.score[1] += !routes[e].empty();
        result.score[2] -= evaluation->urgent_assigned;
        result.score[3] += evaluation->urgent_delay;
        result.score[4] += evaluation->distance;
        result.score[5] += evaluation->travel;
        result.evaluations.push_back(std::move(*evaluation));
    }
    if (p.day_policy) {
        const auto morning = result.score;
        result.score = {morning[0], morning[2], 0, 0, 0, 0, morning[3], morning[4], morning[5]};
        std::vector<int> assigned(p.requests.size(), -1);
        for (std::size_t e = 0; e < routes.size(); ++e) {
            std::vector<int> order;
            for (const auto& v : result.evaluations[e].visits) {
                assigned[v.request] = static_cast<int>(e);
                if (p.reference_engineer[v.request] >= 0) {
                    const auto shift = std::abs(v.start - p.reference_start[v.request]);
                    result.score[4] += shift != 0;
                    result.score[5] += shift;
                }
                if (p.reference_engineer[v.request] == static_cast<int>(e)) order.push_back(p.reference_order[v.request]);
            }
            for (std::size_t i = 0; i < order.size(); ++i)
                for (std::size_t j = i + 1; j < order.size(); ++j) result.score[3] += order[i] > order[j];
        }
        for (std::size_t r = 0; r < p.requests.size(); ++r) {
            const auto owner = p.reference_engineer[r];
            if (owner >= 0 && !p.engineers[owner].unavailable && assigned[r] != owner) ++result.score[2];
        }
    }
    if (p.work_type_priority) {
        Score priorities(3, 0);
        for (std::size_t r = 0; r < p.requests.size(); ++r)
            if (!seen[r]) ++priorities[p.requests[r].priority];
        if (!p.day_policy) priorities.push_back(result.score[1]);
        const auto tail = p.day_policy ? 2 : 3;
        priorities.insert(priorities.end(), result.score.begin() + tail, result.score.end());
        result.score = std::move(priorities);
    }
    if (p.compact_schedule) {
        Int overhead = 0;
        for (const auto& route : result.evaluations) overhead += route.travel + route.waiting;
        result.score.insert(result.score.end() - 2, overhead);
    }
    return result;
}

namespace {
struct BudgetReached { std::string status; };
}

Search::Search(const Problem& problem, Options options)
    : problem_(problem), options_(options), random_(options.seed), started_(std::chrono::steady_clock::now()) {}

void Search::budget() {
    if (statistics.evaluations >= options_.max_evaluations) throw BudgetReached{"evaluation_limit"};
    if (options_.time_limit_ms > 0 &&
        std::chrono::steady_clock::now() - started_ >= std::chrono::milliseconds(options_.time_limit_ms))
        throw BudgetReached{"time_limit"};
}

std::optional<Solution> Search::assess(const Routes& routes) {
    budget();
    ++statistics.evaluations;
    return evaluate(problem_, routes);
}

void Search::remember(const Solution& solution) {
    if (!best_ || solution.score < best_->score) {
        best_ = solution;
        ++statistics.improvements;
    }
}

std::vector<int> Search::unassigned(const Solution& solution) const {
    std::vector<bool> used(problem_.requests.size());
    for (const auto& route : solution.routes) for (auto r : route) used[r] = true;
    std::vector<int> result;
    for (std::size_t r = 0; r < used.size(); ++r) if (!used[r]) result.push_back(static_cast<int>(r));
    std::stable_sort(result.begin(), result.end(), [&](int a, int b) {
        const auto& x = problem_.requests[a];
        const auto& y = problem_.requests[b];
        if (problem_.work_type_priority && x.priority != y.priority) return x.priority < y.priority;
        return std::tie(x.window_end, x.window_start, x.source_order) <
               std::tie(y.window_end, y.window_start, y.source_order);
    });
    return result;
}

std::optional<Solution> Search::insert(const Solution& solution, int request, int forbidden) {
    std::optional<Solution> best;
    for (std::size_t e = 0; e < solution.routes.size(); ++e) {
        if (static_cast<int>(e) == forbidden || !compatible(problem_, e, request)) continue;
        for (std::size_t pos = problem_.frozen[e].size(); pos <= solution.routes[e].size(); ++pos) {
            auto routes = solution.routes;
            routes[e].insert(routes[e].begin() + pos, request);
            auto candidate = assess(routes);
            if (candidate && (!best || candidate->score < best->score)) {
                best = std::move(candidate);
                remember(*best);
            }
        }
    }
    return best;
}

Solution Search::repair(Solution solution, std::vector<int> order, int forbidden) {
    bool changed = true;
    while (changed) {
        changed = false;
        std::vector<int> remaining;
        for (int r : order) {
            auto candidate = insert(solution, r, forbidden);
            if (candidate) {
                solution = std::move(*candidate);
                changed = true;
            } else remaining.push_back(r);
        }
        order = std::move(remaining);
    }
    return solution;
}

Solution Search::eliminate(Solution solution) {
    std::vector<int> engineers(solution.routes.size());
    std::iota(engineers.begin(), engineers.end(), 0);
    std::stable_sort(engineers.begin(), engineers.end(), [&](int a, int b) {
        return solution.routes[a].size() < solution.routes[b].size();
    });
    for (auto e : engineers) {
        if (solution.routes[e].empty() || !problem_.frozen[e].empty()) continue;
        auto routes = solution.routes;
        auto removed = routes[e];
        routes[e].clear();
        auto candidate = assess(routes);
        if (!candidate) continue;
        std::stable_sort(removed.begin(), removed.end(), [&](int a, int b) {
            return problem_.requests[a].window_end < problem_.requests[b].window_end;
        });
        // Operate on a copy: unsuccessful removal must never lose assignments.
        auto repaired = repair(std::move(*candidate), removed, e);
        if (repaired.score < solution.score) {
            solution = std::move(repaired);
            remember(solution);
        }
    }
    return solution;
}

Solution Search::regret_repair(Solution solution) {
    // Recompute the fewest feasible engineers after each insertion. The scarce
    // resource is an engineer with room in the window, not merely a matching skill.
    while (true) {
        std::optional<Solution> chosen;
        std::tuple<int, int, Int, Int, int> chosen_key;
        for (const auto r : unassigned(solution)) {
            std::vector<Solution> choices;
            for (std::size_t e = 0; e < solution.routes.size(); ++e) {
                if (!compatible(problem_, e, r)) continue;
                std::optional<Solution> best;
                for (std::size_t pos = problem_.frozen[e].size(); pos <= solution.routes[e].size(); ++pos) {
                    auto routes = solution.routes;
                    routes[e].insert(routes[e].begin() + pos, r);
                    auto candidate = assess(routes);
                    if (candidate && (!best || candidate->score < best->score)) {
                        best = std::move(candidate);
                        remember(*best);
                    }
                }
                if (best) choices.push_back(std::move(*best));
            }
            if (choices.empty()) continue;
            std::sort(choices.begin(), choices.end(), [](const auto& a, const auto& b) { return a.score < b.score; });
            const auto cost = choices.front().score.size() - (problem_.compact_schedule ? 3 : 2);
            const Int regret = choices.size() > 1 ? choices[1].score[cost] - choices[0].score[cost] : 0;
            const auto& request = problem_.requests[r];
            const auto key = std::make_tuple(problem_.work_type_priority ? request.priority : 0,
                static_cast<int>(choices.size()), -regret, request.window_end, request.source_order);
            if (!chosen || key < chosen_key) {
                chosen_key = key;
                chosen = std::move(choices.front());
            }
        }
        if (!chosen) return solution;
        solution = std::move(*chosen);
    }
}

Solution Search::augment(Solution solution) {
    // Make room for an unassigned job and reinsert the displaced job as one move.
    // Its intermediate route may cost an extra engineer: only the complete score matters.
    for (const auto r : unassigned(solution)) {
        for (std::size_t e = 0; e < solution.routes.size(); ++e) {
            if (!compatible(problem_, e, r)) continue;
            for (std::size_t pos = problem_.frozen[e].size(); pos < solution.routes[e].size(); ++pos) {
                const auto displaced = solution.routes[e][pos];
                const auto owner = problem_.reference_engineer[displaced];
                if (problem_.preserve && owner >= 0 && !problem_.engineers[owner].unavailable) continue;
                auto reduced = solution.routes;
                reduced[e].erase(reduced[e].begin() + pos);
                for (std::size_t dest = problem_.frozen[e].size(); dest <= reduced[e].size(); ++dest) {
                    auto routes = reduced;
                    routes[e].insert(routes[e].begin() + dest, r);
                    auto candidate = assess(routes);
                    if (!candidate) continue;
                    remember(*candidate);
                    auto restored = insert(*candidate, displaced);
                    if (restored && restored->score < solution.score) return std::move(*restored);
                    if (candidate->score < solution.score) return std::move(*candidate);
                }
            }
        }
    }
    return solution;
}

Solution Search::local_search(Solution solution) {
    for (int step = 0; step < 12; ++step) {
        const auto old_score = solution.score;
        solution = repair(solution, unassigned(solution));
        if (options_.use_eliminate && !problem_.day_policy) solution = eliminate(std::move(solution));
        if (!options_.use_local_search) break;
        auto augmented = augment(solution);
        if (augmented.score < solution.score) {
            solution = std::move(augmented);
            remember(solution);
            continue;
        }
        bool moved = false;
        // Relocate within a route or between routes, preserving all jobs.
        for (std::size_t from = 0; from < solution.routes.size() && !moved; ++from) {
            for (std::size_t pos = problem_.frozen[from].size(); pos < solution.routes[from].size() && !moved; ++pos) {
                auto routes = solution.routes;
                const int r = routes[from][pos];
                routes[from].erase(routes[from].begin() + pos);
                // Do not assume deleting a visit remains feasible: matrices can be nonmetric.
                for (std::size_t to = 0; to < routes.size() && !moved; ++to) {
                    if (!compatible(problem_, to, r)) continue;
                    for (std::size_t dest = problem_.frozen[to].size(); dest <= routes[to].size() && !moved; ++dest) {
                        auto candidate_routes = routes;
                        candidate_routes[to].insert(candidate_routes[to].begin() + dest, r);
                        auto candidate = assess(candidate_routes);
                        if (candidate && candidate->score < solution.score) {
                            solution = std::move(*candidate);
                            remember(solution);
                            moved = true;
                        }
                    }
                }
            }
        }
        if (moved) continue;
        // Swap also explores order reversals and compatible cross-engineer exchanges.
        for (std::size_t a = 0; a < solution.routes.size() && !moved; ++a)
            for (std::size_t i = problem_.frozen[a].size(); i < solution.routes[a].size() && !moved; ++i)
                for (std::size_t b = a; b < solution.routes.size() && !moved; ++b)
                    for (std::size_t j = b == a ? i + 1 : problem_.frozen[b].size(); j < solution.routes[b].size() && !moved; ++j) {
                        auto routes = solution.routes;
                        std::swap(routes[a][i], routes[b][j]);
                        auto candidate = assess(routes);
                        if (candidate && candidate->score < solution.score) {
                            solution = std::move(*candidate);
                            remember(solution);
                            moved = true;
                        }
                    }
        if (solution.score == old_score) break;
    }
    return solution;
}

Solution Search::solve(const Routes& initial) {
    // Validate and retain the initial incumbent even with a zero search budget.
    best_ = evaluate(problem_, initial);
    if (!best_) throw std::invalid_argument("Initial plan is infeasible");
    try {
        for (int round = 0; round < options_.rounds; ++round) {
            budget();
            Solution current = *best_;
            if (!problem_.preserve && (round < 3 || round % 4 == 0) && !(problem_.has_state && round == 0)) {
                Routes prefixes(problem_.engineers.size());
                for (std::size_t e = 0; e < prefixes.size(); ++e)
                    for (const auto& visit : problem_.frozen[e]) prefixes[e].push_back(visit.request);
                const auto fresh = evaluate(problem_, prefixes);
                if (!fresh) throw std::invalid_argument("Infeasible frozen prefix");
                current = *fresh;
            } else if (options_.use_destroy && round > 0) {
                auto routes = current.routes;
                std::vector<int> assigned;
                for (std::size_t e = 0; e < routes.size(); ++e)
                    for (std::size_t pos = problem_.frozen[e].size(); pos < routes[e].size(); ++pos) {
                        const auto r = routes[e][pos];
                        const auto owner = problem_.reference_engineer[r];
                        if (!problem_.preserve || owner < 0 || problem_.engineers[owner].unavailable) assigned.push_back(r);
                    }
                // Vary the neighbourhood: isolated moves cannot undo a poor allocation
                // of the few engineers who can serve early customer windows.
                const auto seed = assigned.empty() ? -1 : assigned[random_() % assigned.size()];
                const auto count = std::min<std::size_t>(assigned.size(), 3 + (round % 5) * 3);
                std::vector<int> removal;
                if (round % 3 == 0 && seed >= 0) {
                    for (const auto& route : routes) {
                        if (std::find(route.begin(), route.end(), seed) == route.end()) continue;
                        for (const auto r : route)
                            if (std::find(assigned.begin(), assigned.end(), r) != assigned.end()) removal.push_back(r);
                    }
                }
                for (std::size_t i = assigned.size(); i > 1; --i)
                    std::swap(assigned[i - 1], assigned[random_() % i]);
                for (const auto r : assigned) {
                    if (removal.size() >= count) break;
                    if (std::find(removal.begin(), removal.end(), r) == removal.end()) removal.push_back(r);
                }
                for (const auto r : removal) {
                    for (auto& route : routes) route.erase(std::remove(route.begin(), route.end(), r), route.end());
                }
                const auto reduced = assess(routes);
                if (reduced) current = *reduced;
            }
            auto order = unassigned(current);
            if (round >= 2) {
                // Explicit Fisher-Yates for reproducible RNG usage, independent of std::shuffle.
                for (std::size_t i = order.size(); i > 1; --i) std::swap(order[i - 1], order[random_() % i]);
            }
            current = round == 1 && !problem_.has_state
                ? regret_repair(std::move(current)) : repair(std::move(current), std::move(order));
            current = local_search(std::move(current));
            remember(current);
            ++statistics.rounds_completed;
        }
    } catch (const BudgetReached& limit) {
        statistics.status = limit.status;
    }
    return *best_;
}
} // namespace dispatch
