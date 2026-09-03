# -*- coding: utf-8 -*-
# TaiSEIA HTTP Controller — multicast 224.0.23.0:36110

import socket, struct, threading, time, platform
from datetime import datetime
from typing import List, Tuple, Optional, Dict
from flask import Flask, request, jsonify, render_template_string

MCAST_GRP  = "224.0.23.0"
MCAST_PORT = 36110
RX_BUF_LEN = 4096
LOG_LIMIT  = 600

# ---------------- TaiSEIA helpers ----------------
def xor_checksum(bs: bytes) -> int:
    x = 0
    for b in bs: x ^= b
    return x & 0xFF

def pkt_general(sa: int, rw: int, sid: int, value: int) -> bytes:
    b0 = 0x06
    b1 = sa & 0xFF
    b2 = ((1 if rw else 0) << 7) | (sid & 0x7F)
    b3 = (value >> 8) & 0xFF
    b4 = value & 0xFF
    b5 = xor_checksum(bytes([b0,b1,b2,b3,b4]))
    return bytes([b0,b1,b2,b3,b4,b5])

def pkt_register_req() -> bytes:
    base = [0x06,0x00,0x00,0xFF,0xFF]
    base.append(xor_checksum(bytes(base)))
    return bytes(base)

def pkt_unregister_req() -> bytes:
    base = [0x06,0x00,0x00,0x00,0x00]
    base.append(xor_checksum(bytes(base)))
    return bytes(base)

def parse_general(bs: bytes) -> Optional[dict]:
    if len(bs) != 6: return None
    length, sa, rwsid, d_hi, d_lo, cs = bs
    if length != 0x06: return None
    rw  = (rwsid >> 7) & 0x01
    sid = rwsid & 0x7F
    ok  = (xor_checksum(bs[:5]) == cs)
    return {"sa":sa, "rw":rw, "sid":sid, "value":(d_hi<<8)|d_lo, "xor_ok":ok}

def hex_str(bs: bytes) -> str:
    return " ".join(f"{b:02X}" for b in bs)

# ---------------- UDP socket（單一顆） ----------------
def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

class TaiSeiaSock:
    def __init__(self, mgroup: str, port: int):
        self.group = mgroup
        self.port  = port
        self.local_ip = get_local_ip()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            if platform.system().lower().startswith("win"):
                self.sock.bind((self.local_ip, port))
            else:
                self.sock.bind(("", port))
        except PermissionError:
            self.sock.bind(("", port))

        try:
            mreq = struct.pack("=4s4s", socket.inet_aton(mgroup), socket.inet_aton(self.local_ip))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            mreq = struct.pack("=4sl", socket.inet_aton(mgroup), socket.INADDR_ANY)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.local_ip))
        except OSError:
            pass
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

    def send_to(self, payload: bytes, ip: str):
        self.sock.sendto(payload, (ip, self.port))

    def recvfrom(self, bufsize=RX_BUF_LEN):
        return self.sock.recvfrom(bufsize)

# ---------------- App / storage ----------------
app  = Flask(__name__)
udp  = TaiSeiaSock(MCAST_GRP, MCAST_PORT)
MY_IP = udp.local_ip

# 顯示列表
PACKETS: List[Tuple[str,str,str,str,str]] = []   # (time, T/R/S, ip, hex, parsed)

# 近期 RX（供其他流程參考；這版不再主動送 R，只用備查）
RX_RECENTS: List[Tuple[float,str,bytes]] = []    # (ts, ip, data)

# 裝置狀態快取： device_state[(ip, sa)] = {"power":0/1, "mode":int, "temp":int, "fan":int, "swing":0/1, "ts":float}
device_state: Dict[Tuple[str,int], Dict[str,int]] = {}

def add_pkt(direction: str, ip: str, data: bytes):
    p = parse_general(data)
    if p is None:
        parsed = ""
    else:
        rw_txt = "W" if p["rw"] else "R"
        parsed = f'SA={p["sa"]:02X} {rw_txt}|SID={p["sid"]:02X}|VAL={p["value"]:04X}|XOR={"OK" if p["xor_ok"] else "NG"}'
    PACKETS.append((datetime.now().strftime("%H:%M:%S"), direction, ip, hex_str(data), parsed))
    if len(PACKETS) > LOG_LIMIT:
        del PACKETS[:len(PACKETS)-LOG_LIMIT]

def add_state_row(ip: str, text: str):
    PACKETS.append((datetime.now().strftime("%H:%M:%S"), "S", f"{ip}:{MCAST_PORT}", "— STATE —", text))
    if len(PACKETS) > LOG_LIMIT:
        del PACKETS[:len(PACKETS)-LOG_LIMIT]

def _update_cache_from_parsed(ip: str, p: dict):
    """依 R 封包更新裝置狀態快取（不主動發送任何封包）"""
    if p["rw"] != 0 or not p["xor_ok"]:   # 僅處理 R + XOR OK
        return
    sa, sid, val = p["sa"], p["sid"], p["value"]
    key = (ip, sa)
    st = device_state.get(key, {"power":None,"mode":None,"temp":None,"fan":None,"swing":None})
    # 映射各裝置 SID
    if sa == 0x11:  # Lamp
        if sid == 0x00: st["power"] = 1 if val else 0
    elif sa == 0x0F:  # Fan
        if sid == 0x00: st["power"] = 1 if val else 0
        elif sid == 0x01: st["mode"] = val
        elif sid == 0x02: st["fan"]  = val
        elif sid == 0x05: st["swing"]= 1 if val else 0
    elif sa == 0x01:  # AC
        if sid == 0x00: st["power"] = 1 if val else 0
        elif sid == 0x01: st["mode"]  = val
        elif sid == 0x03: st["temp"]  = val
        elif sid == 0x02: st["fan"]   = val
        elif sid == 0x0E: st["swing"] = 1 if val else 0
    st["ts"] = time.time()
    device_state[key] = st

def rx_loop():
    while True:
        try:
            data, addr = udp.recvfrom()
            # 1) 列表加入 R
            add_pkt("R", addr[0], data)
            # 2) 收到 R 就更新快取
            p = parse_general(data)
            if p: _update_cache_from_parsed(addr[0], p)
            # 3) 留備查
            RX_RECENTS.append((time.time(), addr[0], data))
            if len(RX_RECENTS) > 2000:
                del RX_RECENTS[:1000]
        except Exception:
            time.sleep(0.03)

threading.Thread(target=rx_loop, daemon=True).start()

# ---------------- Web UI（UI 內容完全照舊） ----------------
INDEX_HTML = """<!doctype html><html lang="zh-Hant"><head>
<meta charset="utf-8"><title>TaiSEIA Controller — 224.0.23.0:36110</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
<style>body{background:#fff}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace}.pkt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}</style>
</head><body>
<div class="container py-4">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h4 class="m-0">TaiSEIA Controller</h4>
    <div class="text-muted">Multicast: <span class="mono">{{group}}</span> | Controller IP: <span class="mono">{{myip}}</span> | Port: <span class="mono">{{port}}</span></div>
  </div>

  <div class="card mb-3"><div class="card-body">
    <div class="row g-3 align-items-end">
      <div class="col-sm-4"><label class="form-label">Target IP（單播設備或群播 224.0.23.0）</label><input id="target" class="form-control mono" value="{{group}}"></div>
      <div class="col-sm-2"><label class="form-label">SA</label><input id="sa" class="form-control mono" value="11"></div>
      <div class="col-sm-2"><label class="form-label">R/W</label><select id="rw" class="form-select"><option value="0">R</option><option value="1" selected>W</option></select></div>
      <div class="col-sm-2"><label class="form-label">SID</label><input id="sid" class="form-control mono" value="00"></div>
      <div class="col-sm-2"><label class="form-label">DATA(16-bit)</label><input id="val" class="form-control mono" value="0001"></div>
    </div>
    <div class="mt-3 d-flex gap-2 flex-wrap">
      <button class="btn btn-primary" onclick="sendGeneral()">SEND</button>
      <button class="btn btn-outline-secondary" onclick="sendReg()">Register</button>
      <button class="btn btn-outline-danger" onclick="sendUnreg()">Unregister</button>
      <button class="btn btn-outline-info" onclick="readState()">Read State</button>
      <button class="btn btn-outline-dark" onclick="clearLog()">Clear Log</button>
    </div>
  </div></div>

  <div class="card mb-3">
    <div class="card-header">快速指令</div>
    <div class="card-body">
      <div class="d-flex flex-wrap gap-2">
        <button class="btn btn-outline-primary" onclick="quick(0x11,1)">Lamp ON</button>
        <button class="btn btn-outline-primary" onclick="quick(0x11,0)">Lamp OFF</button>
        <button class="btn btn-outline-success" onclick="quick(0x01,1)">AC ON</button>
        <button class="btn btn-outline-success" onclick="quick(0x01,0)">AC OFF</button>
        <button class="btn btn-outline-secondary" onclick="quick(0x0F,1)">Fan ON</button>
        <button class="btn btn-outline-secondary" onclick="quick(0x0F,0)">Fan OFF</button>
      </div>
      <small class="text-muted d-block mt-2">快速指令固定使用：R/W=W、SID=0x80（電源功能）、VAL=0001/0000</small>
    </div>
  </div>

  <div class="card mb-3"><div class="card-body">
    <div class="row g-2 align-items-end">
      <div class="col-sm-9"><label class="form-label">Free (hex bytes, 原樣送出，請自行處理 XOR)</label>
        <input id="free" class="form-control mono" placeholder="06 11 80 00 01 86"></div>
      <div class="col-sm-3"><button class="btn btn-secondary w-100" onclick="sendFree()">SEND FREE</button></div>
    </div>
  </div></div>

  <div class="card">
    <div class="card-header">Packets Monitor（只解析長度 6 的一般封包）</div>
    <div class="card-body p-0">
      <div class="table-responsive" style="max-height:60vh">
        <table class="table table-sm table-hover table-striped m-0">
          <thead class="table-light"><tr><th style="width:6em">Time</th><th style="width:3em">T/R</th><th style="width:12em">IP</th><th>HEX</th><th>Parsed</th></tr></thead>
          <tbody id="tb"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>
<script>
function hx2(s){return parseInt(String(s).trim(),16)||0}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return await r.json()}
async function sendGeneral(){
  const target=document.getElementById('target').value.trim()||'{{group}}'
  const sa=hx2(document.getElementById('sa').value)&0xFF
  const rw=(document.getElementById('rw').value==='1')?1:0
  const sid=hx2(document.getElementById('sid').value)&0x7F
  const val=hx2(document.getElementById('val').value)&0xFFFF
  await post('/send/general',{target,sa,rw,sid,val})
}
async function quick(sa,on){const target=document.getElementById('target').value.trim()||'{{group}}';await post('/send/quick',{target,sa,on})}
async function sendReg(){const target=document.getElementById('target').value.trim()||'{{group}}';await post('/send/reg',{target})}
async function sendUnreg(){const target=document.getElementById('target').value.trim()||'{{group}}';await post('/send/unreg',{target})}
async function readState(){const target=document.getElementById('target').value.trim()||'{{group}}';await post('/clear',{});await post('/send/read_current_v3',{target});await refresh()}
async function sendFree(){const target=document.getElementById('target').value.trim()||'{{group}}';const hex=document.getElementById('free').value.trim();await post('/send/free',{target,hex})}
async function clearLog(){await post('/clear',{});await refresh()}
async function refresh(){const rows=await (await fetch('/packets')).json();const tb=document.getElementById('tb');tb.innerHTML='';rows.forEach(r=>{const tr=document.createElement('tr');tr.innerHTML=`<td class="mono">${r[0]}</td><td>${r[1]}</td><td class="mono">${r[2]}</td><td class="mono pkt">${r[3]}</td><td class="mono pkt">${r[4]}</td>`;tb.appendChild(tr)})}
setInterval(refresh,1000);refresh()
</script>
</body></html>"""

# ---------------- 狀態摘要（不送封包，純用快取） ----------------
def add_state_snapshot_from_cache(ip: str, sa: int):
    key = (ip, sa)
    st = device_state.get(key)
    onoff = lambda v: ("ON" if v else "OFF") if v in (0,1) else "—"
    if not st:
        add_state_row(ip, "[No cached state]  (device did not reply yet)")
        return
    if sa == 0x11:
        summary = f"[Device: Lamp]  Power: {onoff(st.get('power'))}"
    elif sa == 0x0F:
        mode_map = {0:'Auto',1:'模式一',2:'模式二',3:'模式三',4:'模式四'}
        m = st.get("mode"); m_txt = mode_map.get(m, m if m is not None else "—")
        summary = (f"[Device: Fan]  Power: {onoff(st.get('power'))}"
                   f" | Mode:{m_txt} | Speed:{st.get('fan','—')} | Swing:{onoff(st.get('swing'))}")
    else:
        mode_map = {0:'冷氣',1:'除濕',2:'送風',3:'自動',4:'暖氣'}
        m = st.get("mode"); m_txt = mode_map.get(m, m if m is not None else "—")
        t = st.get("temp","—"); f = st.get("fan","—")
        summary = (f"[Device: AC]  Power: {onoff(st.get('power'))}"
                   f" | Mode:{m_txt} | Temp:{t}°C | Fan:{f} | Swing:{onoff(st.get('swing'))}")
    add_state_row(ip, summary)

# ---------------- Routes ----------------
@app.get("/")
def index():
    return render_template_string(INDEX_HTML, group=MCAST_GRP, port=MCAST_PORT, myip=MY_IP)

@app.post("/send/general")
def send_general():
    j = request.get_json(force=True)
    target = (j.get("target") or MCAST_GRP).strip()
    sa  = int(j.get("sa",0)) & 0xFF
    rw  = 1 if int(j.get("rw",0)) else 0
    sid = int(j.get("sid",0)) & 0x7F
    val = int(j.get("val",0)) & 0xFFFF

    pkt = pkt_general(sa,rw,sid,val)
    udp.send_to(pkt, target); add_pkt("T", f"{target}:{MCAST_PORT}", pkt)

    # 等裝置回覆（單次）
    deadline = time.time() + 1.2
    while time.time() < deadline:
        # RX 迴圈會自動把回覆塞進 PACKETS，這裡僅等一下
        time.sleep(0.05)
        # 我們不做條件判斷，也不再送 R — 僅等待一次回覆時間窗
    # 用快取（可能只有 power，或之前讀到的更多 sid）產生摘要
    add_state_snapshot_from_cache(target, sa)
    return jsonify(ok=True)

@app.post("/send/quick")
def send_quick():
    j = request.get_json(force=True)
    target = (j.get("target") or MCAST_GRP).strip()
    sa  = int(j.get("sa",0)) & 0xFF
    on  = 1 if int(j.get("on",0)) else 0
    pkt = pkt_general(sa, 1, 0x80, 0x0001 if on else 0x0000)
    udp.send_to(pkt, target); add_pkt("T", f"{target}:{MCAST_PORT}", pkt)

    # 同 send_general：只等回覆，不再發任何讀取封包
    deadline = time.time() + 1.2
    while time.time() < deadline:
        time.sleep(0.05)
    add_state_snapshot_from_cache(target, sa)
    return jsonify(ok=True)

@app.post("/send/reg")
def send_reg():
    j = request.get_json(force=True)
    target = (j.get("target") or MCAST_GRP).strip()
    pkt = pkt_register_req()
    udp.send_to(pkt, target); add_pkt("T", f"{target}:{MCAST_PORT}", pkt)
    return jsonify(ok=True)

@app.post("/send/unreg")
def send_unreg():
    j = request.get_json(force=True)
    target = (j.get("target") or MCAST_GRP).strip()
    pkt = pkt_unregister_req()
    udp.send_to(pkt, target); add_pkt("T", f"{target}:{MCAST_PORT}", pkt)
    return jsonify(ok=True)

# Read State 保持原行為（需要讀多個 SID 時才用；如果也要「只送收一次」請再告訴我規則）
@app.post("/send/read_current_v3")
def read_current_v3():
    j = request.get_json(force=True)
    target = (j.get("target") or MCAST_GRP).strip()
    t0 = time.time()

    lamp_q = [(0x11,0x00)]
    fan_q  = [(0x0F,0x00),(0x0F,0x01),(0x0F,0x02),(0x0F,0x05)]
    ac_q   = [(0x01,0x00),(0x01,0x01),(0x01,0x03),(0x01,0x02),(0x01,0x0E)]
    for sa,sid in lamp_q+fan_q+ac_q:
        pkt = pkt_general(sa,0,sid,0x0000)
        udp.send_to(pkt,target); add_pkt("T", f"{target}:{MCAST_PORT}", pkt); time.sleep(0.04)
    time.sleep(1.0)

    # 只從 RX 緩衝取資料、更新快取（rx_loop 已更新），這裡只是找出哪個 SA 回最多，以便顯示摘要
    buckets = {0x11:0, 0x0F:0, 0x01:0}
    for ts, ip, data in RX_RECENTS:
        if ip != target or ts < t0: continue
        p = parse_general(data)
        if p and p["rw"]==0 and p["xor_ok"] and p["sa"] in buckets:
            buckets[p["sa"]] += 1
    best_sa = max(buckets, key=lambda k: buckets[k])
    add_state_snapshot_from_cache(target, best_sa)
    return jsonify(ok=True)

@app.post("/send/free")
def send_free():
    j = request.get_json(force=True)
    target = (j.get("target") or MCAST_GRP).strip()
    text = (j.get("hex") or "").replace(",", " ").strip()
    if not text: return jsonify(ok=False, msg="empty hex"), 400
    try:
        raw = bytes(int(x,16)&0xFF for x in text.split())
    except Exception as e:
        return jsonify(ok=False, msg=str(e)), 400
    udp.send_to(raw, target); add_pkt("T", f"{target}:{MCAST_PORT}", raw)
    return jsonify(ok=True)

@app.get("/packets")
def packets():
    return jsonify(PACKETS[-300:])

@app.post("/clear")
def clear():
    PACKETS.clear()
    return jsonify(ok=True)

def main():
    print(f"* TaiSEIA Controller  |  UDP {MCAST_GRP}:{MCAST_PORT}  (local {MY_IP})")
    print("* Open http://127.0.0.1:8080")
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)

if __name__ == "__main__":
    main()
