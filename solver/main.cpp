#include "solver.hpp"
#include "third_party/json.hpp"

#include <algorithm>
#include <iostream>
#include <map>
#include <set>
#include <stdexcept>

using Json = nlohmann::json;
using namespace dispatch;

namespace {
Int integer(const Json& value, Int low, Int high) {
    if (!value.is_number_integer() || value.is_boolean()) throw std::invalid_argument("Expected integer");
    if (value.is_number_unsigned() && value.get<std::uint64_t>() > static_cast<std::uint64_t>(high))
        throw std::invalid_argument("Integer out of range");
    const auto number = value.get<Int>();
    if (number < low || number > high) throw std::invalid_argument("Integer out of range");
    return number;
}

int profile(const std::string& name) {
    static const std::map<std::string, int> names{{"car",0},{"walk",1},{"bicycle",2},{"public_transport_approx",3}};
    return names.at(name);
}

int skill(const std::string& name) {
    static const std::map<std::string, int> names{{"installation",1},{"local",2},{"emergency",4}};
    return names.at(name);
}

Problem parse_problem(const Json& envelope) {
    if (envelope.at("protocol_version") != "1.0") throw std::invalid_argument("Unsupported protocol");
    const auto& input = envelope.at("scenario");
    if (input.at("schema_version") != "1.0" || input.at("timezone") != "Europe/Moscow")
        throw std::invalid_argument("Unsupported scenario version or timezone");
    Problem p;
    p.id = input.at("id").get<std::string>();
    p.input_hash = envelope.at("input_hash").get<std::string>();
    if (input.contains("schedule_policy") && !input.at("schedule_policy").is_null()) {
        if (input.at("schedule_policy") != "compact_v1")
            throw std::invalid_argument("Unsupported schedule policy");
        p.compact_schedule = true;
    }
    const bool equipment_policy = input.contains("equipment_policy") && !input.at("equipment_policy").is_null();
    if (equipment_policy && input.at("equipment_policy") != "client_devices_v1")
        throw std::invalid_argument("Unsupported equipment policy");
    if (input.contains("objective_policy") && !input.at("objective_policy").is_null()) {
        if (input.at("objective_policy") != "work_type_priority_v1")
            throw std::invalid_argument("Unsupported objective policy");
        p.work_type_priority = true;
    }
    if (input.at("requests").size() > 500 || input.at("engineers").size() > 100)
        throw std::invalid_argument("Scenario exceeds prototype limits");
    std::set<std::string> request_ids, engineer_ids;
    std::set<int> orders;
    std::vector<std::string> locations;
    for (const auto& r : input.at("requests")) {
        const auto id = r.at("id").get<std::string>();
        const int order = integer(r.at("source_order"), 0, 1000000);
        if (id.empty() || !request_ids.insert(id).second || !orders.insert(order).second || r.at("zone") != p.id)
            throw std::invalid_argument("Invalid request identity or zone");
        const Int start = integer(r.at("window_start_s"),0,86399);
        const Int end = integer(r.at("window_end_s"),start,86399);
        int priority = 2;
        bool urgent = r.at("urgent").get<bool>();
        if (p.work_type_priority) {
            // Canonical classification is validated at the Python scenario boundary.
            // Missing classifications must be explicit nulls, never guessed from skills.
            const auto& type = r.at("work_type");
            if (!type.is_null()) {
                static const std::map<std::string,int> priorities{
                    {"emergency",0},{"installation",1},{"repair",2},{"additional",2}};
                priority = priorities.at(type.get<std::string>());
            }
            urgent = priority == 0;
        }
        p.requests.push_back({id,start,end,integer(r.at("service_s"),1,86400),integer(r.value("released_at_s",Json(0)),0,86399),
            skill(r.at("skill")),r.at("required_profile").is_null() ? -1 : profile(r.at("required_profile")),
            order,urgent,priority});
        if (equipment_policy) {
            const auto kind = r.at("kind").get<std::string>();
            p.requests.back().equipment = {kind == "Подключение", kind == "Дозаказ"};
        }
        locations.push_back(r.at("location_id").get<std::string>());
    }
    p.matrices.resize(4);
    std::vector<std::map<std::string,int>> indices(4);
    for (const auto& [name, m] : input.at("matrices").items()) {
        const int mode = profile(name);
        const auto n = m.at("locations").size();
        if (n > 1000) throw std::invalid_argument("Matrix exceeds prototype limit");
        auto& matrix = p.matrices[mode];
        matrix.size = static_cast<int>(n);
        for (std::size_t i = 0; i < n; ++i) {
            if (!indices[mode].emplace(m.at("locations").at(i).get<std::string>(),static_cast<int>(i)).second)
                throw std::invalid_argument("Duplicate location");
        }
        for (const auto& location : locations) matrix.request_locations.push_back(indices[mode].at(location));
        if (m.at("duration_s").size() != n || m.at("distance_m").size() != n)
            throw std::invalid_argument("Invalid matrix dimensions");
        for (std::size_t i = 0; i < n; ++i) {
            if (m.at("duration_s").at(i).size() != n || m.at("distance_m").at(i).size() != n)
                throw std::invalid_argument("Invalid matrix row");
            for (std::size_t j = 0; j < n; ++j) {
                const auto& t = m.at("duration_s").at(i).at(j);
                const auto& d = m.at("distance_m").at(i).at(j);
                if (t.is_null() != d.is_null()) throw std::invalid_argument("Inconsistent unreachable edge");
                const auto duration = t.is_null() ? -1 : integer(t,0,1000000000);
                const auto distance = d.is_null() ? -1 : integer(d,0,1000000000);
                if (i == j && (duration != 0 || distance != 0)) throw std::invalid_argument("Nonzero diagonal");
                matrix.duration.push_back(duration);
                matrix.distance.push_back(distance);
            }
        }
    }
    for (const auto& e : input.at("engineers")) {
        const auto id = e.at("id").get<std::string>();
        if (id.empty() || !engineer_ids.insert(id).second || e.at("zone") != p.id)
            throw std::invalid_argument("Invalid engineer identity or zone");
        const auto mode = profile(e.at("profile"));
        int skills = 0;
        for (const auto& s : e.at("skills")) skills |= skill(s);
        if (!skills) throw std::invalid_argument("Empty skills");
        const auto start = integer(e.at("shift_start_s"),0,86399);
        p.engineers.push_back({id,skills,mode,indices[mode].at(e.at("start_location_id").get<std::string>()),
                              start,integer(e.at("shift_end_s"),start+1,86400)});
    }
    p.frozen.resize(p.engineers.size());
    p.reference_engineer.assign(p.requests.size(), -1);
    p.reference_order.assign(p.requests.size(), -1);
    p.reference_start.assign(p.requests.size(), 0);
    p.eligible.assign(p.requests.size(), true);
    p.protected_routes.resize(p.engineers.size());
    if (input.contains("roster") && !input.at("roster").is_null()) {
        p.day_policy = true;
        std::set<std::string> roster;
        for (const auto& id : input.at("roster")) {
            const auto name = id.get<std::string>();
            if (!engineer_ids.contains(name) || !roster.insert(name).second) throw std::invalid_argument("Invalid roster");
        }
        for (auto& e : p.engineers) e.on_shift = roster.contains(e.id);
    }
    p.equipment_active = input.contains("equipment_issued") && !input.at("equipment_issued").is_null();
    if (p.equipment_active) {
        if (!equipment_policy || !p.day_policy)
            throw std::invalid_argument("Equipment issuance requires policy and roster");
        const auto& issued = input.at("equipment_issued");
        if (!issued.is_object() || issued.size() != p.engineers.size())
            throw std::invalid_argument("Equipment issuance required for every engineer");
        for (auto& e : p.engineers) {
            const auto& stock = issued.at(e.id);
            if (!stock.is_object() || stock.size() != 2)
                throw std::invalid_argument("Invalid equipment stock");
            e.equipment_issued = {integer(stock.at("routers"), 0, 500), integer(stock.at("set_top_boxes"), 0, 500)};
            if (!e.on_shift && (e.equipment_issued[0] || e.equipment_issued[1]))
                throw std::invalid_argument("Equipment issued outside roster");
        }
    } else if (equipment_policy && p.day_policy) {
        throw std::invalid_argument("Published day requires equipment issuance");
    }
    if (input.contains("planning_state") && !input.at("planning_state").is_null()) {
        const auto& state = input.at("planning_state");
        p.at = integer(state.at("at_s"),0,86399);
        p.has_state = true;
        std::map<std::string,int> requests, engineers;
        for (std::size_t r = 0; r < p.requests.size(); ++r) requests.emplace(p.requests[r].id,r);
        for (std::size_t e = 0; e < p.engineers.size(); ++e) engineers.emplace(p.engineers[e].id,e);
        for (const auto& id : state.at("unavailable_engineers")) p.engineers.at(engineers.at(id.get<std::string>())).unavailable = true;
        std::set<int> seen;
        for (const auto& [id, visits] : state.at("frozen_routes").items()) {
            auto& prefix = p.frozen.at(engineers.at(id));
            for (const auto& v : visits) {
                const auto r = requests.at(v.at("request_id").get<std::string>());
                if (!seen.insert(r).second) throw std::invalid_argument("Duplicate frozen request");
                const Visit visit{r,integer(v.at("arrival_s"),0,86400),integer(v.at("start_s"),0,86400),
                    integer(v.at("finish_s"),0,86400),integer(v.at("travel_s"),0,1000000000),
                    integer(v.at("waiting_s"),0,86400),integer(v.at("distance_m"),0,1000000000)};
                if (visit.arrival - visit.travel >= p.at && visit.start > p.at)
                    throw std::invalid_argument("Future journey cannot be frozen");
                prefix.push_back(visit);
            }
        }
        if (state.contains("policy") && !state.at("policy").is_null()) {
            if (!input.contains("roster") || input.at("roster").is_null()) throw std::invalid_argument("Day policy requires roster");
            const auto& policy = state.at("policy");
            const auto mode = policy.at("mode").get<std::string>();
            if (mode != "preserve" && mode != "flexible") throw std::invalid_argument("Invalid replanning mode");
            p.day_policy = true;
            p.preserve = mode == "preserve";
            p.max_delay = integer(policy.at("max_delay_s"),0,7200);
            p.eligible.assign(p.requests.size(), false);
            for (const auto& id : policy.at("eligible_request_ids")) {
                const auto r = requests.at(id.get<std::string>());
                if (p.eligible[r]) throw std::invalid_argument("Duplicate eligible request");
                p.eligible[r] = true;
            }
            for (const auto& [id, visits] : policy.at("reference_routes").items()) {
                const auto e = engineers.at(id);
                int order = 0;
                for (const auto& v : visits) {
                    const auto r = requests.at(v.at("request_id").get<std::string>());
                    if (p.reference_engineer[r] >= 0 || !p.eligible[r]) throw std::invalid_argument("Invalid reference request");
                    p.reference_engineer[r] = e;
                    p.reference_order[r] = order++;
                    p.reference_start[r] = integer(v.at("start_s"),0,86400);
                    if (!p.engineers[e].unavailable) p.protected_routes[e].push_back(r);
                }
            }
        }
    }
    return p;
}

Routes parse_routes(const Problem& p, const Json& input) {
    if (!input.is_array() || input.size() != p.engineers.size()) throw std::invalid_argument("One route per engineer required");
    std::map<std::string,int> ids;
    for (std::size_t r=0; r<p.requests.size(); ++r) ids.emplace(p.requests[r].id,static_cast<int>(r));
    Routes result;
    for (const auto& route : input) {
        if (!route.is_array()) throw std::invalid_argument("Route must be an array");
        std::vector<int> values;
        for (const auto& id : route) values.push_back(ids.at(id.get<std::string>()));
        result.push_back(std::move(values));
    }
    return result;
}

Json plan_json(const Problem& p, const Solution& solution) {
    Json routes = Json::array(), rejections = Json::array();
    std::vector<bool> assigned(p.requests.size());
    Int waiting = 0, service = 0, active = 0, distance = 0, travel = 0;
    for (std::size_t e = 0; e < p.engineers.size(); ++e) {
        Json visits = Json::array();
        const auto& evaluation = solution.evaluations[e];
        for (const auto& v : evaluation.visits) {
            assigned[v.request] = true;
            visits.push_back({{"request_id",p.requests[v.request].id},{"arrival_s",v.arrival},
                {"start_s",v.start},{"finish_s",v.finish},{"travel_s",v.travel},{"waiting_s",v.waiting},{"distance_m",v.distance}});
        }
        routes.push_back({{"engineer_id",p.engineers[e].id},{"visits",visits},{"distance_m",evaluation.distance}});
        waiting += evaluation.waiting;
        service += evaluation.service;
        active += !evaluation.visits.empty();
        distance += evaluation.distance;
        travel += evaluation.travel;
    }
    for (std::size_t r=0; r<p.requests.size(); ++r) if (!assigned[r]) {
        Json checks = Json::object();
        bool possible_insertion = false;
        for (std::size_t e=0; e<p.engineers.size(); ++e) {
            const auto& request = p.requests[r];
            const auto& engineer = p.engineers[e];
            if (!engineer.on_shift) { checks[engineer.id] = "off_shift"; continue; }
            if (engineer.unavailable) { checks[engineer.id] = "unavailable"; continue; }
            if (!(engineer.skills & request.skill)) { checks[engineer.id] = "skill"; continue; }
            if (request.profile >= 0 && request.profile != engineer.profile) { checks[engineer.id] = "transport"; continue; }
            std::set<std::string> failures;
            bool feasible = false;
            for (std::size_t pos=p.frozen[e].size(); pos<=solution.routes[e].size(); ++pos) {
                auto route = solution.routes[e];
                route.insert(route.begin()+pos,static_cast<int>(r));
                std::string failure;
                if (evaluate_route(p,e,route,&failure)) { feasible = true; break; }
                failures.insert(failure);
            }
            possible_insertion |= feasible;
            checks[engineer.id] = feasible ? "search_budget" : failures.size() == 1 ? *failures.begin() : "no_feasible_insertion";
        }
        const std::string reason = p.engineers.empty() ? "no_engineers" : possible_insertion ? "search_budget" : "no_feasible_insertion";
        const std::string explanation = p.engineers.empty() ? "В сценарии нет инженеров." : possible_insertion
            ? "Есть допустимая вставка, но бюджет поиска закончился до её принятия. Увеличьте бюджет расчёта."
            : "Не найдено допустимой вставки в текущие маршруты. Изменение других назначений может помочь; общая невозможность не доказана.";
        rejections.push_back({{"request_id",p.requests[r].id},{"reason",reason},
            {"explanation",explanation},
            {"checks",checks}});
    }
    const auto assigned_count = std::count(assigned.begin(), assigned.end(), true);
    return {{"schema_version","1.0"},{"scenario_id",p.id},{"input_hash",p.input_hash},
        {"algorithm","cpp_insertion_v1"},{"routes",routes},{"unassigned",rejections},
        {"metrics",{{"total",p.requests.size()},{"assigned",assigned_count},{"unassigned",rejections.size()},
            {"active_engineers",active},{"distance_m",distance},{"travel_s",travel},{"waiting_s",waiting},{"service_s",service}}}};
}
} // namespace

int main() {
    try {
        std::string input;
        char buffer[8192];
        while (std::cin.read(buffer,sizeof(buffer)) || std::cin.gcount()) {
            input.append(buffer,static_cast<std::size_t>(std::cin.gcount()));
            if (input.size() > 32*1024*1024) throw std::invalid_argument("Input exceeds 32 MiB");
        }
        const auto envelope = Json::parse(input);
        const auto problem = parse_problem(envelope);
        const auto initial = parse_routes(problem,envelope.at("initial_routes"));
        const auto& config = envelope.at("options");
        Options options;
        options.rounds = integer(config.at("rounds"),0,4096);
        options.max_evaluations = integer(config.at("max_evaluations"),0,100000000);
        options.time_limit_ms = integer(config.at("time_limit_ms"),0,120000);
        options.seed = integer(config.at("seed"),0,4294967295LL);
        options.use_local_search = config.at("use_local_search").get<bool>();
        options.use_eliminate = config.at("use_eliminate").get<bool>();
        options.use_destroy = config.at("use_destroy").get<bool>();
        const auto started = std::chrono::steady_clock::now();
        Statistics statistics;
        std::optional<Solution> result;
        if (envelope.at("mode") == "evaluate") {
            result = evaluate(problem,initial);
            statistics.status = "evaluated";
        } else if (envelope.at("mode") == "optimize") {
            Search search(problem,options);
            result = search.solve(initial);
            statistics = search.statistics;
        } else throw std::invalid_argument("Unknown solver mode");
        if (!result) throw std::invalid_argument("Infeasible routes");
        const auto elapsed = std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
        std::cout << Json{{"protocol_version","1.0"},{"plan",plan_json(problem,*result)},
            {"score",result->score},{"search",{{"status",statistics.status},{"evaluations",statistics.evaluations},
                {"improvements",statistics.improvements},{"rounds_completed",statistics.rounds_completed},{"elapsed_ms",elapsed}}}}.dump() << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << Json{{"error",error.what()}}.dump() << '\n';
        return 2;
    }
}
