// crypto.randomUUID() 只在**安全上下文**(HTTPS 或 http://localhost / 127.0.0.1)可用。
// 本机部署常经明文 HTTP 的内网 IP 访问(Tailscale / LAN,地址见 infra/network.env),那不是安全
// 上下文 → crypto.randomUUID 为 undefined → 首次调用(toast/uid/NodeEditor 建边建节点)整个
// app 崩,表现为**黑屏**。crypto.getRandomValues 在非安全上下文仍可用,用它补一个 RFC4122
// v4 polyfill。必须在任何调用点之前执行 —— 在 main.tsx 作为首个 import 引入(side-effect)。
if (typeof crypto !== 'undefined' && typeof crypto.randomUUID !== 'function') {
  ;(crypto as { randomUUID?: () => string }).randomUUID = function randomUUID(): string {
    const b = crypto.getRandomValues(new Uint8Array(16))
    b[6] = (b[6] & 0x0f) | 0x40 // version 4
    b[8] = (b[8] & 0x3f) | 0x80 // variant 10xx
    const h = Array.from(b, (x) => x.toString(16).padStart(2, '0'))
    return `${h[0]}${h[1]}${h[2]}${h[3]}-${h[4]}${h[5]}-${h[6]}${h[7]}-${h[8]}${h[9]}-${h[10]}${h[11]}${h[12]}${h[13]}${h[14]}${h[15]}`
  }
}
