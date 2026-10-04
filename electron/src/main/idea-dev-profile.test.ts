import { describe, expect, it } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { readIdeaDevProfile } from './idea-dev-profile'

const root = resolve('isolated-idea-test', 'AgentsDockIdeaLabData')
const env = { AGENTSDOCK_IDEA_DEV: '1', AGENTSDOCK_USER_DATA: root }
const profile = { kind: 'agentsdock-isolated-idea-lab-v1', serverUrl: 'http://127.0.0.1:17851', accessToken: 'synthetic-native-development-token' }

describe('isolated Idea Lab bootstrap', () => {
  it('never reads a profile outside explicit development mode', () => {
    expect(readIdeaDevProfile({}, false, root, () => { throw Error('unexpected read') })).toBeNull()
    expect(() => readIdeaDevProfile(env, true, root)).toThrow('source-app')
    expect(() => readIdeaDevProfile({ AGENTSDOCK_IDEA_DEV: '1' }, false, root)).toThrow('source-app')
    expect(() => readIdeaDevProfile(env, false, resolve('another-profile'))).toThrow('source-app')
    const installed = resolve('agentsdock-electron')
    expect(() => readIdeaDevProfile({ ...env, AGENTSDOCK_USER_DATA: installed }, false, installed,
      () => JSON.stringify(profile))).toThrow('source-app')
  })
  it('accepts only its marked dedicated loopback profile', () => {
    expect(readIdeaDevProfile(env, false, root, () => JSON.stringify(profile))).toEqual({ serverUrl: profile.serverUrl, accessToken: profile.accessToken })
    for (const url of ['http://127.0.0.1:7850', 'http://remote.invalid:17851', 'http://127.0.0.1:17851/other', 'http://user@127.0.0.1:17851']) {
      expect(() => readIdeaDevProfile(env, false, root, () => JSON.stringify({ ...profile, serverUrl: url }))).toThrow('dedicated loopback')
    }
    expect(() => readIdeaDevProfile(env, false, root, () => JSON.stringify({ ...profile, kind: 'unknown' }))).toThrow('Invalid')
  })
  it('refuses linked development directories before reading credentials', () => {
    const temporary = mkdtempSync(join(tmpdir(), 'idea-dev-profile-'))
    try {
      const original = join(temporary, 'original-profile')
      mkdirSync(original)
      writeFileSync(join(original, 'idea-lab-dev-profile.json'), JSON.stringify(profile))
      const linked = join(temporary, 'AgentsDockIdeaLabData')
      symlinkSync(original, linked, process.platform === 'win32' ? 'junction' : 'dir')
      expect(() => readIdeaDevProfile({ ...env, AGENTSDOCK_USER_DATA: linked }, false, linked,
        () => { throw Error('credentials must not be read') })).toThrow('symbolic links')
    } finally {
      rmSync(temporary, { recursive: true, force: true })
    }
  })
  it('refuses a populated dedicated directory without its marker', () => {
    const temporary = mkdtempSync(join(tmpdir(), 'idea-dev-profile-'))
    try {
      const directory = join(temporary, 'AgentsDockIdeaLabData')
      mkdirSync(directory)
      writeFileSync(join(directory, 'settings.json'), 'existing settings')
      expect(() => readIdeaDevProfile({ ...env, AGENTSDOCK_USER_DATA: directory }, false, directory)).toThrow()
    } finally {
      rmSync(temporary, { recursive: true, force: true })
    }
  })
})
