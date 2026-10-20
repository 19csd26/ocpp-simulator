export interface DocsRoute { page: string; anchor?: string }
export const docsHash = (page: string, anchor?: string) => `#/docs/${page}${anchor ? `~${anchor}` : ''}`
/** `#/docs/<page>~<section>`: the guide page and the section to scroll to. Null when the address is not a docs address. */
export function parseDocsHash(hash: string): DocsRoute | null {
  const m = hash.match(/^#\/docs(?:\/([^~]*))?(?:~(.*))?$/)
  return m ? { page: m[1] || 'README', anchor: m[2] || undefined } : null
}
