export type ReleaseInfo = {
  version: string
  date: string
  items: { type: string; content: string }[]
}

export function parseChangelog(content: string): ReleaseInfo[] {
  return content
    .split(/^## /m)
    .slice(1)
    .map((block) => {
      const [title = '', ...lines] = block.trim().split('\n')
      const [, version = title.trim(), date = ''] = title.match(/^(.+?)(?:\s+-\s+(.+))?$/) || []
      return {
        version: version.trim(),
        date: date.trim(),
        items: lines
          .map((line) => line.trim().match(/^\+\s+\[(.+?)]\s+(.+)$/))
          .filter((match): match is RegExpMatchArray => Boolean(match))
          .map((match) => ({ type: match[1], content: match[2] })),
      }
    })
    .filter((release) => release.items.length)
}

export function normalizeVersionTag(value: string): string {
  const clean = value.trim()
  if (!clean) return ''
  return clean.startsWith('v') ? clean : `v${clean}`
}

function versionParts(value: string) {
  const match = value.trim().match(/^v?(\d+)\.(\d+)\.(\d+)/)
  return match ? match.slice(1).map(Number) : null
}

export function isNewerVersion(latestVersion: string, currentVersion: string): boolean {
  const latest = versionParts(latestVersion)
  const current = versionParts(currentVersion)
  if (!latest || !current) return false
  for (let index = 0; index < latest.length; index += 1) {
    if (latest[index] > current[index]) return true
    if (latest[index] < current[index]) return false
  }
  return false
}
