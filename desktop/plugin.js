/**
 * Hermes Chronos — native Desktop timeline pane.
 *
 * Loaded uncompiled by the Hermes Desktop plugin loader.  Only the public SDK
 * is imported; the backend is reached through the plugin's own scoped
 * namespace (`ctx.rest` -> /api/plugins/hermes-chronos/...).
 */

import { host, useValue } from '@hermes/plugin-sdk'
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'

const ACCENT = 'var(--ui-accent, #38bdf8)'
const BG = 'var(--ui-bg, #0b1120)'
const PANEL = 'var(--ui-bg-secondary, #0f172a)'
const BORDER = 'var(--ui-border, #1e293b)'
const TEXT = 'var(--ui-text-primary, #e2e8f0)'
const MUTED = 'var(--ui-text-tertiary, #94a3b8)'
const DANGER = 'var(--ui-danger, #ef4444)'
const SUCCESS = 'var(--ui-success, #22c55e)'

function basename(path) {
  if (!path) return 'workspace'
  const parts = String(path).replace(/\\/g, '/').split('/').filter(Boolean)
  return parts.length ? parts[parts.length - 1] : String(path)
}

function relativeTime(timestamp) {
  const value = Number(timestamp)
  if (!value) return 'unknown time'
  const delta = Math.max(0, Math.floor(Date.now() / 1000) - value)
  if (delta < 60) return 'just now'
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return `${Math.floor(delta / 86400)}d ago`
}

function shortHash(value) {
  return String(value || '').slice(0, 8) || '????????'
}

function diffLineColor(line) {
  if (line.startsWith('+++') || line.startsWith('---')) return MUTED
  if (line.startsWith('+')) return SUCCESS
  if (line.startsWith('-')) return DANGER
  if (line.startsWith('@@')) return ACCENT
  return TEXT
}

function DiffView({ text }) {
  const lines = useMemo(() => {
    const raw = String(text || '').split('\n')
    return raw.length > 1800 ? raw.slice(0, 1800).concat(['… [diff truncated in view]']) : raw
  }, [text])

  return React.createElement(
    'pre',
    {
      style: {
        flex: 1,
        margin: 0,
        padding: '12px 14px',
        overflow: 'auto',
        fontSize: '11.5px',
        lineHeight: 1.55,
        fontFamily: 'var(--ui-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)',
        background: BG,
        color: TEXT,
        whiteSpace: 'pre'
      }
    },
    lines.map((line, index) =>
      React.createElement(
        'div',
        {
          key: `${index}-${line.slice(0, 12)}`,
          style: { color: diffLineColor(line), minHeight: '1.55em' }
        },
        line || ' '
      )
    )
  )
}

function Pill({ children, tone = 'muted' }) {
  const color = tone === 'accent' ? ACCENT : tone === 'danger' ? DANGER : MUTED
  return React.createElement(
    'span',
    {
      style: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: '4px',
        padding: '1px 6px',
        borderRadius: '999px',
        border: `1px solid ${BORDER}`,
        color,
        fontSize: '10px',
        lineHeight: '16px',
        whiteSpace: 'nowrap'
      }
    },
    children
  )
}

function CheckpointRow({ cp, selected, armed, busy, onSelect, onArm, onRestore }) {
  const isBaseline = Boolean(cp.is_baseline)
  return React.createElement(
    'div',
    {
      role: 'button',
      tabIndex: 0,
      onClick: onSelect,
      onKeyDown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect()
        }
      },
      style: {
        padding: '8px 9px',
        marginBottom: '6px',
        borderRadius: '7px',
        cursor: 'pointer',
        background: selected ? 'rgba(56, 189, 248, 0.08)' : 'transparent',
        border: `1px solid ${selected ? ACCENT : BORDER}`,
        borderLeft: `3px solid ${selected ? ACCENT : isBaseline ? MUTED : BORDER}`,
        outline: 'none'
      }
    },
    React.createElement(
      'div',
      { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' } },
      React.createElement(
        'span',
        {
          style: {
            fontFamily: 'var(--ui-font-mono, monospace)',
            fontSize: '11px',
            fontWeight: 700,
            color: selected ? ACCENT : TEXT
          }
        },
        `${isBaseline ? '◦' : '•'} ${shortHash(cp.id)}`
      ),
      React.createElement(Pill, { tone: selected ? 'accent' : 'muted' }, cp.tool || 'manual')
    ),
    React.createElement(
      'div',
      {
        style: {
          marginTop: '4px',
          fontSize: '12px',
          color: TEXT,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap'
        }
      },
      cp.description || '(no description)'
    ),
    React.createElement(
      'div',
      { style: { marginTop: '3px', display: 'flex', gap: '8px', fontSize: '10px', color: MUTED } },
      React.createElement('span', null, relativeTime(cp.time)),
      cp.turn != null ? React.createElement('span', null, `turn ${cp.turn}`) : null
    ),
    selected
      ? React.createElement(
          'button',
          {
            type: 'button',
            disabled: busy,
            onClick: (event) => {
              event.stopPropagation()
              if (armed) onRestore()
              else onArm()
            },
            style: {
              marginTop: '7px',
              width: '100%',
              padding: '4px 8px',
              borderRadius: '5px',
              border: `1px solid ${armed ? DANGER : BORDER}`,
              background: armed ? DANGER : 'transparent',
              color: armed ? '#fff' : TEXT,
              fontSize: '11px',
              cursor: busy ? 'wait' : 'pointer'
            }
          },
          busy ? 'Restoring…' : armed ? 'Click again to restore' : '⏮ Restore this checkpoint'
        )
      : null
  )
}

function ChronosTimeline({ ctx }) {
  const cwd = useValue(host.state.cwd)
  const [checkpoints, setCheckpoints] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [diff, setDiff] = useState('')
  const [stat, setStat] = useState('')
  const [config, setConfig] = useState({ auto_snapshot: true, max_checkpoints: 50 })
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [armedId, setArmedId] = useState(null)
  const [error, setError] = useState('')
  const disarmRef = useRef(null)
  const cwdRef = useRef(cwd)
  cwdRef.current = cwd

  const fetchDiff = useCallback(
    async (id) => {
      if (!id) {
        setDiff('')
        setStat('')
        return
      }
      try {
        const res = await ctx.rest('/diff', {
          method: 'POST',
          body: { cwd: cwdRef.current || undefined, id }
        })
        if (res && res.status === 'ok') {
          setDiff(res.diff || '(No file modifications recorded in this checkpoint)')
          setStat(res.stat || '')
        } else {
          setDiff('')
          setStat('')
          setError((res && res.error) || 'Could not load the checkpoint diff.')
        }
      } catch (err) {
        setDiff('')
        setStat('')
        setError('Chronos backend is unavailable. Enable the plugin in Settings → Plugins.')
      }
    },
    [ctx]
  )

  const loadTimeline = useCallback(
    async (manual = false) => {
      if (manual) setRefreshing(true)
      try {
        const res = await ctx.rest('/timeline', {
          method: 'POST',
          body: { cwd: cwdRef.current || undefined, limit: 50 }
        })
        if (!res || res.status !== 'ok') {
          setError((res && res.error) || 'Could not load the Chronos timeline.')
          return
        }
        setError('')
        const items = Array.isArray(res.checkpoints) ? res.checkpoints : []
        setCheckpoints(items)
        if (res.config) setConfig(res.config)
        setSelectedId((current) => {
          if (current && items.some((cp) => cp.id === current)) return current
          return items.length ? items[0].id : null
        })
      } catch (err) {
        setError('Chronos backend is unavailable. Enable the plugin in Settings → Plugins.')
      } finally {
        setLoading(false)
        if (manual) setRefreshing(false)
      }
    },
    [ctx]
  )

  useEffect(() => {
    loadTimeline(false)
    const dispose = ctx.setInterval(() => loadTimeline(false), 5000)
    return () => {
      if (typeof dispose === 'function') dispose()
    }
  }, [loadTimeline, ctx])

  useEffect(() => {
    if (selectedId) fetchDiff(selectedId)
  }, [selectedId, cwd, fetchDiff])

  useEffect(() => {
    return () => {
      if (disarmRef.current && typeof disarmRef.current === 'function') disarmRef.current()
    }
  }, [])

  const selectCheckpoint = (id) => {
    setSelectedId(id)
    setArmedId(null)
  }

  const armRestore = (id) => {
    setArmedId(id)
    if (disarmRef.current && typeof disarmRef.current === 'function') disarmRef.current()
    disarmRef.current = ctx.setTimeout(() => setArmedId(null), 4000)
  }

  const restore = async (id) => {
    setBusy(true)
    setArmedId(null)
    try {
      const res = await ctx.rest('/rollback', {
        method: 'POST',
        body: { cwd: cwdRef.current || undefined, checkpoint_id: id, clean: true }
      })
      if (res && res.status === 'ok') {
        host.notify({
          kind: 'success',
          message: `Chronos restored ${shortHash(id)}`,
          detail: res.safety_checkpoint ? `Safety checkpoint: ${res.safety_checkpoint}` : undefined
        })
        setError('')
        await loadTimeline(false)
        await fetchDiff(id)
      } else {
        const message = (res && res.error) || 'Rollback failed.'
        setError(message)
        host.notify({ kind: 'error', message: 'Chronos rollback failed', detail: message })
      }
    } catch (err) {
      setError('Rollback request failed. Is the Chronos backend enabled?')
      host.notifyError(err, 'Chronos rollback request failed')
    } finally {
      setBusy(false)
    }
  }

  const selected = checkpoints.find((cp) => cp.id === selectedId) || null
  const autoLabel = config.auto_snapshot ? 'auto-snapshot on' : 'auto-snapshot off'

  return React.createElement(
    'div',
    {
      style: {
        display: 'flex',
        height: '100%',
        minHeight: 0,
        color: TEXT,
        background: BG,
        fontFamily: 'var(--ui-font-sans, Inter, system-ui, sans-serif)'
      }
    },
    // Timeline sidebar
    React.createElement(
      'div',
      {
        style: {
          width: '42%',
          minWidth: '260px',
          maxWidth: '520px',
          display: 'flex',
          flexDirection: 'column',
          borderRight: `1px solid ${BORDER}`,
          background: PANEL
        }
      },
      React.createElement(
        'div',
        { style: { padding: '10px 11px 8px', borderBottom: `1px solid ${BORDER}` } },
        React.createElement(
          'div',
          { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' } },
          React.createElement(
            'div',
            { style: { display: 'flex', alignItems: 'center', gap: '7px', minWidth: 0 } },
            React.createElement('span', { style: { fontSize: '14px' } }, '⏱'),
            React.createElement(
              'div',
              { style: { minWidth: 0 } },
              React.createElement(
                'div',
                { style: { fontSize: '12px', fontWeight: 700, color: ACCENT } },
                'Chronos Time Travel'
              ),
              React.createElement(
                'div',
                {
                  style: {
                    fontSize: '10px',
                    color: MUTED,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap'
                  },
                  title: cwd || ''
                },
                basename(cwd)
              )
            )
          ),
          React.createElement(
            'button',
            {
              type: 'button',
              onClick: () => loadTimeline(true),
              disabled: refreshing,
              style: {
                border: `1px solid ${BORDER}`,
                background: 'transparent',
                color: MUTED,
                borderRadius: '5px',
                padding: '2px 7px',
                fontSize: '10px',
                cursor: refreshing ? 'wait' : 'pointer'
              }
            },
            refreshing ? '…' : 'Refresh'
          )
        ),
        React.createElement(
          'div',
          { style: { display: 'flex', gap: '5px', marginTop: '7px', flexWrap: 'wrap' } },
          React.createElement(Pill, { tone: config.auto_snapshot ? 'accent' : 'muted' }, autoLabel),
          React.createElement(Pill, null, `${checkpoints.length} saved`),
          React.createElement(Pill, null, `cap ${config.max_checkpoints || 50}`)
        )
      ),
      React.createElement(
        'div',
        { style: { flex: 1, overflowY: 'auto', padding: '9px' } },
        error
          ? React.createElement(
              'div',
              {
                style: {
                  marginBottom: '8px',
                  padding: '6px 8px',
                  borderRadius: '5px',
                  border: `1px solid ${DANGER}`,
                  color: DANGER,
                  fontSize: '10.5px',
                  lineHeight: 1.4
                }
              },
              error
            )
          : null,
        loading
          ? React.createElement('div', { style: { color: MUTED, fontSize: '11px', padding: '8px' } }, 'Loading checkpoints…')
          : checkpoints.length === 0
            ? React.createElement(
                'div',
                { style: { color: MUTED, fontSize: '11px', lineHeight: 1.5, padding: '8px' } },
                'No checkpoints yet. Chronos snapshots automatically before write_file, patch, terminal, and execute_code.'
              )
            : checkpoints.map((cp) =>
                React.createElement(CheckpointRow, {
                  key: cp.hash || cp.id,
                  cp,
                  selected: cp.id === selectedId,
                  armed: armedId === cp.id,
                  busy: busy && cp.id === selectedId,
                  onSelect: () => selectCheckpoint(cp.id),
                  onArm: () => armRestore(cp.id),
                  onRestore: () => restore(cp.id)
                })
              )
      ),
      React.createElement(
        'div',
        {
          style: {
            padding: '7px 10px',
            borderTop: `1px solid ${BORDER}`,
            fontSize: '10px',
            color: MUTED,
            lineHeight: 1.45
          }
        },
        'Restores are reversible: a safety checkpoint is taken first. Ignored paths such as node_modules are never touched.'
      )
    ),
    // Diff inspector
    React.createElement(
      'div',
      { style: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', background: BG } },
      React.createElement(
        'div',
        {
          style: {
            padding: '9px 12px',
            borderBottom: `1px solid ${BORDER}`,
            background: PANEL,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '8px'
          }
        },
        React.createElement(
          'div',
          { style: { minWidth: 0 } },
          React.createElement(
            'div',
            { style: { fontSize: '11px', color: MUTED } },
            'Diff inspector ',
            React.createElement(
              'span',
              { style: { color: ACCENT, fontFamily: 'var(--ui-font-mono, monospace)' } },
              selected ? shortHash(selected.id) : 'none'
            )
          ),
          selected
            ? React.createElement(
                'div',
                {
                  style: {
                    fontSize: '11px',
                    color: TEXT,
                    marginTop: '2px',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap'
                  }
                },
                selected.description || '(no description)'
              )
            : null
        ),
        selected
          ? React.createElement(Pill, { tone: 'accent' }, selected.tool || 'manual')
          : null
      ),
      stat
        ? React.createElement(
            'pre',
            {
              style: {
                margin: 0,
                padding: '7px 12px',
                borderBottom: `1px solid ${BORDER}`,
                background: PANEL,
                color: MUTED,
                fontSize: '10px',
                lineHeight: 1.45,
                overflowX: 'auto',
                fontFamily: 'var(--ui-font-mono, monospace)'
              }
            },
            stat.trim()
          )
        : null,
      React.createElement(DiffView, { text: diff })
    )
  )
}

export default {
  id: 'hermes-chronos',
  name: 'Chronos Time Travel',
  register(ctx) {
    ctx.register({
      id: 'timeline',
      area: 'panes',
      title: 'Time Travel',
      data: { placement: 'right', width: '460px' },
      render: () => React.createElement(ChronosTimeline, { ctx })
    })
  }
}
