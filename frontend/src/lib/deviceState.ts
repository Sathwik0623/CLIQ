/**
 * deviceState — console-readiness gating for CML device open actions.
 *
 * A console opens only when the device is BOOTED (the SDK-verified ready
 * marker: `Node.is_booted()` ⟺ state == "BOOTED"). Missing/empty/unknown
 * states stay permissive (legacy data, SSH sessions) so the V.3 terminal
 * flow is never broken by absent telemetry. This is UI-level gating only;
 * the terminal WebSocket path itself is untouched.
 */

export function isConsoleBlocked(nodeState?: string | null): boolean {
  if (nodeState == null) return false;
  const clean = nodeState.trim().toUpperCase();
  if (clean === '' || clean === 'UNKNOWN') return false;
  return clean !== 'BOOTED';
}

export function consoleBlockedReason(nodeState?: string | null): string {
  const clean = (nodeState ?? '').trim().toUpperCase();
  if (clean === 'STARTED' || clean === 'QUEUED' || clean === 'STARTING') {
    return `Console unavailable while device is ${nodeState} (still booting)`;
  }
  if (clean === 'STOPPED' || clean === 'DEFINED') {
    return `Console unavailable while device is ${nodeState} (start the lab first)`;
  }
  return `Console unavailable while device state is ${nodeState ?? 'unknown'}`;
}
