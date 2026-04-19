import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

export function usePollingQuery(queryFn, deps = [], intervalMs = 5000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(Boolean(queryFn))
  const [isFetching, setIsFetching] = useState(false)
  const mountedRef = useRef(true)
  const queryFnRef = useRef(queryFn)

  const depKey = useMemo(() => JSON.stringify(deps), [deps])

  useEffect(() => {
    queryFnRef.current = queryFn
  }, [queryFn])

  const execute = useCallback(
    async ({ silent } = { silent: false }) => {
      const activeQueryFn = queryFnRef.current
      if (!activeQueryFn) return null
      if (!silent) setLoading(true)
      setIsFetching(true)
      setError(null)

      try {
        const result = await activeQueryFn()
        if (!mountedRef.current) return null
        setData(result)
        return result
      } catch (err) {
        if (!mountedRef.current) return null
        setError(err)
        return null
      } finally {
        if (mountedRef.current) {
          setLoading(false)
          setIsFetching(false)
        }
      }
    },
    [],
  )

  useEffect(() => {
    mountedRef.current = true
    if (!queryFnRef.current) {
      setData(null)
      setError(null)
      setLoading(false)
      setIsFetching(false)
      return () => {
        mountedRef.current = false
      }
    }

    void execute({ silent: false })

    const timer = window.setInterval(() => {
      void execute({ silent: true })
    }, intervalMs)

    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [depKey, intervalMs, execute])

  return {
    data,
    error,
    loading,
    isFetching,
    refresh: () => execute({ silent: true }),
  }
}
