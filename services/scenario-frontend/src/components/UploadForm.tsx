import { useState } from 'react'
import type { Domain } from '../types'

interface Props {
  domain: Domain
  onDomainChange: (domain: Domain) => void
  onUpload: (file: File) => Promise<void>
  uploading: boolean
  domainDisabled?: boolean
}

export default function UploadForm({ domain, onDomainChange, onUpload, uploading, domainDisabled }: Props) {
  const [file, setFile] = useState<File | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!file) return
    await onUpload(file)
    setFile(null)
    ;(e.target as HTMLFormElement).reset()
  }

  return (
    <form className="upload-form" onSubmit={handleSubmit}>
      <label className="control-field domain-field">
        <span className="field-label">Domain</span>
        <select
          value={domain}
          onChange={(e) => onDomainChange(e.target.value as Domain)}
          disabled={domainDisabled}
        >
          <option value="hr">HR</option>
          <option value="contact_center">Contact Center</option>
        </select>
      </label>

      <label className="control-field file-field">
        <span className="field-label">Scenario file (.xlsx)</span>
        <div className="file-input-shell">
          <span className="file-input-text">{file ? file.name : 'No file chosen'}</span>
          <span className="file-input-button">Choose File</span>
          <input
            type="file"
            accept=".xlsx,.xls"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>
      </label>

      <button type="submit" className="primary-action" disabled={!file || uploading}>
        {uploading ? 'Uploading…' : 'Upload'}
      </button>
    </form>
  )
}
