import { useEffect, useMemo, useRef, useState } from 'react';
import LoginStep, { type SshConnectFields } from './components/LoginStep';
import LabDeviceStep from './components/LabDeviceStep';
import LabCopilotStep from './components/LabCopilotStep';
import MainTerminalUi from './components/MainTerminalUi';
import { postAuth, postDevices, postSshConnect, postDisconnect } from './lib/api';
import { getAvailableLabs } from './lib/labsApi';
import type { Device, Lab, TopologyLink, TopologyNode } from './types/api';
import type { AvailableLab } from './types/labs';
import { AlertTriangleIcon, CloseIcon } from './components/Icons';

type Stage = 'login' | 'select' | 'copilot' | 'terminal';

export default function App() {
  const [stage, setStage] = useState<Stage>('login');

  const [host, setHost] = useState('');
  const [token, setToken] = useState('');
  const [userName, setUserName] = useState('');
  const [labs, setLabs] = useState<Lab[]>([]);
  const [availableLabs, setAvailableLabs] = useState<AvailableLab[]>([]);
  const [labsLoading, setLabsLoading] = useState(false);
  const [labsError, setLabsError] = useState('');
  const [highlightLabId, setHighlightLabId] = useState<string | null>(null);
  const [selectedLabId, setSelectedLabId] = useState('');

  const [devices, setDevices] = useState<Device[]>([]);
  const [topologyNodes, setTopologyNodes] = useState<TopologyNode[]>([]);
  const [topologyLinks, setTopologyLinks] = useState<TopologyLink[]>([]);
  const [openDeviceIds, setOpenDeviceIds] = useState<Set<string>>(new Set());
  const [activeDeviceId, setActiveDeviceId] = useState('');
  const [precheckDeviceId, setPrecheckDeviceId] = useState<string | null>(null);
  const precheckTimerRef = useRef<number | null>(null);
  const connectingRef = useRef(false);

  const selectedLab = labs.find((l) => l.id === selectedLabId);
  const labTitle = selectedLab?.title ?? selectedLabId;
  const labExpiresAt = selectedLab?.expires_at ?? selectedLab?.expiry_at ?? selectedLab?.expiresOn ?? null;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  // Non-blocking reservation warning (unverifiable + 'warn' policy). Shown as a
  // dismissible banner; login still proceeds.
  const [reservationWarning, setReservationWarning] = useState('');
  // Persistent flag for the duration of an UNVERIFIED reservation session. Unlike
  // the dismissible banner, this drives a small always-on indicator so the user
  // stays aware they may be operating an expired lab. Never set for a
  // NetSim-verified login or an SSH session.
  const [unverifiedSession, setUnverifiedSession] = useState(false);

  useEffect(() => {
    return () => {
      if (precheckTimerRef.current) {
        window.clearTimeout(precheckTimerRef.current);
      }
    };
  }, []);

  async function handleLogin(nextHost: string, username: string, password: string) {
    setLoading(true);
    setError('');
    try {
      const data = await postAuth(nextHost, username, password);
      if (!data.success) {
        setError(data.error || 'Authentication was not successful.');
        return;
      }
      // Adopt the backend-normalized host (scheme/trailing slash stripped) so
      // every subsequent call (devices, WS) uses the exact same host string.
      const resolvedHost = data.host || nextHost;
      setHost(resolvedHost);
      setToken(data.token);
      setUserName(username);
      const userLabs = data.labs ?? [];
      setLabs(userLabs);
      // Surface a non-blocking reservation warning when the login proceeded
      // without NetSim verification (unverifiable + 'warn' policy).
      setReservationWarning(data.warning ?? '');
      setUnverifiedSession(data.unverified ?? false);

      // Multi-lab flow: always land on the lab selector so the user chooses
      // which lab to open. Never auto-open the first lab.
      setSelectedLabId('');
      setHighlightLabId(null);
      setDevices([]);
      setTopologyNodes([]);
      setTopologyLinks([]);
      setOpenDeviceIds(new Set());
      setActiveDeviceId('');
      setPrecheckDeviceId(null);
      setStage('select');
      await fetchAvailableLabs(data.token);
    } catch (e) {
      // The gate failed closed (non-2xx). Stay on the login page and show the
      // real reason — never advance to the device list.
      setError(e instanceof Error ? e.message : 'Failed to authenticate');
    } finally {
      setLoading(false);
    }
  }

  async function handleSshConnect(fields: SshConnectFields) {
    if (connectingRef.current || loading) return;
    connectingRef.current = true;
    setLoading(true);
    setError('');
    try {
      const data = await postSshConnect(fields);
      if (!data.success) {
        setError(data.error || 'SSH connection was not successful.');
        return;
      }
      setHost(data.host);
      setToken(data.token);
      setUserName(fields.username);
      setLabs([]);
      setSelectedLabId('');
      setReservationWarning('');
      setUnverifiedSession(false);
      setDevices([data.device]);
      setTopologyNodes([]);
      setTopologyLinks([]);
      setOpenDeviceIds(new Set([data.device.id]));
      setActiveDeviceId(data.device.id);
      setPrecheckDeviceId(null);
      setStage('terminal');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to connect over SSH');
    } finally {
      setLoading(false);
      connectingRef.current = false;
    }
  }

  async function fetchAvailableLabs(authToken: string) {
    if (!authToken) return;
    setLabsLoading(true);
    try {
      const data = await getAvailableLabs(authToken);
      setAvailableLabs(data.labs ?? []);
      setLabsError('');
    } catch (e) {
      setLabsError(e instanceof Error ? e.message : 'Failed to refresh labs');
    } finally {
      setLabsLoading(false);
    }
  }

  async function handleSelectLab(labId: string) {
    setSelectedLabId(labId);
    setHighlightLabId(null);
    if (!host || !token) return;
    setLoading(true);
    setError('');
    try {
      const data = await postDevices(host, token, labId);
      setDevices(data.devices ?? []);
      setTopologyNodes(data.topology?.nodes ?? []);
      setTopologyLinks(data.topology?.links ?? []);
      setOpenDeviceIds(new Set());
      setActiveDeviceId('');
      setPrecheckDeviceId(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to switch lab');
    } finally {
      setLoading(false);
    }
  }

  async function handleBackToLabs(labId: string, title: string) {
    if (!token) return;
    setLabs((prev) => (prev.some((l) => l.id === labId) ? prev : [...prev, { id: labId, title }]));
    await fetchAvailableLabs(token);
    setSelectedLabId(labId);
    setHighlightLabId(labId);
    setDevices([]);
    setTopologyNodes([]);
    setTopologyLinks([]);
    setStage('select');
  }

  async function handleLifecycleEnded(labId: string) {
    if (!token) return;
    await fetchAvailableLabs(token);
    if (labId === selectedLabId && host) {
      try {
        const data = await postDevices(host, token, labId);
        setDevices(data.devices ?? []);
        setTopologyNodes(data.topology?.nodes ?? []);
        setTopologyLinks(data.topology?.links ?? []);
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to refresh devices');
      }
    }
  }

  async function handleReturnToDashboard() {
    // Stage change unmounts the terminal UI, whose cleanup closes the shared
    // device socket; the backend releases runtimes while RETAINING the CML
    // session, so no re-login is needed. Local device state is reset here so
    // nothing stale survives the switch.
    setOpenDeviceIds(new Set());
    setActiveDeviceId('');
    setPrecheckDeviceId(null);
    setDevices([]);
    setTopologyNodes([]);
    setTopologyLinks([]);
    setHighlightLabId(null);
    setError('');
    if (token) {
      await fetchAvailableLabs(token);
    }
    setStage('select');
  }

  async function fetchDevices() {
    if (!selectedLabId || !host || !token) return;
    setLoading(true);
    setError('');
    try {
      const data = await postDevices(host, token, selectedLabId);
      setDevices(data.devices ?? []);
      setTopologyNodes(data.topology?.nodes ?? []);
      setTopologyLinks(data.topology?.links ?? []);
      setOpenDeviceIds(new Set());
      setActiveDeviceId('');
      setPrecheckDeviceId(null);
      // Stay on 'select' so the user sees the live topology before entering the terminal.
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch devices');
    } finally {
      setLoading(false);
    }
  }

  async function handleOpenDeployedLab(labId: string, title: string) {
    if (!host || !token) return;
    setLoading(true);
    setError('');
    try {
      const data = await postDevices(host, token, labId);
      setLabs((prev) => (prev.some((l) => l.id === labId) ? prev : [...prev, { id: labId, title }]));
      setSelectedLabId(labId);
      setDevices(data.devices ?? []);
      setTopologyNodes(data.topology?.nodes ?? []);
      setTopologyLinks(data.topology?.links ?? []);
      setOpenDeviceIds(new Set());
      setActiveDeviceId('');
      setPrecheckDeviceId(null);
      setStage('terminal');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to open deployed lab');
      setStage('select');
    } finally {
      setLoading(false);
    }
  }

  const openDevices = useMemo(
    () => devices.filter((device) => openDeviceIds.has(device.id)),
    [devices, openDeviceIds],
  );

  function openDevice(deviceId: string) {
    if (precheckTimerRef.current) {
      window.clearTimeout(precheckTimerRef.current);
      precheckTimerRef.current = null;
    }

    if (openDeviceIds.has(deviceId)) {
      setActiveDeviceId(deviceId);
      setStage('terminal');
      return;
    }

    // Mandatory precheck workflow before opening a terminal session.
    setStage('terminal');
    setPrecheckDeviceId(deviceId);
    precheckTimerRef.current = window.setTimeout(() => {
      setOpenDeviceIds((prev) => {
        const next = new Set(prev);
        next.add(deviceId);
        return next;
      });
      setActiveDeviceId(deviceId);
      setPrecheckDeviceId(null);
      precheckTimerRef.current = null;
    }, 1400);
  }

  function closeDevice(deviceId: string) {
    if (precheckDeviceId === deviceId) {
      setPrecheckDeviceId(null);
    }
    setOpenDeviceIds((prev) => {
      const next = new Set(prev);
      next.delete(deviceId);
      setActiveDeviceId((currentActive) => {
        if (currentActive !== deviceId) return currentActive;
        const remaining = [...next];
        return remaining[0] ?? '';
      });
      return next;
    });
  }

  function handleExitTerminal() {
    if (precheckTimerRef.current) {
      window.clearTimeout(precheckTimerRef.current);
      precheckTimerRef.current = null;
    }
    if (token) {
      void postDisconnect(token);
    }
    setOpenDeviceIds(new Set());
    setActiveDeviceId('');
    setPrecheckDeviceId(null);
    setDevices([]);
    setToken('');
    setStage(labs.length > 0 ? 'select' : 'login');
  }

  return (
    <div className="min-h-screen" style={{ background: 'var(--ide-bg)', color: 'var(--ide-text)' }}>
      {reservationWarning && stage !== 'login' && (
        <div
          role="alert"
          className="flex items-start justify-between gap-3 border-b px-4 py-2 text-xs"
          style={{ background: '#451a03', borderColor: '#b45309', color: '#fef3c7' }}
        >
          <span className="flex items-start gap-2">
            <AlertTriangleIcon size={14} className="text-amber-400 shrink-0 mt-0.5" />
            <span>{reservationWarning}</span>
          </span>
          <button
            type="button"
            onClick={() => setReservationWarning('')}
            className="shrink-0 rounded p-1 font-semibold text-amber-300 hover:bg-amber-900/50"
            aria-label="Dismiss unverified-access warning"
          >
            <CloseIcon size={12} />
          </button>
        </div>
      )}
      {unverifiedSession && stage !== 'login' && (
        <div
          role="status"
          title="CLIQ cannot confirm this reservation is active or expired (no NetSim key). You may be operating an expired lab."
          className="flex items-center gap-1.5 border-b px-4 py-1 text-[11px] font-semibold uppercase tracking-wide"
          style={{ background: '#291400', borderColor: '#78350f', color: '#fbbf24' }}
        >
          <span aria-hidden className="inline-block h-2 w-2 rounded-full bg-amber-500" />
          Unverified reservation — may be expired
        </div>
      )}
      {stage === 'login' && (
        <LoginStep
          loading={loading}
          error={error}
          labs={labs}
          onSubmit={handleLogin}
          onSubmitSsh={handleSshConnect}
        />
      )}

      {stage === 'select' && (
        <LabDeviceStep
          labs={labs}
          availableLabs={availableLabs}
          labsLoading={labsLoading}
          selectedLabId={selectedLabId}
          highlightLabId={highlightLabId}
          devices={devices}
          topologyNodes={topologyNodes}
          topologyLinks={topologyLinks}
          loading={loading}
          error={error}
          labsError={labsError}
          token={token}
          onSelectLab={handleSelectLab}
          onFetchDevices={fetchDevices}
          onOpenDevice={openDevice}
          onPlanNewLab={() => setStage('copilot')}
          onRefreshLabs={() => fetchAvailableLabs(token)}
          onLifecycleEnded={handleLifecycleEnded}
        />
      )}

      {stage === 'copilot' && (
        <LabCopilotStep
          token={token}
          host={host}
          onBack={() => setStage('select')}
          onOpenLab={handleOpenDeployedLab}
          onBackToLabs={handleBackToLabs}
        />
      )}

      {stage === 'terminal' && (
        <MainTerminalUi
          host={host}
          token={token}
          userName={userName}
          labs={labs}
          selectedLabId={selectedLabId}
          labTitle={labTitle}
          labExpiresAt={labExpiresAt}
          precheckDeviceId={precheckDeviceId}
          allDevices={devices}
          openDevices={openDevices}
          topologyNodes={topologyNodes}
          topologyLinks={topologyLinks}
          activeDeviceId={activeDeviceId}
          onOpenDevice={openDevice}
          onCloseDevice={closeDevice}
          onSetActiveDevice={setActiveDeviceId}
          onSelectLab={handleSelectLab}
          onChangeLab={() => void handleReturnToDashboard()}
          onShowDashboard={() => void handleReturnToDashboard()}
          onExitTerminal={handleExitTerminal}
          onPlanNewLab={labs.length > 0 ? () => setStage('copilot') : undefined}
        />
      )}
    </div>
  );
}
