#!/usr/bin/env python3
"""
gen_topology.py - inventory.json から full_topology.mmd を生成する

すべての要素（ノード・WAN公開ポート・ルータ/外部接続・データフロー・サブネット）
を inventory から導出する「真の生成器」。サイト固有値はコードに一切持たない
（cf. L4 gen_stream_flow.py が depends_on/produces から生成しているのと同じ思想）。

入力スキーマは現行の {_meta, fault_domains, hosts} dict（旧 flat-list も許容）。

Usage:
    python3 gen_topology.py [--spec PATH] [--out PATH]

Defaults:
    --spec  ../inventory.json
    --out   ../full_topology.mmd
"""

import os
import json
import argparse
from pathlib import Path

# ============================================================
# ノードをカテゴリに分類するルール（role のキーワードで判定）
# ※ いずれも一般的な技術語彙。サイト固有名は含めない。
# ============================================================
CATEGORY_RULES = [
    ("net",   ["gateway", "firewall", "openwrt", "router", "wireguard", "proxy", "reverse proxy", "vpn"]),
    ("ai",    ["ollama", "inference", "whisper", "voicevox", "tts", "stable diffusion", "obs", "streaming core", "auth", "monitor", "prometheus", "grafana"]),
    ("game",  ["game server", "spigot", "minecraft", "terraria", "factorio", "sunshine", "streaming"]),
    ("media", ["vseeface", "avatar", "capture", "compositing", "録画"]),
    ("infra", ["nas", "storage", "hypervisor", "thin client", "ntp", "stratum", "作業機"]),
]

# カテゴリ → (subgraph ID, 表示タイトル, classDef名)
CATEGORY_GROUPS = [
    ("net",   "ROUTER_LAYER", "ルーター / ネットワーク", "net_style"),
    ("ai",    "AI_CORE",      "🟣 AI / 配信コア",         "ai_style"),
    ("game",  "GAME",         "🟢 ゲーム / 配信サブ",      "game_style"),
    ("infra", "INFRA",        "⚫ インフラ / ストレージ / 時刻", "infra_style"),
]

STYLE_DEFS = """
classDef net_style     fill:#E6F1FB,stroke:#185FA5,color:#0C447C
classDef ai_style      fill:#EEEDFE,stroke:#534AB7,color:#3C3489
classDef game_style    fill:#EAF3DE,stroke:#3B6D11,color:#27500A
classDef media_style   fill:#FAEEDA,stroke:#BA7517,color:#633806
classDef infra_style   fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A
classDef ext_style     fill:#E1F5EE,stroke:#0F6E56,color:#04342C
classDef wan_style     fill:#FCEBEB,stroke:#A32D2D,color:#501313
classDef offline_style fill:#eeeeee,stroke:#aaaaaa,color:#888888
"""

OFFLINE_STATUS = {"offline", "dormant"}


def classify(node: dict) -> str:
    """role フィールドからカテゴリを決定する（既定は infra）"""
    role = (node.get("role") or "").lower()
    for cat, keywords in CATEGORY_RULES:
        if any(k in role for k in keywords):
            return cat
    return "infra"


def node_id(name: str) -> str:
    """名前を Mermaid ID に変換（英数と _ のみ）"""
    return name.upper().replace("-", "_").replace(".", "_")


def subnet_prefixes(meta: dict):
    """_meta.subnets の CIDR から「ラベルで剥がす IP プレフィクス」を導出する。
    例: 'A.B.C.0/24' -> 'A.B.C.'（先頭3オクテット） / 'A.B.0.0/16' -> 'A.B.'
    /8 単位で先頭オクテットを採り、長いものを優先（最長一致で剥がす）。"""
    prefixes = []
    for cidr in (meta.get("subnets") or {}):
        if "/" not in cidr:
            continue
        net, plen = cidr.split("/", 1)
        try:
            keep = int(plen) // 8
        except ValueError:
            continue
        octs = net.split(".")
        if 0 < keep < 4 and len(octs) >= keep:
            prefixes.append(".".join(octs[:keep]) + ".")
    return sorted(set(prefixes), key=len, reverse=True)


def short_ip(ip: str, prefixes) -> str:
    """サブネットプレフィクスを '.' に短縮（無ければ IP そのまま）"""
    if not ip or ip == "unknown":
        return ""
    for p in prefixes:
        if ip.startswith(p):
            return "." + ip[len(p):]
    return ip


def node_label(node: dict, prefixes) -> str:
    """ノードの表示ラベルを生成"""
    h = node["hostname"]
    ip_part = short_ip(node.get("ip", ""), prefixes)
    hw = node.get("hardware", "")
    role = node.get("role", "")

    head = f"{h}  {ip_part}".rstrip()
    lines = [head]
    if hw:
        lines.append(hw)
    if role:
        lines.append(role[:40] + ("…" if len(role) > 40 else ""))
    return "\\n".join(lines)


def is_offline(node: dict) -> bool:
    # reconcile.py の NOT_RUNNING と同じ判定（意図的に止めてある = 図でグレーアウト）
    return node.get("status", "") in OFFLINE_STATUS


def edge_label(e: dict) -> str:
    """エッジ注釈（via 優先、無ければ kind）。Mermaid 用に短縮・サニタイズ。"""
    txt = (e.get("via") or e.get("kind") or "").strip()
    txt = txt.replace('"', "'").replace("|", "/")
    return txt[:40] + ("…" if len(txt) > 40 else "")


def svc_label(svc: dict) -> str:
    """公開サービスのポート/プロトコル注釈"""
    port = svc.get("port")
    proto = (svc.get("protocol") or "").strip()
    head = f":{port}" if port else (svc.get("name") or "").strip()
    return f"{head} {proto}".strip()


def generate(spec_path: Path, out_path: Path):
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    # 現行 {_meta, fault_domains, hosts} dict。旧 flat-list も許容
    # (reconcile.py / watch.py / gen_stream_flow.py と同じガード)。
    hosts = data["hosts"] if isinstance(data, dict) else data
    meta = data.get("_meta", {}) if isinstance(data, dict) else {}

    prefixes = subnet_prefixes(meta)
    host_set = {h["hostname"] for h in hosts}

    # ── ノードのグループ分け ─────────────────────────────
    # class:"iot" はセンサー群として別出し（reconcile/watch と同じ語彙）
    sensors = [h for h in hosts if h.get("class") == "iot"]
    sensor_names = {h["hostname"] for h in sensors}
    cats = {c: [] for c in ("net", "ai", "game", "media", "infra")}
    for node in hosts:
        if node["hostname"] in sensor_names:
            continue
        cats[classify(node)].append(node)

    # ── エッジ収集（すべて inventory 由来）───────────────
    wan_edges = []          # (dst_host, label) : 公開サービス
    has_inet = False
    for h in hosts:
        for svc in (h.get("services") or []):
            if svc.get("public"):
                has_inet = True
                wan_edges.append((h["hostname"], svc_label(svc)))

    external = []           # hosts に無い edge peer（外部サービス）
    ext_seen = set()

    def note_external(name):
        if name and name != "all" and name not in host_set and name not in ext_seen:
            ext_seen.add(name)
            external.append(name)

    flow_seen = set()
    flow_edges = []         # (src, dst, label)

    def add_flow(src, dst, label):
        key = (src, dst, label)
        if key not in flow_seen:
            flow_seen.add(key)
            flow_edges.append((src, dst, label))

    for h in hosts:
        self_name = h["hostname"]
        for e in (h.get("depends_on") or []):
            peer = e.get("host")
            if not peer or peer == "all":
                continue
            note_external(peer)
            add_flow(peer, self_name, edge_label(e))   # peer -> self（受け手視点）
        for key in ("produces", "provides"):
            for e in (h.get(key) or []):
                peer = e.get("to")
                if not peer or peer == "all":
                    continue
                note_external(peer)
                add_flow(self_name, peer, edge_label(e))  # self -> peer

    # ── ゲートウェイ検出（ルータ→各ノードの放射）─────────
    gateway = None
    for g in (meta.get("implicit_global_dependencies") or []):
        kind = (g.get("kind") or "").lower()
        if ("network" in kind or "dns" in kind) and g.get("host") in host_set:
            gateway = g.get("host")
            break
    if gateway is None:  # flat-list / _meta 無しのフォールバック
        for n in cats["net"]:
            r = (n.get("role") or "").lower()
            if any(k in r for k in ("gateway", "router", "firewall")):
                gateway = n["hostname"]
                break
        if gateway is None and cats["net"]:
            gateway = cats["net"][0]["hostname"]

    net_edges = []          # (gateway, host) : ルータ放射
    if gateway and gateway in host_set:
        for h in hosts:
            if h["hostname"] != gateway:
                net_edges.append((gateway, h["hostname"]))

    # ============================================================
    # Mermaid 出力
    # ============================================================
    lines = ["graph TD", ""]
    lines.append("%% Auto-generated by gen_topology.py — DO NOT EDIT MANUALLY")
    lines.append(f"%% Source: {os.path.basename(spec_path)}")
    lines.append("")

    # ── CLOUD subgraph（INET + 外部ノード）──
    if has_inet or external:
        lines.append('subgraph CLOUD ["☁️ Internet / Cloud"]')
        if has_inet:
            lines.append('    INET["Internet / WAN"]')
        for name in external:
            lines.append(f'    {node_id(name)}["{name}"]')
        lines.append("end")
        lines.append("")

    # ── NET subgraph（LAN）──
    subnet_label = ", ".join((meta.get("subnets") or {}).keys())
    net_title = f"🔵 Network — {subnet_label}" if subnet_label else "🔵 Network"
    lines.append(f'subgraph NET ["{net_title}"]')
    lines.append("")

    def emit_node(node, indent="        "):
        nid = node_id(node["hostname"])
        label = node_label(node, prefixes)
        if is_offline(node):
            label += "\\n⚫ OFFLINE"
        lines.append(f'{indent}{nid}["{label}"]')

    # カテゴリ別 subgraph（game は media を内包・空はスキップ）
    for cat, sub_id, title, _style in CATEGORY_GROUPS:
        group = cats[cat] + (cats["media"] if cat == "game" else [])
        if not group:
            continue
        lines.append(f'    subgraph {sub_id} ["{title}"]')
        for node in group:
            emit_node(node)
        lines.append("    end")
        lines.append("")

    # センサー / IoT
    if sensors:
        lines.append('    subgraph SENSOR ["🟡 センサー / IoT"]')
        for node in sensors:
            emit_node(node)
        lines.append("    end")
        lines.append("")

    lines.append("end")
    lines.append("")

    # ── 接続 ──
    lines.append("%% ============================================================")
    lines.append("%% 接続")
    lines.append("%% ============================================================")
    lines.append("")

    if wan_edges:
        lines.append("%% --- WAN・公開ポート (services[].public) ---")
        for dst, label in wan_edges:
            lines.append(f'INET -->|"{label}"| {node_id(dst)}')
        lines.append("")

    if net_edges:
        lines.append("%% --- ルータ → 各ノード ---")
        for gw, dst in net_edges:
            lines.append(f"{node_id(gw)} --- {node_id(dst)}")
        lines.append("")

    if flow_edges:
        lines.append("%% --- データフロー / 依存 (depends_on・produces・provides) ---")
        for src, dst, label in flow_edges:
            arrow = f'-->|"{label}"|' if label else "-->"
            lines.append(f"{node_id(src)} {arrow} {node_id(dst)}")
        lines.append("")

    # ── スタイル ──
    lines.append("%% ============================================================")
    lines.append("%% スタイル")
    lines.append("%% ============================================================")
    lines.append(STYLE_DEFS.strip())
    lines.append("")

    # カテゴリ → classDef
    style_for_cat = {
        "net": "net_style", "ai": "ai_style", "game": "game_style",
        "media": "media_style", "infra": "infra_style",
    }
    for cat in ("net", "ai", "game", "media", "infra"):
        ids = [node_id(n["hostname"]) for n in cats[cat]]
        if ids:
            lines.append(f"class {','.join(ids)} {style_for_cat[cat]}")

    # INET / 外部 / センサー
    if has_inet:
        lines.append("class INET wan_style")
    ext_ids = [node_id(n) for n in external] + [node_id(s["hostname"]) for s in sensors]
    if ext_ids:
        lines.append(f"class {','.join(ext_ids)} ext_style")

    # offline
    offline_ids = [node_id(n["hostname"]) for n in hosts if is_offline(n)]
    if offline_ids:
        lines.append(f"class {','.join(offline_ids)} offline_style")

    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Generated: {out_path}  (hosts={len(hosts)}, external={len(external)}, "
          f"wan={len(wan_edges)}, flows={len(flow_edges)})")


def main():
    parser = argparse.ArgumentParser(description="Generate full_topology.mmd from inventory.json")
    parser.add_argument("--spec", default="../inventory.json", help="Path to inventory.json")
    parser.add_argument("--out",  default="../full_topology.mmd", help="Output path for full_topology.mmd")
    args = parser.parse_args()

    spec_path = Path(args.spec)
    out_path  = Path(args.out)

    if not spec_path.exists():
        print(f"Error: spec file not found: {spec_path}")
        raise SystemExit(1)

    generate(spec_path, out_path)


if __name__ == "__main__":
    main()
