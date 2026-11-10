// 窗口可见性探针：输出指定进程的窗口列表（JSON）。
// 仅使用 CoreGraphics 公共 API；窗口标题在无屏幕录制权限时可能为空，
// 但 owner/onscreen/bounds 足以判断“是否存在可见窗口”。
//
// 用法：swiftc -O scripts/window_list.swift -o /tmp/window_list
//       /tmp/window_list --on-screen --pid=1234
import CoreGraphics
import Foundation

let arguments = CommandLine.arguments
let onlyOnScreen = arguments.contains("--on-screen")
let filterPid = arguments
    .first(where: { $0.hasPrefix("--pid=") })
    .flatMap { Int($0.dropFirst("--pid=".count)) }

let options: CGWindowListOption = onlyOnScreen
    ? [.optionOnScreenOnly, .excludeDesktopElements]
    : [.optionAll]

var windows: [[String: Any]] = []
if let list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] {
    for window in list {
        let pid = window[kCGWindowOwnerPID as String] as? Int ?? 0
        if let filterPid, pid != filterPid {
            continue
        }
        windows.append([
            "pid": pid,
            "owner": window[kCGWindowOwnerName as String] as? String ?? "",
            "name": window[kCGWindowName as String] as? String ?? "",
            "onscreen": window[kCGWindowIsOnscreen as String] as? Bool ?? false,
            "layer": window[kCGWindowLayer as String] as? Int ?? 0,
            "bounds": window[kCGWindowBounds as String] ?? "",
        ])
    }
}

let data = try! JSONSerialization.data(withJSONObject: windows, options: [])
print(String(data: data, encoding: .utf8)!)
