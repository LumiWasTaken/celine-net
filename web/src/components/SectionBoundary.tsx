import { Component, type ReactNode } from 'react'
import { Notice } from '../ui'

/* One per section: a render or lazy-chunk failure stays in its section instead of blanking the
   app. A chunk that no longer exists (a redeploy) needs the page again; any other error retries
   the section. */
export class SectionBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) { return { error } }
  render() {
    const { error } = this.state
    if (!error) return this.props.children
    const chunk = /dynamically imported module|Importing a module script failed|Loading chunk|ChunkLoadError/i.test(`${error.name} ${error.message}`)
    return (
      <Notice tone="error" action={{ label: 'Retry', onClick: () => (chunk ? location.reload() : this.setState({ error: null })) }}>
        {error.message}
      </Notice>
    )
  }
}
