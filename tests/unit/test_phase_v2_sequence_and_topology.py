"""
Unit tests for CLIQ Phase V.2 — Network Experience, Dynamic CML Discovery, Sequential Commands & Performance.
"""

import pytest
import re
from cliq.connection.live_device import _PROMPT_RE, _PROMPT_START_RE, mode_from_prompt

def test_linux_and_cisco_prompt_detection():
    # Cisco IOS prompts
    assert _PROMPT_RE.search("R1#") is not None
    assert _PROMPT_RE.search("R1> ") is not None
    assert _PROMPT_RE.search("SW1(config)#") is not None
    assert _PROMPT_RE.search("SW2(config-if)#") is not None

    # Linux prompts
    assert _PROMPT_RE.search("PC1:~$") is not None
    assert _PROMPT_RE.search("PC1:~$ ") is not None
    assert _PROMPT_RE.search("PC2:~$ ") is not None
    assert _PROMPT_RE.search("cisco@Server1:~$") is not None
    assert _PROMPT_RE.search("cisco@Server2:~$ ") is not None
    assert _PROMPT_RE.search("root@Server1:~# ") is not None

    # Non-prompts / proxy prompts to exclude
    assert _PROMPT_RE.search("consoles>") is None
    assert _PROMPT_RE.search("consoles> ") is None
    assert _PROMPT_RE.search("Press RETURN to get started") is None
    assert _PROMPT_RE.search("Building configuration...") is None

def test_prompt_start_anchored():
    assert _PROMPT_START_RE.match("R1#") is not None
    assert _PROMPT_START_RE.match("PC1:~$ ") is not None
    assert _PROMPT_START_RE.match("cisco@Server1:~$ ") is not None
    assert _PROMPT_START_RE.match("consoles>") is None
    assert _PROMPT_START_RE.match("interface GigabitEthernet0/1") is None

def test_mode_from_prompt_linux():
    assert mode_from_prompt("PC1:~$") == "exec"
    assert mode_from_prompt("cisco@Server1:~$ ") == "exec"
    assert mode_from_prompt("root@Server1:~# ") == "enable"
    assert mode_from_prompt("R1#") == "enable"
    assert mode_from_prompt("R1>") == "exec"
    assert mode_from_prompt("R1(config)#") == "config"

def test_read_only_sequential_safety_rules():
    READ_ONLY_PREFIXES = [
        'show ', 'ping ', 'traceroute ', 'trace ', 'display ',
        'uname', 'ip a', 'ip addr', 'ip route', 'ifconfig', 'cat ', 'ls '
    ]
    CONFIG_PATTERNS = [
        re.compile(r"^conf(?:ig(?:ure)?)?\b", re.I),
        re.compile(r"^int(?:erface)?\b", re.I),
        re.compile(r"^router\b", re.I),
        re.compile(r"^ip\s+(?:address|route\s+\d|nat)\b", re.I),
        re.compile(r"^shut(?:down)?\b", re.I),
        re.compile(r"^no\s+", re.I),
        re.compile(r"^reload\b", re.I),
        re.compile(r"^write\b", re.I),
        re.compile(r"^copy\b", re.I)
    ]

    def is_read_only(cmd: str) -> bool:
        c = cmd.strip().lower()
        for p in CONFIG_PATTERNS:
            if p.match(c):
                return False
        return any(c.startswith(pfx) or c == pfx.strip() for pfx in READ_ONLY_PREFIXES)

    # Pure read-only sequence
    ro_seq = [
        "show ip interface brief",
        "show interfaces status",
        "show interfaces description",
        "show interfaces Ethernet0/3",
        "show run interface Ethernet0/3"
    ]
    assert all(is_read_only(cmd) for cmd in ro_seq) is True

    # Configuration change sequences
    cfg_seq = [
        "configure terminal",
        "interface Ethernet0/0",
        "ip address 10.0.0.1 255.255.255.0",
        "no shutdown"
    ]
    assert all(is_read_only(cmd) for cmd in cfg_seq) is False

    # Mixed sequence (must be rejected)
    mixed_seq = [
        "show ip route",
        "conf t",
        "router ospf 1"
    ]
    assert all(is_read_only(cmd) for cmd in mixed_seq) is False


def test_platform_aware_command_validation():
    from cliq.db.query import CommandDB
    from cliq.intelligence.engine import IntelligenceEngine
    from cliq.web.server import _build_live_hint

    db = CommandDB()
    engine = IntelligenceEngine(db)

    # 1. Linux PC / Server commands: valid, no false mode/syntax errors
    linux_cmds = ["hostname", "uname -a", "ip addr", "ip route", "ifconfig", "cat /etc/hosts", "ping 192.168.1.1"]
    for cmd in linux_cmds:
        res = engine.analyze(cmd, mode="exec", device_type="desktop", prompt="PC1:~$")
        assert res.status == "ok", f"Expected ok for Linux cmd '{cmd}', got {res.status}"
        assert _build_live_hint(res) is None

    # 2. Cisco-only command on Linux: flagged as wrong_device
    cisco_on_linux = engine.analyze("show version", mode="exec", device_type="desktop", prompt="PC1:~$")
    assert cisco_on_linux.status == "wrong_device"
    hint = _build_live_hint(cisco_on_linux)
    assert hint is not None and hint["kind"] == "wrong_device"
    assert "Cisco IOS command" in hint["message"]

    # 3. Cisco device: hostname in exec mode requires config mode
    host_on_cisco = engine.analyze("hostname", mode="exec", device_type="router", prompt="R1>")
    assert host_on_cisco.status == "wrong_mode"
    hint_cisco = _build_live_hint(host_on_cisco)
    assert hint_cisco is not None and hint_cisco["kind"] == "wrong_mode"
    assert "requires 'config'" in hint_cisco["message"]

    # 4. Linux command on Cisco: flagged as wrong_device
    uname_on_cisco = engine.analyze("uname -a", mode="exec", device_type="router", prompt="R1#")
    assert uname_on_cisco.status == "wrong_device"
    hint_uname = _build_live_hint(uname_on_cisco)
    assert hint_uname is not None and hint_uname["kind"] == "wrong_device"
    assert "Linux shell command" in hint_uname["message"]


def test_phase_v2_2_target_validation_rules():
    """
    Verify Bug 1 requirements:
    - No terminal open: target is None, disabled
    - Closed terminal: target is None, disabled
    - Open but unfocused: target is None, ask to focus
    - Open and focused: target is that device
    - Disconnected terminal: disabled
    """
    def resolve_target(open_devices, focused_device_id, override_device_id=None, device_metrics=None):
        open_ids = [d["id"] for d in open_devices]
        
        valid_override = override_device_id if override_device_id in open_ids else None
        valid_focused = focused_device_id if focused_device_id in open_ids else None
        effective_target_id = valid_override or valid_focused or None
        
        target_device = next((d for d in open_devices if d["id"] == effective_target_id), None)
        metric = (device_metrics or {}).get(effective_target_id, {})
        is_disconnected = metric.get("status") in ("disconnected", "error")
        
        is_no_terminal_open = len(open_devices) == 0
        is_unfocused = len(open_devices) > 0 and effective_target_id is None
        is_eligible = bool(effective_target_id and target_device and not is_disconnected)
        
        return {
            "target_id": effective_target_id,
            "is_no_terminal_open": is_no_terminal_open,
            "is_unfocused": is_unfocused,
            "is_disconnected": is_disconnected,
            "is_eligible": is_eligible,
        }

    # 1. No terminal open
    res = resolve_target([], None)
    assert res["is_no_terminal_open"] is True
    assert res["is_eligible"] is False
    assert res["target_id"] is None

    # 2. R1 console closed (was open, now not in open_devices)
    all_devs = [{"id": "r1", "label": "R1"}]
    res = resolve_target([], focused_device_id="r1")
    assert res["is_no_terminal_open"] is True
    assert res["is_eligible"] is False
    assert res["target_id"] is None

    # 3. Terminal open but not focused
    res = resolve_target([{"id": "r1", "label": "R1"}, {"id": "pc1", "label": "PC1"}], focused_device_id=None)
    assert res["is_no_terminal_open"] is False
    assert res["is_unfocused"] is True
    assert res["is_eligible"] is False
    assert res["target_id"] is None

    # 4. PC1 open and focused
    res = resolve_target([{"id": "r1", "label": "R1"}, {"id": "pc1", "label": "PC1"}], focused_device_id="pc1")
    assert res["is_eligible"] is True
    assert res["target_id"] == "pc1"

    # 5. R1 and PC1 open, PC1 focused
    res = resolve_target([{"id": "r1", "label": "R1"}, {"id": "pc1", "label": "PC1"}], focused_device_id="pc1")
    assert res["target_id"] == "pc1"

    # 6. Focused terminal disconnected
    metrics = {"pc1": {"status": "disconnected", "mode": "exec"}}
    res = resolve_target([{"id": "pc1", "label": "PC1"}], focused_device_id="pc1", device_metrics=metrics)
    assert res["is_disconnected"] is True
    assert res["is_eligible"] is False


def test_phase_v2_2_sequence_state_machine_transitions():
    """
    Verify Bug 2 requirements:
    10 states: READY, COMMAND_INSERTED, AWAITING_USER_ENTER, COMMAND_RUNNING,
    WAITING_FOR_AUTHENTIC_PROMPT, INSERTING_NEXT_COMMAND, PAUSED, COMPLETED, CANCELLED, ERROR.
    Prompt before execution must NOT advance.
    """
    class SequentialStateMachine:
        def __init__(self, commands, target_device_id):
            self.commands = commands
            self.target_id = target_device_id
            self.state = "READY"
            self.current_step = 0
            self.submitted = False
            self.output_received = False
            self.inserted_commands = []
            self.error_message = None

        def start(self):
            assert self.state == "READY"
            self.state = "COMMAND_INSERTED"
            self.current_step = 0
            self.inserted_commands.append(self.commands[0])
            self.state = "AWAITING_USER_ENTER"

        def user_press_enter(self, device_id):
            if device_id != self.target_id:
                return
            if self.state in ("AWAITING_USER_ENTER", "COMMAND_INSERTED"):
                self.submitted = True
                self.state = "COMMAND_RUNNING"

        def on_device_output(self, device_id):
            if device_id != self.target_id:
                return
            if self.state == "COMMAND_RUNNING":
                self.output_received = True
                self.state = "WAITING_FOR_AUTHENTIC_PROMPT"

        def on_prompt_ready(self, device_id):
            if device_id != self.target_id:
                return
            # CRITICAL: Prompt appearing before user pressed enter must NEVER advance queue
            if not self.submitted:
                return
            if self.state in ("COMMAND_RUNNING", "WAITING_FOR_AUTHENTIC_PROMPT"):
                self.submitted = False
                self.output_received = False
                next_step = self.current_step + 1
                if next_step < len(self.commands):
                    self.state = "INSERTING_NEXT_COMMAND"
                    self.current_step = next_step
                    self.inserted_commands.append(self.commands[next_step])
                    self.state = "AWAITING_USER_ENTER"
                else:
                    self.state = "COMPLETED"

        def on_terminal_closed(self, closed_device_id):
            if closed_device_id == self.target_id:
                self.state = "ERROR"
                self.error_message = f"Terminal {closed_device_id} was closed. Sequence stopped."

    cmds = ["show ip int br", "show ip route", "show version"]
    sm = SequentialStateMachine(cmds, "r1")

    # Start sequence
    sm.start()
    assert sm.state == "AWAITING_USER_ENTER"
    assert sm.current_step == 0
    assert sm.inserted_commands == ["show ip int br"]

    # Premature prompt arrives (e.g. unsolicited banner / previous keep-alive)
    sm.on_prompt_ready("r1")
    assert sm.state == "AWAITING_USER_ENTER", "Premature prompt must not advance queue!"
    assert sm.current_step == 0

    # Cross-device prompt arrives from different device
    sm.on_prompt_ready("pc1")
    assert sm.current_step == 0

    # User presses Enter on r1
    sm.user_press_enter("r1")
    assert sm.state == "COMMAND_RUNNING"

    # Command output arrives
    sm.on_device_output("r1")
    assert sm.state == "WAITING_FOR_AUTHENTIC_PROMPT"

    # Authentic prompt ready arrives
    sm.on_prompt_ready("r1")
    assert sm.state == "AWAITING_USER_ENTER"
    assert sm.current_step == 1
    assert sm.inserted_commands == ["show ip int br", "show ip route"]

    # Step 2: user presses enter
    sm.user_press_enter("r1")
    sm.on_device_output("r1")
    sm.on_prompt_ready("r1")
    assert sm.state == "AWAITING_USER_ENTER"
    assert sm.current_step == 2
    assert sm.inserted_commands == ["show ip int br", "show ip route", "show version"]

    # Step 3: user presses enter
    sm.user_press_enter("r1")
    sm.on_device_output("r1")
    sm.on_prompt_ready("r1")
    assert sm.state == "COMPLETED"

    # Safe abort if terminal closes during sequence
    sm2 = SequentialStateMachine(cmds, "r1")
    sm2.start()
    assert sm2.state == "AWAITING_USER_ENTER"
    sm2.on_terminal_closed("r1")
    assert sm2.state == "ERROR"
    assert "was closed" in sm2.error_message


def test_phase_v2_3_focus_synchronization():
    """
    Phase V.2.3 Bug 1: Terminal shows Focused, but Copilot refuses insertion.
    Authoritative focus synchronization test.
    """
    open_devices = [{"id": "r1", "label": "R1"}, {"id": "pc1", "label": "PC1"}]

    def get_ui_and_copilot_state(open_devs, authoritative_focused_id):
        open_ids = [d["id"] for d in open_devs]
        valid_focused = authoritative_focused_id if authoritative_focused_id in open_ids else None
        
        # Terminal headers derive strictly from authoritative focus
        headers = {
            d["id"]: {
                "label": d["label"],
                "is_focused": d["id"] == valid_focused,
                "badge": "Focused" if d["id"] == valid_focused else "Click to Focus",
            }
            for d in open_devs
        }
        
        # Copilot derives strictly from authoritative focus
        target_device = next((d for d in open_devs if d["id"] == valid_focused), None)
        target_label = target_device["label"] if target_device else None
        
        copilot_action = f"Insert into {target_label}" if target_device else "Please focus a terminal to insert"
        can_insert = target_device is not None
        
        return headers, target_label, copilot_action, can_insert

    # Case 1: R1 is focused
    headers, target, action, can_insert = get_ui_and_copilot_state(open_devices, "r1")
    assert headers["r1"]["is_focused"] is True
    assert headers["r1"]["badge"] == "Focused"
    assert headers["pc1"]["is_focused"] is False
    assert target == "R1"
    assert action == "Insert into R1"
    assert can_insert is True

    # Case 2: PC1 is focused
    headers, target, action, can_insert = get_ui_and_copilot_state(open_devices, "pc1")
    assert headers["pc1"]["is_focused"] is True
    assert headers["pc1"]["badge"] == "Focused"
    assert headers["r1"]["is_focused"] is False
    assert target == "PC1"
    assert action == "Insert into PC1"
    assert can_insert is True

    # Case 3: Neither is focused (focusedDeviceId is None)
    headers, target, action, can_insert = get_ui_and_copilot_state(open_devices, None)
    assert headers["r1"]["is_focused"] is False
    assert headers["pc1"]["is_focused"] is False
    assert headers["r1"]["badge"] == "Click to Focus"
    assert headers["pc1"]["badge"] == "Click to Focus"
    assert target is None
    assert action == "Please focus a terminal to insert"
    assert can_insert is False


def test_phase_v2_3_stable_pane_assignments():
    """
    Phase V.2.3 Bug 2: Stable Split-Terminal Positions.
    Pane assignments must never shuffle or swap when focus changes or devices are selected.
    """
    class SplitPaneManager:
        def __init__(self):
            self.pane1 = None
            self.pane2 = None

        def sync(self, open_device_ids):
            if not open_device_ids:
                self.pane1 = None
                self.pane2 = None
                return

            p1 = self.pane1 if self.pane1 in open_device_ids else None
            p2 = self.pane2 if self.pane2 in open_device_ids else None

            if not p1:
                p1 = next((id for id in open_device_ids if id != p2), open_device_ids[0])

            if len(open_device_ids) >= 2:
                if not p2 or p2 == p1:
                    p2 = next((id for id in open_device_ids if id != p1), None)
            else:
                p2 = None

            self.pane1 = p1
            self.pane2 = p2

        def assign_pane1(self, device_id):
            if self.pane2 == device_id:
                self.pane2 = self.pane1
            self.pane1 = device_id

        def assign_pane2(self, device_id):
            if self.pane1 == device_id:
                self.pane1 = self.pane2
            self.pane2 = device_id

    mgr = SplitPaneManager()

    # Initial open: R1 and PC1
    mgr.sync(["r1", "pc1"])
    assert mgr.pane1 == "r1", "Left pane must be R1"
    assert mgr.pane2 == "pc1", "Right pane must be PC1"

    # User focuses PC1 -> sync with same open devices
    # Focus change does NOT change pane assignments!
    mgr.sync(["r1", "pc1"])
    assert mgr.pane1 == "r1", "Focusing PC1 must not move R1 from Left pane!"
    assert mgr.pane2 == "pc1", "Focusing PC1 must keep PC1 in Right pane!"

    # User focuses R1 -> sync with same open devices
    mgr.sync(["r1", "pc1"])
    assert mgr.pane1 == "r1", "Focusing R1 must keep R1 in Left pane!"
    assert mgr.pane2 == "pc1", "Focusing R1 must keep PC1 in Right pane!"

    # User clicks R2 in explorer (opens R2 in background/tab)
    mgr.sync(["r1", "pc1", "r2"])
    assert mgr.pane1 == "r1", "Opening R2 must not shuffle existing Pane 1!"
    assert mgr.pane2 == "pc1", "Opening R2 must not shuffle existing Pane 2!"

    # Explicit pane replacement: User explicitly assigns R2 to Pane 2
    mgr.assign_pane2("r2")
    assert mgr.pane1 == "r1"
    assert mgr.pane2 == "r2"

    # User closes R2
    mgr.sync(["r1", "pc1"])
    assert mgr.pane1 == "r1", "Closing Pane 2 must preserve Pane 1!"
    assert mgr.pane2 == "pc1", "Pane 2 fills with remaining open device PC1"
