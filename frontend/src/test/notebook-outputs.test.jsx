import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import NotebookOutputs from '../components/NotebookOutputs'

afterEach(cleanup)
it('renders saved text results without rendering rich output HTML', () => {
  const { container } = render(<NotebookOutputs cell={{ cell_type: 'code', execution_count: 3, outputs: [
    { output_type: 'stream', name: 'stdout', text: ['[{"text":"Human: Review the draft."}]\n'] },
    { output_type: 'execute_result', data: { 'text/plain': '30', 'text/html': '<b>untrusted</b>' } },
    { output_type: 'display_data', data: { 'text/html': '<img src="example">' } },
  ] }} />)
  expect(screen.getByText('Saved output · execution 3')).toBeInTheDocument()
  expect(screen.getByText('[{"text":"Human: Review the draft."}]')).toBeInTheDocument()
  expect(screen.getByText('30')).toBeInTheDocument()
  expect(screen.getByText('Non-text output retained in the notebook download.')).toBeInTheDocument()
  expect(container.querySelector('img,b')).toBeNull()
})
