// React <-> Phaser 事件总线。React 在播放服务端结算日志时调用 emit，Phaser 场景订阅并播放动画。
const _listeners = new Map()

export const bus = {
  on(event, fn) {
    if (!_listeners.has(event)) _listeners.set(event, [])
    _listeners.get(event).push(fn)
    return () => {
      const arr = _listeners.get(event) || []
      _listeners.set(
        event,
        arr.filter((f) => f !== fn),
      )
    }
  },
  emit(event, payload) {
    ;(_listeners.get(event) || []).forEach((fn) => fn(payload))
  },
  clear() {
    _listeners.clear()
  },
}