import os
import json
import base64
import sys
from urllib.parse import urlparse, parse_qs, unquote

# 修复 base64 填充位数问题的安全函数
def add_padding(s):
    return s + "=" * ((4 - len(s) % 4) % 4)

def generate_config(proxy_url):
    proxy_url = proxy_url.strip()
    if proxy_url.startswith('{') and proxy_url.endswith('}'):
        try:
            json.loads(proxy_url)
            return proxy_url
        except:
            pass

    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()

    outbound = {
        "tag": "proxy"
    }

    if scheme == "tuic":
        outbound["type"] = "tuic"
        outbound["server"] = parsed.hostname
        outbound["server_port"] = parsed.port or 443 # [修复] 防止 port 为 None

        auth_user = unquote(parsed.username or "")
        auth_pass = unquote(parsed.password or "")

        if ":" in auth_user:
            outbound["uuid"], outbound["password"] = auth_user.split(":", 1)
        else:
            outbound["uuid"] = auth_user
            outbound["password"] = auth_pass

        params = parse_qs(parsed.query)
        if "congestion_control" in params:
            outbound["congestion_control"] = unquote(params["congestion_control"][0])
        # [修复] 移除 udp_relay_mode，适配 Sing-box TUIC v5

        outbound["tls"] = {"enabled": True}
        sni = unquote(params.get("sni", [""])[0]) or parsed.hostname # [修复] SNI 兜底
        if sni:
            outbound["tls"]["server_name"] = sni
        if "alpn" in params:
            outbound["tls"]["alpn"] = [unquote(x) for x in params["alpn"][0].split(',') if x]
        if "insecure" in params and params["insecure"][0] in ["1", "true"]:
            outbound["tls"]["insecure"] = True

    elif scheme in ["hysteria2", "hy2"]:
        outbound["type"] = "hysteria2"
        outbound["server"] = parsed.hostname
        outbound["server_port"] = parsed.port or 443
        outbound["password"] = unquote(parsed.username or "")

        params = parse_qs(parsed.query)
        outbound["tls"] = {"enabled": True}
        sni = unquote(params.get("sni", [""])[0]) or parsed.hostname
        if sni:
            outbound["tls"]["server_name"] = sni
        if "alpn" in params:
            outbound["tls"]["alpn"] = [unquote(x) for x in params["alpn"][0].split(',') if x]
        if "insecure" in params and params["insecure"][0] in ["1", "true"]:
            outbound["tls"]["insecure"] = True

    elif scheme == "vless":
        outbound["type"] = "vless"
        outbound["server"] = parsed.hostname
        outbound["server_port"] = parsed.port or 443
        outbound["uuid"] = unquote(parsed.username or "")

        params = parse_qs(parsed.query)

        flow = unquote(params.get("flow", [""])[0])
        if flow:
            outbound["flow"] = flow

        security = unquote(params.get("security", [""])[0])
        tls_enabled = security in ["tls", "reality"]
        if tls_enabled:
            outbound["tls"] = {"enabled": True}
            sni = unquote(params.get("sni", [""])[0]) or parsed.hostname
            if sni:
                outbound["tls"]["server_name"] = sni
            if "fp" in params:
                outbound["tls"]["utls"] = {"enabled": True, "fingerprint": unquote(params["fp"][0])}
            if "pbk" in params:
                outbound["tls"]["reality"] = {
                    "enabled": True,
                    "public_key": unquote(params["pbk"][0])
                }
                sid = unquote(params.get("sid", [""])[0])
                if sid: # [修复] 空 sid 不写入
                    outbound["tls"]["reality"]["short_id"] = sid
            if "alpn" in params:
                outbound["tls"]["alpn"] = [unquote(x) for x in params["alpn"][0].split(',') if x]
            if "allowInsecure" in params and params["allowInsecure"][0] in ["1", "true"]:
                outbound["tls"]["insecure"] = True

        network = unquote(params.get("type", ["tcp"])[0])
        if network == "ws":
            ws_trans = {
                "type": "ws",
                "path": unquote(params.get("path", ["/"])[0])
            }
            host = unquote(params.get("host", [""])[0])
            if host: # [修复] 避免出现 "Host": ""
                ws_trans["headers"] = {"Host": host}
            outbound["transport"] = ws_trans
        elif network == "grpc":
            grpc_trans = {"type": "grpc"}
            service_name = unquote(params.get("serviceName", [""])[0])
            if service_name:
                grpc_trans["service_name"] = service_name
            outbound["transport"] = grpc_trans
        elif network == "http":
            http_trans = {
                "type": "http",
                "path": unquote(params.get("path", ["/"])[0])
            }
            host = unquote(params.get("host", [""])[0])
            if host:
                http_trans["host"] = [host]
            outbound["transport"] = http_trans

    elif scheme == "trojan":
        outbound["type"] = "trojan"
        outbound["server"] = parsed.hostname
        outbound["server_port"] = parsed.port or 443
        outbound["password"] = unquote(parsed.username or "")

        params = parse_qs(parsed.query)
        outbound["tls"] = {"enabled": True}
        sni = unquote(params.get("sni", [""])[0]) or parsed.hostname
        if sni:
            outbound["tls"]["server_name"] = sni
        if "alpn" in params:
            outbound["tls"]["alpn"] = [unquote(x) for x in params["alpn"][0].split(',') if x]
        if "allowInsecure" in params and params["allowInsecure"][0] in ["1", "true"]:
            outbound["tls"]["insecure"] = True

        network = unquote(params.get("type", ["tcp"])[0])
        if network == "ws":
            ws_trans = {
                "type": "ws",
                "path": unquote(params.get("path", ["/"])[0])
            }
            host = unquote(params.get("host", [""])[0])
            if host:
                ws_trans["headers"] = {"Host": host}
            outbound["transport"] = ws_trans
        elif network == "grpc":
            grpc_trans = {"type": "grpc"}
            service_name = unquote(params.get("serviceName", [""])[0])
            if service_name:
                grpc_trans["service_name"] = service_name
            outbound["transport"] = grpc_trans

    elif scheme in ["ss", "shadowsocks"]:
        outbound["type"] = "shadowsocks"
        # [修复] 兼容 SIP002 标准的全段 Base64 格式 (不含 @，且避开 hostname 小写化陷阱)
        netloc = parsed.netloc
        if "@" not in netloc:
            try:
                decoded = base64.urlsafe_b64decode(add_padding(netloc)).decode()
                auth, host_port = decoded.split("@", 1)
                outbound["method"], outbound["password"] = auth.split(":", 1)
                if ":" in host_port:
                    h, p = host_port.split(":", 1)
                    outbound["server"] = h
                    outbound["server_port"] = int(p)
                else:
                    outbound["server"] = host_port
                    outbound["server_port"] = 443
            except Exception as e:
                print(f"Failed to decode SIP002 SS config: {e}")
                sys.exit(1)
        else:
            outbound["server"] = parsed.hostname
            outbound["server_port"] = parsed.port or 443
            if parsed.username:
                try:
                    # [修复] 使用 urlsafe_b64decode 防特殊字符崩溃
                    decoded = base64.urlsafe_b64decode(add_padding(parsed.username)).decode()
                    if ":" in decoded:
                        outbound["method"], outbound["password"] = decoded.split(":", 1)
                    else:
                        outbound["method"] = unquote(parsed.username)
                        outbound["password"] = unquote(parsed.password or "")
                except:
                    outbound["method"] = unquote(parsed.username)
                    outbound["password"] = unquote(parsed.password or "")

    elif scheme == "vmess":
        try:
            raw = parsed.netloc + parsed.path
            decoded = None
            for padding in ["", "=", "=="]:
                try:
                    decoded = base64.b64decode(raw + padding).decode("utf-8")
                    json.loads(decoded)
                    break
                except Exception:
                    continue
            if decoded is None:
                try:
                    decoded = base64.urlsafe_b64decode(add_padding(raw)).decode("utf-8")
                except Exception:
                    raise ValueError(f"Cannot decode VMess base64")
            v_info = json.loads(decoded)
            outbound["type"] = "vmess"
            outbound["server"] = v_info.get("add")
            outbound["server_port"] = int(v_info.get("port", 443))
            outbound["uuid"] = v_info.get("id")
            outbound["security"] = v_info.get("scy") or v_info.get("security") or "auto"
            outbound["alter_id"] = int(v_info.get("aid", 0))

            if v_info.get("tls") == "tls":
                outbound["tls"] = {"enabled": True}
                sni = v_info.get("sni") or v_info.get("host") or v_info.get("add")
                if sni:
                    outbound["tls"]["server_name"] = sni
                if v_info.get("fp"):
                    outbound["tls"]["utls"] = {"enabled": True, "fingerprint": v_info.get("fp")}
                if v_info.get("alpn"):
                    outbound["tls"]["alpn"] = [x for x in v_info.get("alpn", "").split(",") if x]

            net = v_info.get("net", "")
            if net == "ws":
                ws_path = v_info.get("path") or "/"
                ws_host = v_info.get("host") or v_info.get("sni") or v_info.get("add")
                ws_transport = {
                    "type": "ws",
                    "path": ws_path
                }
                if ws_host:
                    ws_transport["headers"] = {"Host": ws_host}
                    
                if "?" in ws_path:
                    path_only, query = ws_path.split("?", 1)
                    ws_transport["path"] = path_only or "/"
                    ws_params = parse_qs(query)
                    if ws_params.get("ed"):
                        ws_transport["max_early_data"] = int(ws_params["ed"][0])
                        ws_transport["early_data_header_name"] = "Sec-WebSocket-Protocol"
                outbound["transport"] = ws_transport
            elif net == "grpc":
                grpc_trans = {"type": "grpc"}
                service_name = v_info.get("path", "")
                if service_name:
                    grpc_trans["service_name"] = service_name
                outbound["transport"] = grpc_trans
            elif net == "http":
                http_trans = {
                    "type": "http",
                    "path": v_info.get("path") or "/"
                }
                host = v_info.get("host") or v_info.get("add")
                if host:
                    http_trans["host"] = [host]
                outbound["transport"] = http_trans
        except Exception as e:
            print(f"Failed to parse VMess config: {e}")
            sys.exit(1)

    elif scheme == "socks5":
        outbound["type"] = "socks"
        outbound["server"] = parsed.hostname
        outbound["server_port"] = parsed.port or 1080
        user = unquote(parsed.username or "")
        passwd = unquote(parsed.password or "")
        if user:
            outbound["username"] = user
            outbound["password"] = passwd

    else:
        print(f"Unknown scheme: {scheme}, please use full JSON for complex configs.")
        sys.exit(1)

    config = {
        "log": {"level": "info"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": 8080
            }
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"}
        ],
        "route": {
            "rules": [
                {
                    "inbound": ["mixed-in"],
                    "outbound": "proxy"
                }
            ]
        }
    }
    return json.dumps(config, indent=2)
