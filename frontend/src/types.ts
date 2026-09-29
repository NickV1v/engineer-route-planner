export interface Zone {
  id: string;
  date: string;
  requests: number;
  rejected: number;
  office_address: string;
  default_engineer_count: number;
}
export interface WorkRequest {
  display_address?: string;
  location_id: string;
  id: string;
  source_order: number;
  address: string;
  kind: string;
  subtype: string;
  window_start_s: number;
  window_end_s: number;
  service_s: number;
  skill: string;
  urgent: boolean;
  work_type?: 'emergency' | 'installation' | 'repair' | 'additional' | null;
}
export interface Scenario {
  upload_id?: string | null;
  objective_policy?: 'work_type_priority_v1' | null;
  schedule_policy?: 'compact_v1' | null;
  equipment_policy?: 'client_devices_v1' | null;
  equipment_issued?: Record<string, { routers: number; set_top_boxes: number }> | null;
  office_location_id?: string;
  geography?: Record<string, GeoLocation>;
  id: string;
  date: string;
  office_address: string;
  requests: WorkRequest[];
  import_report: { accepted: number; rejected: number; source_file: string; source_sha256: string };
}
export interface Engineer {
  id: string;
  name?: string | null;
  start_location_id?: string;
  skills: string[];
  profile: string;
  shift_start_s: number;
  shift_end_s: number;
}
export interface Visit {
  request_id: string;
  arrival_s: number;
  start_s: number;
  finish_s: number;
  travel_s: number;
  waiting_s: number;
  distance_m: number;
}
export interface Route {
  engineer_id: string;
  visits: Visit[];
  distance_m: number;
}
export interface Rejection {
  request_id: string;
  reason: string;
  explanation: string;
  checks: Record<string, string>;
}
export interface Plan {
  scenario_id: string;
  input_hash: string;
  algorithm: string;
  routes: Route[];
  unassigned: Rejection[];
  metrics: {
    total: number;
    assigned: number;
    unassigned: number;
    active_engineers: number;
    distance_m: number;
    travel_s: number;
    waiting_s: number;
    service_s: number;
  };
}
export interface Run {
  performance?: {
    cache_scope: 'calculation';
    network_load_ms: number;
    routing_ms: number;
    archive_write_ms: number;
    solver_ms: number;
    queue_wait_ms: number;
    road_searches: number;
    public_source_searches: number;
    cache_hits: number;
  };
  journeys?: { plan: Record<string, Journey[]>; baseline?: Record<string, Journey[]> };
  geography_updated?: boolean;
  publication?: boolean;
  staffing?: {
    status: 'draft' | 'active';
    engineer_ids: string[];
    scheduled: number;
    unavailable: number;
    with_work: number;
    reserve_ids: string[];
  };
  session_id: string;
  version: number;
  scenario: Scenario;
  event?: DayEvent;
  event_at_s?: number;
  frozen_request_ids?: string[];
  unavailable_engineers?: string[];
  changes?: {
    request_id: string;
    address: string;
    changes: string[];
    before: Assignment | null;
    after: Assignment | null;
  }[];
  plan: Plan;
  engineers: Engineer[];
  manifest: {
    excluded_request_ids?: string[];
    excluded_requests?: WorkRequest[];
    upload_id?: string;
    engineer_inputs?: EngineerInput[];
    assumptions: string[];
    transport: string;
    routing?: {
      package_hash: string;
      networks: Record<string, string>;
      statuses: Record<string, Record<string, number>>;
    };
  };
  validation: { valid: boolean };
  baseline?: Plan;
  search?: {
    status: string;
    elapsed_ms: number;
    evaluations: number;
    rounds_completed: number;
  };
  comparison?: {
    scope?: 'morning';
    input_hash?: string;
    objective_components?: string[];
    improved: boolean;
    assigned_delta: number;
    engineers_delta: number;
    distance_delta_m: number;
    same_assigned_set: boolean;
    baseline_score: number[];
    optimized_score: number[];
  };
}

export interface JourneyLeg {
  mode: 'car' | 'walk' | 'bicycle' | 'metro' | 'bus' | 'tram' | 'train' | 'wait';
  from_id: string;
  to_id: string;
  from_label?: string | null;
  to_label?: string | null;
  duration_s: number;
  distance_m: number;
  geometry: { lat: number; lon: number }[];
  line: string | null;
  quality: 'network' | 'estimated';
  geometry_quality: 'network' | 'estimated';
}
export interface Journey {
  request_id: string;
  from_id: string;
  to_id: string;
  status: string;
  duration_s: number;
  distance_m: number;
  walking_s: number;
  boarding_wait_s: number;
  legs: JourneyLeg[];
}
export interface RoutingStatus {
  mode: 'real' | 'synthetic';
  ready: boolean;
  progress: { stage: string; completed: number; total: number } | null;
}

export interface EngineerInput {
  id: string;
  name?: string | null;
  skills: string[];
  profile: string;
  shift_start_s: number;
  shift_end_s: number;
}
export interface EngineerFile {
  engineers: EngineerInput[];
  report: { source_file: string; source_sha256: string; accepted: number };
}
export interface UploadedData {
  upload_id: string;
  scenario: Scenario;
  engineers: EngineerInput[];
  engineer_count: number;
}
export interface AddressReview {
  upload_id: string;
  office_location_id: string;
  geography: Record<string, GeoLocation>;
  requests: WorkRequest[];
  excluded_request_ids: string[];
}
export interface CalculationProgress {
  id: string;
  stage: string;
  percent: number;
  status: 'running' | 'completed' | 'failed' | 'needs_input';
}

export interface GeoPoint {
  lat: number;
  lon: number;
  label: string;
  precision: 'house' | 'street' | 'locality' | 'manual' | 'unknown';
  source: string;
}
export interface GeoLocation {
  location_id: string;
  address: string;
  revision: number;
  status: 'matched' | 'review' | 'missing' | 'manual';
  point: GeoPoint | null;
  candidates: GeoPoint[];
  query: string;
  updated_at: string;
  note: string;
}

export interface AddressSuggestion {
  id: string;
  label: string;
  provider: 'catalog' | 'dadata';
  precision: 'house' | 'street' | 'locality' | 'unknown';
  house: string;
  notice: string;
  point: GeoPoint | null;
}

export interface Assignment {
  engineer_id: string;
  start_s: number;
}
export interface DayEvent {
  mode?: 'preserve' | 'flexible';
  max_delay_s?: number;
  event_id: string;
  expected_version: number;
  at_s: number;
  type: 'add_urgent' | 'cancel' | 'engineer_unavailable';
  request?: {
    id: string;
    address: string;
    kind: string;
    window_start_s: number;
    window_end_s: number;
    required_profile: string | null;
    coordinates?: GeoPoint;
    original_address?: string;
  };
  request_id?: string;
  engineer_id?: string;
}

export interface Preview {
  preview_id: string;
  base_version: number;
  result: Run;
}
