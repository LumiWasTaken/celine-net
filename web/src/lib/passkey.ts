/* The login's credentials: the Security page's calls (passkeys, passwords, PINs, and
   Confirm access, the proof some of them need first), and the WebAuthn JSON conversions
   (the server speaks the JSON form of the options and credentials, base64url for bytes). The login screen is a page of its own, served by the
   server (server/celine/auth/page.py), with the same conversions inline. */
import { del, json, post, request } from './api'

export type CredentialKind = 'passkey' | 'password' | 'pin'
export type Credential = {
  id: string
  kind: CredentialKind
  label: string
  created_at: number | string
  last_used_at: number | string | null
  /** the one this browser logged in with */
  this_session: boolean
}

type Json = Record<string, unknown>

const enc = (b: ArrayBuffer) =>
  btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const dec = (s: string) =>
  Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0))

function creationOptions(o: Json): PublicKeyCredentialCreationOptions {
  const user = o.user as Json
  const exclude = (o.excludeCredentials as { id: string; type: 'public-key' }[] | undefined) ?? []
  return {
    ...(o as unknown as PublicKeyCredentialCreationOptions),
    challenge: dec(o.challenge as string),
    user: { ...(user as unknown as PublicKeyCredentialUserEntity), id: dec(user.id as string) },
    excludeCredentials: exclude.map(c => ({ ...c, id: dec(c.id) })),
  }
}

function registrationJSON(c: PublicKeyCredential): Json {
  const r = c.response as AuthenticatorAttestationResponse
  return {
    id: c.id, rawId: enc(c.rawId), type: c.type,
    authenticatorAttachment: c.authenticatorAttachment ?? undefined,
    clientExtensionResults: c.getClientExtensionResults(),
    response: {
      clientDataJSON: enc(r.clientDataJSON),
      attestationObject: enc(r.attestationObject),
      transports: r.getTransports ? r.getTransports() : [],
    },
  }
}

function requestOptions(o: Json): PublicKeyCredentialRequestOptions {
  const allow = (o.allowCredentials as { id: string; type: 'public-key' }[] | undefined) ?? []
  return {
    ...(o as unknown as PublicKeyCredentialRequestOptions),
    challenge: dec(o.challenge as string),
    allowCredentials: allow.map(c => ({ ...c, id: dec(c.id) })),
  }
}

function assertionJSON(c: PublicKeyCredential): Json {
  const r = c.response as AuthenticatorAssertionResponse
  return {
    id: c.id, rawId: enc(c.rawId), type: c.type,
    authenticatorAttachment: c.authenticatorAttachment ?? undefined,
    clientExtensionResults: c.getClientExtensionResults(),
    response: {
      clientDataJSON: enc(r.clientDataJSON),
      authenticatorData: enc(r.authenticatorData),
      signature: enc(r.signature),
      ...(r.userHandle ? { userHandle: enc(r.userHandle) } : {}),
    },
  }
}

/** what a failed passkey prompt means, in one sentence */
function passkeyError(e: unknown): Error {
  if (e instanceof DOMException) {
    if (e.name === 'NotAllowedError') return new Error('You closed the passkey prompt, or it timed out.')
    if (e.name === 'InvalidStateError') return new Error('This passkey is already registered here.')
  }
  return e instanceof Error ? e : new Error(String(e))
}

export interface NamedToken { id: number; name: string; created_at: string }

/** the server's code when an action needs Confirm access first (auth/routes.py) */
export const PROOF_NEEDED = 'proof_needed'

export const securityApi = {
  credentials: () => request<{ credentials: Credential[] }>('/auth/credentials'),
  /** a password (at least 10 characters) or a PIN (8 digits); the server's sentence says what is wrong */
  addCredential: (kind: 'password' | 'pin', secret: string, label?: string) =>
    request<{ ok: boolean; id: string }>('/auth/credentials', json('POST', { kind, secret, ...(label ? { label } : {}) })),
  /** 409 on the last one; `logged_out` when it was this browser's */
  deleteCredential: (id: string) =>
    del<{ ok: boolean; logged_out: boolean }>(`/auth/credentials/${encodeURIComponent(id)}`),

  /** asks the browser for a new passkey and registers it */
  add: async () => {
    const { ceremony, options } = await post<{ ceremony: string; options: Json }>('/auth/passkeys/start')
    let cred: PublicKeyCredential
    try {
      cred = await navigator.credentials.create({ publicKey: creationOptions(options) }) as PublicKeyCredential
    } catch (e) { throw passkeyError(e) }
    await request('/auth/passkeys/finish', json('POST', { ceremony, credential: registrationJSON(cred) }))
  },

  newRecoveryCodes: () => post<{ recovery_codes: string[] }>('/auth/recovery-codes'),

  /** Confirm access with a passkey: the browser's prompt */
  proveWithPasskey: async () => {
    const { ceremony, options } = await post<{ ceremony: string; options: Json }>('/auth/reauth/start')
    let cred: PublicKeyCredential
    try {
      cred = await navigator.credentials.get({ publicKey: requestOptions(options) }) as PublicKeyCredential
    } catch (e) { throw passkeyError(e) }
    await request('/auth/reauth/finish', json('POST', { ceremony, credential: assertionJSON(cred) }))
  },
  /** Confirm access with a password or a PIN */
  prove: (method: 'password' | 'pin', secret: string) =>
    request<{ ok: boolean }>('/auth/reauth', json('POST', { method, secret })),

  /** named tokens: a host script sends on the intercom as system:<name> */
  tokens: () => request<{ tokens: NamedToken[] }>('/tokens'),
  /** the token is in this answer only */
  makeToken: (name: string) => request<{ name: string; token: string }>('/tokens', json('POST', { name })),
  revokeToken: (id: number) => post<{ ok: boolean }>(`/tokens/${id}/revoke`),

  logout: async () => { await post('/auth/logout'); location.replace('/') },
}
