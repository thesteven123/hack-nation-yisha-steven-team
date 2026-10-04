import { lstatSync, readFileSync } from 'node:fs'
import { basename, dirname, isAbsolute, join, resolve } from 'node:path'

export interface IdeaDevProfile { serverUrl: string; accessToken: string }
export const IDEA_DEV_DATA_DIRECTORY = 'AgentsDockIdeaLabData'

function rejectLinkedPath(path: string): void {
  let current = resolve(path)
  while (true) {
    try {
      if (lstatSync(current).isSymbolicLink()) throw new Error('Idea Lab development paths must not contain symbolic links.')
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
    const parent = dirname(current)
    if (parent === current) break
    current = parent
  }
}

/** Opt-in source-development bootstrap, never a production connection fallback. */
export function readIdeaDevProfile(
  env: NodeJS.ProcessEnv, packaged: boolean, userData: string,
  read: (path: string) => string = path => readFileSync(path, 'utf8')
): IdeaDevProfile | null {
  if (env.AGENTSDOCK_IDEA_DEV !== '1') return null
  if (packaged || !env.AGENTSDOCK_USER_DATA || !isAbsolute(env.AGENTSDOCK_USER_DATA)
    || basename(resolve(userData)) !== IDEA_DEV_DATA_DIRECTORY
    || resolve(env.AGENTSDOCK_USER_DATA) !== resolve(userData)) {
    throw new Error('Idea Lab development requires an explicit, separate source-app data directory.')
  }
  const marker = join(userData, 'idea-lab-dev-profile.json')
  rejectLinkedPath(marker)
  const value = JSON.parse(read(marker)) as Record<string, unknown>
  if (value.kind !== 'agentsdock-isolated-idea-lab-v1' || typeof value.serverUrl !== 'string'
    || typeof value.accessToken !== 'string' || value.accessToken.length < 24) {
    throw new Error('Invalid isolated Idea Lab profile.')
  }
  const url = new URL(value.serverUrl)
  const port = Number(url.port)
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || port < 15000 || port > 65535
    || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('Idea Lab development requires a dedicated loopback port (15000–65535).')
  }
  return { serverUrl: url.origin, accessToken: value.accessToken }
}
