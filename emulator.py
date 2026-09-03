# -*- coding: utf-8 -*-
"""
三合一 Taiseia UDP 模擬器（Lamp / Fan / AC）
- 多播：224.0.23.0:36110
- 單播：(0.0.0.0:36110) 通配
- 額外：每個裝置可各自綁定獨立 IP:36110，讓 Controller 可用固定 IP 控制
  （必須是本機網卡已設定的 IP，否則綁定會失敗；失敗不影響其他通道）

封包（通用，表10）：
  byte0  len (=0x06)
  byte1  SA
  byte2  (R/W<<7) | SID
  byte3  data_hi
  byte4  data_lo
  byte5  XOR(byte0..byte4)

註冊（表18）：
  06 00 00 FF FF xor   註冊
  06 00 00 00 00 xor   解除

【本版差異】
  1) 若封包送到某裝置的「獨立綁定 IP」socket，註冊/解除只作用在該裝置；多播或 ANY 則三裝置一起處理。
  2) **未註冊就不能控制**：對未註冊裝置的寫入指令（R/W=1）會被忽略，僅回覆目前狀態。
"""
import os
import socket
import struct
import threading
import tkinter as tk
from tkinter import ttk

# ---------- 網路參數 ----------
MCAST_GRP = "224.0.23.0"
UDP_PORT  = 36110
SOCK_TIMEOUT = 0.5

# ---------- 圖檔 ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG = {
    "lamp_off": os.path.join(BASE_DIR, "lamp_off.png"),
    "lamp_on" : os.path.join(BASE_DIR, "lamp_on.png"),
    "fan_off" : os.path.join(BASE_DIR, "fan_off.png"),
    "fan_on"  : os.path.join(BASE_DIR, "fan_on.png"),
    "ac_off"  : os.path.join(BASE_DIR, "AC_off.png"),
    "ac_cool" : os.path.join(BASE_DIR, "AC_COOL.png"),
    "ac_dry"  : os.path.join(BASE_DIR, "AC_DRY.png"),
    "ac_fan"  : os.path.join(BASE_DIR, "AC_FAN.png"),
    "ac_auto" : os.path.join(BASE_DIR, "AC_AUTO.png"),
    "ac_heat" : os.path.join(BASE_DIR, "AC_HEAT.png"),
}

# ---------- SA / SID ----------
SA_REG = 0x00      # 註冊專用
SA_AC  = 0x01
SA_FAN = 0x0F
SA_LAMP= 0x11

SID_LAMP_POWER = 0x00

SID_AC_POWER   = 0x00
SID_AC_MODE    = 0x01   # 0=COOL 1=DRY 2=FAN 3=AUTO 4=HEAT
SID_AC_FAN     = 0x02   # 1~5
SID_AC_TEMP    = 0x03
SID_AC_SWING   = 0x0E

SID_FAN_POWER  = 0x00
SID_FAN_MODE   = 0x01   # 0=Auto 1..4
SID_FAN_SPEED  = 0x02   # 1..5
SID_FAN_SWING  = 0x05   # 0/1


# ---------- 小工具 ----------
def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def xor_checksum(b: bytes) -> int:
    x = 0
    for v in b:
        x ^= v
    return x & 0xFF

def hexline(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)

def build_general(sa: int, rw: int, sid: int, value: int) -> bytes:
    b0 = 0x06
    b1 = sa & 0xFF
    b2 = ((1 if rw else 0) << 7) | (sid & 0x7F)
    b3 = (value >> 8) & 0xFF
    b4 = value & 0xFF
    body = bytes([b0, b1, b2, b3, b4])
    return body + bytes([xor_checksum(body)])

def build_register(is_register: bool) -> bytes:
    body = bytes([0x06, 0x00, 0x00, 0xFF if is_register else 0x00, 0xFF if is_register else 0x00])
    return body + bytes([xor_checksum(body)])


# ---------- GUI ----------
class SimulatorGUI:
    def __init__(self, master: tk.Tk):
        self.master = master
        master.configure(bg="white")
        master.title("Simulator (Taiseia UDP + per-device IP)")

        # 狀態
        self.lamp_registered = False
        self.lamp_state = 0

        self.fan_registered = False
        self.fan_power, self.fan_mode, self.fan_speed, self.fan_swing = 0, 0, 1, 0

        self.ac_registered = False
        self.ac_power, self.ac_mode, self.ac_temp, self.ac_fan, self.ac_swing = 0, 0, 24, 1, 0

        # 圖片
        self._imgs = {k: tk.PhotoImage(file=v) for k, v in IMG.items()}

        # ---- 標頭 ----
        head = tk.Label(
            master,
            text=f"My IP: {local_ip()}    UDP *:{UDP_PORT}    MCast: {MCAST_GRP}:{UDP_PORT}",
            bg="white", anchor="w")
        head.pack(fill="x", padx=10, pady=(10, 4))

        # ---- 每裝置固定 IP 綁定 ----
        ipbox = tk.LabelFrame(master, text="每裝置固定 IP (綁定到 36110)", bg="white")
        ipbox.pack(fill="x", padx=10, pady=(0, 8))

        row = tk.Frame(ipbox, bg="white"); row.pack(fill="x", padx=8, pady=6)
        tk.Label(row, text="Lamp IP：", bg="white").pack(side="left")
        self.ip_lamp = ttk.Entry(row, width=16); self.ip_lamp.pack(side="left", padx=(2, 12))
        tk.Label(row, text="Fan IP：", bg="white").pack(side="left")
        self.ip_fan = ttk.Entry(row, width=16); self.ip_fan.pack(side="left", padx=(2, 12))
        tk.Label(row, text="AC IP：", bg="white").pack(side="left")
        self.ip_ac = ttk.Entry(row, width=16); self.ip_ac.pack(side="left", padx=(2, 12))
        ttk.Button(row, text="啟用 / 重綁", command=self.rebind_device_ips).pack(side="left")

        self.bind_status = tk.Label(ipbox, text="（未綁定。留空代表不使用獨立 IP，只收多播/通配）", anchor="w", bg="white", fg="#555")
        self.bind_status.pack(fill="x", padx=8, pady=(0, 6))

        # ---- 三個設備 ----
        devices = tk.Frame(master, bg="white"); devices.pack(padx=10, pady=6)

        # Lamp
        lamf = tk.Frame(devices, bg="white"); lamf.pack(side="left", padx=12)
        self.pic_lamp = tk.Label(lamf, image=self._imgs["lamp_off"], bg="white"); self.pic_lamp.pack()
        lrow = tk.Frame(lamf, bg="white"); lrow.pack(pady=(6, 2))
        ttk.Button(lrow, text="Power ON",  width=12, command=self._lamp_local_on).pack(side="left", padx=4)
        ttk.Button(lrow, text="Power OFF", width=12, command=self._lamp_local_off).pack(side="left", padx=4)
        self.lbl_lamp = tk.Label(lamf, text=self._lamp_text(), bg="white"); self.lbl_lamp.pack(pady=(2, 0))

        # Fan
        fanf = tk.Frame(devices, bg="white"); fanf.pack(side="left", padx=12)
        self.pic_fan = tk.Label(fanf, image=self._imgs["fan_off"], bg="white"); self.pic_fan.pack()
        frow = tk.Frame(fanf, bg="white"); frow.pack(pady=(6, 2))
        ttk.Button(frow, text="Power ON",  width=12, command=self._fan_local_on).pack(side="left", padx=4)
        ttk.Button(frow, text="Power OFF", width=12, command=self._fan_local_off).pack(side="left", padx=4)
        self.lbl_fan = tk.Label(fanf, text=self._fan_text(), bg="white"); self.lbl_fan.pack(pady=(2, 0))

        # AC
        acf = tk.Frame(devices, bg="white"); acf.pack(side="left", padx=12)
        self.pic_ac = tk.Label(acf, image=self._imgs["ac_off"], bg="white"); self.pic_ac.pack()
        arow = tk.Frame(acf, bg="white"); arow.pack(pady=(6, 2))
        ttk.Button(arow, text="Power ON",  width=12, command=self._ac_local_on).pack(side="left", padx=4)
        ttk.Button(arow, text="Power OFF", width=12, command=self._ac_local_off).pack(side="left", padx=4)
        self.lbl_ac = tk.Label(acf, text=self._ac_text(), bg="white"); self.lbl_ac.pack(pady=(2, 0))

        # ---- 封包區 ----
        box = tk.LabelFrame(master, text="封包 (TX / RX)", bg="white")
        box.pack(fill="both", expand=False, padx=10, pady=(6, 12))
        self.rx_lbl = tk.Label(box, text="RX: —", anchor="w", justify="left", bg="white")
        self.tx_lbl = tk.Label(box, text="TX: —", anchor="w", justify="left", bg="white")
        self.rx_lbl.pack(fill="x", padx=8, pady=2)
        self.tx_lbl.pack(fill="x", padx=8, pady=2)

        # ---- 啟動 socket ----
        self.socks = []   # [(sock, force_dev_name or None)]
        self.rebind_device_ips()            # 先依 UI 綁一次（可能都空）
        self.start_multicast_and_any()      # 多播 + 通配

    # ====== UI 動作（本地快速鍵，不送網路） ======
    def _lamp_local_on(self):  self.lamp_state = 1; self._upd_lamp()
    def _lamp_local_off(self): self.lamp_state = 0; self._upd_lamp()
    def _fan_local_on(self):   self.fan_power = 1; self._upd_fan()
    def _fan_local_off(self):  self.fan_power = 0; self._upd_fan()
    def _ac_local_on(self):    self.ac_power = 1; self._upd_ac()
    def _ac_local_off(self):   self.ac_power = 0; self._upd_ac()

    def _lamp_text(self):
        return f"Lamp: {'ON' if self.lamp_state else 'OFF'} | Registered:{'Y' if self.lamp_registered else 'N'}"
    def _fan_text(self):
        mode_map = {0:'Auto',1:'模式一',2:'模式二',3:'模式三',4:'模式四'}
        return f"Fan: {'ON' if self.fan_power else 'OFF'} | Mode:{mode_map.get(self.fan_mode,self.fan_mode)} | Speed:{self.fan_speed} | Swing:{'ON' if self.fan_swing else 'OFF'} | Registered:{'Y' if self.fan_registered else 'N'}"
    def _ac_text(self):
        mode_map = {0:'冷氣',1:'除濕',2:'送風',3:'自動',4:'暖氣'}
        return f"AC: {'ON' if self.ac_power else 'OFF'} | Mode:{mode_map.get(self.ac_mode,self.ac_mode)} | Temp:{self.ac_temp}°C | Fan:{self.ac_fan} | Swing:{'ON' if self.ac_swing else 'OFF'} | Registered:{'Y' if self.ac_registered else 'N'}"

    def _upd_lamp(self):
        self.pic_lamp.config(image=self._imgs["lamp_on" if self.lamp_state else "lamp_off"])
        self.lbl_lamp.config(text=self._lamp_text())
    def _upd_fan(self):
        self.pic_fan.config(image=self._imgs["fan_on" if self.fan_power else "fan_off"])
        self.lbl_fan.config(text=self._fan_text())
    def _upd_ac(self):
        key = "ac_off"
        if self.ac_power:
            key = {0:'ac_cool',1:'ac_dry',2:'ac_fan',3:'ac_auto',4:'ac_heat'}.get(self.ac_mode, 'ac_cool')
        self.pic_ac.config(image=self._imgs[key])
        self.lbl_ac.config(text=self._ac_text())

    # ====== 綁定 per-device IP ======
    def rebind_device_ips(self):
        # 關閉舊 socket（per-device）
        for s, tag in list(self.socks):
            try:
                if tag in ("lamp","fan","ac"):
                    s.close()
                    self.socks.remove((s, tag))
            except Exception:
                pass

        bind_msgs = []
        for tag, ip_entry in (("lamp", self.ip_lamp), ("fan", self.ip_fan), ("ac", self.ip_ac)):
            ip = ip_entry.get().strip()
            if not ip:
                bind_msgs.append(f"{tag}: 未設定")
                continue
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((ip, UDP_PORT))  # 同一個埠，綁不同本機 IP
                s.settimeout(SOCK_TIMEOUT)
                self.socks.append((s, tag))
                threading.Thread(target=self._recv_loop, args=(s, tag), daemon=True).start()
                bind_msgs.append(f"{tag}: 綁定 {ip}:{UDP_PORT} 成功")
            except Exception as e:
                bind_msgs.append(f"{tag}: 綁定失敗 ({e})")

        self.bind_status.config(text="；".join(bind_msgs))

    # ====== 多播 + ANY ======
    def start_multicast_and_any(self):
        # ANY（含單播送到本機任何 IP）
        s_any = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_any.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s_any.bind(("", UDP_PORT))
        s_any.settimeout(SOCK_TIMEOUT)
        self.socks.append((s_any, None))
        threading.Thread(target=self._recv_loop, args=(s_any, None), daemon=True).start()

        # Multicast
        try:
            s_mc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s_mc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s_mc.bind(("", UDP_PORT))
            mreq = struct.pack("=4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
            s_mc.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            s_mc.settimeout(SOCK_TIMEOUT)
            self.socks.append((s_mc, None))
            threading.Thread(target=self._recv_loop, args=(s_mc, None), daemon=True).start()
        except Exception:
            pass  # 有些環境不允許重複綁；ANY 已足夠

    # ====== 接收迴圈 ======
    def _recv_loop(self, sock: socket.socket, force_tag):
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            self.master.after(0, self.rx_lbl.config, {"text": f"RX {addr[0]}:{addr[1]}  {hexline(data)}"})
            self.handle_frame(data, addr, force_tag, resp_sock=sock)

    # ====== 解析封包 ======
    def handle_frame(self, data: bytes, addr, force_tag, resp_sock: socket.socket):
        if len(data) < 6 or data[0] != 0x06 or xor_checksum(data[:-1]) != data[-1]:
            return

        sa_from_pkt  = data[1]          # 原始 SA
        rw  = (data[2] >> 7) & 1
        sid = data[2] & 0x7F
        val = (data[3] << 8) | data[4]

        # 若是 per-device socket，強制視為該裝置（僅影響一般命令 SA 判別）
        sa = sa_from_pkt
        if force_tag == "lamp":
            sa = SA_LAMP
        elif force_tag == "fan":
            sa = SA_FAN
        elif force_tag == "ac":
            sa = SA_AC

        # ---- 註冊/解除 ----
        if sa_from_pkt == SA_REG and sid == 0x00 and rw == 0:
            is_register = (data[3], data[4]) == (0xFF, 0xFF)
            if force_tag in ("lamp", "fan", "ac"):
                if force_tag == "lamp":
                    self.lamp_registered = is_register; self._upd_lamp()
                elif force_tag == "fan":
                    self.fan_registered  = is_register; self._upd_fan()
                elif force_tag == "ac":
                    self.ac_registered   = is_register; self._upd_ac()
                self._send(resp_sock, build_register(is_register), addr)
            else:
                self.lamp_registered = self.fan_registered = self.ac_registered = is_register
                self._upd_lamp(); self._upd_fan(); self._upd_ac()
                self._send(resp_sock, build_register(is_register), addr)
            return

        # ---- 寫入權限檢查（未註冊不可寫） ----
        def can_write_device(tag: str) -> bool:
            if tag == "lamp": return self.lamp_registered
            if tag == "fan":  return self.fan_registered
            if tag == "ac":   return self.ac_registered
            return False

        # Lamp
        if sa == SA_LAMP:
            if sid == SID_LAMP_POWER:
                if rw == 1 and can_write_device("lamp"):
                    self.lamp_state = 1 if val else 0
                    self._upd_lamp()
                # 仍回覆目前狀態（即使未註冊時被忽略）
                self._send(resp_sock, build_general(SA_LAMP, 0, SID_LAMP_POWER, 1 if self.lamp_state else 0), addr)
            return

        # Fan
        if sa == SA_FAN:
            if sid == SID_FAN_POWER:
                if rw == 1 and can_write_device("fan"):
                    self.fan_power = 1 if val else 0; self._upd_fan()
                self._send(resp_sock, build_general(SA_FAN, 0, SID_FAN_POWER, 1 if self.fan_power else 0), addr)
            elif sid == SID_FAN_MODE:
                if rw == 1 and can_write_device("fan"):
                    self.fan_mode = val & 0xFFFF; self._upd_fan()
                self._send(resp_sock, build_general(SA_FAN, 0, SID_FAN_MODE, self.fan_mode), addr)
            elif sid == SID_FAN_SPEED:
                if rw == 1 and can_write_device("fan"):
                    self.fan_speed = max(1, min(5, val & 0xFFFF)); self._upd_fan()
                self._send(resp_sock, build_general(SA_FAN, 0, SID_FAN_SPEED, self.fan_speed), addr)
            elif sid == SID_FAN_SWING:
                if rw == 1 and can_write_device("fan"):
                    self.fan_swing = 1 if val else 0; self._upd_fan()
                self._send(resp_sock, build_general(SA_FAN, 0, SID_FAN_SWING, self.fan_swing), addr)
            return

        # AC
        if sa == SA_AC:
            if sid == SID_AC_POWER:
                if rw == 1 and can_write_device("ac"):
                    self.ac_power = 1 if val else 0; self._upd_ac()
                self._send(resp_sock, build_general(SA_AC, 0, SID_AC_POWER, 1 if self.ac_power else 0), addr)
            elif sid == SID_AC_MODE:
                if rw == 1 and can_write_device("ac"):
                    self.ac_mode = val & 0xFFFF; self._upd_ac()
                self._send(resp_sock, build_general(SA_AC, 0, SID_AC_MODE, self.ac_mode), addr)
            elif sid == SID_AC_FAN:
                if rw == 1 and can_write_device("ac"):
                    self.ac_fan = max(1, min(5, val & 0xFFFF)); self._upd_ac()
                self._send(resp_sock, build_general(SA_AC, 0, SID_AC_FAN, self.ac_fan), addr)
            elif sid == SID_AC_TEMP:
                if rw == 1 and can_write_device("ac"):
                    self.ac_temp = val & 0xFFFF; self._upd_ac()
                self._send(resp_sock, build_general(SA_AC, 0, SID_AC_TEMP, self.ac_temp), addr)
            elif sid == SID_AC_SWING:
                if rw == 1 and can_write_device("ac"):
                    self.ac_swing = 1 if val else 0; self._upd_ac()
                self._send(resp_sock, build_general(SA_AC, 0, SID_AC_SWING, self.ac_swing), addr)
            return

    def _send(self, sock: socket.socket, payload: bytes, addr):
        try:
            sock.sendto(payload, addr)
            self.master.after(0, self.tx_lbl.config, {"text": f"TX {addr[0]}:{addr[1]}  {hexline(payload)}"})
        except Exception:
            pass


# ---------- 進入點 ----------
if __name__ == "__main__":
    root = tk.Tk()
    app = SimulatorGUI(root)

    # 視窗置中 & 白色底
    root.update_idletasks()
    W, H = 1050, 650
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    x, y = (sw - W) // 2, (sh - H) // 2
    root.geometry(f"{W}x{H}+{x}+{y}")
    root.configure(bg="white")
    root.mainloop()
