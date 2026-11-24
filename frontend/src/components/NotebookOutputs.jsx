// Only text representations are rendered. Notebook MIME bundles remain untrusted.
function asText(value) {
  if (typeof value === 'string') return value
  if (Array.isArray(value) && value.every(part => typeof part === 'string')) return value.join('')
  return null
}

export default function NotebookOutputs({ cell }) {
  if (cell.cell_type !== 'code' || !Array.isArray(cell.outputs) || !cell.outputs.length) return null
  return <section aria-label="Saved cell outputs" className="border-t border-gray-600 pt-2 space-y-2">
    <p className="text-xs text-gray-400">Saved output{Number.isInteger(cell.execution_count) ? ` · execution ${cell.execution_count}` : ''}</p>
    {cell.outputs.map((output, index) => {
      const text = output.output_type === 'stream' ? asText(output.text) :
        output.output_type === 'error' ? asText(output.traceback) : asText(output.data?.['text/plain'])
      return text !== null ? <pre key={index} className="whitespace-pre-wrap break-words text-sm overflow-auto">{text}</pre> :
        <p key={index} className="text-sm text-gray-400">Non-text output retained in the notebook download.</p>
    })}
  </section>
}
