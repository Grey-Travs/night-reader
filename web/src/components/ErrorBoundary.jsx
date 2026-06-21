import { Component } from 'react'

// Top-level safety net: a render error shows a friendly fallback instead of a blank
// white screen, with a Reload to recover.
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('App error:', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-page p-8 text-center text-ink">
          <div className="text-3xl">😵</div>
          <h1 className="font-reading text-xl font-medium">Something went wrong</h1>
          <p className="max-w-md text-sm text-muted">{String(this.state.error?.message || this.state.error)}</p>
          <button onClick={() => { this.setState({ error: null }); window.location.reload() }} className="btn btn-primary px-5 py-2.5">Reload</button>
        </div>
      )
    }
    return this.props.children
  }
}
