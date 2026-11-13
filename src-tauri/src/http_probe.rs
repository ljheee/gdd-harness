//! 只读探测 `GET /api/overview`，用于识别本机 GDD 控制台。
//!
//! 控制台是本地 `BaseHTTPRequestHandler`，返回 HTTP/1.0 + `Content-Length` 固定 JSON；
//! 这里手写最小 HTTP 请求，避免为本地探针引入 HTTP 客户端/TLS/异步运行时依赖。

use serde_json::Value;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

const READ_TIMEOUT: Duration = Duration::from_secs(2);
const MAX_RESPONSE: usize = 1 << 20;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProbeOutcome {
    /// 200 且响应为 GDD 控制台的 `/api/overview` 形状。
    Gdd,
    /// 401：GDD 控制台处于鉴权模式，需要登录（桌面 app 只支持本地模式）。
    AuthRequired,
    /// 端口无监听（连接被拒绝）。
    NoListener,
    /// 端口有响应但不是可识别的 GDD 控制台。
    NonGdd(String),
    /// 连接建立后读写失败（无法判断端口状态）。
    Io(String),
}

/// 对 `127.0.0.1:<port>/api/overview` 发起一次只读 GET。
pub fn probe(port: u16, connect_timeout: Duration) -> ProbeOutcome {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = match TcpStream::connect_timeout(&addr, connect_timeout) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::ConnectionRefused => {
            return ProbeOutcome::NoListener
        }
        Err(e) => {
            return ProbeOutcome::Io(format!("连接 127.0.0.1:{port} 失败：{e}"));
        }
    };
    let _ = stream.set_read_timeout(Some(READ_TIMEOUT));
    let _ = stream.set_write_timeout(Some(READ_TIMEOUT));

    let request = format!(
        "GET /api/overview HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    );
    if let Err(e) = stream.write_all(request.as_bytes()) {
        return ProbeOutcome::Io(format!("向 127.0.0.1:{port} 发送探测请求失败：{e}"));
    }

    let mut raw = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&chunk[..n]);
                if raw.len() > MAX_RESPONSE {
                    return ProbeOutcome::NonGdd("响应超过 1MB，不视为 GDD 控制台".to_string());
                }
            }
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                // 已读到的数据可能完整（服务端未及时关闭连接）；交给解析器判断。
                break;
            }
            Err(e) => {
                return ProbeOutcome::Io(format!("读取 127.0.0.1:{port} 响应失败：{e}"));
            }
        }
    }

    classify(&raw)
}

fn classify(raw: &[u8]) -> ProbeOutcome {
    let (status, body) = match parse_http_response(raw) {
        Ok(parts) => parts,
        Err(detail) => return ProbeOutcome::NonGdd(detail),
    };
    // 鉴权模式控制台的未登录响应是 401；这与「端口被其他服务占用」是两回事，
    // 桌面壳据此提示需要登录，而不是报 NonGdd/PortOccupied。
    if status == 401 {
        return ProbeOutcome::AuthRequired;
    }
    if status != 200 {
        return ProbeOutcome::NonGdd(format!("HTTP 状态码 {status}"));
    }
    match validate_overview(body) {
        Ok(()) => ProbeOutcome::Gdd,
        Err(detail) => ProbeOutcome::NonGdd(detail),
    }
}

fn parse_http_response(raw: &[u8]) -> Result<(u16, &[u8]), String> {
    let split = find_subslice(raw, b"\r\n\r\n").ok_or_else(|| "响应头不完整".to_string())?;
    let head =
        std::str::from_utf8(&raw[..split]).map_err(|_| "响应头不是 UTF-8 文本".to_string())?;
    let mut lines = head.split("\r\n");
    let status_line = lines.next().ok_or_else(|| "缺少状态行".to_string())?;
    let mut parts = status_line.split_whitespace();
    let _http = parts.next().ok_or_else(|| "状态行非法".to_string())?;
    let status = parts
        .next()
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(|| format!("状态行非法：{status_line}"))?;

    let mut content_length: Option<usize> = None;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse::<usize>().ok();
            }
        }
    }

    let body = &raw[split + 4..];
    let body = match content_length {
        Some(n) if body.len() >= n => &body[..n],
        Some(n) => {
            return Err(format!(
                "响应体不完整：Content-Length={n}，实际 {} 字节",
                body.len()
            ))
        }
        None => body,
    };
    Ok((status, body))
}

fn validate_overview(body: &[u8]) -> Result<(), String> {
    let value: Value =
        serde_json::from_slice(body).map_err(|e| format!("响应不是合法 JSON：{e}"))?;
    let obj = value
        .as_object()
        .ok_or_else(|| "响应 JSON 不是对象".to_string())?;
    if obj.get("ok") != Some(&Value::Bool(true)) {
        return Err("响应 JSON 缺少 ok:true".to_string());
    }
    if !obj.get("templates").map(Value::is_array).unwrap_or(false) {
        return Err("响应 JSON 缺少 templates 数组".to_string());
    }
    if !obj.get("tasks").map(Value::is_array).unwrap_or(false) {
        return Err("响应 JSON 缺少 tasks 数组".to_string());
    }
    Ok(())
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn response(status: &str, body: &str) -> Vec<u8> {
        format!(
            "HTTP/1.0 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
        .into_bytes()
    }

    #[test]
    fn accepts_gdd_overview_shape() {
        let raw = response("200 OK", r#"{"ok": true, "templates": [], "tasks": []}"#);
        assert_eq!(classify(&raw), ProbeOutcome::Gdd);
    }

    #[test]
    fn classifies_401_as_auth_required() {
        // 鉴权模式控制台未登录时的真实响应形状。
        let raw = response(
            "401 Unauthorized",
            r#"{"ok": false, "error": "未登录或登录已失效"}"#,
        );
        assert_eq!(classify(&raw), ProbeOutcome::AuthRequired);

        // 401 不依赖响应体形状：关键信息在状态码。
        let raw = response("401 Unauthorized", "");
        assert_eq!(classify(&raw), ProbeOutcome::AuthRequired);
    }

    #[test]
    fn rejects_non_gdd_json() {
        let raw = response("200 OK", r#"{"ok": true}"#);
        assert!(matches!(classify(&raw), ProbeOutcome::NonGdd(_)));

        let raw = response("200 OK", r#"{"ok": false, "templates": [], "tasks": []}"#);
        assert!(matches!(classify(&raw), ProbeOutcome::NonGdd(_)));

        let raw = response("200 OK", r#"{"ok": true, "templates": {}, "tasks": []}"#);
        assert!(matches!(classify(&raw), ProbeOutcome::NonGdd(_)));
    }

    #[test]
    fn rejects_error_status_and_invalid_json() {
        let raw = response(
            "500 Internal Server Error",
            r#"{"ok": true, "templates": [], "tasks": []}"#,
        );
        assert!(matches!(classify(&raw), ProbeOutcome::NonGdd(_)));

        // 其他非 200（例如 Host 校验的 403）仍不是「需要登录」。
        let raw = response("403 Forbidden", r#"{"ok": false, "error": "forbidden"}"#);
        assert!(matches!(classify(&raw), ProbeOutcome::NonGdd(_)));

        let raw = response("200 OK", "not json");
        assert!(matches!(classify(&raw), ProbeOutcome::NonGdd(_)));
    }

    #[test]
    fn rejects_truncated_body() {
        let mut raw = response("200 OK", r#"{"ok": true, "templates": [], "tasks": []}"#);
        raw.truncate(raw.len() - 5);
        assert!(matches!(classify(&raw), ProbeOutcome::NonGdd(_)));
    }

    #[test]
    fn connection_refused_reports_no_listener() {
        // 绑定后立即释放，得到一个大概率空闲的端口。
        let port = {
            let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind");
            listener.local_addr().expect("addr").port()
        };
        assert_eq!(
            probe(port, Duration::from_millis(300)),
            ProbeOutcome::NoListener
        );
    }
}
