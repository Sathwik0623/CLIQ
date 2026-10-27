import { useEffect, useMemo, useRef, useState } from 'react';
import LoginStep, { type SshConnectFields } from './components/LoginStep';
import LabDeviceStep from './components/LabDeviceStep';
import MainTerminalUi from './components/MainTerminalUi';
import { postAuth, postDevices, postSshConnect } from './lib/api';
import type { Device, Lab, TopologyLink, TopologyNode } from './types/api';

type Stage = 'login' | 'select' | 'terminal';

export default function App() {
  const [stage, setStage] = useState<Stage>('login');

  const [host, setHost] = useState('');
  const [token, setToken] = useState('');
  const [userName, setUserName] = useState('');
  const [labs, setLabs] = useState<Lab[]>([]);
  const [selectedLabId, setSelectedLabId] = useState('');

  const [devices, setDevices] = useState<Device[]>([]);
  const [topologyNodes, setTopologyNodes] = useState<TopologyNode[]>([]);
  const [topologyLinks, setTopologyLinks] = useState<TopologyLink[]>([]);
  const [openDeviceIds, setOpenDeviceIds] = useState<Set<string>>(new Set());
  const [activeDeviceId, setActiveDeviceId] = useState('');
  const [precheckDeviceId, setPrecheckDeviceId] = useState<string | null>(null);
  const precheckTimerRef = useRef<number | null>(null);

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
      setLabs(data.labs ?? []);
      // Surface a non-blocking reservation warning when the login proceeded
      // without NetSim verification (unverifiable + 'warn' policy).
      setReservationWarning(data.warning ?? '');
      setUnverifiedSession(data.unverified ?? false);
      setStage('select');
    } catch (e) {
      // The gate failed closed (non-2xx). Stay on the login page and show the
      // real reason — never advance to the device list.
      setError(e instanceof Error ? e.message : 'Failed to authenticate');
    } finally {
      setLoading(false);
    }
  }

  async function handleSshConnect(fields: SshConnectFields) {
    setLoading(true);
    setError('');
    try {
      const data = await postSshConnect(
        fields.host,
        fields.consoleUser,
        fields.consolePort,
        fields.password,
      );
      if (!data.success) {
        setError(data.error || 'SSH connection was not successful.');
        return;
      }
      // SSH sessions have no CML labs/topology — seed the single real device and
      // jump straight into the SAME terminal UI the CML path uses.
      setHost(data.host);
      setToken(data.token);
      setUserName(fields.consoleUser);
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
    }
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
    setOpenDeviceIds(new Set());
    setActiveDeviceId('');
    setPrecheckDeviceId(null);
    setStage('select');
  }

  return (
    <div className="min-h-screen bg-white">
      {reservationWarning && stage !== 'login' && (
        <div
          role="alert"
          className="flex items-start justify-between gap-3 border-b-2 border-amber-500 bg-amber-100 px-4 py-3 text-sm text-amber-900"
        >
          <span className="flex items-start gap-2">
            <span aria-hidden className="text-lg leading-none">⚠</span>
            <span>{reservationWarning}</span>
          </span>
          <button
            type="button"
            onClick={() => setReservationWarning('')}
            className="shrink-0 rounded px-2 font-semibold text-amber-800 hover:bg-amber-200"
            aria-label="Dismiss unverified-access warning"
          >
            ✕
          </button>
        </div>
      )}
      {unverifiedSession && stage !== 'login' && (
        <div
          role="status"
          title="CLIQ cannot confirm this reservation is active or expired (no NetSim key). You may be operating an expired lab."
          className="flex items-center gap-1.5 border-b border-amber-300 bg-amber-50 px-4 py-1 text-xs font-semibold uppercase tracking-wide text-amber-700"
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
          selectedLabId={selectedLabId}
          devices={devices}
          topologyNodes={topologyNodes}
          topologyLinks={topologyLinks}
          loading={loading}
          error={error}
          onSelectLab={setSelectedLabId}
          onFetchDevices={fetchDevices}
          onOpenDevice={openDevice}
        />
      )}

      {stage === 'terminal' && (
        <MainTerminalUi
          host={host}
          token={token}
          userName={userName}
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
          onChangeLab={() => setStage('select')}
          onExitTerminal={handleExitTerminal}
        />
      )}
    </div>
  );
}
