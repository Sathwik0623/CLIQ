import type { TopologyLink, TopologyNode } from './api';

export type LabBlueprintNode = {
  id: string;
  label: string;
  node_definition: string;
  image_definition?: string | null;
  x: number;
  y: number;
  vcpus?: number | null;
  ram_mb?: number | null;
};

export type LabBlueprintLink = {
  id: string;
  node_a: string;
  node_b: string;
  interface_a: string;
  interface_b: string;
};

export type LabBlueprint = {
  version: string;
  title: string;
  nodes: LabBlueprintNode[];
  links: LabBlueprintLink[];
  notes?: string | null;
};

export type ValidationIssue = {
  code: string;
  message: string;
  node_id?: string | null;
  link_id?: string | null;
};

export type ResourceEstimate = {
  total_nodes: number;
  total_links: number;
  total_vcpus: number;
  total_ram_mb: number;
};

export type ValidationResult = {
  valid: boolean;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
  resource_estimate: ResourceEstimate;
};

export type PlanResponse = {
  blueprint: LabBlueprint;
  warnings: string[];
  provider: string;
  model: string;
  mock: boolean;
  interfaces_verified?: boolean;
  live?: boolean;
};

export type PreviewResponse = {
  title: string;
  version: string;
  nodes: TopologyNode[];
  links: TopologyLink[];
  summary: {
    total_nodes: number;
    total_links: number;
    node_definitions: Record<string, number>;
    total_vcpus: number;
    total_ram_mb: number;
  };
  notes?: string | null;
};

export type ApprovalTicket = {
  ticket_id: string;
  lab_title: string;
  lab_title_original?: string | null;
  blueprint_hash: string;
  blueprint: LabBlueprint;
  validation: ValidationResult;
  live: boolean;
  expires_at: number;
};

export type DeployStep = {
  seq: number;
  kind: string;
  label: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped';
  controller_id?: string | null;
  detail: string;
};

export type DeployJob = {
  job_id: string;
  ticket_id: string;
  lab_title: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  steps: DeployStep[];
  lab_id?: string | null;
  lab_state?: string | null;
  error: string;
  blueprint_hash: string;
  interface_map: Record<string, string>;
  created_at: number;
  updated_at: number;
};

export type DeployTopology = {
  lab_id: string;
  lab_title: string;
  lab_state?: string | null;
  nodes: TopologyNode[];
  links: TopologyLink[];
};

export type LabDisplayState = 'Defined' | 'Starting' | 'Stopping' | 'Running' | 'Failed' | 'Unknown';

export type AvailableLab = {
  id: string;
  title: string;
  state: string;
  state_display: LabDisplayState;
  owner: string;
  device_count?: number | null;
  converged?: boolean | null;
  nodes_booted?: number | null;
  nodes_active?: number | null;
  nodes_total?: number | null;
  error?: string;
};

export type AvailableLabsResponse = {
  count: number;
  labs: AvailableLab[];
};

export type StartOperation = {
  op_id: string;
  lab_id: string;
  lab_title: string;
  kind: 'start' | 'stop';
  status: 'starting' | 'stopping' | 'running' | 'stopped' | 'failed';
  state_display: LabDisplayState;
  error: string;
  raw_state: string;
  converged?: boolean | null;
  nodes_booted?: number | null;
  nodes_active?: number | null;
  nodes_total?: number | null;
  detail: string;
  created_at: number;
  updated_at: number;
};
