import { useEffect, useRef, useState } from 'react'
import type { ScenarioDatasetSummary } from '../types'

interface Props {
  datasets: ScenarioDatasetSummary[]
  selectedId: number | null
  onSelect: (id: number) => void
  onDelete: (id: number) => void
  deleting: boolean
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

export default function DatasetPicker({ datasets, selectedId, onSelect, onDelete, deleting }: Props) {
  const [open, setOpen] = useState(false)
  const [highlightedIndex, setHighlightedIndex] = useState(-1)
  const rootRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const optionsRef = useRef<(HTMLButtonElement | null)[]>([])

  useEffect(() => {
    if (!open) return
    const onClickOutside = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [open])

  // Keyboard navigation
  useEffect(() => {
    if (!open) return
    
    const handleKeyDown = (e: KeyboardEvent) => {
      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault()
          setHighlightedIndex((i) => Math.min(i + 1, datasets.length - 1))
          break
        case 'ArrowUp':
          e.preventDefault()
          setHighlightedIndex((i) => Math.max(i - 1, -1))
          break
        case 'Enter':
          e.preventDefault()
          if (highlightedIndex >= 0 && highlightedIndex < datasets.length) {
            onSelect(datasets[highlightedIndex].id)
            setOpen(false)
          }
          break
        case 'Escape':
          e.preventDefault()
          setOpen(false)
          break
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open, highlightedIndex, datasets, onSelect])

  // Scroll highlighted item into view
  useEffect(() => {
    if (highlightedIndex >= 0 && optionsRef.current[highlightedIndex]) {
      optionsRef.current[highlightedIndex]?.scrollIntoView({ block: 'nearest' })
    }
  }, [highlightedIndex])

  if (datasets.length === 0) return null

  const selected = datasets.find((ds) => ds.id === selectedId) ?? null

  return (
    <div className="dataset-picker-row">
      <div className="dataset-select" ref={rootRef}>
        <span className="dataset-select-label">Previous scenario sets</span>
        <button type="button" className="dataset-select-trigger" onClick={() => {
          setOpen((o) => !o)
          setHighlightedIndex(-1)
        }}>
          {selected ? (
            <span className="dataset-option-content">
              <span className="dataset-option-name">{selected.source_filename}</span>
              <span className="dataset-option-meta">
                <span className="dataset-option-date">{formatDate(selected.uploaded_at)}</span>
                <span className="dataset-option-time">{formatTime(selected.uploaded_at)}</span>
                <span className="dataset-option-rows">{selected.row_count} rows</span>
              </span>
            </span>
          ) : (
            <span className="dataset-option-name">Select a scenario set…</span>
          )}
          <span className={`dataset-select-chevron ${open ? 'open' : ''}`}>▾</span>
        </button>
        {open && (
          <div className="dataset-select-menu" ref={menuRef} role="listbox">
            {datasets.length === 0 ? (
              <div style={{ padding: '12px', textAlign: 'center', color: 'var(--muted)' }}>
                No scenario sets available
              </div>
            ) : (
              datasets.map((ds, idx) => (
                <button
                  key={ds.id}
                  ref={(el) => {
                    optionsRef.current[idx] = el
                  }}
                  role="option"
                  aria-selected={ds.id === selectedId}
                  className={`dataset-option ${ds.id === selectedId ? 'selected' : ''} ${
                    idx === highlightedIndex ? 'highlighted' : ''
                  }`}
                  onClick={() => {
                    onSelect(ds.id)
                    setOpen(false)
                  }}
                  onMouseEnter={() => setHighlightedIndex(idx)}
                  onMouseLeave={() => setHighlightedIndex(-1)}
                >
                  <span className="dataset-option-content">
                    <span className="dataset-option-name">{ds.source_filename}</span>
                    <span className="dataset-option-meta">
                      <span className="dataset-option-date">{formatDate(ds.uploaded_at)}</span>
                      <span className="dataset-option-time">{formatTime(ds.uploaded_at)}</span>
                      <span className="dataset-option-rows">{ds.row_count} rows</span>
                    </span>
                  </span>
                </button>
              ))
            )}
          </div>
        )}
      </div>
      <button
        className="danger"
        disabled={selectedId == null || deleting}
        onClick={() => selectedId != null && onDelete(selectedId)}
      >
        {deleting ? 'Deleting…' : 'Delete this set'}
      </button>
    </div>
  )
}
