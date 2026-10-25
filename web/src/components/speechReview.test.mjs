// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { act, createElement as h, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { SpeechTest } from './SpeechTest'
import { FORMS } from '../views/WelcomeForms'

const state = vi.hoisted(() => ({ route: {}, settings: null, setSettings: vi.fn() }))
vi.mock('../lib/nav', () => ({ useNav: () => ({ route: state.route }), format: () => '' }))
vi.mock('../lib/socket', () => ({ useOnline: () => true }))
vi.mock('../lib/useResource', () => ({ useResource: () => ({ data: state.settings, set: state.setSettings }) }))
vi.mock('../lib/speechApi', () => ({ speechApi: {
  transcribe: vi.fn(), synthesize: vi.fn(), voices: vi.fn(), save: vi.fn(), models: vi.fn(),
} }))
vi.mock('../views/settings/Models', () => ({ reach: vi.fn() }))
vi.mock('../views/settings/parts', async () => {
  const { createElement: h } = await import('react')
  return { SchemaForm: ({ values, onChange }) => h('input', {
    'aria-label': 'Address', value: values.url, onChange: e => onChange('url', e.target.value),
  }) }
})
vi.mock('../ui', async () => {
  const { createElement: h } = await import('react')
  return {
    Button: ({ children, loading, ...props }) => h('button', { ...props, disabled: props.disabled || loading }, children),
    Input: props => h('input', props),
    Field: ({ label, children }) => h('label', {}, label, children),
    Switch: ({ label, checked, onChange }) => h('label', {}, label, h('input', { type: 'checkbox', checked, onChange: e => onChange(e.target.checked) })),
    InlineError: ({ message }) => h('p', { role: 'alert' }, message),
    Choice: () => null, Dot: () => null, LoadingRow: () => null, copied: vi.fn(),
  }
})
import { speechApi } from '../lib/speechApi'

const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
let root, host, tracks, recordings
const button = text => [...host.querySelectorAll('button')].find(b => b.textContent === text)
const click = async text => { await act(async () => { button(text).click() }) }
const render = async component => { await act(async () => { root.render(component) }) }
const edit = async (label, value) => {
  const input = [...host.querySelectorAll('input')].find(i => i.getAttribute('aria-label') === label || i.closest('label')?.textContent === label)
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  globalThis.IS_REACT_ACT_ENVIRONMENT = true
  state.route = { section: 'settings', page: 'features' }
  state.settings = { endpoint: { url: 'https://old.test/v1', model: 'old-model' }, version: 'v1', key: { state: 'set' }, fields: [] }
  state.setSettings.mockImplementation(saved => { state.settings = saved })
  speechApi.models.mockResolvedValue({ models: ['discovered'] })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  tracks = [{ stop: vi.fn() }]; recordings = []
  vi.stubGlobal('MediaRecorder', class {
    static isTypeSupported() { return true }
    constructor(stream) { this.stream = stream; this.state = 'inactive'; recordings.push(this) }
    start() { this.state = 'recording' }
    stop() { this.state = 'inactive'; this.onstop?.() }
  })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => tracks }) } })
})
afterEach(async () => {
  await act(async () => { root.unmount() }); host.remove(); vi.unstubAllGlobals()
})

const sample = () => h(SpeechTest, { direction: 'input', identity: 'same-provider', ready: true, save: async () => {} })

test('navigation stops a mounted test recording without uploading the hidden sample', async () => {
  await render(sample()); await click('Record test')
  expect(recordings[0].state).toBe('recording')
  state.route = { section: 'chats' }; await render(sample())
  expect(recordings[0].state).toBe('inactive')
  expect(tracks[0].stop).toHaveBeenCalled()
  expect(speechApi.transcribe).not.toHaveBeenCalled()
})

test('a microphone permission grant after navigation releases tracks without recording', async () => {
  const permission = deferred(); navigator.mediaDevices.getUserMedia.mockReturnValue(permission.promise)
  await render(sample()); await click('Record test')
  state.route = { section: 'chats' }; await render(sample())
  await act(async () => { permission.resolve({ getTracks: () => tracks }) })
  expect(tracks[0].stop).toHaveBeenCalled()
  expect(recordings).toHaveLength(0)
  expect(speechApi.transcribe).not.toHaveBeenCalled()
})

test.each(['input', 'output'])('navigation aborts an in-flight %s sample request and ignores a late response', async direction => {
  const pending = deferred()
  const request = direction === 'input' ? speechApi.transcribe : speechApi.synthesize
  request.mockReturnValue(pending.promise)
  const component = () => h(SpeechTest, { direction, identity: 'same-provider', ready: true, save: async () => {} })
  await render(component()); await click(direction === 'input' ? 'Record test' : 'Test speech')
  if (direction === 'input') await click('Finish and transcribe')
  const signal = request.mock.calls[0][2]
  expect(signal.aborted).toBe(false)
  state.route = { section: 'chats' }; await render(component())
  expect(signal.aborted).toBe(true)
  await act(async () => { pending.resolve(direction === 'input' ? { text: 'Late transcript' } : new Blob(['audio'])) })
  expect(host.textContent).not.toContain('Late transcript')
  expect(host.querySelector('audio')).toBeNull()
})

function Form({ initial }) {
  const [draft, setDraft] = useState(initial)
  return h(FORMS.speech, { feature: { id: 'speech_output' }, value: draft, onChange: setDraft,
    saveRef: { current: null }, reprobe: async () => {} })
}

test.each(['Save', 'Discover models'])('%s preserves model edits made while saving and clears only the submitted key', async action => {
  const save = deferred(); speechApi.save.mockReturnValue(save.promise)
  await render(h(Form, { initial: { endpoint: { ...state.settings.endpoint, model: '' }, key: 'submitted', clear: false } }))
  await click(action); await edit('Model', 'typed-while-saving')
  await act(async () => { save.resolve({ ...state.settings, version: 'v2' }) })
  expect(host.querySelector('label:has(input[type="password"]) input').value).toBe('')
  expect([...host.querySelectorAll('label')].find(l => l.textContent === 'Model').querySelector('input').value).toBe('typed-while-saving')
})

test.each([false, true])('a save preserves a newly typed key (address edited: %s)', async changeAddress => {
  const save = deferred(); speechApi.save.mockReturnValue(save.promise)
  await render(h(Form, { initial: { endpoint: state.settings.endpoint, key: 'submitted', clear: false } }))
  await click('Discover models')
  if (changeAddress) await edit('Address', 'https://new.test/v1')
  await edit('API key', 'new-key')
  await act(async () => { save.resolve({ ...state.settings, version: 'v2' }) })
  expect(host.querySelector('[aria-label="Address"]').value).toBe(changeAddress ? 'https://new.test/v1' : 'https://old.test/v1')
  expect(host.querySelector('input[type="password"]').value).toBe('new-key')
  if (changeAddress) expect(speechApi.models).not.toHaveBeenCalled()
})

test('a save preserves a clear-key choice made after submission', async () => {
  const save = deferred(); speechApi.save.mockReturnValue(save.promise)
  await render(h(Form, { initial: { endpoint: state.settings.endpoint, key: '', clear: false } }))
  await click('Save')
  await act(async () => { host.querySelector('input[type="checkbox"]').click() })
  await act(async () => { save.resolve({ ...state.settings, version: 'v2' }) })
  expect(host.querySelector('input[type="checkbox"]').checked).toBe(true)
})
