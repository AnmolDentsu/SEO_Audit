import React, {
  useEffect,
  useState,
} from 'react'

import {
  getQueueStatus,
} from '../compliance.js'


const LIVE = new Set([
  'queued',
  'claimed',
  'running',
  'cancelling',
])


function formatDuration(seconds) {
  const total = Math.max(
    0,
    Math.round(
      Number(seconds) || 0
    )
  )

  if (total < 60) {
    return `${total}s`
  }

  const hours = Math.floor(
    total / 3600
  )

  const minutes = Math.floor(
    (total % 3600) / 60
  )

  if (hours) {
    return `${hours}h ${minutes}m`
  }

  return `${minutes}m`
}


function formatRange(range) {
  if (!range) {
    return 'Calculating…'
  }

  const low = Number(
    range.low || 0
  )

  const high = Number(
    range.high || 0
  )

  if (
    low <= 5
    && high <= 5
  ) {
    return 'Starting shortly'
  }

  if (
    Math.abs(
      high - low
    ) < 30
  ) {
    return `~${formatDuration(high)}`
  }

  return (
    `~${formatDuration(low)}`
    + '–'
    + formatDuration(high)
  )
}


export default function QueueStatus({
  kind,
  run,
}) {
  const [data, setData] =
    useState(null)

  const [error, setError] =
    useState('')


  useEffect(() => {
    if (
      !run?.id
      || !LIVE.has(
        run.status
      )
    ) {
      setData(null)
      setError('')
      return undefined
    }

    let cancelled = false

    const refresh = async () => {
      try {
        const result =
          await getQueueStatus(
            kind,
            run.id
          )

        if (!cancelled) {
          setData(result)
          setError('')
        }
      } catch (err) {
        if (!cancelled) {
          setError(
            err.message
          )
        }
      }
    }

    refresh()

    const timer = setInterval(
      refresh,
      5000
    )

    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [
    kind,
    run?.id,
    run?.status,
  ])


  if (
    !run?.id
    || !LIVE.has(
      run.status
    )
  ) {
    return null
  }


  if (
    error
    && !data
  ) {
    return (
      <div className="queue-status-card muted">
        Queue ETA temporarily unavailable.
      </div>
    )
  }


  if (!data) {
    return (
      <div className="queue-status-card">
        Calculating queue position and ETA…
      </div>
    )
  }


  if (run.status === 'queued') {
    return (
      <div className="queue-status-card">
        <div>
          <strong>
            {kind === 'page'
              ? 'Page Analysis Queue'
              : 'Sitemap Analysis Queue'}
          </strong>

          <span>
            Queue position #
            {data.queue_position ?? '—'}
          </span>
        </div>

        <div>
          <small>
            Estimated start
          </small>

          <strong>
            {formatRange(
              data.estimated_start_seconds
            )}
          </strong>
        </div>

        <div>
          <small>
            Confidence
          </small>

          <strong>
            {data.confidence || 'LOW'}
          </strong>
        </div>

        <div>
          <small>
            Jobs ahead
          </small>

          <strong>
            {Number(
              data.jobs_ahead || 0
            ).toLocaleString()}
          </strong>
        </div>

        <div className="queue-basis">
          {data.basis}
        </div>
      </div>
    )
  }


  return (
    <div className="queue-status-card active">
      <div>
        <strong>
          Worker active
        </strong>

        <span>
          {kind === 'page'
            ? 'Page Analysis'
            : 'Sitemap Analysis'}
        </span>
      </div>

      {data.estimated_completion_seconds && (
        <div>
          <small>
            Estimated remaining
          </small>

          <strong>
            {formatRange(
              data.estimated_completion_seconds
            )}
          </strong>
        </div>
      )}

      <div>
        <small>
          Confidence
        </small>

        <strong>
          {data.confidence}
        </strong>
      </div>

      <div className="queue-basis">
        {data.basis}
      </div>
    </div>
  )
}