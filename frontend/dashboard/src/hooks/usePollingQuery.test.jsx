import React from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, test, vi } from 'vitest'

import { usePollingQuery } from './usePollingQuery'

function Probe({ queryFn }) {
  const { data, loading, error } = usePollingQuery(queryFn, [], 1000)

  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="error">{error ? 'yes' : 'no'}</span>
      <span data-testid="value">{data ? String(data.value) : '-'}</span>
    </div>
  )
}

describe('usePollingQuery', () => {
  test('polls repeatedly on interval', async () => {
    vi.useFakeTimers()
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ value: 1 })
      .mockResolvedValueOnce({ value: 2 })
      .mockResolvedValue({ value: 3 })

    render(<Probe queryFn={queryFn} />)

    await act(async () => {
      await Promise.resolve()
    })
    expect(queryFn).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('value')).toHaveTextContent('1')

    act(() => {
      vi.advanceTimersByTime(1000)
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(queryFn).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('value')).toHaveTextContent('2')

    vi.useRealTimers()
  })

  test('captures error state when query throws', async () => {
    const queryFn = vi.fn().mockRejectedValue(new Error('boom'))

    render(<Probe queryFn={queryFn} />)

    await waitFor(() => expect(queryFn).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('error')).toHaveTextContent('yes')
  })
})
