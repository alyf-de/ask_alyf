# AGENTS.md

Traps in this codebase that cost us time more than once.

## Frappe caches misses in `frappe.local.cache`

`frappe.cache().get_value(key)` stores its result in `frappe.local.cache` —
**including a miss** — unless you pass `expires=True`:

```python
# frappe/utils/redis_wrapper.py, RedisWrapper.get_value
if not expires:
	if val is None and generator:
		...
	else:
		local_cache[key] = val      # val may well be None
```

`frappe.local.cache` lives for a whole request or a whole background job. A
value that another process writes *after* your first read is therefore
invisible for the rest of that job.

So any flag that one process writes and another polls — a stop request, a
progress counter, a cancellation — must be read with `expires=True`:

```python
frappe.cache().get_value(key, expires=True)
```

This holds on version-15, version-16 and develop alike. `RedisWrapper.get_value`
is unchanged across all three; version-16 only added a `use_local_cache=True`
keyword, which defaults to the old behaviour.

### Why a test can miss it

`set_value` is *not* the same across versions:

| | writes `frappe.local.cache`? |
| --- | --- |
| version-15 | only when `expires_in_sec` is unset |
| version-16, develop | always |

A test that writes the flag and reads it back in one process therefore passes
on version-16 and develop even with the bug present: the write quietly repairs
the poisoned local cache that the read left behind.

Make the test the two processes it really is — keep the reader's
`frappe.local.cache` across the write:

```python
self.assertFalse(is_stop_requested(RUN))
worker_cache = dict(frappe.local.cache)
request_stop(RUN)                           # the web request, elsewhere
frappe.local.cache = worker_cache           # the worker never saw that write
self.assertTrue(is_stop_requested(RUN))
```

See `test_a_stop_pressed_mid_run_is_seen_by_the_next_check` in
`ask_alyf/ask_alyf/test_checkpointer.py`. `read_running_steps` in `toolset.py`
is the other read that gets this right.
