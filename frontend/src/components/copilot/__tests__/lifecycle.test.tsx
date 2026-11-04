/**
 * Conversation lifecycle regression tests (UI usability fix).
 *
 * Opening a lab starts fresh (history is never auto-opened), History stays
 * available for explicit selection, New Chat creates a genuinely new
 * conversation, and switching labs resets to a fresh chat.
 */
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import CopilotPanel from '../CopilotPanel';
import type { Conversation, StoredMessage } from '../../../types/copilot';

vi.mock('../../../lib/copilotApi', async (importOriginal) => {
  const orig = await importOriginal<typeof import('../../../lib/copilotApi')>();
  return {
    ...orig,
    fetchCopilotProviders: vi.fn(),
    fetchConversations: vi.fn(),
    fetchConversation: vi.fn(),
    createConversation: vi.fn(),
  };
});

import {
  createConversation,
  fetchConversation,
  fetchConversations,
  fetchCopilotProviders,
} from '../../../lib/copilotApi';

function msg(id: string, role: string, content: string): StoredMessage {
  return { id, conversation_id: 'c-old', role, content, created_at: 1 };
}

const OLD_CONV: Conversation = {
  id: 'c-old', title: 'Old VLAN chat', created_at: 1, updated_at: 2,
  model: 'muse-spark-1.3', provider: 'meta',
};
const OLD_FULL: Conversation = {
  ...OLD_CONV,
  messages: [msg('m1', 'user', 'inspect SW1 vlans'), msg('m2', 'assistant', 'VLAN 10 is present')],
};
const NEW_CONV: Conversation = {
  id: 'c-new', title: 'New Conversation', created_at: 3, updated_at: 3,
  model: 'muse-spark-1.3', provider: 'meta', messages: [],
};

function mockApi() {
  vi.mocked(fetchCopilotProviders).mockResolvedValue({ providers: [], models: [] });
  vi.mocked(fetchConversations).mockResolvedValue([OLD_CONV]);
  vi.mocked(fetchConversation).mockImplementation(async (id: string) =>
    (id === 'c-old' ? OLD_FULL : NEW_CONV));
  vi.mocked(createConversation).mockResolvedValue(NEW_CONV);
}

function renderPanel(labId = 'lab-1') {
  return render(
    <CopilotPanel
      isOpen={true}
      onClose={() => {}}
      devices={[]}
      token="tok-test"
      labId={labId}
    />,
  );
}

describe('CopilotPanel conversation lifecycle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi();
  });

  it('starts with a fresh chat and never auto-opens history', async () => {
    const { container } = renderPanel();
    // Fresh empty state, default subheader title.
    expect(await screen.findByText('CLIQ Network Intelligence Copilot')).toBeTruthy();
    const subheader = container.querySelector('span.truncate.text-neutral-400');
    expect(subheader?.textContent).toBe('CLIQ Copilot');
    await waitFor(() => expect(fetchConversations).toHaveBeenCalled());
    // History was listed but no conversation was auto-opened.
    expect(fetchConversation).not.toHaveBeenCalled();
    expect(screen.queryByText('inspect SW1 vlans')).toBeNull();
  });

  it('keeps history available for explicit selection', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(fetchConversations).toHaveBeenCalled());
    await user.click(screen.getByText('History'));
    expect(await screen.findByText('Old VLAN chat')).toBeTruthy();
  });

  it('selecting a historical chat loads exactly its messages', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(fetchConversations).toHaveBeenCalled());
    await user.click(screen.getByText('History'));
    await user.click(await screen.findByText('Old VLAN chat'));
    expect(await screen.findByText('inspect SW1 vlans')).toBeTruthy();
    expect(screen.getByText('VLAN 10 is present')).toBeTruthy();
    expect(fetchConversation).toHaveBeenCalledWith('c-old', 'tok-test');
  });

  it('New Chat creates a genuinely new conversation', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(fetchConversations).toHaveBeenCalled());
    await user.click(screen.getByTitle('New Conversation (+)'));
    await waitFor(() => expect(createConversation).toHaveBeenCalledTimes(1));
    // Fresh message area for the new conversation (skipNextLoad avoids a refetch).
    expect(screen.getByText('CLIQ Network Intelligence Copilot')).toBeTruthy();
    await user.click(screen.getByText('History'));
    // New conversation is active (subheader) and listed in history.
    await waitFor(() =>
      expect(screen.getAllByText('New Conversation').length).toBeGreaterThanOrEqual(2));
    expect(screen.getByText('Old VLAN chat')).toBeTruthy();
  });

  it('switching labs resets to a fresh chat instead of a stale conversation', async () => {
    const user = userEvent.setup();
    const { rerender } = renderPanel('lab-1');
    await waitFor(() => expect(fetchConversations).toHaveBeenCalled());
    await user.click(screen.getByText('History'));
    await user.click(await screen.findByText('Old VLAN chat'));
    expect(await screen.findByText('inspect SW1 vlans')).toBeTruthy();

    rerender(
      <CopilotPanel isOpen={true} onClose={() => {}} devices={[]} token="tok-test" labId="lab-2" />,
    );
    await waitFor(() =>
      expect(screen.getByText('CLIQ Network Intelligence Copilot')).toBeTruthy());
    expect(screen.queryByText('inspect SW1 vlans')).toBeNull();
  });
});
