/** Break a label into lines of about `width` characters, keeping the author's own line breaks. */
export function wrap(text: string, width = 30): string[] {
  return text.split('\n').flatMap((para) => {
    const out: string[] = []
    let line = ''
    for (const word of para.split(/\s+/).filter(Boolean)) {
      if (line && (line + ' ' + word).length > width) { out.push(line); line = word } else line = line ? line + ' ' + word : word
    }
    return line ? [...out, line] : out.length ? out : ['']
  })
}
