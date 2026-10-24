/* The part of noVNC's RFB the browser page uses (@novnc/novnc ships no types). */
declare module '@novnc/novnc' {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, urlOrChannel: string | WebSocket)
    viewOnly: boolean
    scaleViewport: boolean
    background: string
    focus(): void
    disconnect(): void
    clipboardPasteFrom(text: string): void
    sendKey(keysym: number, code: string, down?: boolean): void
  }
}
