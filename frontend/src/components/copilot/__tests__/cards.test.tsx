/**
 * Card collapse/expand regression tests (UI usability fix).
 *
 * Completed execution and plan cards must collapse by default, expand
 * independently via their own headers, keep approval controls available,
 * and never remove or obscure surrounding conversation messages.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import CopilotExecCard from '../CopilotExecCard';
import ConfigPlanCard from '../ConfigPlanCard';
import type { ConfigPlanView, ExecResultPayload, ToolRun } from '../../../lib/copilotApi';

function execResult(overrides: Partial<ExecResultPayload> = {}): ExecResultPayload {
  return {
    success: true,
    device_id: 'sw1',
    device_label: 'SW1',
    lab_id: 'lab-1',
    platform: 'cisco_ios',
    command: 'show vlan brief',
    send_command: 'show vlan brief',
    stdout: 'VLAN Name\n1 default\n10 Users',
    stderr: '',
    exit_status: 0,
    error: '',
    error_code: '',
    latency_ms: 123.4,
    truncated: false,
    approval_required: false,
    ...overrides,
  };
}

function toolRun(overrides: Partial<ToolRun> = {}): ToolRun {
  return {
    key: 'sw1::show vlan brief',
    lab_id: 'lab-1',
    device_id: 'sw1',
    device_label: 'SW1',
    command: 'show vlan brief',
    status: 'success',
    result: execResult(),
    ...overrides,
  };
}

function planView(overrides: Partial<ConfigPlanView> = {}): ConfigPlanView {
  return {
    plan_id: 'cfg-test1',
    lab_id: 'lab-1',
    lab_title: 'lab-1',
    title: 'VLAN 10 rollout',
    summary: 'Configure VLAN 10 access on SW1 Gi0/1.',
    version: 1,
    fingerprint: 'abc123',
    status: 'completed',
    devices: [
      {
        device_id: 'sw1',
        device_label: 'SW1',
        platform: 'cisco_ios',
        role: 'switch',
        node_type: 'iosvl2',
        steps: [
          {
            step_id: 's1', device_id: 'sw1', command: 'vlan 10',
            purpose: 'create vlan', read_only: false, depends_on: [],
            critical: true, target: {},
          },
        ],
        unverified: false,
      },
    ],
    questions: [],
    connectivity: [],
    assumptions: [],
    evidence: {},
    created_at: 1,
    updated_at: 2,
    blocking_open: [],
    ...overrides,
  };
}

describe('CopilotExecCard', () => {
  it('completed cards collapse by default but keep header metadata', () => {
    render(<CopilotExecCard run={toolRun()} approving={false} onApprove={() => {}} onReject={() => {}} />);
    // Compact header: device, command, status, duration.
    expect(screen.getByText('SW1')).toBeTruthy();
    expect(screen.getByText('show vlan brief')).toBeTruthy();
    expect(screen.getByText('Done')).toBeTruthy();
    expect(screen.getByText('123 ms')).toBeTruthy();
    // Details hidden until expanded.
    expect(screen.queryByText(/Show output/)).toBeNull();
  });

  it('header click expands and collapses output access', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <CopilotExecCard run={toolRun()} approving={false} onApprove={() => {}} onReject={() => {}} />,
    );
    const header = container.querySelector('button[aria-expanded]')!;
    expect(header.getAttribute('aria-expanded')).toBe('false');
    await user.click(header);
    expect(header.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText(/Show output/)).toBeTruthy();
    await user.click(header);
    expect(header.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText(/Show output/)).toBeNull();
  });

  it('multiple cards expand independently', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <>
        <CopilotExecCard run={toolRun({ key: 'a', command: 'show vlan brief' })} approving={false} onApprove={() => {}} onReject={() => {}} />
        <CopilotExecCard run={toolRun({ key: 'b', command: 'show interfaces trunk', result: execResult({ command: 'show interfaces trunk' }) })} approving={false} onApprove={() => {}} onReject={() => {}} />
      </>,
    );
    const headers = Array.from(container.querySelectorAll('button[aria-expanded]'));
    expect(headers).toHaveLength(2);
    await user.click(headers[0]);
    expect(headers[0].getAttribute('aria-expanded')).toBe('true');
    expect(headers[1].getAttribute('aria-expanded')).toBe('false');
    // Exactly one card exposes its output toggle.
    expect(screen.getAllByText(/Show output/)).toHaveLength(1);
  });

  it('failed cards stay identifiable when collapsed', () => {
    render(
      <CopilotExecCard
        run={toolRun({ status: 'error', result: execResult({ success: false, error: 'boom', error_code: 'timeout' }) })}
        approving={false}
        onApprove={() => {}}
        onReject={() => {}}
      />,
    );
    expect(screen.getByText('Failed')).toBeTruthy();
    expect(screen.queryByText(/Show output/)).toBeNull();
  });

  it('running cards stay open with a compact progress indicator', () => {
    const { container } = render(
      <CopilotExecCard run={toolRun({ status: 'running', result: undefined })} approving={false} onApprove={() => {}} onReject={() => {}} />,
    );
    expect(screen.getByText('Running')).toBeTruthy();
    expect(container.querySelector('button[aria-expanded]')!.getAttribute('aria-expanded')).toBe('true');
  });

  it('approval controls are preserved and wired', async () => {
    const user = userEvent.setup();
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(
      <CopilotExecCard
        run={toolRun({ status: 'approval_required', result: execResult({ approval_required: true }) })}
        approving={false}
        onApprove={onApprove}
        onReject={onReject}
      />,
    );
    expect(screen.getByText('Approval required')).toBeTruthy();
    await user.click(screen.getByText('Approve & run'));
    await user.click(screen.getByText('Reject'));
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it('expanding a card does not remove subsequent messages', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <div>
        <CopilotExecCard run={toolRun()} approving={false} onApprove={() => {}} onReject={() => {}} />
        <div data-testid="followup">Summary: VLAN 10 present on Gi0/1.</div>
      </div>,
    );
    await user.click(container.querySelector('button[aria-expanded]')!);
    const followup = screen.getByTestId('followup');
    // Follow-up message still rendered, positioned after the card in flow.
    expect(followup.textContent).toContain('Summary: VLAN 10');
    const card = container.querySelector('button[aria-expanded]')!;
    expect(card.compareDocumentPosition(followup) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe('ConfigPlanCard', () => {
  it('finished plans collapse by default with status in the header', () => {
    render(<ConfigPlanCard plan={planView()} onPlanUpdated={() => {}} />);
    expect(screen.getByText('VLAN 10 rollout')).toBeTruthy();
    expect(screen.getByText('completed')).toBeTruthy();
    expect(screen.getByText('v1')).toBeTruthy();
    expect(screen.getByText('cfg-test1')).toBeTruthy();
    expect(screen.queryByText(/Configure VLAN 10 access/)).toBeNull();
  });

  it('draft plans start open with review controls', () => {
    render(<ConfigPlanCard plan={planView({ status: 'draft' })} onPlanUpdated={() => {}} />);
    expect(screen.getByText(/Configure VLAN 10 access/)).toBeTruthy();
    expect(screen.getByText('Review & prepare')).toBeTruthy();
  });

  it('multiple plan cards expand independently', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <>
        <ConfigPlanCard plan={planView({ plan_id: 'cfg-a', title: 'Plan A' })} onPlanUpdated={() => {}} />
        <ConfigPlanCard plan={planView({ plan_id: 'cfg-b', title: 'Plan B' })} onPlanUpdated={() => {}} />
      </>,
    );
    const headers = within(container as HTMLElement).getAllByRole('button', { expanded: false });
    // Header toggle buttons for the two cards (plus per-card internals are hidden while collapsed).
    expect(headers.length).toBeGreaterThanOrEqual(2);
    await user.click(headers[0]);
    expect(screen.getByText('Plan A')).toBeTruthy();
    expect(screen.getByText('Plan B')).toBeTruthy();
    // Only the first card reveals its summary; the second stays collapsed.
    expect(screen.getAllByText(/Configure VLAN 10 access/)).toHaveLength(1);
  });

  it('new plan versions reopen for review', () => {
    const { rerender } = render(<ConfigPlanCard plan={planView()} onPlanUpdated={() => {}} />);
    expect(screen.queryByText(/Configure VLAN 10 access/)).toBeNull();
    rerender(<ConfigPlanCard plan={planView({ version: 2 })} onPlanUpdated={() => {}} />);
    expect(screen.getByText(/Configure VLAN 10 access/)).toBeTruthy();
    expect(screen.getByText('v2')).toBeTruthy();
  });
});
