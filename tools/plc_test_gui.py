#!/usr/bin/env python3
"""PLC test driver — Tkinter GUI + CLI for both legacy_fronback and v2_unified.

GUI mode (default):
    python tools/plc_test_gui.py
    python tools/plc_test_gui.py --plc 192.168.1.10

The window has:
    * Connection bar (IP / port / connect-disconnect with green/red indicator)
    * Two tabs:
        - "Legacy Fronback": FIRE FRONTBACK / FIRE HEIGHT buttons + D0..D40 status
        - "v2 Unified":      camera + ProductType selector, FIRE button, per-cam
                             status (D1/D2 status, D14/D34 product type, D82/D102
                             result, plus decoded output_x/y from D70+/D90+)
    * Activity log showing every action + handshake trace

CLI mode (kept for scripted use / SSH sessions; legacy only):
    python tools/plc_test_gui.py status
    python tools/plc_test_gui.py frontback
    python tools/plc_test_gui.py height
    python tools/plc_test_gui.py watch

Non-destructive: legacy mode writes only D2+D1; v2 mode writes only the
ProductType word (D14/D34) + camera status (D1/D2). Algorithm parameters
stay at whatever the customer's PLC ladder programmed.
"""

from __future__ import annotations

import argparse
import struct
import sys
import threading
import time
import tkinter as tk
from queue import Empty, Queue
from tkinter import messagebox, scrolledtext, ttk

from pyModbusTCP.client import ModbusClient

# --------------------------------------------------------------------------- #
# Register maps
# --------------------------------------------------------------------------- #

# Legacy fronback (see legacy/fronback_protocol.py)
D0_RECOGNITION = 0
D1_TRIGGER = 1
D2_MODE = 2
D3_CAM1_STATUS = 3
D4_CAM2_STATUS = 4
D10_CAM1_EXP = 10
D11_CAM2_EXP = 11
D20_EDGE1_LOW = 20
D30_HEIGHT_EXP = 30
D31_BRIGHTNESS = 31
D32_MIN_Y = 32
D35_HEIGHT_COMP = 35
D40_HEIGHT_RESULT = 40

LEGACY_TRIGGER_FIRE, LEGACY_TRIGGER_IDLE, LEGACY_TRIGGER_DONE = 10, 0, 1
LEGACY_TRIGGER_LOOP = 11   # extension shipped in legacy v0.3.6+
LEGACY_MODE_HEIGHT, LEGACY_MODE_FRONTBACK = 0, 1


# v2_unified (see plc/manager.py + plc/enums.py)
# Per-camera config block is 18 words. cam1 starts at D10, cam2 at D30.
# Within each block:
#   +0 trigger, +1 exposure, +2-3 pixel_distance(f32), +4 product_type, +5..+17 algo params
V2_CAM1_STATUS = 1     # D1 — same address as legacy D1 but the values mean different things:
V2_CAM2_STATUS = 2     # D2 — see CameraStatus enum (0 IDLE, 3 TASK_COMPLETED, 10 START_TASK)
V2_CAM1_CONFIG_START = 10  # D10..D27
V2_CAM2_CONFIG_START = 30  # D30..D47
V2_CAM1_RESULT = 82    # D82 (1 word: 1=OK, 2=NG)
V2_CAM2_RESULT = 102   # D102
V2_CAM1_OUTPUT_X = 70  # D70-73 (double, 4 words)
V2_CAM1_OUTPUT_Y = 74  # D74-77
V2_CAM1_AREA = 83      # D83-84 (uint32, 2 words)
V2_CAM2_OUTPUT_X = 90
V2_CAM2_OUTPUT_Y = 94
V2_CAM2_AREA = 103

V2_OFFSET_TRIGGER = 0
V2_OFFSET_EXPOSURE = 1
V2_OFFSET_PRODUCT_TYPE = 4

V2_STATUS_NAMES = {
    0: "IDLE", 1: "READING_DATA", 2: "PROCESSING_DATA",
    3: "TASK_COMPLETED", 10: "START_TASK", 11: "START_LOOP",
}
V2_PRODUCT_NAMES = {
    0: "NONE", 1: "TOOTHPASTE_FRONTBACK", 2: "HEIGHT_CHECK", 3: "BRUSH_HEAD",
}
V2_FIRE = 10        # CameraStatus.START_TASK   — single capture
V2_FIRE_LOOP = 11   # CameraStatus.START_LOOP   — continuous capture
V2_IDLE = 0         # CameraStatus.IDLE         — stops a running loop


# --------------------------------------------------------------------------- #
# Modbus client (used by both CLI and GUI; supports both protocols)
# --------------------------------------------------------------------------- #
class PLC:
    """Single Modbus connection wrapping legacy + v2 register operations.

    The two protocols share register space at the same PLC; only the
    semantics differ. Methods are namespaced (`legacy_*` / `v2_*`) so the
    caller picks the right one based on the target NanoPi's config.json.
    """

    def __init__(self, host: str, port: int = 502) -> None:
        self.client = ModbusClient(host=host, port=port, timeout=2, auto_open=True)
        self._lock = threading.Lock()

    def open(self) -> bool:
        with self._lock:
            return bool(self.client.open())

    def close(self) -> None:
        with self._lock:
            self.client.close()

    def read_block(self, count: int = 111) -> list[int] | None:
        """Read D0..D110 (covers both legacy and v2 result blocks)."""
        with self._lock:
            return self.client.read_holding_registers(0, count)

    # ---------------- Legacy fronback ----------------

    def legacy_read_d1(self) -> int | None:
        with self._lock:
            r = self.client.read_holding_registers(D1_TRIGGER, 1)
        return r[0] if r else None

    def legacy_fire(self, mode: int) -> None:
        with self._lock:
            self.client.write_single_register(D2_MODE, mode)
            self.client.write_single_register(D1_TRIGGER, LEGACY_TRIGGER_FIRE)

    def legacy_fire_loop(self, mode: int) -> None:
        """Start continuous capture in legacy mode (binary v0.3.6+ required)."""
        with self._lock:
            self.client.write_single_register(D2_MODE, mode)
            self.client.write_single_register(D1_TRIGGER, LEGACY_TRIGGER_LOOP)

    def legacy_stop_loop(self) -> None:
        """Halt a running legacy LOOP by writing IDLE (0) to D1."""
        with self._lock:
            self.client.write_single_register(D1_TRIGGER, LEGACY_TRIGGER_IDLE)

    # ---------------- v2_unified ----------------

    def v2_read_camera_status(self, camera_num: int) -> int | None:
        addr = V2_CAM1_STATUS if camera_num == 1 else V2_CAM2_STATUS
        with self._lock:
            r = self.client.read_holding_registers(addr, 1)
        return r[0] if r else None

    def v2_fire(self, camera_num: int, product_type: int) -> None:
        """Set D14/D34 = ProductType, then D1/D2 = 10 (CameraStatus.START_TASK)."""
        if camera_num == 1:
            config_base = V2_CAM1_CONFIG_START
            status_addr = V2_CAM1_STATUS
        else:
            config_base = V2_CAM2_CONFIG_START
            status_addr = V2_CAM2_STATUS
        with self._lock:
            self.client.write_single_register(config_base + V2_OFFSET_PRODUCT_TYPE, product_type)
            self.client.write_single_register(status_addr, V2_FIRE)

    def v2_fire_loop(self, camera_num: int, product_type: int) -> None:
        """Set D14/D34 = ProductType, then D1/D2 = 11 (CameraStatus.START_LOOP).

        Loop mode runs the algorithm continuously — TaskManager.process_continuous_capture
        keeps capturing until the camera status register is changed away from 11.
        Use v2_stop_loop() to halt.
        """
        if camera_num == 1:
            config_base = V2_CAM1_CONFIG_START
            status_addr = V2_CAM1_STATUS
        else:
            config_base = V2_CAM2_CONFIG_START
            status_addr = V2_CAM2_STATUS
        with self._lock:
            self.client.write_single_register(config_base + V2_OFFSET_PRODUCT_TYPE, product_type)
            self.client.write_single_register(status_addr, V2_FIRE_LOOP)

    def v2_stop_loop(self, camera_num: int) -> None:
        """Halt a running START_LOOP by writing IDLE (0) to the camera status register."""
        addr = V2_CAM1_STATUS if camera_num == 1 else V2_CAM2_STATUS
        with self._lock:
            self.client.write_single_register(addr, V2_IDLE)


# --------------------------------------------------------------------------- #
# Decoders for v2 result block — match plc/codec.py byte order exactly
# --------------------------------------------------------------------------- #
def words_to_double_le(words: list[int]) -> float:
    """Decode 4 uint16 words into a little-endian float64 (matches double_to_words)."""
    packed = b""
    for w in words:
        packed += bytes([w & 0xFF, (w >> 8) & 0xFF])
    return struct.unpack("<d", packed)[0]


def words_to_uint32_le(low: int, high: int) -> int:
    return (high << 16) | low


# --------------------------------------------------------------------------- #
# CLI mode (legacy only — v2 testing uses the GUI)
# --------------------------------------------------------------------------- #
def format_legacy_status(block: list[int]) -> list[tuple[str, str, str]]:
    edge1 = block[20] | (block[21] << 16)
    edge2 = block[22] | (block[23] << 16)
    return [
        ("D0  recognition", str(block[0]), "1=front/OK  2=back/NG  3=empty"),
        ("D1  trigger", str(block[1]), "10=fire  0=idle/ack  1=done"),
        ("D2  mode", str(block[2]), "0=height  1=frontback"),
        ("D3  cam1 status", str(block[3]), "1=ok  0=offline"),
        ("D4  cam2 status", str(block[4]), "1=ok  0=offline"),
        ("D10 cam1 exp", f"{block[10]} us", ""),
        ("D11 cam2 exp", f"{block[11]} us", ""),
        ("D20-23 edge1/edge2", f"{edge1} / {edge2}", ""),
        ("D30 height exp", f"{block[30]} us", ""),
        ("D31 brightness threshold", str(block[31]), ""),
        ("D32 min_y", str(block[32]), ""),
        ("D35 height comparison", str(block[35]), ""),
        ("D40 height result", str(block[40]), ""),
    ]


def cli_status(plc: PLC) -> None:
    block = plc.read_block()
    if not block:
        print("  read failed (PLC unreachable?)")
        return
    for label, value, hint in format_legacy_status(block):
        suffix = f"   ({hint})" if hint else ""
        print(f"  {label:<26} = {value}{suffix}")


def cli_fire(plc: PLC, mode: int, label: str) -> None:
    print(f"-> writing D2 = {mode} ({label})")
    print("-> writing D1 = 10 (TRIGGER_FIRE)")
    plc.legacy_fire(mode)

    print("polling D1 for orchestrator handshake (timeout 5s)")
    print("expect: 10 -> 0 (ack within ~50ms) -> 1 (done within ~1-2s)")
    saw_ack = False
    last = -999
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        d1 = plc.legacy_read_d1()
        d1_show = -1 if d1 is None else d1
        if d1_show != last:
            print(f"  t={time.monotonic() - (deadline - 5.0):4.2f}s  D1 = {d1_show}")
            last = d1_show
        if d1_show == LEGACY_TRIGGER_IDLE and not saw_ack:
            saw_ack = True
        if d1_show == LEGACY_TRIGGER_DONE:
            break
        time.sleep(0.05)
    else:
        if not saw_ack:
            print("  X NEVER GOT ACK — orchestrator not running / wrong PLC IP / network down")
        else:
            print("  X ACK seen but never DONE — algorithm hung or crashed mid-cycle")
            print("    SSH NanoPi: tail -n 50 ~/my_app.log")

    print("\nstatus after fire:")
    cli_status(plc)


def cli_watch(plc: PLC) -> None:
    print("polling every 1s, Ctrl+C to stop\n")
    try:
        while True:
            print(f"--- {time.strftime('%H:%M:%S')} ---")
            cli_status(plc)
            print()
            time.sleep(1)
    except KeyboardInterrupt:
        print("stopped.")


# --------------------------------------------------------------------------- #
# GUI mode
# --------------------------------------------------------------------------- #
class PLCTesterGUI:
    POLL_INTERVAL_MS = 1000

    def __init__(self, root: tk.Tk, host: str, port: int) -> None:
        self.root = root
        root.title("PLC Tester — legacy_fronback + v2_unified")
        root.geometry("840x780")

        self.plc: PLC | None = None
        self.auto_poll = tk.BooleanVar(value=False)
        self.queue: Queue[tuple[str, object]] = Queue()

        # Per-tab widget registries (label keys -> ttk.Label).
        self.legacy_status_labels: dict[str, ttk.Label] = {}
        self.v2_status_labels: dict[str, ttk.Label] = {}

        # v2 selectors
        self.v2_camera = tk.IntVar(value=1)
        self.v2_product_type = tk.IntVar(value=1)

        self._build_connection_bar(host, port)
        self._build_notebook()
        self._build_log_panel()

        root.after(100, self._process_queue)

    # ----------------------------------------------------------- UI builders

    def _build_connection_bar(self, host: str, port: int) -> None:
        frame = ttk.LabelFrame(self.root, text="Connection", padding=6)
        frame.pack(fill="x", padx=8, pady=4)

        ttk.Label(frame, text="PLC IP:").pack(side="left")
        self.ip_entry = ttk.Entry(frame, width=15)
        self.ip_entry.insert(0, host)
        self.ip_entry.pack(side="left", padx=4)

        ttk.Label(frame, text="Port:").pack(side="left")
        self.port_entry = ttk.Entry(frame, width=6)
        self.port_entry.insert(0, str(port))
        self.port_entry.pack(side="left", padx=4)

        self.connect_btn = ttk.Button(frame, text="Connect", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=8)

        self.conn_status = ttk.Label(frame, text="Disconnected", foreground="red")
        self.conn_status.pack(side="left", padx=8)

        ttk.Checkbutton(
            frame, text="Auto-refresh state every 1s",
            variable=self.auto_poll, command=self._on_auto_poll_toggle,
        ).pack(side="right", padx=8)

    def _build_notebook(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", padx=8, pady=4, expand=True)

        legacy_frame = ttk.Frame(notebook)
        notebook.add(legacy_frame, text="Legacy Fronback")
        self._build_legacy_tab(legacy_frame)

        v2_frame = ttk.Frame(notebook)
        notebook.add(v2_frame, text="v2 Unified")
        self._build_v2_tab(v2_frame)

    def _build_legacy_tab(self, parent: ttk.Frame) -> None:
        # Single-trigger buttons (D1=10)
        single = ttk.LabelFrame(parent, text="Single trigger (writes D2 + D1=10)", padding=6)
        single.pack(fill="x", padx=4, pady=2)

        self.legacy_fb_btn = ttk.Button(
            single, text="FIRE FRONTBACK  (D2=1, dual-cam)",
            command=lambda: self._fire_legacy(LEGACY_MODE_FRONTBACK, "FRONTBACK / 正反"),
            width=34,
        )
        self.legacy_fb_btn.pack(side="left", padx=4, pady=4)

        self.legacy_height_btn = ttk.Button(
            single, text="FIRE HEIGHT  (D2=0, cam2 only)",
            command=lambda: self._fire_legacy(LEGACY_MODE_HEIGHT, "HEIGHT / 高度"),
            width=34,
        )
        self.legacy_height_btn.pack(side="left", padx=4, pady=4)

        # Continuous-loop buttons (D1=11; STOP writes D1=0). Requires binary
        # v0.3.6+ — older legacy binaries silently ignore D1=11.
        loop = ttk.LabelFrame(
            parent,
            text="Continuous loop (writes D2 + D1=11; STOP writes D1=0)  -  binary v0.3.6+",
            padding=6,
        )
        loop.pack(fill="x", padx=4, pady=2)

        self.legacy_loop_fb_btn = ttk.Button(
            loop, text="LOOP FRONTBACK  (D2=1, D1=11)",
            command=lambda: self._loop_legacy(LEGACY_MODE_FRONTBACK, "LOOP FRONTBACK"),
            width=28,
        )
        self.legacy_loop_fb_btn.pack(side="left", padx=4, pady=4)

        self.legacy_loop_height_btn = ttk.Button(
            loop, text="LOOP HEIGHT  (D2=0, D1=11)",
            command=lambda: self._loop_legacy(LEGACY_MODE_HEIGHT, "LOOP HEIGHT"),
            width=28,
        )
        self.legacy_loop_height_btn.pack(side="left", padx=4, pady=4)

        self.legacy_stop_btn = ttk.Button(
            loop, text="STOP LOOP  (D1=0)",
            command=self._stop_legacy_loop,
            width=20,
        )
        self.legacy_stop_btn.pack(side="left", padx=4, pady=4)

        # Status panel
        status = ttk.LabelFrame(parent, text="Live PLC State (legacy)", padding=6)
        status.pack(fill="both", padx=4, pady=4, expand=True)

        labels = [
            ("D0  recognition", "1=front/OK  2=back/NG  3=empty"),
            ("D1  trigger", "10=fire  0=idle/ack  1=done"),
            ("D2  mode", "0=height  1=frontback"),
            ("D3  cam1 status", "1=ok  0=offline"),
            ("D4  cam2 status", "1=ok  0=offline"),
            ("D10 cam1 exp", "frontback mode"),
            ("D11 cam2 exp", "frontback mode"),
            ("D20-23 edge1/edge2", "frontback algorithm result"),
            ("D30 height exp", "height mode"),
            ("D31 brightness threshold", "height mode"),
            ("D32 min_y", "height mode"),
            ("D35 height comparison", "height mode"),
            ("D40 height result", "height algorithm result"),
        ]
        for i, (key, hint) in enumerate(labels):
            ttk.Label(status, text=key + ":", width=24).grid(row=i, column=0, sticky="w")
            value_lbl = ttk.Label(status, text="—", width=14, foreground="blue", font=("Consolas", 10, "bold"))
            value_lbl.grid(row=i, column=1, sticky="w")
            self.legacy_status_labels[key] = value_lbl
            ttk.Label(status, text=hint, foreground="gray").grid(row=i, column=2, sticky="w")

    def _build_v2_tab(self, parent: ttk.Frame) -> None:
        # Selector + fire
        ctrl = ttk.LabelFrame(parent, text="Trigger (writes D14/D34 ProductType + D1/D2 START_TASK)", padding=6)
        ctrl.pack(fill="x", padx=4, pady=4)

        # Camera radio buttons
        cam_frame = ttk.Frame(ctrl)
        cam_frame.pack(side="left", padx=4)
        ttk.Label(cam_frame, text="Camera:").pack(anchor="w")
        ttk.Radiobutton(cam_frame, text="cam1 (D14, D70+)", variable=self.v2_camera, value=1).pack(anchor="w")
        ttk.Radiobutton(cam_frame, text="cam2 (D34, D90+)", variable=self.v2_camera, value=2).pack(anchor="w")

        # ProductType radio buttons
        pt_frame = ttk.Frame(ctrl)
        pt_frame.pack(side="left", padx=20)
        ttk.Label(pt_frame, text="ProductType:").pack(anchor="w")
        for value, name in [(1, "TOOTHPASTE_FRONTBACK"), (2, "HEIGHT_CHECK"), (3, "BRUSH_HEAD")]:
            ttk.Radiobutton(
                pt_frame, text=f"{name} ({value})",
                variable=self.v2_product_type, value=value,
            ).pack(anchor="w")

        # Fire / Loop / Stop buttons (vertical stack)
        fire_frame = ttk.Frame(ctrl)
        fire_frame.pack(side="left", padx=20, fill="y")
        self.v2_fire_btn = ttk.Button(
            fire_frame, text="FIRE SINGLE\n(D=10, START_TASK)",
            command=self._fire_v2, width=22,
        )
        self.v2_fire_btn.pack(pady=2, fill="x")

        self.v2_fire_loop_btn = ttk.Button(
            fire_frame, text="FIRE LOOP\n(D=11, START_LOOP)",
            command=self._fire_v2_loop, width=22,
        )
        self.v2_fire_loop_btn.pack(pady=2, fill="x")

        self.v2_stop_btn = ttk.Button(
            fire_frame, text="STOP LOOP\n(D=0, IDLE)",
            command=self._stop_v2, width=22,
        )
        self.v2_stop_btn.pack(pady=2, fill="x")

        # Per-camera state panels (side by side)
        state_frame = ttk.Frame(parent)
        state_frame.pack(fill="both", padx=4, pady=4, expand=True)

        for column, cam_num in enumerate([1, 2]):
            self._build_v2_camera_panel(state_frame, cam_num, column)
        state_frame.columnconfigure(0, weight=1)
        state_frame.columnconfigure(1, weight=1)

    def _build_v2_camera_panel(self, parent: ttk.Frame, cam_num: int, column: int) -> None:
        config_base = V2_CAM1_CONFIG_START if cam_num == 1 else V2_CAM2_CONFIG_START
        status_addr = V2_CAM1_STATUS if cam_num == 1 else V2_CAM2_STATUS
        result_addr = V2_CAM1_RESULT if cam_num == 1 else V2_CAM2_RESULT
        x_addr = V2_CAM1_OUTPUT_X if cam_num == 1 else V2_CAM2_OUTPUT_X
        y_addr = V2_CAM1_OUTPUT_Y if cam_num == 1 else V2_CAM2_OUTPUT_Y
        area_addr = V2_CAM1_AREA if cam_num == 1 else V2_CAM2_AREA

        panel = ttk.LabelFrame(parent, text=f"Cam {cam_num} state", padding=6)
        panel.grid(row=0, column=column, padx=4, pady=4, sticky="nsew")

        labels = [
            (f"D{status_addr}  status", "0=IDLE 3=DONE 10=START"),
            (f"D{config_base + V2_OFFSET_EXPOSURE}  exposure (us)", ""),
            (f"D{config_base + V2_OFFSET_PRODUCT_TYPE}  product_type", "1/2/3 see above"),
            (f"D{result_addr}  result", "1=OK 2=NG"),
            (f"D{x_addr}-{x_addr + 3}  output_x", "decoded float64"),
            (f"D{y_addr}-{y_addr + 3}  output_y", "decoded float64"),
            (f"D{area_addr}-{area_addr + 1}  area", "uint32"),
        ]
        for i, (key, hint) in enumerate(labels):
            ttk.Label(panel, text=key + ":", width=22).grid(row=i, column=0, sticky="w")
            value_lbl = ttk.Label(panel, text="—", width=18, foreground="blue", font=("Consolas", 10, "bold"))
            value_lbl.grid(row=i, column=1, sticky="w")
            # Store under "cam{num}.{key}" so refresh can find each
            self.v2_status_labels[f"cam{cam_num}.{key}"] = value_lbl
            ttk.Label(panel, text=hint, foreground="gray").grid(row=i, column=2, sticky="w")

    def _build_log_panel(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Activity Log", padding=6)
        frame.pack(fill="x", padx=8, pady=4)
        self.log = scrolledtext.ScrolledText(frame, height=8, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    # ----------------------------------------------------------- behaviour

    def _log(self, msg: str) -> None:
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")

    def _toggle_connect(self) -> None:
        if self.plc is not None:
            self.plc.close()
            self.plc = None
            self.auto_poll.set(False)
            self.conn_status.config(text="Disconnected", foreground="red")
            self.connect_btn.config(text="Connect")
            self._log("disconnected")
            return

        host = self.ip_entry.get().strip()
        try:
            port = int(self.port_entry.get())
        except ValueError:
            messagebox.showerror("Bad port", "Port must be a number")
            return

        try:
            plc = PLC(host, port)
            if not plc.open():
                self._log(f"connect failed: {host}:{port}")
                messagebox.showerror("Connect failed", f"Cannot reach PLC at {host}:{port}")
                return
        except Exception as exc:
            self._log(f"connect error: {exc}")
            messagebox.showerror("Connect error", str(exc))
            return

        self.plc = plc
        self.conn_status.config(text=f"Connected to {host}:{port}", foreground="green")
        self.connect_btn.config(text="Disconnect")
        self._log(f"connected to {host}:{port}")
        self._refresh_status()

    # ---------------- legacy tab actions ----------------

    def _fire_legacy(self, mode: int, label: str) -> None:
        if self.plc is None:
            messagebox.showwarning("Not connected", "Connect to the PLC first.")
            return
        self._log(f"[legacy] firing {label}: D2={mode}, D1=10")
        self.legacy_fb_btn.config(state="disabled")
        self.legacy_height_btn.config(state="disabled")
        threading.Thread(target=self._fire_legacy_worker, args=(mode,), daemon=True).start()

    def _fire_legacy_worker(self, mode: int) -> None:
        plc = self.plc
        assert plc is not None
        try:
            plc.legacy_fire(mode)
            self._wait_handshake(
                read_fn=plc.legacy_read_d1,
                ack_value=LEGACY_TRIGGER_IDLE,
                done_value=LEGACY_TRIGGER_DONE,
                tag="legacy",
            )
        except Exception as exc:
            self.queue.put(("log", f"  [legacy] fire error: {exc}"))
        finally:
            self.queue.put(("legacy_buttons_on", None))
            self.queue.put(("refresh", None))

    def _loop_legacy(self, mode: int, label: str) -> None:
        """Start a legacy LOOP — write D2 + D1=11. No handshake polling
        because the loop runs until the user clicks STOP."""
        if self.plc is None:
            messagebox.showwarning("Not connected", "Connect to the PLC first.")
            return
        self._log(f"[legacy LOOP] start {label}: D2={mode}, D1=11")
        try:
            self.plc.legacy_fire_loop(mode)
        except Exception as exc:
            self._log(f"  [legacy LOOP] error: {exc}")
            return
        self._log("  -> orchestrator now running LOOP. Click STOP LOOP to halt. "
                  "(Requires binary v0.3.6+; older builds silently ignore D1=11.)")
        self._refresh_status()

    def _stop_legacy_loop(self) -> None:
        """Halt a running legacy LOOP by writing D1=0 (TRIGGER_IDLE)."""
        if self.plc is None:
            messagebox.showwarning("Not connected", "Connect to the PLC first.")
            return
        self._log("[legacy STOP] D1=0 (IDLE)")
        try:
            self.plc.legacy_stop_loop()
        except Exception as exc:
            self._log(f"  [legacy STOP] error: {exc}")
            return
        self._refresh_status()

    # ---------------- v2 tab actions ----------------

    def _fire_v2(self) -> None:
        if self.plc is None:
            messagebox.showwarning("Not connected", "Connect to the PLC first.")
            return
        cam = self.v2_camera.get()
        pt = self.v2_product_type.get()
        pt_name = V2_PRODUCT_NAMES.get(pt, str(pt))
        config_base = V2_CAM1_CONFIG_START if cam == 1 else V2_CAM2_CONFIG_START
        status_addr = V2_CAM1_STATUS if cam == 1 else V2_CAM2_STATUS
        self._log(
            f"[v2] firing cam{cam} {pt_name}: "
            f"D{config_base + V2_OFFSET_PRODUCT_TYPE}={pt}, D{status_addr}=10"
        )
        self.v2_fire_btn.config(state="disabled")
        threading.Thread(target=self._fire_v2_worker, args=(cam, pt), daemon=True).start()

    def _fire_v2_worker(self, camera_num: int, product_type: int) -> None:
        plc = self.plc
        assert plc is not None
        try:
            plc.v2_fire(camera_num, product_type)
            self._wait_handshake(
                read_fn=lambda: plc.v2_read_camera_status(camera_num),
                ack_value=0,  # IDLE
                done_value=3,  # TASK_COMPLETED
                tag=f"v2 cam{camera_num}",
            )
        except Exception as exc:
            self.queue.put(("log", f"  [v2] fire error: {exc}"))
        finally:
            self.queue.put(("v2_buttons_on", None))
            self.queue.put(("refresh", None))

    def _fire_v2_loop(self) -> None:
        """Start continuous capture on the selected camera. Loop runs until
        the user clicks STOP LOOP — no handshake polling because START_LOOP
        does not terminate by itself."""
        if self.plc is None:
            messagebox.showwarning("Not connected", "Connect to the PLC first.")
            return
        cam = self.v2_camera.get()
        pt = self.v2_product_type.get()
        pt_name = V2_PRODUCT_NAMES.get(pt, str(pt))
        config_base = V2_CAM1_CONFIG_START if cam == 1 else V2_CAM2_CONFIG_START
        status_addr = V2_CAM1_STATUS if cam == 1 else V2_CAM2_STATUS
        self._log(
            f"[v2 LOOP] start cam{cam} {pt_name}: "
            f"D{config_base + V2_OFFSET_PRODUCT_TYPE}={pt}, D{status_addr}=11"
        )
        try:
            self.plc.v2_fire_loop(cam, pt)
        except Exception as exc:
            self._log(f"  [v2 LOOP] start error: {exc}")
            return
        self._log("  -> orchestrator now running continuously. Click STOP LOOP to halt. "
                  "Enable Auto-refresh to see live state.")
        self._refresh_status()

    def _stop_v2(self) -> None:
        """Halt a running START_LOOP by writing IDLE (0) to the camera status."""
        if self.plc is None:
            messagebox.showwarning("Not connected", "Connect to the PLC first.")
            return
        cam = self.v2_camera.get()
        status_addr = V2_CAM1_STATUS if cam == 1 else V2_CAM2_STATUS
        self._log(f"[v2 STOP] cam{cam}: D{status_addr}=0 (IDLE)")
        try:
            self.plc.v2_stop_loop(cam)
        except Exception as exc:
            self._log(f"  [v2 STOP] error: {exc}")
            return
        self._refresh_status()

    # ---------------- shared handshake polling ----------------

    def _wait_handshake(self, read_fn, ack_value: int, done_value: int, tag: str) -> None:
        """Poll a status register until it reaches done_value or 5s elapses.

        Both legacy (D1: 10→0→1) and v2 (D1/D2: 10→0→3) follow the same
        fire→ack→done pattern, just with different "done" sentinel values.
        """
        saw_ack = False
        last = -999
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            value = read_fn()
            value_show = -1 if value is None else value
            if value_show != last:
                self.queue.put(("log", f"  [{tag}] status = {value_show}"))
                last = value_show
            if value_show == ack_value:
                saw_ack = True
            if value_show == done_value:
                self.queue.put(("log", f"  [{tag}] -> done"))
                return
            time.sleep(0.05)
        if not saw_ack:
            self.queue.put(("log", f"  [{tag}] X NEVER GOT ACK — orchestrator down or wrong protocol?"))
        else:
            self.queue.put(("log", f"  [{tag}] X ACK seen but never DONE — algorithm hung; check ~/my_app.log"))

    # ---------------- status refresh ----------------

    def _refresh_status(self) -> None:
        if self.plc is None:
            return
        try:
            block = self.plc.read_block()
        except Exception as exc:
            self._log(f"status read error: {exc}")
            return
        if not block or len(block) < 107:
            self._log(f"status read failed (got {len(block) if block else 0} regs)")
            return

        self._refresh_legacy(block)
        self._refresh_v2(block)

    def _refresh_legacy(self, block: list[int]) -> None:
        for label, value, _ in format_legacy_status(block):
            lbl = self.legacy_status_labels.get(label)
            if lbl is None:
                continue
            color = "blue"
            if label.startswith("D3 ") or label.startswith("D4 "):
                color = "green" if value == "1" else "red"
            lbl.config(text=value, foreground=color)

    def _refresh_v2(self, block: list[int]) -> None:
        for cam in (1, 2):
            config_base = V2_CAM1_CONFIG_START if cam == 1 else V2_CAM2_CONFIG_START
            status_addr = V2_CAM1_STATUS if cam == 1 else V2_CAM2_STATUS
            result_addr = V2_CAM1_RESULT if cam == 1 else V2_CAM2_RESULT
            x_addr = V2_CAM1_OUTPUT_X if cam == 1 else V2_CAM2_OUTPUT_X
            y_addr = V2_CAM1_OUTPUT_Y if cam == 1 else V2_CAM2_OUTPUT_Y
            area_addr = V2_CAM1_AREA if cam == 1 else V2_CAM2_AREA

            status = block[status_addr]
            exposure = block[config_base + V2_OFFSET_EXPOSURE]
            product_type = block[config_base + V2_OFFSET_PRODUCT_TYPE]
            result = block[result_addr]
            x_val = words_to_double_le(block[x_addr : x_addr + 4])
            y_val = words_to_double_le(block[y_addr : y_addr + 4])
            area = words_to_uint32_le(block[area_addr], block[area_addr + 1])

            self._set_v2(cam, "status", f"{status}  {V2_STATUS_NAMES.get(status, '?')}")
            self._set_v2(cam, "exposure (us)", f"{exposure}")
            self._set_v2(cam, "product_type", f"{product_type}  {V2_PRODUCT_NAMES.get(product_type, '?')}")
            self._set_v2(cam, "result", f"{result}  {'OK' if result == 1 else 'NG' if result == 2 else '?'}",
                         color=("green" if result == 1 else "red" if result == 2 else "blue"))
            self._set_v2(cam, "output_x", f"{x_val:.4f}")
            self._set_v2(cam, "output_y", f"{y_val:.4f}")
            self._set_v2(cam, "area", str(area))

    def _set_v2(self, cam_num: int, suffix: str, value: str, color: str = "blue") -> None:
        # Find the matching key by suffix — the prefix encodes the D-address
        # which the user doesn't care about for setting purposes.
        key_prefix = f"cam{cam_num}."
        for full_key, lbl in self.v2_status_labels.items():
            if full_key.startswith(key_prefix) and full_key.endswith(suffix):
                lbl.config(text=value, foreground=color)
                return

    # ---------------- auto-poll loop ----------------

    def _on_auto_poll_toggle(self) -> None:
        if self.auto_poll.get() and self.plc is not None:
            self._poll_loop()

    def _poll_loop(self) -> None:
        if not self.auto_poll.get() or self.plc is None:
            return
        self._refresh_status()
        self.root.after(self.POLL_INTERVAL_MS, self._poll_loop)

    # ---------------- thread→UI message pump ----------------

    def _process_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "refresh":
                    self._refresh_status()
                elif kind == "legacy_buttons_on":
                    self.legacy_fb_btn.config(state="normal")
                    self.legacy_height_btn.config(state="normal")
                elif kind == "v2_buttons_on":
                    self.v2_fire_btn.config(state="normal")
        except Empty:
            pass
        self.root.after(100, self._process_queue)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--plc", default="192.168.1.10", help="PLC IP")
    p.add_argument("--port", type=int, default=502)
    p.add_argument(
        "command", nargs="?", default="gui",
        choices=["gui", "status", "frontback", "height", "watch"],
        help="default: gui",
    )
    args = p.parse_args()

    if args.command == "gui":
        root = tk.Tk()
        PLCTesterGUI(root, host=args.plc, port=args.port)
        root.mainloop()
        return

    plc = PLC(args.plc, args.port)
    if not plc.open():
        sys.exit(f"failed to connect to {args.plc}:{args.port}")
    print(f"connected: {args.plc}:{args.port}\n")

    try:
        if args.command == "status":
            cli_status(plc)
        elif args.command == "frontback":
            cli_fire(plc, LEGACY_MODE_FRONTBACK, "frontback / 正反检测")
        elif args.command == "height":
            cli_fire(plc, LEGACY_MODE_HEIGHT, "height / 高度检测")
        elif args.command == "watch":
            cli_watch(plc)
    finally:
        plc.close()


if __name__ == "__main__":
    main()
