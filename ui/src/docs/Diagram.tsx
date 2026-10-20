import { useMemo } from 'react'
import FlowDiagram from './FlowDiagram'
import { parseMermaid } from './mermaid'
import SeqDiagram from './SeqDiagram'

/** Draws a Mermaid block from the guide as an interactive React Flow diagram; shows the source if it cannot be read. */
export default function Diagram({ source, title }: { source: string; title: string }) {
  const parsed = useMemo(() => { try { return { diagram: parseMermaid(source) } } catch (e) { return { error: (e as Error).message } } }, [source])
  if (!parsed.diagram) return <pre className="vk-code"><small className="error">Diagram could not be drawn: {parsed.error}</small>{'\n'}{source}</pre>
  return parsed.diagram.kind === 'flow' ? <FlowDiagram flow={parsed.diagram} title={title} /> : <SeqDiagram seq={parsed.diagram} title={title} />
}
